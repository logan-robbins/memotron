"""OpenTelemetry setup and ASGI instrumentation. (#129)

VENDORED, NOT WRITTEN HERE. Copied verbatim from
``mcp-forge/libs/mcp-jedai-runtime/src/jedai_mcp_runtime/otel.py`` at version 0.1.13,
which is the chassis 13 generated MCP servers use. The only edit is this header.

Why a copy rather than a dependency
-----------------------------------
``mcp-jedai-runtime`` is consumed **only as a uv workspace path dependency** --
``mcp-forge/uv.lock`` resolves it as ``editable = "libs/mcp-jedai-runtime"`` -- so a
separate repository cannot depend on it. Querying Nexus for a published copy returned
**401 Unauthorized**, which is inconclusive rather than a "no". **If it is published,
delete this file and add the dependency**: that is strictly better than a copy, and the
copy will drift.

Why this chassis and not another
--------------------------------
Checked three candidates. ``gateway`` is a fork of upstream LiteLLM (``name = "litellm"``)
with **zero** first-party OTel files -- its telemetry is LiteLLM's own callback system, not
a reusable chassis. ``jedai-mcp-template`` carries an inline copy of THIS file, differing by
one ``noqa`` comment, so it is a stale duplicate of the same thing.

The evidence it works is not in any repository: the **deployed** ``mcp-jedai-postgres`` pod
in the latest cluster carries exactly the environment this module sets, including the two
blank Policy-P4 header variables (observed 2026-09-03). A running service configured by this
chassis is a stronger signal than either repo.

What is deliberately NOT vendored
---------------------------------
The runtime's ``logging.py`` needs ``SecureFormatter`` from ``jedai_mcp_library`` and
``get_correlation_id`` from ``jedai_mcp_runtime.correlation`` -- two more packages and a
Starlette middleware, to obtain structured logs with secret scrubbing. **Memotron already
has redaction** (``redaction.redact_sensitive_text``), and a second scrubber would be a
competing implementation of the same rule. See ``observability/_logging.py`` for the local
formatter that reuses the existing one.

Policy P4 (jedai-system-design / core-framework):
  OTel trace exporters must NOT capture HTTP headers as span attributes.

  Enforced by setting the opentelemetry-instrumentation-asgi capture env vars to
  empty strings before the middleware is constructed.  The same vars are mirrored
  in Helm values so they survive container restarts independently of in-process
  setup order.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Policy P4: suppress header capture on both sides of the ASGI boundary.
_P4_HEADERS_ENV: dict[str, str] = {
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST": "",
    "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE": "",
}


def _enforce_p4_header_policy() -> None:
    """Set env vars that prevent OTEL ASGI middleware from capturing headers."""
    for key, value in _P4_HEADERS_ENV.items():
        if os.environ.get(key, value) != value:
            logger.warning("Policy P4: overriding %s to suppress header capture in OTEL spans", key)
        os.environ[key] = value


class _Providers:
    """Wraps both OTEL providers so server.py can call a single .shutdown()."""

    def __init__(self, tracer_provider: Any, meter_provider: Any) -> None:
        self._tracer = tracer_provider
        self._meter = meter_provider

    def shutdown(self) -> None:
        self._tracer.shutdown()
        self._meter.shutdown()


def setup_telemetry(*, service_name: str) -> Any | None:
    """Configure global TracerProvider + MeterProvider and return them for lifecycle management.

    Returns None when OTEL packages are not installed.
    Exports traces, metrics, and (when configure_logging is called with otel_enabled=True) logs
    to the OTLP HTTP endpoint configured via OTEL_EXPORTER_OTLP_ENDPOINT (default port 4318).
    """
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        logger.warning("OTEL packages missing, tracing disabled: %s", e)
        return None

    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": os.environ.get("ENVIRONMENT", "local"),
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
    )
    metrics.set_meter_provider(meter_provider)

    logger.info("OpenTelemetry tracer and meter providers configured for %s", service_name)
    return _Providers(tracer_provider, meter_provider)


def flush_telemetry(timeout_millis: int = 5000) -> None:
    """Force-flush all active OTEL signal providers (traces, metrics, logs).

    Call this in integration tests after driving tool calls so the
    PeriodicExportingMetricReader (default 60s interval) doesn't force a
    long wait before assertions.  No-op when OTEL packages are absent or
    providers are not SDK instances.
    """
    try:
        from opentelemetry import metrics, trace
        from opentelemetry._logs import get_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider as _SdkLogProvider
        from opentelemetry.sdk.metrics import MeterProvider as _SdkMeterProvider
        from opentelemetry.sdk.trace import TracerProvider as _SdkTracerProvider

        tp = trace.get_tracer_provider()
        if isinstance(tp, _SdkTracerProvider):
            tp.force_flush(timeout_millis)

        mp = metrics.get_meter_provider()
        if isinstance(mp, _SdkMeterProvider):
            mp.force_flush(timeout_millis)

        lp = get_logger_provider()
        if isinstance(lp, _SdkLogProvider):
            lp.force_flush(timeout_millis)
    except ImportError:
        pass


def wrap_asgi_with_otel(app: Any) -> Any:
    """Wrap an ASGI app with OpenTelemetry middleware.

    Reads traces from the globally-configured provider; call setup_telemetry first.
    Returns app unchanged when the instrumentation package is not installed.

    Policy P4 header suppression is applied before constructing the middleware.
    """
    _enforce_p4_header_policy()
    try:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
    except ImportError as e:
        logger.warning("OTEL ASGI instrumentation unavailable: %s", e)
        return app
    return OpenTelemetryMiddleware(app)
