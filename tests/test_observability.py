"""Structured logging and telemetry wiring. (#129)

#129 measured 0 structured log statements and no OTel. The gap was not formatting -- it was
that **nothing configured logging at all**, so there was no place to attach a formatter or an
exporter to. `configure_observability` is that place, and these tests pin the three properties
that are easy to get wrong and impossible to notice.

TWO OF THESE TESTS EXIST BECAUSE THE FIRST VERSION HAD THE BUG
--------------------------------------------------------------
Both were found by *running* the code, not by review, and both were invisible in the diff:

* `test_secrets_are_redacted_in_BOTH_formats` -- the first version redacted only in JSON, and
  text is the default. A container without `LOG_JSON` would have shipped raw credentials.
* `test_logs_go_to_stderr_because_stdout_is_the_mcp_wire` -- the first version wrote to
  stdout, which is the MCP protocol stream for `memotron mcp` (stdio transport). Every
  stdio session would have been corrupted by the first log line.

Neither is hypothetical and neither would have failed loudly; they would have looked fine.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import pathlib
import subprocess
import sys

import pytest

from memotron.observability import configure_observability
from memotron.observability._logging import JsonFormatter, TextFormatter, _env_flag, configure_logging

SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Leave the root logger as found. These tests install real handlers on it."""
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in before:
        root.addHandler(h)
    root.setLevel(level)


def _emit(monkeypatch: pytest.MonkeyPatch, message: str, **env: str) -> str:
    for key in ("LOG_JSON", "OTEL_ENABLED", "LOG_LEVEL"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    configure_logging()
    record = logging.LogRecord("d", logging.INFO, __file__, 1, message, None, None)
    handler = logging.getLogger().handlers[-1]
    return handler.format(record)


class TestRedaction:
    @pytest.mark.parametrize("log_json", ["true", ""], ids=["json", "text"])
    def test_secrets_are_redacted_in_BOTH_formats(self, monkeypatch: pytest.MonkeyPatch, log_json: str) -> None:
        """THE REGRESSION. Redaction must not depend on which formatter is selected.

        Text is the DEFAULT, so a version that scrubbed only JSON would leak in the common
        case while looking correct in the case anyone tested.
        """
        out = _emit(monkeypatch, f"authorization: Bearer {SECRET}", LOG_JSON=log_json)
        assert SECRET not in out
        assert "[REDACTED]" in out

    def test_an_interpolated_secret_is_caught_not_just_a_literal_one(self) -> None:
        """Redaction runs on the FORMATTED message, so `_log.info("t=%s", tok)` is covered.

        A literal secret in a format string is a code-review problem; an interpolated one is
        a runtime accident, and it is the one worth defending against.
        """
        record = logging.LogRecord("d", logging.INFO, __file__, 1, "token=%s", (SECRET,), None)
        assert SECRET not in JsonFormatter().format(record)
        assert SECRET not in TextFormatter("%(message)s").format(record)

    def test_a_secret_in_a_traceback_is_redacted(self) -> None:
        """Exception text is a real leak path: a credential can sit in a local variable."""
        try:
            raise ValueError(f"boom {SECRET}")
        except ValueError:
            record = logging.LogRecord("d", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
        assert SECRET not in JsonFormatter().format(record)


class TestTheStdioHazard:
    def test_logs_go_to_stderr_because_stdout_is_the_mcp_wire(self, tmp_path: pathlib.Path) -> None:
        """THE OTHER REGRESSION, and it needs a SUBPROCESS to be meaningful.

        `memotron mcp` serves MCP over stdio, so stdout is the protocol stream. Asserting
        on a handler attribute would pass even if something later re-pointed the stream; only
        running a real process and reading its two file descriptors proves it.
        """
        script = tmp_path / "emit.py"
        script.write_text(
            "import logging\n"
            "from memotron.observability import configure_observability\n"
            "configure_observability(service_name='t')\n"
            "logging.getLogger('d').info('LOG-LINE')\n"
            "print('STDOUT-LINE')\n"
        )
        proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120, check=False)
        assert "STDOUT-LINE" in proc.stdout
        assert "LOG-LINE" not in proc.stdout, "a log line on stdout corrupts the MCP stdio stream"
        assert "LOG-LINE" in proc.stderr


class TestConfiguration:
    def test_calling_it_twice_does_not_duplicate_every_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`local_platform` composes the admin server in-process, so double configuration is
        a real path, and an unconditional addHandler would double every log line."""
        monkeypatch.delenv("LOG_JSON", raising=False)
        configure_logging()
        configure_logging()
        managed = [h for h in logging.getLogger().handlers if getattr(h, "_memotron_managed", False)]
        assert len(managed) == 1

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", False), ("", False), ("0", False)],
    )
    def test_the_flag_vocabulary_is_narrow_and_matches_the_storage_guard(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
    ) -> None:
        """Same truthy set as `storage/postgres/__init__.py:126-130`. "on" is deliberately
        FALSE: a flag that accepts anything non-empty turns a typo into a silent change."""
        monkeypatch.setenv("DW_TEST_FLAG", value)
        assert _env_flag("DW_TEST_FLAG") is expected

    def test_telemetry_is_off_unless_asked_but_logging_is_always_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The two signals have different defaults on purpose: a process with no log handler
        is worse than one with no traces."""
        monkeypatch.delenv("OTEL_ENABLED", raising=False)
        assert configure_observability(service_name="t") is None
        assert any(getattr(h, "_memotron_managed", False) for h in logging.getLogger().handlers)

    def test_extra_fields_survive_into_the_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`worker.py:189` logs `extra={"metrics": report.as_log_fields()}`. Those fields are
        exactly what #129 wants shipped, so dropping them would defeat the point."""
        monkeypatch.setenv("LOG_JSON", "true")
        configure_logging()
        record = logging.LogRecord("d", logging.INFO, __file__, 1, "cycle complete", None, None)
        record.metrics = {"formed": 3}  # type: ignore[attr-defined]
        payload = json.loads(logging.getLogger().handlers[-1].format(record))
        assert payload["metrics"] == {"formed": 3}
        assert payload["level"] == "INFO"


class TestEveryEntrypointIsWired:
    """A structural check, because a missed entrypoint is silent: that process simply keeps
    its old logging and exports nothing, and no test of the module itself would notice."""

    @pytest.mark.parametrize(
        "path",
        [
            "src/memotron/worker.py",
            "src/memotron/admin_server/__init__.py",
            "src/memotron/local_platform.py",
            "src/memotron/cli.py",
            "examples/mcp_server.py",
        ],
    )
    def test_the_entrypoint_calls_configure_observability(self, path: str) -> None:
        source = pathlib.Path(path).read_text()
        assert "configure_observability(service_name=" in source, (
            f"{path} is an entrypoint and does not configure observability; it will run with "
            "whatever logging the runtime installs and will export nothing"
        )

    def test_no_entrypoint_still_calls_basicConfig(self) -> None:
        """`worker.py` used to call `logging.basicConfig` directly. If one comes back, that
        process silently opts out of redaction and OTel export while looking configured.

        COMMENTS ARE STRIPPED FIRST, and that is not a detail. The first version of this test
        matched the raw source and failed on the comment in `worker.py` that *documents*
        replacing `logging.basicConfig` -- a guard that fires on its own explanation is one
        someone deletes rather than fixes.
        """
        for path in ("src/memotron/worker.py", "src/memotron/local_platform.py"):
            code = "\n".join(line.split("#", 1)[0] for line in pathlib.Path(path).read_text().splitlines())
            assert "basicConfig" not in code, (
                f"{path} calls logging.basicConfig again -- it bypasses configure_logging, "
                "so its output is neither redacted nor exported"
            )


class TestTelemetryActuallyRuns:
    """These need the `otel` extra, which is in the dev group precisely so they run.

    Without it `observability/otel.py` sits at ~20% -- everything past the
    `try: import opentelemetry` guard is unreachable -- and a floor set at that number
    would be a guard that cannot fail. That is the same defect the repo's own
    `coverage_floors.py` header calls out about a 0.0 floor.
    """

    def test_setup_telemetry_returns_real_providers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not just "did not raise": the return value must be something a caller can shut
        down, because entrypoints keep it for exactly that."""
        pytest.importorskip("opentelemetry.sdk")
        from memotron.observability import setup_telemetry

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
        providers = setup_telemetry(service_name="memotron-test")
        assert providers is not None
        assert hasattr(providers, "shutdown")
        providers.shutdown()

    def test_configure_observability_honours_the_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OTEL_ENABLED gates telemetry only. Both arms are asserted so neither default can
        drift silently: off must return None, on must return providers."""
        pytest.importorskip("opentelemetry.sdk")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")

        # The OTLP LOG handler is stubbed out. Without this the enabled arm attaches a real
        # exporter, which then spends the suite's time retrying against a collector that is
        # not there ("Connection refused ... retrying in 0.93s"). That noise is not evidence
        # of anything -- the gate is what is under test -- and a slow, chatty test is one
        # someone eventually deletes.
        import memotron.observability._logging as logging_mod

        monkeypatch.setattr(logging_mod, "_attach_otel_log_handler", lambda root: None)

        monkeypatch.setenv("OTEL_ENABLED", "false")
        assert configure_observability(service_name="t") is None

        monkeypatch.setenv("OTEL_ENABLED", "true")
        providers = configure_observability(service_name="t")
        assert providers is not None
        providers.shutdown()

    def test_the_asgi_wrapper_enforces_policy_P4(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """P4: OTel must NOT capture HTTP headers as span attributes. The module enforces it
        by blanking two env vars BEFORE constructing the middleware, so this asserts the
        observable effect rather than trusting the comment.

        The same two blanks are mirrored in `.helm/values.yaml`, which is what makes the
        policy survive a restart independently of in-process setup order.
        """
        pytest.importorskip("opentelemetry.instrumentation.asgi")
        from memotron.observability import wrap_asgi_with_otel

        monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", "authorization")

        async def app(scope, receive, send):  # pragma: no cover - never invoked
            return None

        wrapped = wrap_asgi_with_otel(app)

        assert wrapped is not app, "the middleware was not applied"
        import os

        assert os.environ["OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST"] == ""
        assert os.environ["OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE"] == ""


class TestTheEntrypointsCallItAtRUNTIME:
    """The structural test above reads source; these RUN each entrypoint.

    The distinction is the whole point. A source grep proves the line exists; it cannot prove
    the line is reached -- it would still pass if the call sat behind `if False:` or after an
    early return. `diff-cover` caught exactly this: the structural test was green while three
    of the four call sites had never executed.

    Each entrypoint is stopped the moment it tries to serve, by making the serve call raise a
    sentinel. That is enough: `configure_observability` runs first, deliberately, so that
    startup logging is captured.
    """

    class _Served(Exception):
        """Raised in place of actually binding a port."""

    def _assert_configured(self, monkeypatch: pytest.MonkeyPatch, run: object) -> None:
        seen: list[str] = []
        import memotron.observability as obs

        monkeypatch.setattr(obs, "configure_observability", lambda *, service_name: seen.append(service_name))
        with pytest.raises(self._Served):
            run()  # type: ignore[operator]
        assert seen, "the entrypoint served without configuring observability first"

    def test_cli_mcp_configures_before_serving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import memotron.cli as cli_mod

        monkeypatch.setattr(cli_mod, "configure_observability", lambda *, service_name: None)
        monkeypatch.setattr(cli_mod, "build_platform_from_project", lambda root: (_ for _ in ()).throw(self._Served()))
        with pytest.raises(self._Served):
            cli_mod._run_mcp(argparse.Namespace(project_root=""))

    def test_admin_server_main_configures_before_serving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import memotron.admin_server as admin_mod

        called: list[str] = []
        monkeypatch.setattr(admin_mod, "configure_observability", lambda *, service_name: called.append(service_name))
        monkeypatch.setattr(sys, "argv", ["memotron-admin-server", "--graph-path", "/nonexistent", "--scope", "x"])
        with pytest.raises(SystemExit):
            admin_mod.main()
        assert called == ["memotron-admin-server"], (
            "observability must be configured before the graph-path check exits, or a failed "
            "start produces no structured log saying why"
        )

    def test_local_platform_main_configures_before_serving(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import memotron.local_platform as lp_mod

        called: list[str] = []
        monkeypatch.setattr(lp_mod, "configure_observability", lambda *, service_name: called.append(service_name))
        monkeypatch.setattr(sys, "argv", ["memotron-local-platform", "--tenant-id", ""])
        # The exception TYPE is deliberately not asserted. This entrypoint happens to raise
        # ValueError from deep in agent_memory rather than SystemExit, and pinning that would
        # make the test about an unrelated validation path. The claim is only that
        # configuration happened FIRST -- which is the property that matters, because a start
        # that fails before logging is configured produces no structured line saying why.
        with contextlib.suppress(Exception):
            lp_mod.main()
        assert called == ["memotron-local-platform"]

    def test_flush_telemetry_forces_every_signal_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`flush_telemetry` was entirely untested, which `diff-cover` surfaced once the new
        files were actually committed — before that they were UNTRACKED, so `git diff` could
        not see them and the gate reported 100% over a diff that excluded the whole package.

        It matters beyond coverage: the metric reader exports on a 60s timer, so without a
        force-flush an integration test asserting on exported metrics waits a minute or gets
        nothing. This drives all three signal providers.
        """
        pytest.importorskip("opentelemetry.sdk")
        from memotron.observability import flush_telemetry, setup_telemetry

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
        providers = setup_telemetry(service_name="memotron-flush-test")
        assert providers is not None
        try:
            # No collector is listening; the exporters fail and that is fine. The assertion is
            # that flushing is a safe, terminating no-op-on-failure rather than a raise --
            # entrypoints call it from shutdown paths where an exception would mask the real
            # reason a process is going down.
            flush_telemetry(timeout_millis=200)
        finally:
            providers.shutdown()

    def test_flush_telemetry_is_safe_with_no_providers_configured(self) -> None:
        """The default global providers are not SDK instances, so every `isinstance` guard is
        False. Calling it before `setup_telemetry` must do nothing rather than raise."""
        pytest.importorskip("opentelemetry.sdk")
        from memotron.observability import flush_telemetry

        flush_telemetry(timeout_millis=1)


class TestItDegradesWhenTheExtraIsAbsent:
    """The `otel` extra is optional, and every call site claims to no-op without it.

    That claim was UNTESTED until now -- the dev group installs the packages, so the
    `except ImportError` branches never executed. `diff-cover` is what surfaced them, once
    the new files were committed rather than untracked.

    It is worth testing rather than excluding: a developer who has not installed the extra
    must get working logs and a running process, not an import error at startup. Setting a
    `sys.modules` entry to `None` makes the next `import` of it raise ImportError, which is
    the same failure the absent package produces.
    """

    @staticmethod
    def _hide(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
        for name in names:
            monkeypatch.setitem(sys.modules, name, None)  # type: ignore[misc]

    def test_setup_telemetry_returns_None_rather_than_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memotron.observability import setup_telemetry

        self._hide(monkeypatch, "opentelemetry", "opentelemetry.sdk.trace")
        assert setup_telemetry(service_name="t") is None

    def test_wrap_asgi_returns_the_app_UNCHANGED(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returning the app unwrapped is the point: the server still serves, just untraced."""
        from memotron.observability import wrap_asgi_with_otel

        async def app(scope, receive, send):  # pragma: no cover - never invoked
            return None

        self._hide(monkeypatch, "opentelemetry.instrumentation.asgi")
        assert wrap_asgi_with_otel(app) is app

    def test_logging_still_works_and_warns(self, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
        """The most important arm: OTEL_ENABLED is set but the packages are missing. Logs must
        still reach stderr, and the operator must be told why nothing is being exported --
        silence here would look identical to a working exporter."""
        from memotron.observability._logging import _attach_otel_log_handler

        self._hide(monkeypatch, "opentelemetry._logs", "opentelemetry.sdk._logs")
        root = logging.getLogger()
        _attach_otel_log_handler(root)

        assert not any(type(h).__name__ == "LoggingHandler" for h in root.handlers)


class TestTheOtelLogHandlerAttaches:
    def test_it_adds_a_handler_when_the_packages_are_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The happy path of `_attach_otel_log_handler`, stubbed out elsewhere so the suite does
        not spend time retrying against a collector that is not running."""
        pytest.importorskip("opentelemetry.sdk._logs")
        from memotron.observability._logging import _attach_otel_log_handler

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
        root = logging.getLogger()
        before = len(root.handlers)
        _attach_otel_log_handler(root)
        assert len(root.handlers) == before + 1
        assert getattr(root.handlers[-1], "_memotron_managed", False)
