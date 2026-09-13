"""Build the provisional-patent PDFs from their markdown specifications.

Converts each provisional-patent markdown to LaTeX, appends its FIG. drawing
pages, and compiles with tectonic. The markdown files are the single source of
truth; this script is the deterministic export path.

    uv run python patent/build_pdf.py            # build all specs
    uv run python patent/build_pdf.py coherence  # build one spec by name

Figures are produced by patent/make_receipt_figures.py and
patent/make_coherence_figures.py (run those first).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIGDIR = os.path.join(ROOT, "patent", "figures")

# Registry of provisional specs. One canonical builder, parameterised per spec.
SPECS = {
    "replay": {
        "md": "PATENT_REPLAY_RECEIPTS_SPEC.md",
        "pdf": "PATENT_REPLAY_RECEIPTS_SPEC.pdf",
        "title": (
            "Replayable Write-Side Receipts and Counterfactual Policy "
            "Evaluation for Motive-Governed Autonomous Agent Memory"
        ),
        "figures": [
            ("fig1_architecture.png", "FIG. 1 --- Layered architecture of the receipt-backed memory-formation system."),
            ("fig2_receipt.png", "FIG. 2 --- Receipt record, receipt hash chain, and per-run Merkle checkpoint."),
            ("fig3_formation.png", "FIG. 3 --- Receipt-backed memory-formation method."),
            ("fig4_replay.png", "FIG. 4 --- Byte-replay method with fail-closed verification gate."),
            ("fig5_counterfactual.png", "FIG. 5 --- Counterfactual policy evaluation without production mutation."),
            ("fig6_negative_space.png", "FIG. 6 --- Negative-space memory query."),
            ("fig7_certification.png", "FIG. 7 --- Policy certification harness over a fixture corpus."),
        ],
    },
    "coherence": {
        "md": "PATENT_COHERENCE_SPEC.md",
        "pdf": "PATENT_COHERENCE_SPEC.pdf",
        "title": (
            "Cross-Artifact Behavioral Coherence for Autonomous Agents Governed "
            "by Heterogeneous Persistent Context, Including Escalation-Windup "
            "Detection and Anti-Windup Reconciliation"
        ),
        "figures": [
            ("coh_fig1_architecture.png", "FIG. 1 --- Cross-artifact coherence architecture."),
            (
                "coh_fig2_directive.png",
                "FIG. 2 --- Common directive representation and the coherence directory keyed by semantic identity.",
            ),
            (
                "coh_fig3_contradiction.png",
                "FIG. 3 --- Cross-artifact contradiction detection and culprit attribution.",
            ),
            ("coh_fig4_windup.png", "FIG. 4 --- Escalation-windup detector and anti-windup hold actuator."),
            (
                "coh_fig5_failure_resolution.png",
                "FIG. 5 --- Feedback-futility escalation failure mode and its resolution.",
            ),
        ],
    },
}


def preamble(title):
    return r"""\documentclass[11pt]{article}
\usepackage[letterpaper,margin=1in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{microtype}
\usepackage{graphicx}
\usepackage[hidelinks]{hyperref}
\hypersetup{pdftitle={%(title)s},pdfauthor={Memotron},
  pdfsubject={U.S. Provisional Patent Application}}
\setlength{\parindent}{1.6em}
\setlength{\emergencystretch}{2em}
\sloppy
\title{}\author{}\date{}
\begin{document}
""" % {"title": title}  # noqa: UP031 - a LaTeX template: str.format would
    # require doubling every brace in the preamble, of which there are many


POSTAMBLE = r"""
\end{document}
"""

TOKEN = re.compile(r"(`[^`]+`|\*\*[^*]+?\*\*|\*[^*]+?\*)")


def esc(s):
    s = s.replace("\\", r"\textbackslash{}")
    for a, b in [
        ("&", r"\&"),
        ("%", r"\%"),
        ("$", r"\$"),
        ("#", r"\#"),
        ("_", r"\_"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("~", r"\textasciitilde{}"),
        ("^", r"\textasciicircum{}"),
    ]:
        s = s.replace(a, b)
    # Map unicode punctuation to LaTeX (applied last so the commands stay intact).
    for a, b in [
        ("—", "---"),
        ("–", "--"),
        ("§", r"\S{}"),
        ("·", r"\textperiodcentered{}"),
        ("“", "``"),
        ("”", "''"),
        ("‘", "`"),
        ("’", "'"),
        ("…", r"\dots{}"),
        ("≈", r"$\approx$"),
        ("≥", r"$\ge$"),
        ("≤", r"$\le$"),
        ("×", r"$\times$"),
        ("→", r"$\rightarrow$"),
    ]:
        s = s.replace(a, b)
    return s


def join_para(para):
    """Join soft-wrapped lines; a trailing in-word hyphen joins without a space."""
    text = para[0].strip()
    for nxt in para[1:]:
        nxt = nxt.strip()
        text += nxt if re.search(r"[A-Za-z0-9]-$", text) else " " + nxt
    return text


def inline(s):
    out = []
    for part in TOKEN.split(s):
        if not part:
            continue
        if part.startswith("`") and part.endswith("`"):
            out.append(r"\texttt{" + esc(part[1:-1]) + "}")
        elif part.startswith("**") and part.endswith("**"):
            out.append(r"\textbf{" + esc(part[2:-2]) + "}")
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out.append(r"\emph{" + esc(part[1:-1]) + "}")
        else:
            out.append(esc(part))
    return "".join(out)


def parse_blocks(md):
    lines = md.split("\n")
    blocks, i, n = [], 0, len(md.split("\n"))
    while i < n:
        line = lines[i]
        if line.startswith("```"):
            i += 1
            code = []
            while i < n and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(code)))
        elif line.strip() == "---":
            blocks.append(("rule", None))
            i += 1
        elif line.startswith("### "):
            blocks.append(("h3", line[4:]))
            i += 1
        elif line.startswith("## "):
            blocks.append(("h2", line[3:]))
            i += 1
        elif line.startswith("# "):
            blocks.append(("h1", line[2:]))
            i += 1
        elif line.strip() == "":
            i += 1
        else:
            para = [line]
            i += 1
            while (
                i < n and lines[i].strip() != "" and not lines[i].startswith(("#", "```")) and lines[i].strip() != "---"
            ):
                para.append(lines[i])
                i += 1
            blocks.append(("p", join_para(para)))
    return blocks


def render(blocks):
    out = []
    for kind, text in blocks:
        if kind == "h1":
            out.append(r"\begin{center}{\Large\bfseries " + esc(text) + r"}\end{center}\vspace{0.5em}")
        elif kind == "h2":
            out.append(r"\section*{" + esc(text) + "}")
        elif kind == "h3":
            out.append(r"\subsection*{" + esc(text) + "}")
        elif kind == "rule":
            out.append(r"\begin{center}\rule{0.35\textwidth}{0.4pt}\end{center}")
        elif kind == "code":
            out.append(
                r"\begin{quote}\small\begin{verbatim}"
                "\n" + text + "\n" + r"\end{verbatim}\end{quote}"
            )
        else:
            out.append(inline(text) + r"\par")
    return "\n\n".join(out)


def figure_pages(figures):
    out = [r"\clearpage"]
    for fname, caption in figures:
        out.append(
            r"\begin{center}"
            "\n"
            r"\includegraphics[width=0.94\textwidth,height=0.82\textheight,"
            r"keepaspectratio]{figures/" + fname + r"}\\[1.2em]"
            "\n"
            r"\textit{" + caption + r"}"
            "\n"
            r"\end{center}"
            "\n"
            r"\clearpage"
        )
    return "\n".join(out)


def build_spec(spec):
    md_path = os.path.join(ROOT, spec["md"])
    pdf_out = os.path.join(ROOT, spec["pdf"])
    with open(md_path, encoding="utf-8") as fh:
        md = fh.read()
    for fname, _ in spec["figures"]:
        if not os.path.exists(os.path.join(FIGDIR, fname)):
            raise SystemExit(
                f"missing figure {fname}; run the figure generator first "
                f"(patent/make_receipt_figures.py or patent/make_coherence_figures.py)"
            )

    tex = preamble(spec["title"]) + render(parse_blocks(md)) + "\n\n" + figure_pages(spec["figures"]) + POSTAMBLE

    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(FIGDIR, os.path.join(tmp, "figures"))
        texpath = os.path.join(tmp, "spec.tex")
        with open(texpath, "w", encoding="utf-8") as fh:
            fh.write(tex)
        subprocess.run(["tectonic", "--chatter", "minimal", "--outdir", tmp, texpath], check=True)
        shutil.copyfile(os.path.join(tmp, "spec.pdf"), pdf_out)
    print("wrote", pdf_out)


def main():
    names = sys.argv[1:] or list(SPECS)
    unknown = [n for n in names if n not in SPECS]
    if unknown:
        raise SystemExit(f"unknown spec(s) {unknown}; choose from {sorted(SPECS)}")
    for name in names:
        build_spec(SPECS[name])


if __name__ == "__main__":
    main()
