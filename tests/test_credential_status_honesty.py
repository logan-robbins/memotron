"""`has_api_key` must mean "usable", not "a row exists". (#123)

Both engines hardcoded ``has_api_key: True`` whenever a row was present. Postgres even
carried a comment explaining why that was safe — *"a stored row always carries a sealed key;
the column is NOT NULL"* — which is true about **presence** and says nothing about whether the
value can still be **decrypted**.

Before the durable-KEK fix, every Postgres pod came up with a fresh ephemeral key, so after
any restart the sealed credential was unreadable while status still reported the tenant
configured. `probe_kek.py` printed `[status still reports CONFIGURED]` on exactly that.

**Nothing tested it.** The full suite passed with the dishonest behaviour in place, which is
why it survived — so this file is the coverage that was missing, not a regression guard for a
bug someone else introduced.

Both engines, because #163 and #164 were both "one engine changed and the other did not", and
this is the same shape: two hand-written implementations of one rule.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator

import pytest

from memotron.crypto import LocalKeyManager
from memotron.storage.base import StorageBackend
from memotron.storage.sqlite import SQLiteStorageBackend

TENANT = "acme"


def _postgres_dsn_or_skip() -> str:
    dsn = os.environ.get("MEMOTRON_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("set MEMOTRON_TEST_POSTGRES_DSN to run: needs a live Postgres")
    return dsn


def _reset_postgres_schema(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def engine(request: pytest.FixtureRequest, tmp_path) -> Iterator[str]:
    """Yields an engine name; each test opens its OWN stores so it can swap the KEK."""
    if request.param == "postgres":
        _reset_postgres_schema(_postgres_dsn_or_skip())
    return request.param


def _open(engine: str, tmp_path, key_manager: LocalKeyManager) -> StorageBackend:
    if engine == "postgres":
        from memotron.storage.postgres import PostgresStorageBackend

        return PostgresStorageBackend(_postgres_dsn_or_skip(), min_size=1, max_size=2, key_manager=key_manager)
    return SQLiteStorageBackend(str(tmp_path / "graph.db"), key_manager=key_manager)


def _seal(store: StorageBackend) -> None:
    store.set_tenant_llm_credentials(
        tenant_id=TENANT, provider="litellm", api_key="sk-real", base_url="http://x.invalid", model="m"
    )


class TestStatusTellsTheTruthAboutTheKey:
    def test_a_readable_credential_reports_configured(self, engine: str, tmp_path) -> None:
        """The control. Without this, a test that only checks the broken case would pass on an
        implementation that always answered False."""
        kek = LocalKeyManager(secrets.token_bytes(32))
        store = _open(engine, tmp_path, kek)
        try:
            _seal(store)
            state = store.tenant_llm_credential_state(TENANT)
        finally:
            store.close()

        assert state is not None
        assert state["has_api_key"] is True
        assert state["api_key_unreadable"] is False

    def test_a_credential_sealed_under_a_DIFFERENT_key_reports_NOT_configured(self, engine: str, tmp_path) -> None:
        """THE REGRESSION, and it is exactly a pod restart under an ephemeral KEK.

        Same store, same row, different key-encryption key. The credential is present and
        permanently unreadable. Reporting it configured is what made the outage invisible.
        """
        first, second = LocalKeyManager(secrets.token_bytes(32)), LocalKeyManager(secrets.token_bytes(32))

        store = _open(engine, tmp_path, first)
        try:
            _seal(store)
        finally:
            store.close()

        reopened = _open(engine, tmp_path, second)
        try:
            state = reopened.tenant_llm_credential_state(TENANT)
        finally:
            reopened.close()

        assert state is not None, "the ROW must still be reported -- it exists"
        assert state["has_api_key"] is False, "an undecryptable credential is not 'configured'"
        assert state["api_key_unreadable"] is True

    def test_absent_and_unreadable_stay_DISTINGUISHABLE(self, engine: str, tmp_path) -> None:
        """Why `api_key_unreadable` exists rather than only flipping `has_api_key`.

        *No credential* is a normal state an operator resolves by configuring one. *A
        credential we cannot decrypt* is an emergency: the tenant's key material is
        effectively lost. Collapsing both into `has_api_key: False` would hide the second
        inside the first.
        """
        kek = LocalKeyManager(secrets.token_bytes(32))
        store = _open(engine, tmp_path, kek)
        try:
            assert store.tenant_llm_credential_state("never-configured") is None
        finally:
            store.close()


class TestKeyManagerFromEnv:
    """`crypto.key_manager_from_env` — the resolver that makes a durable KEK reachable. (#123)

    Every branch here is a way to get a WRONG key silently, which is worse than no key: the
    fail-closed guard only fires when there is no key manager at all, so anything this
    function returns is trusted from then on.
    """

    def test_returns_None_when_nothing_is_configured(self) -> None:
        """None means "no durable key", which leaves the existing guard in charge. It must not
        be confused with "here is a key" -- that is what made the ephemeral default silent."""
        from memotron.crypto import key_manager_from_env

        assert key_manager_from_env({}) is None

    def test_the_same_KEK_yields_the_same_key_id(self) -> None:
        """The property that makes replicas agree. Two pods reading one Secret must produce
        identical wrapping keys, or each seals content the others cannot read."""
        import base64

        from memotron.crypto import KEK_B64_ENV, key_manager_from_env

        raw = base64.b64encode(secrets.token_bytes(32)).decode()
        first, second = key_manager_from_env({KEK_B64_ENV: raw}), key_manager_from_env({KEK_B64_ENV: raw})
        assert first is not None and second is not None
        assert first.key_id() == second.key_id()

    def test_a_missing_KEK_FILE_is_refused_and_NOT_created(self, tmp_path) -> None:
        """THE ONE THAT MATTERS FOR A DEPLOYMENT. `LocalKeyManager.from_file` creates the file
        when absent, which in a Deployment means every replica mints a DIFFERENT key and
        silently cannot read its neighbours' content -- with no guard to catch it, because a
        key manager would be present. So this path must refuse, and must not write."""
        from memotron.crypto import KEK_FILE_ENV, key_manager_from_env

        missing = tmp_path / "not-there.kek"
        with pytest.raises(ValueError, match="does not exist"):
            key_manager_from_env({KEK_FILE_ENV: str(missing)})
        assert not missing.exists(), "the resolver created a key instead of refusing"

    def test_an_existing_KEK_FILE_is_loaded(self, tmp_path) -> None:
        from memotron.crypto import KEK_FILE_ENV, key_manager_from_env

        path = tmp_path / "kek"
        path.write_bytes(secrets.token_bytes(32))
        assert key_manager_from_env({KEK_FILE_ENV: str(path)}) is not None

    @pytest.mark.parametrize(
        ("value", "match"),
        [("not base64 at all!", "not valid base64"), ("c2hvcnQ=", "must be exactly")],
        ids=["invalid-base64", "wrong-length"],
    )
    def test_a_malformed_KEK_is_refused_rather_than_padded(self, value: str, match: str) -> None:
        """Refuse, never coerce. A silently truncated or padded key would seal content under a
        key nobody can reproduce."""
        from memotron.crypto import KEK_B64_ENV, key_manager_from_env

        with pytest.raises(ValueError, match=match):
            key_manager_from_env({KEK_B64_ENV: value})

    def test_a_wrong_length_KEK_FILE_is_refused(self, tmp_path) -> None:
        """Renamed and re-asserted after review found the original vacuous.

        It matched ``"corrupt"``, and pytest's ``tmp_path`` is derived from the TEST NAME --
        which was ``test_a_corrupt_KEK_FILE_is_refused``. The word appeared only in the path
        embedded in the message, so the assertion passed against a message that never said
        it, and would have passed against an empty one. Strip the path before asserting.
        """
        from memotron.crypto import KEK_FILE_ENV, key_manager_from_env

        path = tmp_path / "kek"
        path.write_bytes(b"too short")
        with pytest.raises(ValueError) as excinfo:
            key_manager_from_env({KEK_FILE_ENV: str(path)})
        message = str(excinfo.value).replace(str(path), "<PATH>")
        assert "is not a 32-byte key" in message
        assert "found 9 bytes" in message

    def test_setting_BOTH_forms_is_refused(self, tmp_path) -> None:
        """Ambiguity about which key seals content is not resolvable by precedence -- picking
        one silently would mean the operator's other key is quietly ignored."""
        from memotron.crypto import KEK_B64_ENV, KEK_FILE_ENV, key_manager_from_env

        with pytest.raises(ValueError, match="both"):
            key_manager_from_env({KEK_B64_ENV: "x", KEK_FILE_ENV: str(tmp_path / "k")})


class TestTheSharedReadabilityRule:
    """`_shared/_governance.llm_credential_is_readable` — shared so the engines cannot diverge."""

    def test_a_correctly_sealed_value_is_readable(self) -> None:
        from memotron.crypto import seal_content
        from memotron.storage._shared._governance import llm_credential_is_readable

        key = secrets.token_bytes(32)
        assert llm_credential_is_readable(seal_content("sk-real", key), key) is True

    @pytest.mark.parametrize("case", ["wrong-key", "no-key", "blank", "not-sealed"])
    def test_everything_else_is_not_readable(self, case: str) -> None:
        """`not-sealed` is the subtle one. The obvious implementation uses `reveal_content`,
        which passes plaintext through UNCHANGED and returns a placeholder for shredded
        content -- it never raises, by design. Using it here reported a garbage string as a
        working credential. Caught by running the helper, not by reading it."""
        from memotron.crypto import seal_content
        from memotron.storage._shared._governance import llm_credential_is_readable

        key = secrets.token_bytes(32)
        sealed = seal_content("sk-real", key)
        cases = {
            "wrong-key": (sealed, secrets.token_bytes(32)),
            "no-key": (sealed, None),
            "blank": ("", key),
            "not-sealed": ("plaintext-junk", key),
        }
        value, k = cases[case]
        assert llm_credential_is_readable(value, k) is False


class TestTheEdgesFoundByAdversarialReview:
    """Three defects in the #123 fix itself, found by attacking it rather than re-reading it."""

    def test_a_configured_KEK_is_honoured_on_SQLITE_too(self, tmp_path, monkeypatch) -> None:
        """It was SILENTLY IGNORED, on the engine `latest` actually runs today.

        The client only passed the env key manager on the Postgres branch; the SQLite branch
        minted its own `.kek` sibling and used a DIFFERENT key. An operator verifying "did my
        KEK take effect?" on latest would have seen everything work and concluded it had --
        the exact silently-inert-configuration shape this repo keeps producing.
        """
        import base64

        from memotron import Memotron
        from memotron.crypto import KEK_B64_ENV, LocalKeyManager

        raw = secrets.token_bytes(32)
        monkeypatch.setenv(KEK_B64_ENV, base64.b64encode(raw).decode())
        monkeypatch.delenv("MEMOTRON_OPERATIONAL_STORE_DSN", raising=False)

        dw = Memotron(graph_path=str(tmp_path / "g.db"))
        try:
            assert dw.graph.key_manager.key_id() == LocalKeyManager(raw).key_id()
        finally:
            dw.graph.close()

    def test_with_no_KEK_configured_the_sibling_file_still_works(self, tmp_path, monkeypatch) -> None:
        """The control for the test above. Without it, honouring the env key could have been
        implemented by breaking the default local path and nothing would have said so."""
        from memotron import Memotron
        from memotron.crypto import KEK_B64_ENV, KEK_FILE_ENV

        for var in (KEK_B64_ENV, KEK_FILE_ENV, "MEMOTRON_OPERATIONAL_STORE_DSN"):
            monkeypatch.delenv(var, raising=False)

        dw = Memotron(graph_path=str(tmp_path / "g.db"))
        try:
            assert dw.graph.key_manager is not None
        finally:
            dw.graph.close()
        assert any(f.name.endswith(".kek") for f in tmp_path.iterdir())

    # The two-mistake diagnosis test that lived here was replaced by
    # `TestTheDiagnosticHintsDiagnoseContentNotLength` below. It asserted the base64 case with
    # a payload of `b"a" * 44`, which is 44 bytes of valid base64 alphabet that decodes to 33
    # bytes -- so it was never an example of the mistake it named (a Secret holding the base64
    # of a 32-byte key), and it only passed because the hint was chosen by LENGTH alone.

    def test_a_STORAGE_fault_is_not_reported_as_an_unreadable_credential(self, tmp_path) -> None:
        """The readability check caught bare `Exception`, so a database fault came back as
        "credential unreadable" -- #123's own dishonest-status defect, reintroduced inside the
        fix for it. Only key failures may be swallowed; a fault must propagate as a fault.
        """
        kek = LocalKeyManager(secrets.token_bytes(32))
        store = SQLiteStorageBackend(str(tmp_path / "g.db"), key_manager=kek)
        try:
            _seal(store)

            def boom(*_args, **_kwargs):
                raise RuntimeError("database is on fire")

            store.get_governance_key = boom  # type: ignore[method-assign]
            with pytest.raises(RuntimeError, match="on fire"):
                store.tenant_llm_credential_state(TENANT)
        finally:
            store.close()

    def test_a_DELIBERATE_shred_is_not_reported_as_an_incident(self, engine: str, tmp_path) -> None:
        """Three states, three answers. Added after review found only two.

        `api_key_unreadable` was meant to mean "page someone: this tenant's key material is
        effectively lost". But a crypto-shred (RTBF) is a DESIGNED erasure that issues a
        certificate, and it was raising the same flag -- so the signal that should mean
        emergency also fired on routine compliance work, which is how a signal stops being
        read at all.
        """
        kek = LocalKeyManager(secrets.token_bytes(32))
        store = _open(engine, tmp_path, kek)
        try:
            _seal(store)
            assert store.tenant_llm_credential_state(TENANT)["has_api_key"] is True

            store.shred_governance_key(f"tenant:{TENANT}", subject_key="llm_credentials")
            state = store.tenant_llm_credential_state(TENANT)
        finally:
            store.close()

        assert state["has_api_key"] is False, "a shredded credential is genuinely not usable"
        assert state["api_key_shredded"] is True
        assert state["api_key_unreadable"] is False, "a deliberate erasure must not page anyone"


class TestTheKEKReachesEverySurfaceThatOpensTheStore:
    """Findings from the red-team pass on the first version of this change.

    That version taught only ``Memotron()`` to read the env KEK. `adoption` and
    `runtime` -- the surfaces behind ``memotron llm configure`` -- kept opening the
    SAME store with no key manager, so the CLI sealed under the per-store `.kek` sibling
    while the serving process read under the env key. Status said "configured"; the
    server could not decrypt it. That is precisely the failure this file exists to
    prevent, relocated one seam over, and NOTHING here caught it: the test that looked
    like it covered the case asserted a `key_id` and never touched the store.

    So these tests go through the real entry points and round-trip a real value.
    """

    def test_a_credential_written_by_one_surface_is_readable_by_another(self, tmp_path, monkeypatch) -> None:
        """The regression test for the split-brain. Write with the shape `adoption` uses,
        read with the shape the client uses, same store, same environment."""
        import base64

        from memotron.storage import open_storage

        monkeypatch.setenv("MEMOTRON_KEK_B64", base64.b64encode(secrets.token_bytes(32)).decode())
        graph = str(tmp_path / "graph.db")

        writer = open_storage(graph)  # adoption.py / runtime.py shape: no key manager passed
        try:
            _seal(writer)
            writer_key = writer.key_manager.key_id()
        finally:
            writer.close()

        reader = open_storage(graph)  # a different process would look exactly like this
        try:
            assert reader.key_manager.key_id() == writer_key, "the two surfaces disagree on the key"
            state = reader.tenant_llm_credential_state(TENANT)
            assert state["has_api_key"] is True, "written by one surface, unreadable by another"
            assert reader.tenant_llm_credentials(TENANT)["api_key"] == "sk-real"  # pragma: allowlist secret
        finally:
            reader.close()

    def test_without_a_KEK_the_two_surfaces_still_agree(self, tmp_path) -> None:
        """The positive control. If this passed only because the env var was set, the test
        above would prove nothing -- the sibling-key path must agree with itself too."""
        from memotron.storage import open_storage

        graph = str(tmp_path / "graph.db")
        writer = open_storage(graph)
        try:
            _seal(writer)
        finally:
            writer.close()
        reader = open_storage(graph)
        try:
            assert reader.tenant_llm_credential_state(TENANT)["has_api_key"] is True
        finally:
            reader.close()

    def test_a_split_deployment_gives_the_KEK_to_BOTH_engines(self, tmp_path, monkeypatch) -> None:
        """`graph_kwargs` was declared and never populated, so the memory graph was built
        with no key manager while the operational store got the configured one."""
        import base64

        from memotron.storage.factory import create_storage_backend
        from memotron.storage.settings import EngineSettings, StorageSettings

        monkeypatch.setenv("MEMOTRON_KEK_B64", base64.b64encode(secrets.token_bytes(32)).decode())
        settings = StorageSettings(
            memory_graph=EngineSettings(engine="sqlite", path=str(tmp_path / "graph.db")),
            operational_store=EngineSettings(engine="sqlite", path=str(tmp_path / "ops.db")),
        )
        backend = create_storage_backend(settings)
        try:
            # Private attributes on purpose: the composite forwards `key_manager` to the
            # OPERATIONAL half only, which is exactly why the graph half going keyless was
            # invisible through the public surface.
            graph_key = backend._memory_graph.key_manager.key_id()
            ops_key = backend._operational.key_manager.key_id()
        finally:
            backend.close()
        assert graph_key == ops_key, "the split halves sealed under different keys"


class TestASetButEmptyKEKVariableIsAMisconfiguration:
    """A Secret that fails to resolve arrives as an empty string, not as an absent one.

    Treating it as "no KEK configured" is the silent environment-controlled downgrade the
    resolver exists to refuse: on SQLite it reverts to the per-replica sibling key and says
    nothing at all.
    """

    @pytest.mark.parametrize("value", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
    @pytest.mark.parametrize("name", ["MEMOTRON_KEK_B64", "MEMOTRON_KEK_FILE"])
    def test_set_but_empty_is_refused(self, name: str, value: str) -> None:
        from memotron.crypto import key_manager_from_env

        with pytest.raises(ValueError) as excinfo:
            key_manager_from_env({name: value})
        assert "set but empty" in str(excinfo.value)

    def test_genuinely_unset_is_still_None(self) -> None:
        """The control: refusing empty must not turn 'unconfigured' into an error."""
        from memotron.crypto import key_manager_from_env

        assert key_manager_from_env({}) is None


class TestTheDiagnosticHintsDiagnoseContentNotLength:
    """The hints guess what an operator did wrong. Guessing from the byte count alone is
    the same defect one level down -- so they must read the bytes."""

    @pytest.mark.parametrize("trailer", [b"\n", b"\r\n"], ids=["LF", "CRLF"])
    def test_a_trailing_newline_is_named_including_the_windows_case(self, tmp_path, trailer: bytes) -> None:
        from memotron.crypto import KEK_FILE_ENV, key_manager_from_env

        path = tmp_path / "kek"
        path.write_bytes(secrets.token_bytes(32) + trailer)
        with pytest.raises(ValueError) as excinfo:
            key_manager_from_env({KEK_FILE_ENV: str(path)})
        assert "trailing" in str(excinfo.value)

    def test_base64_TEXT_in_the_file_is_named_as_such(self, tmp_path) -> None:
        import base64

        from memotron.crypto import KEK_FILE_ENV, key_manager_from_env

        path = tmp_path / "kek"
        path.write_bytes(base64.b64encode(secrets.token_bytes(32)))
        with pytest.raises(ValueError) as excinfo:
            key_manager_from_env({KEK_FILE_ENV: str(path)})
        assert "base64 TEXT" in str(excinfo.value)

    def test_a_44_byte_BINARY_file_is_not_misdiagnosed_as_base64(self, tmp_path) -> None:
        """44 bytes is the length of base64-encoded 32 bytes, so the length-only check told
        an operator holding a 44-byte binary key to re-encode text they did not have."""
        from memotron.crypto import KEK_FILE_ENV, key_manager_from_env

        path = tmp_path / "kek"
        path.write_bytes(bytes(range(44)))  # 44 bytes, definitively not base64 text
        with pytest.raises(ValueError) as excinfo:
            key_manager_from_env({KEK_FILE_ENV: str(path)})
        message = str(excinfo.value)
        assert "found 44 bytes" in message
        assert "base64 TEXT" not in message, "diagnosed from length instead of content"


class TestAShredClaimMustRestOnEvidence:
    """`api_key_shredded` suppresses the `api_key_unreadable` alarm, so what proves a shred
    happened matters. It used to be "the wrapped_dek column is empty" -- a fact any writer can
    manufacture, which would let key destruction downgrade itself to routine compliance work.
    """

    def test_a_real_shred_is_reported_as_shredded(self, engine: str, tmp_path) -> None:
        """The control: the evidence requirement must not break the legitimate path."""
        store = _open(engine, tmp_path, LocalKeyManager(secrets.token_bytes(32)))
        try:
            _seal(store)
            store.shred_governance_key(f"tenant:{TENANT}", subject_key="llm_credentials")
            state = store.tenant_llm_credential_state(TENANT)
        finally:
            store.close()
        assert state["api_key_shredded"] is True
        assert state["api_key_unreadable"] is False

    def test_a_key_blanked_WITHOUT_an_erasure_record_still_raises_the_alarm(self, tmp_path) -> None:
        """Destroying key material outside the erasure path is an incident, not compliance.

        SQLite-only: this reaches past the storage API to corrupt a row the way a buggy job
        or a hostile writer would, and the engines share the decision, not the SQL.
        """
        store = _open("sqlite", tmp_path, LocalKeyManager(secrets.token_bytes(32)))
        try:
            _seal(store)
            store._connection.execute(
                "UPDATE governance_keys SET wrapped_dek = '' WHERE scope_key = ? AND subject_key = ?",
                (f"tenant:{TENANT}", "llm_credentials"),
            )
            store._connection.commit()
            state = store.tenant_llm_credential_state(TENANT)
        finally:
            store.close()

        assert state["api_key_shredded"] is False, "an empty column is not proof of a shred"
        assert state["api_key_unreadable"] is True, "silently destroyed key material must page"
