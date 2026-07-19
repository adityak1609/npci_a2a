"""Core ingestion and metrics endpoints."""

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from core.metrics import render_prometheus, set_gauge
from core.telemetry import _log_internal_error
from database import A2AIngestionJob, SessionLocal, engine
from rag.ingest import run_ingestion
from rag.retriever import index_exists, invalidate_cache
from routes.auth_routes import require_metrics_access
from security.auth import User, get_current_admin
from security.compliance import ComplianceBlockedError
from security.guardrails import classifier_circuit_state

router = APIRouter()

@router.get("/health", tags=["System"])
async def health_check():
    """Backward-compatible health check (kept for existing probes)."""
    pool = engine.pool
    return {
        "status": "ok",
        "index_ready": index_exists(),
        "db_pool": {
            "checked_in": pool.checkedin(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
            "size": pool.size(),
        },
    }


@router.get("/health/live", tags=["System"])
async def health_live():
    """Kubernetes liveness probe — confirms the process is running.

    Always returns 200 unless the process has crashed.  Intentionally
    lightweight: no external calls so it cannot trigger a pod restart
    due to a transient downstream outage.
    """
    return {"status": "alive"}


@router.get("/health/ready", tags=["System"])
async def health_ready():
    """Kubernetes readiness probe — confirms the app can serve traffic.

    Checks:
    - PostgreSQL reachability (SELECT 1)
    - Redis reachability (PING), if REDIS_URL is configured
    - Vector index loaded

    Returns 200 when all checks pass, 503 when any check fails.
    """
    from fastapi.responses import JSONResponse
    checks: dict[str, object] = {}
    all_ok = True

    # Database check
    try:
        db = SessionLocal()
        try:
            from sqlalchemy import text
            db.execute(text("SELECT 1"))
            checks["db"] = {"ok": True}
        finally:
            db.close()
    except Exception as _db_exc:
        checks["db"] = {"ok": False, "error": "unreachable"}
        all_ok = False

    # Redis check (only when configured)
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        try:
            import redis as _redis
            _r = _redis.Redis.from_url(
                redis_url,
                socket_connect_timeout=1,
                socket_timeout=1,
            )
            _r.ping()
            checks["redis"] = {"ok": True}
        except Exception:
            checks["redis"] = {"ok": False, "error": "unreachable"}
            all_ok = False
    else:
        checks["redis"] = {"ok": True, "note": "not configured"}

    # Vector index check
    index_ready = index_exists()
    checks["index"] = {"ok": index_ready}
    if not index_ready:
        all_ok = False

    status_code = 200 if all_ok else 503
    return JSONResponse(
        content={"status": "ready" if all_ok else "degraded", "checks": checks},
        status_code=status_code,
    )


@router.get("/metrics", tags=["System"])
async def metrics_endpoint(_access=Depends(require_metrics_access)):
    """Prometheus-compatible application metrics."""
    pool = engine.pool
    set_gauge("db_pool_checked_in_connections", pool.checkedin(), {"pool": "default"})
    set_gauge("db_pool_checked_out_connections", pool.checkedout(), {"pool": "default"})
    set_gauge("db_pool_overflow_connections", pool.overflow(), {"pool": "default"})
    set_gauge("db_pool_size_connections", pool.size(), {"pool": "default"})
    set_gauge("guardrail_classifier_circuit_open", classifier_circuit_state())
    db = SessionLocal()
    try:
        for status_value in ("queued", "processing", "failed"):
            count = db.query(A2AIngestionJob).filter(A2AIngestionJob.status == status_value).count()
            set_gauge(
                "a2a_ingestion_jobs",
                count,
                {"status": status_value},
            )
    finally:
        db.close()
    return PlainTextResponse(render_prometheus(), media_type="text/plain; version=0.0.4")


@router.post("/ingest", tags=["System"])
async def ingest_endpoint(current_user: User = Depends(get_current_admin)):
    """
    Trigger PDF ingestion (or re-ingestion if the PDF was updated).
    Requires authentication. Runs ingestion in a thread to avoid blocking.
    """
    try:
        result = await asyncio.to_thread(run_ingestion)
        invalidate_cache()
        return result
    except ComplianceBlockedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        _log_internal_error("system_ingest", e)
        raise HTTPException(status_code=500, detail="Ingestion failed.")

