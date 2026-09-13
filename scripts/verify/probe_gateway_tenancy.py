#!/usr/bin/env python3
"""Re-prove the ADR 0008 tenancy invariants against a live JEDAI Gateway.

    LITELLM_MASTER_KEY_FILE=/path/to/key \\
        uv run python scripts/verify/probe_gateway_tenancy.py --env latest

Exit 0 if every boundary holds, 1 if any forgery SUCCEEDED, 2 if it could not run.

Why this exists
---------------
ADR 0008 binds Memotron's ``principal_id`` to LiteLLM's ``user_id`` and ``tenant_id``
to ``team_id``, on the strength of one claim: **a non-admin cannot forge either field.**
That claim was measured once, against ``latest``, on vendored LiteLLM ``a9cdc23834``.
Forgery enforcement is exactly the kind of behaviour that moves between LiteLLM releases,
so the ADR is only as good as someone's ability to re-run this somewhere else. That is
this file's whole job: make "re-verify before trusting ADR 0008 in <env>" a command.

This is NOT wired into check.sh, on purpose
-------------------------------------------
It needs a live gateway and a master key, and it mutates remote state (briefly). It is a
by-hand tool run at environment-promotion time, in the same family as ``sweep_admin.py``.
Adding it to the check lanes would put a network round-trip and a credential in the inner
loop for a fact that changes about once per LiteLLM upgrade.

What it pins, and what it deliberately does not
-----------------------------------------------
It pins the four *negative* results the ADR rests on -- foreign ``user_id`` at mint,
cross-team mint, rebinding an existing key, and claiming a CO-MEMBER's id -- plus the one
*positive* control (T7) without which the negatives are worthless, and the regeneration
survival of ``user_id`` / ``team_id`` / ``key_alias``.

It also pins **key_alias global uniqueness**, which is the unstated premise of DW-026
(`docs/authorization-decisions.md`): that entry resolves principal, role and scopes by
looking the alias up in Memotron's own registry, and a member picks their own alias
at mint time. If aliases could collide, Alice mints Bob's alias and resolves as Bob.

T7 is not decoration. Without it, a run where every route 500s reads as "all forgeries
blocked -- PASS", which is this project's recurring defect: a check whose passing
condition is satisfied by the very problem it should surface. If T7 fails, the run aborts
as INCONCLUSIVE (exit 2) rather than reporting a green boundary it never actually tested.

It does not assert anything about *which* teams or users exist -- that is environment
drift, not an invariant. Every object it creates is a throwaway; it never adds a member
to, or mints into, a real team.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HOST = "https://{env}.jedai-gateway-admin.wdprapps.disney.com"
PREFIX = "dw-tenancy-probe"


class Gateway:
    def __init__(self, base: str, master: str) -> None:
        self.base, self.master = base, master

    def call(self, path: str, token: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data else "GET")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:400]
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {"raw": body}
        except OSError as e:
            return 0, {"raw": str(e)[:200]}


def why(d: dict) -> str:
    e = d.get("error") or d.get("detail") or d
    return str(e.get("message", e) if isinstance(e, dict) else e)[:150]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default="latest", help="gateway environment (latest|stage|load|prod)")
    args = ap.parse_args()

    key_file = os.environ.get("LITELLM_MASTER_KEY_FILE")
    if not key_file or not os.path.exists(key_file):
        print("SKIPPED: set LITELLM_MASTER_KEY_FILE to a proxy-admin key file")
        return 2
    gw = Gateway(HOST.format(env=args.env), open(key_file).read().strip())
    MK = gw.master

    alice, bob = f"{PREFIX}-alice", f"{PREFIX}-bob"
    team_id, keys, failures, inconclusive = None, [], [], []

    def mint(token, **kw):
        st, d = gw.call("/key/generate", token, {"duration": "15m", "max_budget": 0.01, **kw})
        if d.get("key"):
            keys.append(d["key"])
        return st, d

    try:
        print(f"=== setup on {args.env} (throwaway team + two co-members) ===")
        st, t = gw.call("/team/new", MK, {"team_alias": f"{PREFIX}-throwaway", "max_budget": 0.05})
        team_id = t.get("team_id")
        if not team_id:
            print(f"  FATAL: /team/new -> {st} {why(t)}")
            return 2
        for who in (alice, bob):
            gw.call("/user/new", MK, {"user_id": who, "user_role": "internal_user", "max_budget": 0.05})
            gw.call("/team/member_add", MK, {"team_id": team_id, "member": {"user_id": who, "role": "user"}})
        st, k = mint(MK, key_alias=f"{PREFIX}-alice-key", user_id=alice, team_id=team_id)
        AK = k.get("key", "")
        if not AK:
            print(f"  FATAL: could not mint member key -> {st} {why(k)}")
            return 2
        print(f"  team={team_id} members={alice},{bob}")

        # --- positive control: without this the negatives below prove nothing ---
        print("\n[T7 ] control: member CAN mint a team key")
        st, d = mint(AK, key_alias=f"{PREFIX}-control", team_id=team_id)
        if st == 200:
            print(f"       200 user_id={d.get('user_id')!r}  (rights confirmed)")
        else:
            print(f"       {st} {why(d)}")
            inconclusive.append(
                "T7 control failed: the member could not mint at all, so the negative "
                "results below do not demonstrate a boundary"
            )

        # --- the four forgeries ---
        checks = [
            (
                "T8 ",
                "mint with a NON-MEMBER user_id",
                lambda: mint(AK, key_alias=f"{PREFIX}-t8", team_id=team_id, user_id=f"{PREFIX}-outsider"),
                "user_id",
            ),
            (
                "T9 ",
                "mint into a team it does NOT belong to",
                lambda: mint(AK, key_alias=f"{PREFIX}-t9", team_id="TEAM_DX0021"),
                "team_id",
            ),
            (
                "T10",
                "rebind its OWN key's user_id",
                lambda: gw.call("/key/update", AK, {"key": AK, "user_id": bob}),
                "user_id",
            ),
            (
                "T11",
                "claim a CO-MEMBER's user_id",
                lambda: mint(AK, key_alias=f"{PREFIX}-t11", team_id=team_id, user_id=bob),
                "user_id",
            ),
        ]
        for tag, desc, fn, field in checks:
            st, d = fn()
            if st == 200:
                print(f"\n[{tag}] {desc}\n       *** FORGED (200, {field}={d.get(field)!r}) ***")
                failures.append(f"{tag}: {desc} SUCCEEDED -- {field} is forgeable")
            else:
                print(f"\n[{tag}] {desc}\n       blocked {st}: {why(d)}")

        # --- alias uniqueness: the premise DW-026's principal lookup rests on ---
        # DW-026 maps key_alias -> principal/role/scopes via Memotron's own registry.
        # A member picks their own alias at mint time, so if aliases could collide,
        # Alice mints Bob's alias and resolves as Bob. Uniqueness is what forbids that.
        print("\n[ALI] key_alias uniqueness (DW-026 principal lookup premise)")
        victim_alias = f"{PREFIX}-bob-privileged"
        st, _ = mint(MK, key_alias=victim_alias, user_id=bob, team_id=team_id)
        if st != 200:
            inconclusive.append("ALI: could not mint the victim-alias key; uniqueness untested")
        else:
            for tag, desc, fn in (
                (
                    "mint",
                    "member mints a key using another user's alias",
                    lambda: mint(AK, key_alias=victim_alias, team_id=team_id),
                ),
                (
                    "rename",
                    "member renames its own key onto that alias",
                    lambda: gw.call("/key/update", AK, {"key": AK, "key_alias": victim_alias}),
                ),
            ):
                st, d = fn()
                if st == 200:
                    print(f"       *** COLLIDED via {tag}: {desc} ***")
                    failures.append(f"ALI/{tag}: {desc} SUCCEEDED -- DW-026 principal lookup is forgeable")
                else:
                    print(f"       {tag:6} blocked {st}: {why(d)}")

        # --- regeneration survival ---
        print("\n[REG] fields surviving key regeneration")
        st, before = mint(MK, key_alias=f"{PREFIX}-regen", user_id=alice, team_id=team_id)
        st, after = gw.call(f"/key/{before.get('key', '')}/regenerate", MK, {})
        if after.get("key"):
            keys.append(after["key"])
        if st != 200:
            print(f"       could not regenerate: {why(after)}")
            inconclusive.append("REG: regeneration call failed; survival unverified")
        else:
            for f in ("user_id", "team_id", "key_alias"):
                b, a = before.get(f), after.get(f)
                ok = b == a and b is not None
                print(f"       {f:10} {b!r} -> {a!r}  {'ok' if ok else '*** LOST ***'}")
                if not ok:
                    failures.append(f"REG: {f} did not survive regeneration ({b!r}->{a!r})")
    finally:
        print("\n=== teardown ===")
        for k in keys:
            gw.call("/key/delete", MK, {"keys": [k]})
        if team_id:
            gw.call("/team/delete", MK, {"team_ids": [team_id]})
        for who in (alice, bob):
            gw.call("/user/delete", MK, {"user_ids": [who]})
        print(f"  removed {len(keys)} key(s), 1 team, 2 users")

    print()
    if inconclusive:
        for m in inconclusive:
            print(f"INCONCLUSIVE: {m}")
        print(f"\nprobe INCONCLUSIVE on {args.env} -- ADR 0008 neither confirmed nor refuted")
        return 2
    if failures:
        for m in failures:
            print(f"FAIL: {m}")
        print(f"\nprobe FAILED on {args.env} -- ADR 0008's binding is NOT safe here")
        return 1
    print(
        f"probe PASSED on {args.env} -- user_id/team_id unforgeable, key_alias unique, "
        f"identity survives regeneration; DW-026 + ADR 0008 hold"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
