"""Stage 1: one episode plus one motive to validated claims to ledger entries.

The first stage of the caveman flow, and the only one that reads raw turns. It
appends and does nothing else: no node is created, no node is read, no binding is
resolved. ``node_ids`` is left empty on every entry it writes, because resolving a
surface name to a concept needs the graph and this stage has never seen one.

**This module imports nothing from the node graph, and a test proves it** by
walking this file's own import statements. The invariant matters because it is
what keeps extraction reproducible: a stage that could see the graph would
extract different claims from the same episode depending on what was already
known, and the ledger would stop being a faithful record of what was said.

.. rubric:: Where each validation rule lives

``ExtractResponse`` enforces everything the response can check on its own — the
claim length, the sigil, the speech act, one-to-four subjects, and the rule that
``supersedes_claim_index`` points strictly backwards. This module enforces the
two rules that need the episode:

* every ``turns`` member is a real turn index, and
* **every identifier appears verbatim in the episode text.**

Both reject the whole response. A partial accept would put a hallucinated
identifier in the ledger next to real ones, and identifiers are the highest-value
tokens in the system — an issue number or a model alias is exactly what a later
session would otherwise pay a search to re-find.

.. rubric:: The identifier rule, exactly

An identifier is accepted when it is a **case-sensitive substring** of
``Episode.as_prompt_text()`` — the same string the model was shown, so the check
and the prompt cannot disagree. Deliberately not looser and not stricter:

* **Case-sensitive**, because case is part of an identifier's identity.
  ``CLAUDE-HAIKU-4-5`` is not what the episode said, and the dreamer will copy
  whatever the ledger holds into a node line verbatim.
* **Substring, not token match**, because a word-boundary rule would reject
  ``3072`` quoted from ``3072-dimensional`` and ``#245`` quoted from ``(#245)``,
  both of which are the model doing the right thing. Substring is the loosest
  deterministic rule that still catches the failure it exists for: an identifier
  the model never read. An invented ``#999`` is absent from the text and fails.
* **No normalisation of whitespace or punctuation**, so the rule can be stated in
  one line and produces the same verdict on every machine.

.. rubric:: Receipts stay content-free

The rejection receipt names the claim positions and how many identifiers failed;
it never carries the identifier itself. The raised
:class:`~memotron.caveman.errors.OutOfContractResponse` does name it, because
that is a developer-facing exception at the moment of failure rather than a
durable audit row. Same rule as
:mod:`memotron.caveman.llm`, which strips pydantic's ``input_value`` for
exactly this reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from memotron.caveman.errors import OutOfContractResponse
from memotron.caveman.llm import CONTRACT_CLOSING_LINE, strict_json_call
from memotron.caveman.models import (
    MAX_CLAIM_TEXT,
    ClaimKind,
    ClaimMode,
    Episode,
    ExtractResponse,
    FactKind,
    LedgerEntry,
    Receipt,
    ReceiptOp,
)
from memotron.caveman.motive import CavemanMotive, render_motive_block
from memotron.caveman.receipts import canonical_digest, new_receipt_id
from memotron.caveman.render import format_prompt_block
from memotron.caveman.seams import LedgerStore, ReceiptSink
from memotron.synthesis import SynthesisTransport

EXTRACT_SOURCE = "EXTRACT"
"""Names this stage in the parse error, on the exception, and in the receipt.

Reads correctly inside ``parse_first_json_object``'s own message: *"EXTRACT
content did not contain a valid JSON object"*.
"""

ENTRY_ID_PREFIX = "e-"
"""Ledger entry ids are ``e-<uuid4 hex>``.

Random rather than derived from the episode and the claim's position, for the
same reason :func:`~memotron.caveman.receipts.new_receipt_id` is random: a
derived id would collide the moment one episode is legitimately extracted twice
-- under a second motive, or after a prompt change -- and
``LedgerStore.append`` raises on a repeated id. Keeping the id meaningless keeps
that duplicate check a real defect check rather than a policy nobody chose.
"""

EXTRACT_SYSTEM = """\
You read one raw episode of conversation and emit the claims worth knowing.

You judge "worth knowing" ONLY by the motive rubric below. Nothing else.

You have never seen the memory graph and you must not guess at one. Do not
normalise, merge or rename anything: name the concepts a claim is about using
the episode's OWN surface words, exactly as they appear in the turns.

Each claim is SELF-CONTAINED: resolvable by someone who never reads the
episode, in compact information-dense text (fragments are fine, grammar is
optional, identifiers verbatim). "It was fixed in #245" is not a claim;
"gateway Host-header refusal fixed in #245" is.

Copy identifiers -- issue numbers, model aliases, counts, dimensions, versions --
character for character out of the episode. An identifier you did not read in
the episode is a fabrication, and the whole response is rejected for it.

List the claims in the order the episode states them: earliest turn first. Not
by importance -- ordering by importance would put a correction ahead of the thing
it corrects, and the supersession rule below could then not be obeyed at all.

Where a later turn corrects something claimed in an earlier turn of this SAME
episode, emit BOTH claims, in turn order, and set supersedes_claim_index on the
LATER one:

  - The later claim -- the correction -- carries
    supersedes_claim_index = the list index of the earlier claim it replaces.
  - The earlier claim -- the one being replaced -- carries null. It does not
    point at its own correction.

Read the field as "the index of the claim THIS claim supersedes", never "the
index of the claim that supersedes this one". It always points BACKWARDS, to a
smaller index than its own, and never at itself.\
"""


def _contract_block() -> str:
    """The literal JSON contract, one worked example, and the field rules.

    The claim-kind and speech-act lists are generated from
    :class:`~memotron.caveman.models.ClaimKind` and
    :class:`~memotron.caveman.models.ClaimMode` rather than written out, so a
    seventh kind or a new mode cannot be added without the prompt learning
    about it.
    """
    return (
        "ANSWER SHAPE\n"
        '{"claims": [{\n'
        '  "claim": "Every real run of Memotron goes through the JedAI Gateway; '
        'hermetic mode is never used for a real run.",\n'
        '  "kind": "rule", "claim_mode": "directive",\n'
        '  "subjects": ["the JedAI Gateway"], "objects": [],\n'
        '  "identifiers": [], "supersedes_claim_index": null,\n'
        '  "confidence": 0.95, "turns": [1]\n'
        "}]}\n"
        "\n"
        "A CORRECTION, WORKED. Turn 4 said 24; turn 9 corrected it to 31. Both\n"
        "claims are emitted, in turn order, and the LATER one carries the pointer:\n"
        '{"claims": [\n'
        '  {"claim": "The C4 memory server returns 24 tools.",\n'
        '   "kind": "attribute", "claim_mode": "report", "subjects": ["C4"], "objects": [],\n'
        '   "identifiers": ["24"], "supersedes_claim_index": null,\n'
        '   "confidence": 0.9, "turns": [4]},\n'
        '  {"claim": "The C4 memory server returns 31 tools after #248, not 24.",\n'
        '   "kind": "attribute", "claim_mode": "correction", "subjects": ["C4"], "objects": [],\n'
        '   "identifiers": ["31", "#248", "24"], "supersedes_claim_index": 0,\n'
        '   "confidence": 0.95, "turns": [9]}\n'
        "]}\n"
        "Index 0 is the claim being replaced. The replaced claim points at nothing.\n"
        "\n"
        "FIELD RULES\n"
        "  claims                  1 to 64 of them, in turn order, earliest first.\n"
        f"  claim                   8 to {MAX_CLAIM_TEXT} characters, ONE line. Self-contained,\n"
        "                          compact, grammar optional.\n"
        f"  kind                    one of: {', '.join(kind.value for kind in ClaimKind)}\n"
        f"  claim_mode              one of: {', '.join(mode.value for mode in ClaimMode)}\n"
        "                          what the speaker was DOING, not what the line is.\n"
        "  subjects                1 to 4 surface names, the episode's own words.\n"
        "  objects                 the other end of a relation, same rule. May be empty.\n"
        "  identifiers             every identifier the claim carries, verbatim from\n"
        "                          the episode. An identifier absent from the episode\n"
        "                          rejects the whole answer.\n"
        "  supersedes_claim_index  null, or the index of an EARLIER claim in this list --\n"
        "                          the one THIS claim replaces. Strictly less than this\n"
        "                          claim's own index. Set it on the correction, never on\n"
        "                          the claim being corrected.\n"
        "  confidence              0.0 to 1.0.\n"
        "  turns                   the turn numbers this claim came from. Real ones only.\n"
    )


def render_extract_prompt(episode: Episode, motive: CavemanMotive) -> str:
    """The user prompt stage 1 sends: format, motive, contract, then the episode.

    The order is the one every caveman prompt uses -- format block, motive
    block, literal contract, then the closing line -- with the episode last
    before the closing line so the text being read is nearest the instruction to
    answer.

    The episode is rendered by :meth:`~memotron.caveman.models.Episode.as_prompt_text`
    and by nothing else, which is what makes the identifier check provably a
    check against what the model actually saw.
    """
    return (
        # Every kind: stage 1 emits CLAIMS and no facts at all, so the block is
        # here to teach what a belief IS rather than to bound this answer.
        format_prompt_block(max_facts=motive.max_facts_per_node, kinds=tuple(FactKind))
        + "\n"
        + render_motive_block(motive, stage="extract")
        + "\n"
        + _contract_block()
        + "\n"
        "EPISODE\n"
        f"{episode.as_prompt_text()}\n"
        "\n"
        f"{CONTRACT_CLOSING_LINE}\n"
    )


def _new_entry_id() -> str:
    return f"{ENTRY_ID_PREFIX}{uuid.uuid4().hex}"


def _unreal_turns(episode: Episode, response: ExtractResponse) -> tuple[tuple[int, int], ...]:
    """``(claim position, turn index)`` for every turn number the episode lacks."""
    real = episode.turn_indices()
    return tuple(
        (position, turn) for position, spec in enumerate(response.claims) for turn in spec.turns if turn not in real
    )


def _unquoted_identifiers(episode: Episode, response: ExtractResponse) -> tuple[tuple[int, str], ...]:
    """``(claim position, identifier)`` for every identifier not in the episode.

    A blank identifier fails too: it is a substring of everything, so accepting
    it would let an empty string pass a check whose entire purpose is that the
    model read the token it is quoting.
    """
    text = episode.as_prompt_text()
    return tuple(
        (position, identifier)
        for position, spec in enumerate(response.claims)
        for identifier in spec.identifiers
        if not identifier.strip() or identifier not in text
    )


def _stage_rejection(
    episode: Episode,
    response: ExtractResponse,
) -> tuple[tuple[str, ...], str] | None:
    """The episode-dependent rules. Returns ``(exception errors, receipt detail)``.

    Two return channels because they carry different things on purpose: the
    exception names the offending value for whoever is reading the failure now,
    and the receipt detail names only positions and counts, so the audit stream
    stays content-free.
    """
    unreal = _unreal_turns(episode, response)
    unquoted = _unquoted_identifiers(episode, response)
    if not unreal and not unquoted:
        return None

    errors: list[str] = [
        f"claims.{position}.turns: turn {turn} is not a turn in this episode" for position, turn in unreal
    ]
    errors += [
        f"claims.{position}.identifiers: {identifier!r} does not appear verbatim in the episode"
        for position, identifier in unquoted
    ]

    parts: list[str] = []
    if unreal:
        positions = ",".join(str(position) for position, _ in unreal)
        parts.append(f"{len(unreal)} turn reference(s) outside the episode at claims [{positions}]")
    if unquoted:
        positions = ",".join(str(position) for position, _ in unquoted)
        parts.append(f"{len(unquoted)} identifier(s) not verbatim in the episode at claims [{positions}]")
    return tuple(errors), "; ".join(parts)


def _entries_from(
    response: ExtractResponse,
    *,
    episode: Episode,
    motive: CavemanMotive,
    receipt_id: str,
    now: datetime,
) -> tuple[LedgerEntry, ...]:
    """One entry per claim, with ``supersedes`` resolved to a sibling entry id.

    Every id is minted before any entry is built, because ``supersedes`` points
    at a sibling and the sibling's id has to exist to be pointed at. The index is
    already known to point strictly backwards -- ``ExtractResponse`` rejects
    anything else -- so the lookup cannot miss.
    """
    entry_ids = [_new_entry_id() for _ in response.claims]
    return tuple(
        LedgerEntry(
            entry_id=entry_ids[position],
            ts=now,
            episode_id=episode.episode_id,
            scope=episode.scope,
            claim=spec.claim,
            kind=spec.kind,
            claim_mode=spec.claim_mode,
            subjects=spec.subjects,
            objects=spec.objects,
            identifiers=spec.identifiers,
            node_ids=(),
            motive=motive.name,
            confidence=spec.confidence,
            supersedes=(None if spec.supersedes_claim_index is None else entry_ids[spec.supersedes_claim_index]),
            turns=spec.turns,
            receipt_id=receipt_id,
        )
        for position, spec in enumerate(response.claims)
    )


async def extract(
    *,
    episode: Episode,
    motive: CavemanMotive,
    transport: SynthesisTransport,
    ledger: LedgerStore,
    receipts: ReceiptSink,
    now: datetime,
) -> tuple[LedgerEntry, ...]:
    """Extract one episode's claims under one motive and append them to the ledger.

    One LLM call. Returns the appended entries in claim order, every one with
    ``node_ids=()``: binding them is stage 2's job.

    Raises :class:`~memotron.caveman.errors.OutOfContractResponse` if the
    model answers out of contract, whether the violation was structural (caught
    by ``ExtractResponse``) or episode-dependent (caught here). Either way
    exactly one ``EXTRACT_REJECTED`` receipt is emitted and **no entry is
    appended** -- a rejection is all-or-nothing, so the ledger never holds half a
    response.

    The one accepted receipt is emitted after the last append, so a receipt
    saying claims were accepted is never left behind by a failed write.
    """
    system_prompt = EXTRACT_SYSTEM
    user_prompt = render_extract_prompt(episode, motive)
    response = await strict_json_call(
        transport=transport,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_model=ExtractResponse,
        source=EXTRACT_SOURCE,
        receipts=receipts,
        scope=episode.scope,
        subject=episode.episode_id,
        reject_op=ReceiptOp.EXTRACT_REJECTED,
        now=now,
    )

    inputs_digest = canonical_digest({"system": system_prompt, "user": user_prompt})
    outputs_digest = canonical_digest(response.model_dump(mode="json"))

    rejection = _stage_rejection(episode, response)
    if rejection is not None:
        errors, detail = rejection
        receipts.emit(
            Receipt(
                receipt_id=new_receipt_id(),
                op=ReceiptOp.EXTRACT_REJECTED,
                ts=now,
                scope=episode.scope,
                subject=episode.episode_id,
                inputs_digest=inputs_digest,
                outputs_digest=outputs_digest,
                detail=detail,
            )
        )
        raise OutOfContractResponse(source=EXTRACT_SOURCE, errors=errors, raw_digest=outputs_digest[:16])

    receipt_id = new_receipt_id()
    entries = _entries_from(response, episode=episode, motive=motive, receipt_id=receipt_id, now=now)
    for entry in entries:
        ledger.append(entry)

    superseding = sum(1 for entry in entries if entry.supersedes is not None)
    receipts.emit(
        Receipt(
            receipt_id=receipt_id,
            op=ReceiptOp.EXTRACT_ACCEPTED,
            ts=now,
            scope=episode.scope,
            subject=episode.episode_id,
            inputs_digest=inputs_digest,
            outputs_digest=outputs_digest,
            detail=(
                f"{len(entries)} claim(s) appended under motive {motive.name!r}, "
                f"{superseding} superseding, from {len(episode.turns)} turn(s)"
            ),
        )
    )
    return entries
