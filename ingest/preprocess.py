"""MDX -> episode preprocessor for the JedAI docs portal (INGEST.md Phase 0).

Pure and testable: every transform below is a module-level function over strings.
Nothing here touches the network, the graph, or the portal checkout (read-only).

Pipeline per file
-----------------
1. ``split_frontmatter``   -- YAML frontmatter off the top, never executed.
2. ``strip_imports``       -- drop ESM ``import`` statements and ``export const`` lines.
3. ``unwrap_components``   -- Starlight JSX -> plain labelled text (Aside/Card/
   CardGrid/LinkCard/Tabs/TabItem/Steps/Badge) and ``:::`` directives.
4. ``rewrite_internal_links`` -- ``[label](/path/)`` -> ``label (→ /path/)`` so the
   link target survives as an entity coordinate in the episode text.
5. ``split_sections``      -- H2-bounded sections carrying a ``title > heading``
   breadcrumb; short sections merge into the page preamble.

Two documented deviations from INGEST.md §2 (both measured, see ingest/README.md):

* **Fence-aware heading detection.** A naive ``^## `` scan counts headings inside
  fenced code blocks. ``sources/dscribe/special-offers.mdx`` embeds a ```` ```md ````
  sample whose body contains ten ``## `` lines; splitting on those produces ten
  junk episodes and destroys the real section. All heading scans here skip fenced
  regions.
* **H3 fallback for pages with no H2.** 15 of the 77 pilot files have zero real H2
  headings -- including ``platform-decision-log/approved.mdx``, which carries
  D001..D011 entirely as H3. Pure H2 splitting collapses that page into one
  ~13 KB section, which then gets blind-chunked by ``add_context`` at 4000 chars,
  destroying per-decision ``slug#anchor`` evidence and making INGEST.md's G3-3
  ("edit one requirement-bearing section in platform-decision-log") untestable.
  When a page has no fence-free H2, this splitter bounds on H3 instead and records
  ``heading_level`` on every section so the choice is auditable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

# --------------------------------------------------------------------------
# Pilot slice definition (INGEST.md §1: 77 files)
# --------------------------------------------------------------------------

DEFAULT_CONTENT_ROOT = Path("/Users/logan.robbins/jedai/portal/src/content/docs")

PILOT_SUBTREES: tuple[str, ...] = (
    "products/jedai-gateway",
    "platform/knowledgebase",
    "solution_engineering/platform-decision-log",
)

#: Sections shorter than this merge into the page preamble (INGEST.md §2).
MIN_SECTION_CHARS = 300

CONTENT_EXTENSIONS = (".mdx", ".md")


# --------------------------------------------------------------------------
# Frontmatter
# --------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter_dict, body)``.

    Uses ``yaml.safe_load`` -- never ``yaml.load`` -- so no constructor in the
    document can instantiate a Python object. A malformed or non-mapping
    frontmatter block yields ``{}`` rather than raising, because a single bad
    page must not abort a corpus walk.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        parsed = None
    if not isinstance(parsed, dict):
        parsed = {}
    return parsed, text[match.end() :]


# --------------------------------------------------------------------------
# Fence tracking -- every structural scan below must skip fenced code
# --------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")


def fence_mask(lines: Sequence[str]) -> list[bool]:
    """Return a per-line mask: ``True`` where the line sits inside a code fence.

    The opening and closing fence lines themselves are marked ``True``. Fences
    close only on a matching marker of at least the opening length, so a ```` ``` ````
    inside a ```` ```` ```` block does not terminate it.
    """
    mask = [False] * len(lines)
    marker: str | None = None
    for index, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if marker is None:
            if match:
                marker = match.group(2)
                mask[index] = True
            continue
        mask[index] = True
        if (
            match
            and match.group(2)[0] == marker[0]
            and len(match.group(2)) >= len(marker)
            # A closing fence carries no info string.
            and not match.group(3).strip()
        ):
            marker = None
    return mask


# --------------------------------------------------------------------------
# Imports / exports
# --------------------------------------------------------------------------

_IMPORT_RE = re.compile(r"^\s*import\s.+?$", re.MULTILINE)
_EXPORT_RE = re.compile(r"^\s*export\s+(const|let|default)\s.+?$", re.MULTILINE)


def strip_imports(body: str) -> str:
    """Drop ESM ``import`` / ``export const`` lines outside code fences."""
    lines = body.split("\n")
    mask = fence_mask(lines)
    kept = [
        line
        for line, fenced in zip(lines, mask, strict=True)
        if fenced or not (_IMPORT_RE.match(line) or _EXPORT_RE.match(line))
    ]
    return "\n".join(kept)


# --------------------------------------------------------------------------
# Starlight component unwrapping
# --------------------------------------------------------------------------

_ATTR_RE = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|\{([^}]*)\})""")


def _attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _ATTR_RE.finditer(raw):
        key = match.group(1)
        value = match.group(2) or match.group(3) or match.group(4) or ""
        out[key] = value.strip()
    return out


#: Components that carry no text of their own -- unwrap to their children.
_TRANSPARENT = ("CardGrid", "Steps", "Tabs", "TabItem", "Card", "Aside")

_SELF_CLOSING_HANDLERS: dict[str, Any] = {}


def _linkcard_text(attrs: dict[str, str]) -> str:
    title = attrs.get("title", "").strip()
    href = attrs.get("href", "").strip()
    description = attrs.get("description", "").strip()
    parts = [p for p in (title, f"(→ {href})" if href else "", description and f"— {description}") if p]
    return " ".join(parts)


def unwrap_components(body: str) -> str:
    """Rewrite Starlight JSX and ``:::`` directives to plain labelled text.

    Labels are preserved as text so an extractor sees ``en_us`` next to the table
    it labels, and an ``Aside`` reads as ``Note: ...`` rather than vanishing.
    Code fences are never touched.
    """
    lines = body.split("\n")
    mask = fence_mask(lines)
    out: list[str] = []

    for line, fenced in zip(lines, mask, strict=True):
        if fenced:
            out.append(line)
            continue
        out.append(_unwrap_line(line))

    text = "\n".join(out)
    # ::: directives -> "Label: " / "Label — Title: "
    text = re.sub(
        r"^:::(note|tip|caution|danger|warning)(?:\[([^\]]*)\])?\s*$",
        lambda m: f"{m.group(1).capitalize()}{f' — {m.group(2)}' if m.group(2) else ''}:",
        text,
        flags=re.MULTILINE,
    )
    return re.sub(r"^:::\s*$", "", text, flags=re.MULTILINE)


def _unwrap_line(line: str) -> str:
    # Self-closing LinkCard -> "Title (→ /href/) — description"
    def _linkcard(match: re.Match[str]) -> str:
        return _linkcard_text(_attrs(match.group(1)))

    line = re.sub(r"<LinkCard\b([^>]*?)/>", _linkcard, line)

    # Badge -> its text (decision dates in platform-decision-log ride on these)
    line = re.sub(r"<Badge\b([^>]*?)/>", lambda m: _attrs(m.group(1)).get("text", ""), line)

    # Icon and any other self-closing component we do not model -> drop
    line = re.sub(r"<Icon\b[^>]*?/>", "", line)

    # Opening tags of labelled containers -> a label line
    def _open(match: re.Match[str]) -> str:
        name = match.group(1)
        attrs = _attrs(match.group(2))
        label = attrs.get("label") or attrs.get("title") or ""
        if name == "Aside":
            kind = (attrs.get("type") or "note").capitalize()
            # Trailing space: an inline <Aside>text</Aside> must not render as
            # "Note:text" once the tag is substituted out.
            return f"{kind}{f' — {label}' if label else ''}: "
        if label:
            return f"[{label}] "
        return ""

    line = re.sub(rf"<({'|'.join(_TRANSPARENT)})\b([^>]*)>", _open, line)
    line = re.sub(rf"</({'|'.join(_TRANSPARENT)})>", "", line)

    # Any residual self-closing component tag -> drop
    return re.sub(r"<[A-Z]\w*\b[^>]*?/>", "", line)


# --------------------------------------------------------------------------
# Internal links
# --------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((/[^)\s]*)\)")


def rewrite_internal_links(body: str) -> tuple[str, list[str]]:
    """``[label](/path/)`` -> ``label (→ /path/)``; returns ``(text, targets)``.

    INGEST.md §5c: this deliberately injects the link target into the episode
    text so the target path survives as an entity coordinate and raises
    cross-page lexical overlap for linked concepts. External (``http``) links are
    left alone.
    """
    targets: list[str] = []
    lines = body.split("\n")
    mask = fence_mask(lines)

    def _sub(match: re.Match[str]) -> str:
        label, href = match.group(1), match.group(2)
        targets.append(href)
        return f"{label} (→ {href})"

    rewritten = [line if fenced else _MD_LINK_RE.sub(_sub, line) for line, fenced in zip(lines, mask, strict=True)]
    # LinkCard hrefs were already rendered as "(→ /path/)" by unwrap_components.
    for line, fenced in zip(lines, mask, strict=True):
        if not fenced:
            targets.extend(re.findall(r"\(→ (/[^)\s]*)\)", line))
    ordered = list(dict.fromkeys(targets))
    return "\n".join(rewritten), ordered


# --------------------------------------------------------------------------
# Section splitting
# --------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _headings_at(lines: Sequence[str], mask: Sequence[bool], level: int) -> list[int]:
    out: list[int] = []
    for index, (line, fenced) in enumerate(zip(lines, mask, strict=True)):
        if fenced:
            continue
        match = _HEADING_RE.match(line)
        if match and len(match.group(1)) == level:
            out.append(index)
    return out


def slugify_anchor(heading: str) -> str:
    """GitHub/Starlight-compatible anchor slug for a heading string."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\(→ [^)]*\)", "", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-")


@dataclass(slots=True)
class Section:
    """One episode-to-be: an H2- (or H3-) bounded slice of one page."""

    slug: str
    anchor: str
    title: str
    heading: str
    breadcrumb: str
    section_text: str
    heading_level: int
    internal_links: list[str]
    content_sha256: str
    reference_time: str
    source_path: str
    frontmatter: dict[str, Any] = field(default_factory=dict)

    @property
    def custom_id(self) -> str:
        """``slug#anchor`` -- the ``add_context`` ``custom_id`` and manifest key."""
        return f"{self.slug}#{self.anchor}" if self.anchor else self.slug

    @property
    def chars(self) -> int:
        return len(self.section_text)


def split_sections(
    *,
    slug: str,
    title: str,
    body: str,
    min_section_chars: int = MIN_SECTION_CHARS,
) -> list[tuple[str, str, int]]:
    """Split a cleaned body into ``(heading, text, heading_level)`` triples.

    Bounds on H2. When the page has no fence-free H2, falls back to H3 (see the
    module docstring). Sections under ``min_section_chars`` are folded into the
    page preamble with their heading preserved as a bold label line, so no text
    is lost and no sub-300-char episode is paid for.
    """
    lines = body.split("\n")
    mask = fence_mask(lines)

    level = 2
    starts = _headings_at(lines, mask, 2)
    if not starts:
        starts = _headings_at(lines, mask, 3)
        level = 3
    if not starts:
        text = body.strip()
        return [("", text, 0)] if text else []

    preamble = "\n".join(lines[: starts[0]]).strip()
    bounds = [*starts, len(lines)]

    kept: list[tuple[str, str, int]] = []
    folded: list[str] = []
    for position, start in enumerate(starts):
        match = _HEADING_RE.match(lines[start])
        heading = match.group(2).strip() if match else ""
        text = "\n".join(lines[start + 1 : bounds[position + 1]]).strip()
        if len(text) < min_section_chars:
            if text or heading:
                folded.append(f"**{heading}**\n{text}".strip())
            continue
        kept.append((heading, text, level))

    preamble_parts = [part for part in (preamble, *folded) if part]
    out: list[tuple[str, str, int]] = []
    if preamble_parts:
        out.append(("", "\n\n".join(preamble_parts), 0))
    out.extend(kept)
    return out


# --------------------------------------------------------------------------
# Git reference times
# --------------------------------------------------------------------------


def git_last_commit_times(repo_root: Path, rel_paths: Iterable[str]) -> dict[str, str]:
    """Map ``relative path -> ISO8601 last-commit time`` in one ``git log`` pass.

    ``reference_time`` for every section of a page is that page's last commit
    time (INGEST.md §2: it becomes the default ``valid_from`` of extracted facts,
    so valid-time ordering is right by construction).
    """
    wanted = set(rel_paths)
    if not wanted:
        return {}
    proc = subprocess.run(
        ["git", "log", "--format=@%cI", "--name-only", "--no-renames", "--", *sorted(wanted)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    out: dict[str, str] = {}
    stamp = ""
    for line in proc.stdout.splitlines():
        if line.startswith("@"):
            stamp = line[1:].strip()
        elif line.strip() and stamp:
            out.setdefault(line.strip(), stamp)
    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def iter_content_files(content_root: Path, subtrees: Sequence[str]) -> Iterator[Path]:
    for subtree in subtrees:
        base = content_root / subtree
        if not base.exists():
            raise FileNotFoundError(f"content subtree does not exist: {base}")
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in CONTENT_EXTENSIONS:
                yield path


def page_slug(content_root: Path, path: Path) -> str:
    """Astro route slug for a content file (``index`` collapses to its directory)."""
    rel = path.relative_to(content_root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "index":
        parts = parts[:-1]
    return "/" + "/".join(parts) + "/" if parts else "/"


def preprocess_file(
    path: Path,
    *,
    content_root: Path,
    reference_time: str,
    min_section_chars: int = MIN_SECTION_CHARS,
) -> list[Section]:
    raw = path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(raw)
    title = str(frontmatter.get("title") or path.stem)

    body = strip_imports(body)
    body = unwrap_components(body)
    body, page_links = rewrite_internal_links(body)

    slug = page_slug(content_root, path)
    sections: list[Section] = []
    for heading, text, level in split_sections(slug=slug, title=title, body=body, min_section_chars=min_section_chars):
        breadcrumb = f"{title} > {heading}" if heading else title
        section_text = f"{breadcrumb}\n\n{text}".strip()
        links = [link for link in page_links if link in text]
        sections.append(
            Section(
                slug=slug,
                anchor=slugify_anchor(heading),
                title=title,
                heading=heading,
                breadcrumb=breadcrumb,
                section_text=section_text,
                heading_level=level,
                internal_links=links,
                content_sha256=hashlib.sha256(section_text.encode("utf-8")).hexdigest(),
                reference_time=reference_time,
                source_path=str(path.relative_to(content_root)),
                frontmatter=frontmatter,
            )
        )
    return sections


def preprocess(
    *,
    content_root: Path = DEFAULT_CONTENT_ROOT,
    subtrees: Sequence[str] = PILOT_SUBTREES,
    min_section_chars: int = MIN_SECTION_CHARS,
) -> list[Section]:
    """Preprocess a content slice into episode-ready :class:`Section` records."""
    content_root = content_root.resolve()
    files = list(iter_content_files(content_root, subtrees))
    repo_root = _git_root(content_root)
    rel_to_repo = {path: str(path.relative_to(repo_root)) for path in files}
    times = git_last_commit_times(repo_root, rel_to_repo.values())

    sections: list[Section] = []
    for path in files:
        stamp = times.get(rel_to_repo[path], "")
        if not stamp:
            raise RuntimeError(
                f"no git commit timestamp for {rel_to_repo[path]}; reference_time is "
                "required (INGEST.md §2) and must not be faked"
            )
        sections.extend(
            preprocess_file(
                path,
                content_root=content_root,
                reference_time=stamp,
                min_section_chars=min_section_chars,
            )
        )
    return sections


def _git_root(path: Path) -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(proc.stdout.strip())


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _percentile(values: Sequence[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1)))
    return ordered[index]


def print_stats(sections: Sequence[Section], files: int) -> None:
    chars = [section.chars for section in sections]
    total = sum(chars)
    by_level: dict[int, int] = {}
    for section in sections:
        by_level[section.heading_level] = by_level.get(section.heading_level, 0) + 1

    per_page: dict[str, int] = {}
    for section in sections:
        per_page[section.slug] = per_page.get(section.slug, 0) + 1

    print("=" * 68)
    print("PREPROCESS STATS (fence-aware; H3 fallback for pages with no H2)")
    print("=" * 68)
    print(f"  files walked            {files}")
    print(f"  sections emitted        {len(sections)}")
    print(f"    preamble (level 0)    {by_level.get(0, 0)}")
    print(f"    H2-bounded            {by_level.get(2, 0)}")
    print(f"    H3-bounded (fallback) {by_level.get(3, 0)}")
    print(f"  total chars             {total:,}")
    print(f"  chars/section  min      {min(chars) if chars else 0:,}")
    print(f"                 p50      {_percentile(chars, 50):,}")
    print(f"                 p90      {_percentile(chars, 90):,}")
    print(f"                 max      {max(chars) if chars else 0:,}")
    print(f"                 mean     {total // len(chars) if chars else 0:,}")
    over = [section for section in sections if section.chars > 4000]
    print(f"  sections > 4000 chars   {len(over)}  (these chunk into >1 episode)")
    est_episodes = sum(max(1, -(-section.chars // 4000)) for section in sections)
    print(f"  estimated episodes      {est_episodes}  (at max_chars_per_episode=4000)")
    print(f"  sections/page  max      {max(per_page.values()) if per_page else 0}")
    print(f"  pages with 1 section    {sum(1 for count in per_page.values() if count == 1)}")
    print("=" * 68)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MDX -> episode preprocessor (INGEST.md Phase 0)")
    parser.add_argument("--content-root", type=Path, default=DEFAULT_CONTENT_ROOT)
    parser.add_argument("--subtree", action="append", dest="subtrees", default=None)
    parser.add_argument("--min-section-chars", type=int, default=MIN_SECTION_CHARS)
    parser.add_argument("--stats", action="store_true", help="print distribution and exit")
    parser.add_argument("--json", action="store_true", help="dump section records as JSON")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    subtrees = tuple(args.subtrees) if args.subtrees else PILOT_SUBTREES
    content_root = args.content_root.resolve()
    files = list(iter_content_files(content_root, subtrees))
    sections = preprocess(
        content_root=content_root,
        subtrees=subtrees,
        min_section_chars=args.min_section_chars,
    )
    if args.limit:
        sections = sections[: args.limit]

    if args.json:
        print(json.dumps([asdict(section) for section in sections], indent=2))
        return 0
    print_stats(sections, len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
