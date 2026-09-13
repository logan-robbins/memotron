"""Telemetry and structured logging — the one place an entrypoint calls. (#129)

Every deployed and local entrypoint calls :func:`configure_observability` first. Doing it in
one function rather than making each entrypoint remember two calls in the right order is the
point: `setup_telemetry` must run **before** `wrap_asgi_with_otel`, because the middleware
reads the globally-installed tracer provider, and an entrypoint that got that order wrong
would export nothing while looking correctly instrumented.

Everything degrades to a no-op when the `otel` extra is not installed, so the local
developer experience is unchanged and no entrypoint needs a conditional import.
"""

from __future__ import annotations

import os
from typing import Any

from memotron.observability._logging import JsonFormatter as JsonFormatter
from memotron.observability._logging import configure_logging as configure_logging
from memotron.observability.otel import flush_telemetry as flush_telemetry
from memotron.observability.otel import setup_telemetry as setup_telemetry
from memotron.observability.otel import wrap_asgi_with_otel as wrap_asgi_with_otel


def configure_observability(*, service_name: str) -> Any | None:
    """Configure logging and telemetry for one process. Returns the providers, or ``None``.

    The return value is the OTel providers, so a caller with a shutdown hook can flush them;
    ignoring it is fine and is what most entrypoints do.

    ``OTEL_ENABLED`` gates the telemetry half only — logging is always configured, because a
    process with no log handler is worse than one with no traces. That split is why this is a
    function and not two lines in each entrypoint: the two signals have different failure
    modes and different defaults.
    """
    configure_logging()
    if os.environ.get("OTEL_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        return None
    return setup_telemetry(service_name=service_name)
