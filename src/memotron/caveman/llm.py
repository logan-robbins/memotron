"""One strict-JSON exchange: render, call, strip fences, parse, validate, receipt.

The only behaviour all four stages share, so the only thing that belongs in a
module none of them owns. It **composes** the transport and never subclasses it:
the transport's job ends at "here is the model's text", and everything after that
is contract enforcement.

.. rubric:: There is no repair pass

No re-prompt, no relaxed fallback model, no "ask it again more firmly". A repair
loop is a second, unreviewed code path whose behaviour depends on a model's
second guess, and it hides the prompt defect that caused the violation. Transport
retry is ``_post_with_retry``'s job (the gateway layer, for failures that never
got a verdict); a contract violation got a verdict, and it is a defect in the
prompt or the model choice, so it surfaces as one — receipted with the raw
digest, so a systematic violation is visible in the receipt stream rather than
absorbed by a retry that eventually succeeds.

.. rubric:: One known footgun

``parse_first_json_object`` is tolerant by design and wraps a bare top-level
array rather than rejecting it (``extraction.py:115-138``): an array of objects
carrying ``name`` and no relation endpoints becomes ``{"entities": [...],
"relations": []}``, and any other array becomes ``{"memories": [...]}``. So a
model that answers an extract prompt with a bare array of claims fails with
*"claims: Field required"* rather than with a parse error. That is the correct
outcome — it IS out of contract — but the error names the missing field rather
than the real mistake, so read a ``claims``/``bindings``/``lines``/``ops``
"Field required" on a non-empty response as "the model sent a bare array".
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ValidationError

from memotron.caveman.errors import OutOfContractResponse
from memotron.caveman.models import Receipt, ReceiptOp
from memotron.caveman.receipts import canonical_digest, new_receipt_id, text_digest
from memotron.caveman.seams import ReceiptSink
from memotron.extraction import parse_first_json_object
from memotron.synthesis import SynthesisTransport, strip_markdown_fences

MAX_RECEIPTED_ERRORS = 3
"""Validation errors carried on a reject receipt.

Three is enough to diagnose a systematic violation and few enough to stay inside
``Receipt.detail``'s 600-character bound. A model that broke the contract twelve
ways broke it one way twelve times.
"""

DETAIL_LIMIT = 600
"""Mirrors ``Receipt.detail``'s own 600-character ceiling.

A long error truncates rather than failing the receipt that was supposed to
explain it -- a rejection that cannot be recorded because its explanation is too
long is the worst possible outcome of this path.
"""

CONTRACT_CLOSING_LINE = "Answer with one JSON object and nothing else."
"""The closing line every caveman prompt ends with.

Here rather than in each stage so all four say it identically -- the gateway
discards ``response_format`` (``gateway.py:432-434``), so this sentence is the
only thing asking for a bare object, and four paraphrases of it would be four
different requests.
"""


def _error_lines(error: ValidationError) -> tuple[str, ...]:
    """The first few validation failures as ``field: message`` strings.

    Deliberately not the pydantic repr: that includes ``input_value``, which
    carries the model's own content into a receipt that is supposed to be
    content-free.
    """
    lines: list[str] = []
    for detail in error.errors()[:MAX_RECEIPTED_ERRORS]:
        location = ".".join(str(part) for part in detail["loc"]) or "<root>"
        lines.append(f"{location}: {detail['msg']}")
    return tuple(lines)


async def strict_json_call[R: BaseModel](
    *,
    transport: SynthesisTransport,
    system_prompt: str,
    user_prompt: str,
    response_model: type[R],
    source: str,
    receipts: ReceiptSink,
    scope: str,
    subject: str,
    reject_op: ReceiptOp,
    now: datetime,
) -> R:
    """Call the model and return a validated *response_model*, or raise.

    On any content failure — unparseable text, a payload the model rejects —
    emits exactly one *reject_op* receipt and then raises
    :class:`~memotron.caveman.errors.OutOfContractResponse`. The receipt is
    emitted **before** the raise, so a stage that lets the exception propagate
    still leaves the rejection in the audit stream.

    A transport-level failure (missing key, HTTP error, malformed provider
    envelope) raises ``ValueError`` from the transport and is deliberately NOT
    receipted here: it is not a decision, and the gateway layer has already
    exhausted its retries. Turning it into a rejection receipt would put "the
    gateway was down" and "the prompt is wrong" in the same bucket.

    Args:
        source: Names the stage in the parse error and on the exception, e.g.
            ``"EXTRACT"``. It is what ``parse_first_json_object`` puts in its
            own message, so it must read well inside
            ``"{source} content did not contain a valid JSON object"``.
        subject: What the decision was about — an episode id, a node id, a
            batch key. Rides the receipt so a rejection is attributable.
    """
    raw = await transport.synthesize(user_prompt, system_prompt=system_prompt)
    inputs_digest = canonical_digest({"system": system_prompt, "user": user_prompt})

    def reject(errors: tuple[str, ...]) -> OutOfContractResponse:
        outputs_digest = text_digest(raw)
        receipts.emit(
            Receipt(
                receipt_id=new_receipt_id(),
                op=reject_op,
                ts=now,
                scope=scope,
                subject=subject,
                inputs_digest=inputs_digest,
                outputs_digest=outputs_digest,
                detail="; ".join(errors)[:DETAIL_LIMIT],
            )
        )
        return OutOfContractResponse(source=source, errors=errors, raw_digest=outputs_digest[:16])

    try:
        payload = parse_first_json_object(strip_markdown_fences(raw), source=source)
    except ValueError as exc:
        raise reject((str(exc),)) from exc

    try:
        return response_model.model_validate(payload)
    except ValidationError as exc:
        raise reject(_error_lines(exc)) from exc
