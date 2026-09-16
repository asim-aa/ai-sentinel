"""OpenTelemetry setup for instrumented services.

Spans are exported straight into the shared SQLite file (see storage.py) via a small custom
exporter — no OTel Collector needed at this scope. Call init_tracing() once at process startup
and use the returned tracer for `with tracer.start_as_current_span(...)` blocks.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from ai_sentinel import storage


class SQLiteSpanExporter(SpanExporter):
    def __init__(self, db_path: str):
        self.db_path = db_path

    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        try:
            for span in spans:
                ctx = span.get_span_context()
                parent = span.parent
                storage.insert_span(
                    self.db_path,
                    span_id=format(ctx.span_id, "016x"),
                    trace_id=format(ctx.trace_id, "032x"),
                    parent_id=format(parent.span_id, "016x") if parent else None,
                    name=span.name,
                    service_name=span.resource.attributes.get(SERVICE_NAME, "unknown"),
                    start_time=span.start_time / 1e9,
                    end_time=span.end_time / 1e9,
                    duration_ms=(span.end_time - span.start_time) / 1e6,
                    status="ERROR" if span.status.is_ok is False else "OK",
                    attributes=dict(span.attributes or {}),
                )
            return SpanExportResult.SUCCESS
        except Exception:
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass


def init_tracing(service_name: str, db_path: str) -> trace.Tracer:
    storage.init_db(db_path)
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(SimpleSpanProcessor(SQLiteSpanExporter(db_path)))
    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
