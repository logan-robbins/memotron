# Audit — Unit 9: Entrypoints (`cli`, `mcp_server`, `worker`, `local_platform`, `agent_memory_mcp`, `adoption`, `admin_server`)

**Verdict: NO ACTION on the refactor candidate · 1 DEFECT quarantined out (recommend filing) · 0 items admitted**
Commit audited: `a95152b` · Audited: 2026-09-02 · Tier-C spend: none

Unit 9 was carried into the programme on **X8** — *"expect one to three more `s.value`-class
field-name errors in untested paths"*, based on `mcp_server.py` hand-rolling 32 `json.dumps`
payloads against 15 `model_dump`. **X8 is false, and the audit found a security defect instead.**

---

## X8 is dead — proved by control, not by inspection

The precedent is real: `mcp_server.py:729` documents a shipped bug where a tool emitted `s.value`,
*"a field that never existed"*, and raised on every call.

**But that bug class is already closed.** `mcp_server` suppresses only `arg-type` (15 rows) —
**not** `attr-defined`, which #132's fix (`3cf0139`) removed. Planted both shapes of the original
bug and mypy caught both:

| Planted | Result |
|---|---|
| `"planted": proof.this_field_does_not_exist` | `error: … has no attribute … [attr-defined]` |
| `{s.name: {"count": s.nonexistent_attr} for s in proof.signals}` — the exact iteration-variable form | `error: … has no attribute … [attr-defined]` |

Both reverted; tree clean. Since the `types` lane passes at 16/16, **there cannot be an
undiscovered `s.value`-class error in these modules** — mypy would already be red. X8's expected
yield of "one to three more" is zero, and the number that proves it is the passing `types` lane.

**Consequence:** converting the 32 hand-rolled `json.dumps` payloads to `model_dump` would be a
style change with a wire-contract risk and **no correctness benefit**, since the correctness
property is already enforced. Not admitted.

---

## Defect quarantined — recommend filing as a bug, not fixing here

**An unauthenticated admin HTTP surface echoes internal exception text to the client.**

`admin_server/__init__.py:310` and `:376`:

```python
except HttpApiError as exc:                                  # controlled type — fine
    self._send_json({"error": str(exc)}, status=exc.status)
except Exception as exc:                                     # ← any internal error, verbatim
    self._send_json({"error": str(exc)}, status=400)
```

**Refines X7 from "6 sites" to "2 that matter."** Of the six `str(exc)` sites: two are the bare
`except Exception` handlers above (the defect); two are `except HttpApiError`, which is a
controlled type with a deliberate message and is correct; two are dream-event logging, not
response bodies.

Severity comes from the context, both measured:

* **No authentication.** Zero credential reads anywhere in `admin_server/` — grepped for
  `authorization`, `x-api-key`, `bearer`, `x-litellm`: **no hits**. This independently confirms
  STATE.md's finding that no request path reads a credential.
* **Reaches the database.** 39 `asyncio.run` call sites in the handler, into storage including
  `storage/postgres/_engine.py`, where `PostgresEngineError` and raw psycopg exceptions live —
  the class of error whose text can carry connection details and query fragments.

So: an unauthenticated surface that will return a Postgres error message to whoever asks.

**Not fixed here, deliberately.** The method's defect-quarantine rule: a behaviour fix riding
inside a refactor destroys `pure_move.py`'s only proof. It should be filed and fixed alone.

**Recommended fix direction:** the `except Exception` branches should return a fixed message and
log the detail server-side — the `HttpApiError` branch already demonstrates the right shape.

---

## Instrument readings

| Instrument | Reading | Denominator | Control |
|---|---|---|---|
| `types` (mypy) | PASS, 16/16 lane | `mcp_server` suppresses `arg-type` only; `attr-defined` live | ✅ two planted attribute errors, **both caught**, reverted |
| suppression baseline | `mcp_server` 15 rows, all `arg-type` | 93 total | ✅ 186 errors hidden, non-zero (ANSI trap not firing) |
| serialisation split | `mcp_server` 32 `json.dumps` / 15 `model_dump`; `agent_memory_mcp` 17 / 20 | — | — |

---

## Candidates rejected

| Candidate | The number that killed it | Source |
|---|---|---|
| **X8** — hunt for more `s.value`-class field errors | mypy catches both shapes; `types` lane passes → **zero exist** | planted control, `pyproject.toml` override block |
| Convert 32 hand-rolled payloads to `model_dump` | No correctness benefit (above); each conversion is a wire-contract change unless byte-identical | — |
| Split `admin_server/__init__.py` (2,205 lines) | *"Already made of small pieces that happen to share a file"* | `docs/BRANCH-OVERVIEW.md`, prior |
| Admin payload signature-dispatch | Claimed 347 extractable lines; **measured 2** | STATE.md, prior |

---

## The Any-source check — run, because it was the one thing that could overturn X8's refutation

X8's refutation rests on mypy checking these payload objects, which it does **only if they are
typed**. So I asked mypy directly, with `--disallow-any-expr`:

| | |
|---|---:|
| `Any`-typed expressions in `mcp_server.py` | 13 |
| `json.dumps` payload spans | 32 |
| **`Any` expressions inside a payload span** | **1** |

One line, and reading it resolves it — `mcp_server.py:459`:

```python
"similarity": r.similarity if hasattr(r, "similarity") else None,
```

The `Any` comes from `hasattr` narrowing. This is a **deliberate duck-typed access behind a
guard**, and the guard is exactly what prevents the failure — it yields `None` when the attribute
is absent, so it cannot produce the `AttributeError` that X8 is about.

**X8's refutation stands, now on an exhaustive basis rather than a strong prior.** Note my own
check reported "risk is LIVE on 1 line" before I read the code; the automated answer was a false
positive of my own construction, which is why the line got read rather than reported.

## Where this is wrong

The defect finding assumes an attacker or user can reach the admin HTTP surface. I measured that
**nothing authenticates it** and that it reaches Postgres, but I did **not** check its network
exposure — whether the admin server is bound to localhost, cluster-internal, or ingress-reachable
in any deployed environment. If it is unreachable from outside the pod, the severity drops from
information disclosure to a robustness bug. That is the check that should precede any priority
assessment, and I have not run it.
