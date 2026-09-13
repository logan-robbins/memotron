"""The exactly-once dream-work claim plane, shared by both engines.

Why these two methods can be shared at all
------------------------------------------
They are the exclusive-write path, and the engines could hardly be less alike
underneath: SQLite takes a ``BEGIN IMMEDIATE`` whole-database write lock, Postgres
takes a ``pg_advisory_xact_lock``. The bodies are nonetheless identical, and that
is not a coincidence to be exploited -- it is the ``exclusive_write_transaction``
seam working exactly as designed. The claim algorithm depends on the *contract*
"no other writer runs between the re-check and the write", and on nothing about
how that guarantee is obtained.

So sharing them is not a claim that the two engines behave the same. It is a
claim that the difference is entirely inside ``exclusive_write_transaction``, and
``tests/test_exclusive_write_conformance.py`` plus
``tests/test_dream_concurrency.py`` (#134) are what test that, on both engines.
If the seam ever leaks, those fail -- not this module.

The stale-claim window
----------------------
One constant, previously two. SQLite held a module-level
``DREAM_CLAIM_STALE_SECONDS = 900.0``; Postgres held a class attribute
``_DREAM_CLAIM_STALE_SECONDS = 900.0`` under a comment reading "Kept in sync by
value so ...". Two numbers kept equal by hand, in a default argument, is the kind
of thing that stays right until someone tunes one of them. Both names still exist
and both now resolve to the value defined here, so no import and no default
argument changed and the sync is structural instead of clerical.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from memotron.storage.base import normalize_key

# How long an unreleased dream-work claim stays live before another run may
# re-claim the work.  Every non-crash path releases its claims explicitly in
# ``DreamEngine.run_job``'s ``finally``, so expiry only matters after a hard
# kill.  15 minutes comfortably exceeds the slowest plausible live batch —
# ``max_items_per_run`` episodes through a gateway extraction whose transport
# already bounds each call with its own retry policy (3 attempts, sub-minute)
# — while bounding how long a crashed run can wedge its queue slice to one
# window.
DREAM_CLAIM_STALE_SECONDS = 900.0

if TYPE_CHECKING:
    from memotron.storage._shared._protocol import SharedPlaneBackend

    _Base = SharedPlaneBackend
else:
    _Base = object


class DreamClaimPlaneMixin(_Base):
    """Exactly-once claiming of dream work, over an engine-agnostic write lock."""

    def claim_episodes(
        self,
        episode_uuids: Sequence[str],
        *,
        consumer_key: str,
        run_uuid: str,
        now: datetime,
        stale_after_seconds: float = DREAM_CLAIM_STALE_SECONDS,
        limit: int | None = None,
    ) -> set[str]:
        """Atomically claim up to *limit* unprocessed episodes for one run.

        The claim and the processed re-check happen inside one
        ``exclusive_write_transaction``, so no two runs — same process or
        different processes — can both claim (and therefore both extract) the
        same episode for the same formation consumer.  An episode is claimable
        when it is not yet marked processed for *consumer_key* AND no other run
        holds an unexpired claim on it.  Returns the claimed episode uuids; a
        concurrent second run simply claims the remainder (possibly none).

        The lock is a ``BEGIN IMMEDIATE`` whole-database write lock on SQLite and
        a ``pg_advisory_xact_lock`` on Postgres.  This method must not be able to
        tell, and does not.
        """
        normalized_consumer = normalize_key(consumer_key)
        if not normalized_consumer:
            raise ValueError("consumer_key cannot be blank")
        if not run_uuid or not run_uuid.strip():
            raise ValueError("run_uuid cannot be blank")
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be greater than zero")
        cutoff = now - timedelta(seconds=stale_after_seconds)
        claimed: set[str] = set()
        with self.exclusive_write_transaction():
            for episode_uuid in episode_uuids:
                if limit is not None and len(claimed) >= limit:
                    break
                # Processed markers supersede claims: re-check under the writer
                # lock so a claim can never resurrect an already-processed
                # episode observed as pending before the lock was taken.
                if self.is_episode_processed(episode_uuid, consumer_key=normalized_consumer):
                    continue
                claim_key = f"episode:{episode_uuid}:{normalized_consumer}"
                if self._claim_is_live(claim_key, run_uuid=run_uuid, cutoff=cutoff):
                    continue
                self._write_claim(claim_key, run_uuid=run_uuid, now=now)
                claimed.add(episode_uuid)
        return claimed

    def claim_scope_work(
        self,
        *,
        work_kind: str,
        scope_key: str,
        consumer_key: str,
        run_uuid: str,
        now: datetime,
        stale_after_seconds: float = DREAM_CLAIM_STALE_SECONDS,
    ) -> bool:
        """Atomically claim one scope's *work_kind* pass for one run.

        Same contract as :meth:`claim_episodes` at scope granularity — used by
        consolidation, whose rollup synthesis is a non-idempotent
        ``add_relationship`` over a cluster selection read seconds earlier.
        """
        normalized_consumer = normalize_key(consumer_key)
        if not normalized_consumer:
            raise ValueError("consumer_key cannot be blank")
        if not work_kind or not work_kind.strip():
            raise ValueError("work_kind cannot be blank")
        if not scope_key or not scope_key.strip():
            raise ValueError("scope_key cannot be blank")
        if not run_uuid or not run_uuid.strip():
            raise ValueError("run_uuid cannot be blank")
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be greater than zero")
        cutoff = now - timedelta(seconds=stale_after_seconds)
        claim_key = f"{work_kind}:{scope_key}:{normalized_consumer}"
        with self.exclusive_write_transaction():
            if self._claim_is_live(claim_key, run_uuid=run_uuid, cutoff=cutoff):
                return False
            self._write_claim(claim_key, run_uuid=run_uuid, now=now)
        return True
