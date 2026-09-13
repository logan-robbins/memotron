"""
research-temporal prompt profiles.

These profiles are designed for high-fidelity temporal memory extraction. They target:
- Explicit valid_from / valid_to extraction with calendar-phrase conversion
- Node property capture that reflects entity state at the moment of the episode
- Supersession-aware extraction that keeps the same predicate so single-active
  cardinality fires correctly
- Confidence calibration tied to source quality, not LLM hedging

Profile index
─────────────
research-temporal@v1   General-purpose temporally-precise extraction with three
                       annotated few-shot examples covering the main update patterns:
                       property evolution, temporary facts, and fact supersession.

research-temporal@v2   Stricter variant that requires a source_text quote for every
                       extracted memory and refuses low-confidence speculation.
                       Intended for high-stakes compliance and operator-correction flows.
"""

from __future__ import annotations

from memotron.config import DreamPromptProfile, FewShotExample

PROFILES: tuple[DreamPromptProfile, ...] = (
    DreamPromptProfile(
        name="research-temporal",
        version="v1",
        goal=(
            "Extract temporally precise, entity-rich memories with explicit validity windows, "
            "supersession awareness, and full subject/object property capture."
        ),
        include=(
            "All facts with an explicit or inferable effective date, expiry, or temporal qualifier.",
            "Entity-state attributes (tier, region, system, role, kind) that were true at the episode reference time.",
            "Facts that contradict existing graph context with the same subject and predicate — "
            "extract the new version with the correct valid_from so the graph can supersede the old one.",
            "Temporary facts with an explicit end date — include valid_to so pruning does not need to infer expiration.",
            "Facts that reinforce existing graph context, but only when new evidence or a newer "
            "reference_time is present in this episode.",
        ),
        exclude=(
            "Unverified speculation or hypotheticals without an asserting subject.",
            "Duplicate facts already represented in graph context with the same valid_from, subject, predicate, and object.",
            "Transient routing, UI state, or session-local details with no future operational value.",
            "Personally sensitive details that are not necessary for the configured memory type.",
            "Facts below 0.60 confidence — do not guess.",
        ),
        rules=(
            "State each memory as exactly one atomic subject–predicate–object triple.",
            "When the episode text contains 'previously', 'until', 'as of', 'starting from', "
            "or 'effective', use that phrase as the temporal anchor for valid_from or valid_to.",
            "When the source entity has changed properties (tier upgraded, region changed), "
            "update subject_properties to reflect the new state at valid_from.",
            "When a fact corrects a prior fact for the same subject–predicate key, keep the same "
            "predicate so single-active supersession fires automatically in the graph.",
            "Prefer source_text quotes that directly support the extracted fact over paraphrases.",
            "Confidence should reflect source reliability: "
            "operator-injected correction > explicit policy statement > chat transcript > inferred context.",
            "If valid_to cannot be determined precisely, prefer a conservative estimate (end of stated period) "
            "over omitting it when the source clearly indicates a temporary fact.",
        ),
        few_shot_examples=(
            FewShotExample(
                description="Entity property evolution with precise temporal anchor",
                episode_body=(
                    "Acme Parks was upgraded to enterprise tier at the start of Q2 2026 and now requires "
                    "both SOC2 and ISO27001 compliance before any vendor is approved. The tier change "
                    "was retroactively effective April 1."
                ),
                reference_time_note="episode reference_time = 2026-06-04T00:00:00Z",
                expected_memories=(
                    {
                        "subject": "Acme Parks",
                        "predicate": "requires",
                        "object": "SOC2 and ISO27001 compliance before vendor approval",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.97,
                        "valid_from": "2026-04-01T00:00:00Z",
                        "valid_to": None,
                        "subject_label": "Entity",
                        "object_label": "Entity",
                        "subject_properties": {"tier": "enterprise"},
                        "object_properties": {"kind": "compliance requirement"},
                        "source_text": ("requires both SOC2 and ISO27001 compliance before any vendor is approved"),
                        "metadata": {"temporal_anchor": "Q2 2026 / April 1"},
                    },
                ),
            ),
            FewShotExample(
                description="Temporary preference with explicit expiry and reason",
                episode_body=(
                    "User 42 needs phone support only until the end of this week — she has a laptop "
                    "issue and cannot use the chat interface. After that she will return to her normal "
                    "chat preference."
                ),
                reference_time_note="episode reference_time = 2026-06-02T09:00:00Z",
                expected_memories=(
                    {
                        "subject": "User 42",
                        "predicate": "prefers",
                        "object": "phone support",
                        "relationship_type": "PREFERS",
                        "confidence": 0.93,
                        "valid_from": "2026-06-02T09:00:00Z",
                        "valid_to": "2026-06-07T23:59:59Z",
                        "subject_label": "Entity",
                        "object_label": "Entity",
                        "subject_properties": {},
                        "object_properties": {"kind": "support channel"},
                        "source_text": "needs phone support only until the end of this week",
                        "metadata": {"reason": "laptop issue"},
                    },
                ),
            ),
            FewShotExample(
                description=(
                    "Supersession — new requirement replaces old, same predicate, graph context shows prior active fact"
                ),
                episode_body=(
                    "Correction confirmed by compliance: Acme Parks now requires ISO27001 only — "
                    "the SOC2 requirement was removed after the June 1 audit. This is effective June 1."
                ),
                reference_time_note=(
                    "episode reference_time = 2026-06-04T00:00:00Z; "
                    "graph context includes ACTIVE relationship: "
                    "Acme Parks REQUIRES SOC2 compliance (valid_from 2026-04-01)"
                ),
                expected_memories=(
                    {
                        "subject": "Acme Parks",
                        "predicate": "requires",
                        "object": "ISO27001 compliance before vendor approval",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.98,
                        "valid_from": "2026-06-01T00:00:00Z",
                        "valid_to": None,
                        "subject_label": "Entity",
                        "object_label": "Entity",
                        "subject_properties": {"tier": "enterprise"},
                        "object_properties": {"kind": "compliance requirement"},
                        "source_text": (
                            "requires ISO27001 only — the SOC2 requirement was removed after the June 1 audit"
                        ),
                        "metadata": {"correction": True, "audit_date": "2026-06-01"},
                    },
                ),
            ),
        ),
    ),
    DreamPromptProfile(
        name="research-temporal",
        version="v2",
        goal=(
            "Extract temporally precise memories for high-stakes compliance and correction flows. "
            "Every memory must have a source_text quote and meet a 0.80 confidence floor. "
            "Reject anything speculative, hedged, or unverifiable from the episode text."
        ),
        include=(
            "Explicitly stated requirements, policies, approvals, and compliance obligations.",
            "Operator-confirmed corrections that supersede prior active facts.",
            "Temporary facts with a stated expiry — include valid_to.",
            "Entity properties changed in this episode — record the new state at valid_from.",
        ),
        exclude=(
            "Inferred or speculative facts not directly stated in the episode text.",
            "Facts with confidence below 0.80 — do not extract uncertain information.",
            "Facts copied from graph context without independent support in this episode.",
            "Transient details (routing, UI state, in-progress status) with no durable graph value.",
        ),
        rules=(
            "Every extracted memory must have a non-empty source_text that quotes the episode.",
            "Emit an independently corroborated active fact so the graph can record a new observation and reinforce it deterministically.",
            "Do not extract a memory if you cannot find a direct quote that supports it.",
            "Confidence 0.95+ for operator-confirmed or policy-stated facts. "
            "Confidence 0.80–0.94 for clearly implied but not explicitly confirmed facts.",
            "Always use the same predicate as the prior active fact when extracting a correction, "
            "so the graph's single-active supersession fires without requiring a repair job.",
            "Include valid_to for any fact the source marks as temporary, conditional, or time-bounded.",
            "Record subject_properties and object_properties when the source states entity attributes "
            "that were true at valid_from — do not infer properties not mentioned in the episode.",
        ),
    ),
)
