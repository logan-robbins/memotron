"""WS-9: Multimodal ingestion — MLLM-to-text normalization stage.

Design
------
``MultimodalNormalizer`` is a Protocol defining the normalization contract:
given an artifact (any modality), return a plain-text string suitable for
feeding into the existing dream pipeline unchanged.

``LocalMultimodalNormalizer`` is the hermetic default.  It handles all
supported modalities deterministically with zero network calls and zero new
dependencies (pure Python stdlib).

Modality handling
-----------------
``structured``  — JSON / dict / records: flattened to readable key=value lines.
``code``        — source text: passed through with a code-location citation header.
``document``    — prose text: stripped and returned as-is.
``text``        — alias for document; passthrough.
``image`` / ``diagram`` / ``screenshot`` / ``audio``
                — The caller MUST supply at least one textual hint
                  (``caption``, ``alt_text``, ``ocr_text``, or ``transcript``).
                  The normalizer merges them into a readable text block.

                  MLLM SEAM: In production, swap ``LocalMultimodalNormalizer``
                  for a ``MlmmMultimodalNormalizer`` that calls a vision/audio
                  LLM to generate the textual representation when no textual hint
                  is supplied.  The seam is the ``mllm_transport`` slot on
                  ``LocalMultimodalNormalizer.__init__``; when a transport is
                  provided, it is called before falling back to the hint-merge.
                  The transport must implement:
                      def normalize(self, artifact: Artifact) -> str

                  Until a real MLLM transport is wired in, the default normalizer
                  raises ``ValueError`` if an image-family artifact arrives with no
                  textual hint — this is the "fail fast" canonical path.

Provenance
----------
Every normalized artifact carries an ``ArtifactProvenance`` block that flows
through the episode metadata and into the graph relationship properties.  The
provenance fields are:

    artifact_id       — caller-supplied or auto-generated UUID
    artifact_type     — the raw modality string (e.g. "image", "code", "structured")
    artifact_location — source coordinate (file path + line range, URL, filename…)
    artifact_checksum — optional SHA-256 hex digest of the raw content

These fields appear in:
    - the ``Episode.metadata`` that ``add_artifact`` creates
    - the ``ExtractedMemory.metadata`` threaded through the dream pipeline
    - the materialized relationship ``properties["metadata"]`` in the graph

This means ``memory_evidence(relationship_uuid)`` on any fact that originated
from an artifact will show ``metadata["artifact_id"]``, ``artifact_type``, and
``artifact_location`` — satisfying the provenance round-trip requirement.

Supported modalities (canonical set)
-------------------------------------
TEXT_MODALITIES     = {"text", "document"}
CODE_MODALITIES     = {"code"}
STRUCTURED_MODALITIES = {"structured"}
IMAGE_MODALITIES    = {"image", "diagram", "screenshot", "audio"}

Any modality outside this union raises ``ValueError`` at normalization time.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol
from uuid import uuid4

# ---------------------------------------------------------------------------
# Modality constants
# ---------------------------------------------------------------------------

TEXT_MODALITIES: frozenset[str] = frozenset({"text", "document"})
CODE_MODALITIES: frozenset[str] = frozenset({"code"})
STRUCTURED_MODALITIES: frozenset[str] = frozenset({"structured"})
IMAGE_MODALITIES: frozenset[str] = frozenset({"image", "diagram", "screenshot", "audio"})

_ALL_MODALITIES: frozenset[str] = TEXT_MODALITIES | CODE_MODALITIES | STRUCTURED_MODALITIES | IMAGE_MODALITIES


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class ArtifactProvenance:
    """Source coordinates for a multimodal artifact.

    Parameters
    ----------
    artifact_id:
        Stable caller-supplied identifier for the artifact (e.g. a git SHA,
        a CMS asset ID, a ticket number).  Auto-generated UUID if not supplied.
    artifact_type:
        The raw modality string (e.g. ``"image"``, ``"code"``, ``"structured"``).
    artifact_location:
        A human-readable source coordinate — file path + optional line range for
        code (``"src/auth.py:L120-L145"``), URL or filename for docs/images.
    artifact_checksum:
        Optional SHA-256 hex digest of the raw artifact payload, enabling
        change-detection without storing the payload.
    """

    __slots__ = ("artifact_checksum", "artifact_id", "artifact_location", "artifact_type")

    def __init__(
        self,
        *,
        artifact_type: str,
        artifact_location: str = "",
        artifact_id: str | None = None,
        artifact_checksum: str | None = None,
    ) -> None:
        self.artifact_id: str = artifact_id.strip() if artifact_id and artifact_id.strip() else str(uuid4())
        self.artifact_type: str = artifact_type
        self.artifact_location: str = artifact_location
        self.artifact_checksum: str | None = artifact_checksum

    def as_metadata(self) -> dict[str, Any]:
        """Return a flat dict suitable for embedding in episode / relationship metadata."""
        meta: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "artifact_location": self.artifact_location,
        }
        if self.artifact_checksum is not None:
            meta["artifact_checksum"] = self.artifact_checksum
        return meta


# ---------------------------------------------------------------------------
# Artifact input model
# ---------------------------------------------------------------------------


class Artifact:
    """Input carrier for a multimodal artifact.

    Parameters
    ----------
    modality:
        One of the supported modality strings (see module-level constants).
    payload:
        The artifact's content.  For text/code/document modalities this is
        a plain ``str``.  For ``structured``, it may be a ``str`` (raw JSON)
        or a ``dict``/``list`` (Python object).  For image-family modalities,
        a ``bytes`` payload is accepted but the default normalizer requires
        at least one of the textual hints below.
    caption:
        Human-supplied image/diagram caption (image-family modalities).
    alt_text:
        Accessibility alt-text for an image.
    ocr_text:
        OCR output for images/screenshots.
    transcript:
        Transcript for audio.
    location:
        Source coordinate string (file path + line range, URL, …).
    artifact_id:
        Stable caller-supplied artifact identifier (auto-UUID if omitted).
    checksum:
        Optional SHA-256 hex digest of the raw payload.  When omitted and
        the payload is a ``str`` or ``bytes``, ``Artifact.compute_checksum()``
        can generate it.
    metadata:
        Arbitrary caller metadata merged into the episode metadata block.
    """

    __slots__ = (
        "alt_text",
        "artifact_id",
        "caption",
        "checksum",
        "location",
        "metadata",
        "modality",
        "ocr_text",
        "payload",
        "transcript",
    )

    def __init__(
        self,
        *,
        modality: str,
        payload: str | bytes | dict[str, Any] | list[Any],
        caption: str | None = None,
        alt_text: str | None = None,
        ocr_text: str | None = None,
        transcript: str | None = None,
        location: str = "",
        artifact_id: str | None = None,
        checksum: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized_modality = modality.strip().lower()
        if normalized_modality not in _ALL_MODALITIES:
            raise ValueError(
                f"Unsupported artifact modality: {modality!r}. Supported modalities: {sorted(_ALL_MODALITIES)}"
            )
        self.modality: str = normalized_modality
        self.payload = payload
        self.caption: str | None = caption
        self.alt_text: str | None = alt_text
        self.ocr_text: str | None = ocr_text
        self.transcript: str | None = transcript
        self.location: str = location.strip()
        self.artifact_id: str | None = artifact_id.strip() if artifact_id and artifact_id.strip() else None
        self.checksum: str | None = checksum
        self.metadata: dict[str, Any] = dict(metadata or {})

    def provenance(self) -> ArtifactProvenance:
        """Build the provenance descriptor for this artifact."""
        checksum = self.checksum
        if checksum is None:
            payload = self.payload
            if isinstance(payload, (str, bytes)):
                raw = payload.encode("utf-8") if isinstance(payload, str) else payload
                checksum = hashlib.sha256(raw).hexdigest()
        return ArtifactProvenance(
            artifact_id=self.artifact_id,
            artifact_type=self.modality,
            artifact_location=self.location,
            artifact_checksum=checksum,
        )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class MultimodalNormalizer(Protocol):
    """Convert a multimodal artifact to a plain-text representation.

    The returned text is fed into the existing dream-extraction pipeline
    unchanged.  Implementations must be deterministic for the same artifact
    input and must raise ``ValueError`` for artifacts they cannot handle.
    """

    def normalize(self, artifact: Artifact) -> str:
        """Return a plain-text representation of *artifact*."""
        ...


# ---------------------------------------------------------------------------
# Default hermetic implementation
# ---------------------------------------------------------------------------


class LocalMultimodalNormalizer:
    """Hermetic, dependency-free multimodal normalizer.

    Converts artifacts to text using only Python stdlib.
    Makes zero network calls.  Safe for offline simulation and Docker demo.

    MLLM SEAM
    ---------
    To enable live image/audio normalization via a multimodal LLM, supply an
    ``mllm_transport`` object at construction time.  The transport must expose:

        def normalize(self, artifact: Artifact) -> str

    When ``mllm_transport`` is provided and the artifact is an image-family
    modality with no textual hints, the transport is called and its output is
    returned as the normalized text.  This is the extension point where a
    real vision/audio model plugs in for live mode (e.g., Claude claude-opus-4-5 or
    GPT-4o vision).  The default (``mllm_transport=None``) raises ``ValueError``
    when no textual hint is available, which is the one canonical fail-fast path.
    """

    def __init__(self, *, mllm_transport: Any = None) -> None:
        """
        Parameters
        ----------
        mllm_transport:
            Optional MLLM transport (any object with a ``normalize(Artifact) -> str``
            method).  When None (default), image-family artifacts without textual
            hints raise ``ValueError``.
        """
        self._mllm_transport = mllm_transport

    def normalize(self, artifact: Artifact) -> str:
        """Normalize *artifact* to a plain-text representation.

        Dispatch table
        --------------
        text / document  → stripped passthrough
        code             → citation header + source text
        structured       → flattened key=value lines (JSON/dict/list)
        image-family     → merged textual hints (caption / alt_text / ocr_text /
                           transcript); or MLLM transport if configured; or
                           ValueError when no hint and no transport.
        """
        modality = artifact.modality
        if modality in TEXT_MODALITIES:
            return self._normalize_text(artifact)
        if modality in CODE_MODALITIES:
            return self._normalize_code(artifact)
        if modality in STRUCTURED_MODALITIES:
            return self._normalize_structured(artifact)
        if modality in IMAGE_MODALITIES:
            return self._normalize_image_family(artifact)
        # Should never reach here — Artifact.__init__ validates modality.
        raise ValueError(f"Unsupported artifact modality: {modality!r}")

    # ------------------------------------------------------------------
    # Modality-specific normalization
    # ------------------------------------------------------------------

    def _normalize_text(self, artifact: Artifact) -> str:
        payload = artifact.payload
        if not isinstance(payload, str):
            raise ValueError(f"text/document artifact payload must be a str, got {type(payload).__name__!r}")
        return payload.strip()

    def _normalize_code(self, artifact: Artifact) -> str:
        payload = artifact.payload
        if not isinstance(payload, str):
            raise ValueError(f"code artifact payload must be a str, got {type(payload).__name__!r}")
        location = artifact.location
        lines: list[str] = []
        if location:
            lines.append(f"Code artifact from: {location}")
        else:
            lines.append("Code artifact:")
        lines.append(payload.rstrip())
        return "\n".join(lines)

    def _normalize_structured(self, artifact: Artifact) -> str:
        payload = artifact.payload
        if isinstance(payload, str):
            try:
                parsed: Any = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError(f"structured artifact payload is not valid JSON: {exc}") from exc
        elif isinstance(payload, (dict, list)):
            parsed = payload
        else:
            raise ValueError(
                f"structured artifact payload must be str (JSON), dict, or list, got {type(payload).__name__!r}"
            )
        return self._flatten_structured(parsed, prefix="")

    def _normalize_image_family(self, artifact: Artifact) -> str:
        """Merge textual hints for image/diagram/screenshot/audio artifacts.

        Hint priority: ocr_text / transcript → caption → alt_text.
        If no hint is available and no MLLM transport is configured, raises ValueError.
        """
        # Gather available hints.
        hints: list[str] = []
        if artifact.ocr_text and artifact.ocr_text.strip():
            hints.append(f"OCR text: {artifact.ocr_text.strip()}")
        if artifact.transcript and artifact.transcript.strip():
            hints.append(f"Transcript: {artifact.transcript.strip()}")
        if artifact.caption and artifact.caption.strip():
            hints.append(f"Caption: {artifact.caption.strip()}")
        if artifact.alt_text and artifact.alt_text.strip():
            hints.append(f"Alt text: {artifact.alt_text.strip()}")

        if hints:
            header_parts: list[str] = [f"{artifact.modality.capitalize()} artifact"]
            if artifact.location:
                header_parts.append(f"from: {artifact.location}")
            header = " ".join(header_parts)
            return header + "\n" + "\n".join(hints)

        # No textual hints — try MLLM transport.
        if self._mllm_transport is not None:
            result: str = self._mllm_transport.normalize(artifact)
            if not isinstance(result, str) or not result.strip():
                raise ValueError(f"MLLM transport returned empty text for {artifact.modality!r} artifact")
            return result.strip()

        # Canonical fail-fast path: no hints, no MLLM transport.
        raise ValueError(
            f"{artifact.modality!r} artifact has no textual representation. "
            "Provide at least one of: caption, alt_text, ocr_text, transcript; "
            "or configure an mllm_transport on the normalizer for live MLLM-to-text conversion."
        )

    # ------------------------------------------------------------------
    # Structured flattening helpers
    # ------------------------------------------------------------------

    def _flatten_structured(self, obj: Any, prefix: str) -> str:
        """Recursively flatten a parsed JSON object to readable key=value lines."""
        lines: list[str] = []
        if isinstance(obj, dict):
            for key, value in obj.items():
                qualified = f"{prefix}.{key}" if prefix else key
                if isinstance(value, (dict, list)):
                    nested = self._flatten_structured(value, prefix=qualified)
                    if nested:
                        lines.append(nested)
                else:
                    lines.append(f"{qualified}={self._scalar_repr(value)}")
        elif isinstance(obj, list):
            for index, item in enumerate(obj):
                qualified = f"{prefix}[{index}]" if prefix else f"[{index}]"
                if isinstance(item, (dict, list)):
                    nested = self._flatten_structured(item, prefix=qualified)
                    if nested:
                        lines.append(nested)
                else:
                    lines.append(f"{qualified}={self._scalar_repr(item)}")
        else:
            lines.append(self._scalar_repr(obj))
        return "\n".join(lines)

    @staticmethod
    def _scalar_repr(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
