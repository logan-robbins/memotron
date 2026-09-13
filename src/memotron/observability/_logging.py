"""Structured logging, and the one place that configures it. (#129)

#129 measured **0 structured log statements** across ~35k lines, and the cause is upstream
of formatting: there is **no central logging setup to attach one to**. Seven `getLogger`
sites, and the only `basicConfig` call lives inside `worker.py`'s own `main()`, so the two
MCP servers and the admin server each inherit whatever the runtime happens to install.
`configure_logging` is that missing place.

WHY THIS IS NOT THE VENDORED `logging.py`
-----------------------------------------
`mcp-jedai-runtime` ships one (225 lines) and it is deliberately **not** vendored beside
`otel.py`. It needs `SecureFormatter` from `jedai_mcp_library` and `get_correlation_id` from
`jedai_mcp_runtime.correlation` -- two further packages and a Starlette middleware -- and its
value over stdlib is secret scrubbing.

**Memotron already scrubs secrets**: `redaction.redact_sensitive_text` handles PII plus
three raw-credential forms (assignment, `Bearer`, bare token). Importing a second scrubber
would give this repository two implementations of one rule, differing in what they catch, and
the divergence would surface as a leak in whichever path used the weaker one. So the field
shape matches the runtime's JSON (`timestamp`/`level`/`logger`/`message`) for dashboard
compatibility, and the scrubbing is ours.

WHAT THIS DOES NOT DO
---------------------
No correlation id. The runtime derives one from `traceparent` via an ASGI middleware; nothing
in Memotron sets one today, so emitting an always-empty field would be worse than omitting
it. When request-scoped identity arrives (#126 / #137) that is the moment to add it, and the
`extra` passthrough below is the seam.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from memotron.redaction import redact_sensitive_text

#: Keys `logging` puts on every record. Anything else a caller passes via `extra=` is
#: application data and is forwarded, which is what makes `report.as_log_fields()`
#: (`worker.py:130`) usable without a bespoke handler.
_RESERVED = frozenset(
    [
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    ]
)


class TextFormatter(logging.Formatter):
    """The human-readable format, redacted.

    THIS CLASS EXISTS BECAUSE OF A BUG FOUND BY RUNNING THE CODE. The first version used
    `JsonFormatter` for JSON output and a plain `logging.Formatter` for text, which meant
    **secrets were scrubbed only in JSON mode** -- and text is the default. A container
    started without `LOG_JSON` would have shipped raw credentials, and the asymmetry was
    invisible in review because both branches looked equally reasonable.

    Redaction now happens in both, so it cannot depend on a format choice.
    """

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record))


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the message run through Memotron's redactor.

    The redactor is applied to the **formatted** message rather than the format string, so
    an interpolated secret (`_log.info("token=%s", tok)`) is caught. That is the case worth
    covering: a literal secret in a format string is a code review problem, an interpolated
    one is a runtime accident.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": redact_sensitive_text(record.getMessage()),
        }
        payload.update(
            {key: value for key, value in record.__dict__.items() if key not in _RESERVED and not key.startswith("_")}
        )
        if record.exc_info:
            # Redacted too: a traceback frame can carry a credential in a local.
            payload["exception"] = redact_sensitive_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(
    *,
    level: str | None = None,
    json_output: bool | None = None,
    otel_enabled: bool | None = None,
) -> None:
    """Install the root handler. Idempotent, and safe to call from every entrypoint.

    Each argument falls back to an environment variable so the chart configures this without
    any entrypoint knowing the names: ``LOG_LEVEL`` (default INFO), ``LOG_JSON``, and
    ``OTEL_ENABLED`` -- the same three the deployed `mcp-jedai-postgres` pod sets.

    ``json_output`` defaults to **off**, so a developer running the CLI gets readable lines
    and a container that sets ``LOG_JSON=true`` gets machine-readable ones. Defaulting it on
    would make the local experience worse to satisfy a deployment that configures itself.

    Idempotency matters more than it looks: `configure_logging` is called from entrypoints
    that can be composed (`local_platform` builds the admin server in-process), and a second
    unconditional `addHandler` would duplicate every line.
    """
    level_name = (level or os.environ.get("LOG_LEVEL") or "INFO").strip().upper()
    as_json = json_output if json_output is not None else _env_flag("LOG_JSON")
    with_otel = otel_enabled if otel_enabled is not None else _env_flag("OTEL_ENABLED")

    root = logging.getLogger()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    for existing in list(root.handlers):
        if getattr(existing, "_memotron_managed", False):
            root.removeHandler(existing)

    # STDERR, NOT STDOUT, AND THIS IS NOT A STYLE CHOICE. `memotron mcp` serves the MCP
    # protocol over stdio (`cli.py:_run_mcp` -> `mcp.run(transport="stdio")`), so stdout IS
    # the wire. A log line written there corrupts the session for every client. The first
    # version of this function used stdout and would have broken that entrypoint the moment
    # it was wired -- caught by asking what the fifth caller actually does, not by a test.
    #
    # Nothing is lost in a container: both streams are collected identically.
    handler: logging.Handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter() if as_json else TextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler._memotron_managed = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    if with_otel:
        _attach_otel_log_handler(root)


def _env_flag(name: str) -> bool:
    """The same truthy set `storage/postgres/__init__.py:126-130` uses for its own flag.

    Deliberately narrow: "on" and "yes please" are NOT true. A flag that accepts anything
    non-empty turns a typo into a silent behaviour change.
    """
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _attach_otel_log_handler(root: logging.Logger) -> None:
    """Route records to the OTLP collector as well as stdout.

    Absent packages are a no-op, not an error: `otel.py` makes the same choice, and the
    result is that a developer without the `otel` extra installed sees ordinary logs rather
    than an import failure at startup.
    """
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    except ImportError:
        logging.getLogger(__name__).warning(
            "OTEL_ENABLED is set but the OpenTelemetry log packages are missing; "
            "logs go to stdout only. Install the `otel` extra."
        )
        return

    provider = LoggerProvider()
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(provider)
    otel_handler = LoggingHandler(logger_provider=provider)
    otel_handler._memotron_managed = True  # type: ignore[attr-defined]
    root.addHandler(otel_handler)
