"""OpenTelemetry setup for distributed tracing."""

import os
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from fastapi import FastAPI

def configure_telemetry(app: FastAPI) -> None:
    """Configures OpenTelemetry for the FastAPI app if enabled."""
    otlp_endpoint = os.getenv("OTLP_ENDPOINT")
    if not otlp_endpoint:
        # Tracing disabled
        return
        
    resource = Resource(attributes={
        "service.name": "npci-a2a-api"
    })
    
    provider = TracerProvider(resource=resource)
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)
    
    # Instrumentations
    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    
    try:
        from database import engine
        SQLAlchemyInstrumentor().instrument(
            engine=engine,
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to instrument SQLAlchemy: %s", e)


def _log_internal_error(scope: str, exc: Exception) -> None:
    import logging
    logging.getLogger(__name__).exception("Internal error in %s", scope, exc_info=exc)

