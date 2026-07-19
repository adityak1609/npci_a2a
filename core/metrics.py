"""Prometheus metrics via prometheus_client with multi-process Gunicorn support.

When running under Gunicorn with multiple worker processes, set
``PROMETHEUS_MULTIPROC_DIR`` to a writable tmpfs path (e.g. /tmp/prom_multiproc).
Each worker writes its metric files there; the /metrics endpoint aggregates them
via MultiProcessCollector so Prometheus sees a single consistent view.

Without PROMETHEUS_MULTIPROC_DIR (single-process / dev), the default global
registry is used directly.

Public API is intentionally identical to the previous in-process implementation
so all call sites in main.py, rate_limit.py, routes/, and guardrails.py are
unchanged:

    inc_counter(name, labels=None, amount=1.0)
    set_gauge(name, value, labels=None)
    observe_histogram(name, value, *, labels=None, buckets=...)
    time_request() -> float
    render_prometheus() -> str
"""

from __future__ import annotations

import os
import threading
import time

# PROMETHEUS_MULTIPROC_DIR must exist before prometheus_client is imported.
_MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR", "").strip()
if _MULTIPROC_DIR:
    os.makedirs(_MULTIPROC_DIR, exist_ok=True)

from prometheus_client import (  # noqa: E402
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# MultiProcessCollector is only available (and only needed) in multiprocess mode.
try:
    from prometheus_client.multiprocess import MultiProcessCollector  # type: ignore[import]
    _HAS_MULTIPROCESS = True
except ImportError:
    _HAS_MULTIPROCESS = False

_REQUEST_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# Thread-safe caches — metrics are created once per (name, label_names) key.
_lock = threading.Lock()
_counters: dict[str, Counter] = {}
_gauges: dict[str, Gauge] = {}
_histograms: dict[str, Histogram] = {}


# ---------------------------------------------------------------------------
# Internal helpers — create or return cached prometheus_client metric objects
# ---------------------------------------------------------------------------

def _get_counter(name: str, label_names: tuple[str, ...]) -> Counter:
    with _lock:
        if name not in _counters:
            _counters[name] = Counter(
                name,
                name.replace("_", " "),
                list(label_names),
            )
        return _counters[name]


def _get_gauge(name: str, label_names: tuple[str, ...]) -> Gauge:
    with _lock:
        if name not in _gauges:
            _gauges[name] = Gauge(
                name,
                name.replace("_", " "),
                list(label_names),
            )
        return _gauges[name]


def _get_histogram(
    name: str,
    label_names: tuple[str, ...],
    buckets: tuple[float, ...],
) -> Histogram:
    with _lock:
        if name not in _histograms:
            _histograms[name] = Histogram(
                name,
                name.replace("_", " "),
                list(label_names),
                buckets=list(buckets),
            )
        return _histograms[name]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def inc_counter(
    name: str,
    labels: dict[str, object] | None = None,
    amount: float = 1.0,
) -> None:
    """Increment a counter metric by ``amount``."""
    d = labels or {}
    label_names = tuple(sorted(d.keys()))
    counter = _get_counter(name, label_names)
    if d:
        counter.labels(**{k: str(v) for k, v in sorted(d.items())}).inc(amount)
    else:
        counter.inc(amount)


def set_gauge(
    name: str,
    value: float,
    labels: dict[str, object] | None = None,
) -> None:
    """Set a gauge metric to an absolute value."""
    d = labels or {}
    label_names = tuple(sorted(d.keys()))
    gauge = _get_gauge(name, label_names)
    if d:
        gauge.labels(**{k: str(v) for k, v in sorted(d.items())}).set(value)
    else:
        gauge.set(value)


def observe_histogram(
    name: str,
    value: float,
    *,
    labels: dict[str, object] | None = None,
    buckets: tuple[float, ...] = _REQUEST_BUCKETS,
) -> None:
    """Record a single histogram observation."""
    d = labels or {}
    label_names = tuple(sorted(d.keys()))
    histogram = _get_histogram(name, label_names, buckets)
    if d:
        histogram.labels(**{k: str(v) for k, v in sorted(d.items())}).observe(value)
    else:
        histogram.observe(value)


def time_request() -> float:
    """Return a high-resolution timestamp for latency measurement.

    Usage::

        started = time_request()
        # … do work …
        observe_histogram("my_latency_seconds", time_request() - started)
    """
    return time.perf_counter()


def render_prometheus() -> str:
    """Return all metrics in Prometheus text exposition format.

    In multiprocess mode (``PROMETHEUS_MULTIPROC_DIR`` is set), aggregates
    metric files from all Gunicorn worker processes before rendering so the
    scrape endpoint returns a unified view.
    """
    if _MULTIPROC_DIR and _HAS_MULTIPROCESS:
        registry = CollectorRegistry()
        MultiProcessCollector(registry)
        return generate_latest(registry).decode("utf-8")
    return generate_latest(REGISTRY).decode("utf-8")
