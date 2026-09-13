"""Generate patent-style line drawings (FIG. 1-5) for the cross-artifact
coherence provisional (PATENT_COHERENCE_SPEC.md). Black-and-white schematics with
reference numerals matching the specification's detailed description. Output:
patent/figures/coh_*.png.

Run (no project dependency pollution):
    uv run --with matplotlib --with numpy python patent/make_coherence_figures.py
"""

from __future__ import annotations

import os

import matplotlib.pyplot as plt
from _draw import OUT, arrow, box, diamond, save
from matplotlib.patches import FancyArrowPatch


def fig1_architecture():
    fig, ax = plt.subplots(figsize=(10, 11))
    ax.axis("off")
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 16)
    cx = 6.6
    box(ax, 0.8, 14.4, 5.0, 1.0, "MEMORY STORE\n(typed temporal relationships)", "100")
    box(ax, 8.0, 14.4, 5.2, 1.0, "ARTIFACT SOURCES\nskill registry · file directory", "105", fs=8)
    box(ax, 3.6, 12.3, 6.0, 1.0, "DIRECTIVE PROJECTOR\nmemory + skill + file -> directive", "110", fs=8)
    box(
        ax,
        10.2,
        10.9,
        3.5,
        2.7,
        "COHERENCE POLICY\n\nidentity threshold ·\nescalation-cycle K ·\nclass precedence ·\nparticipating types",
        "115",
        fs=7.6,
    )
    box(
        ax,
        3.6,
        10.2,
        6.0,
        1.1,
        "COHERENCE DIRECTORY\ndirectives keyed by semantic\nidentity (topic embedding)",
        "200",
        fs=8,
    )
    box(ax, 3.6, 8.1, 6.0, 1.2, "DETECTOR\ncross-artifact contradiction +\nescalation windup", "120")
    box(ax, 3.6, 6.2, 6.0, 1.1, "CULPRIT ATTRIBUTOR\nexecution precedence + staleness", "125", fs=8)
    box(
        ax,
        3.6,
        4.1,
        6.0,
        1.3,
        "RECONCILIATION / DECISION RECORDER\nauditable decision + proposed\nrepair of governing artifact",
        "130",
        fs=7.8,
    )
    box(ax, 10.2, 3.9, 3.4, 1.2, "GOVERNOR REPAIR RECEIPT\nincident id + artifact id +\nversion digest", "140", fs=7.1)
    box(ax, 0.6, 4.3, 2.5, 1.0, "ANTI-WINDUP\nHOLD", "135")
    arrow(ax, 3.3, 14.4, 5.4, 13.3)
    arrow(ax, 9.9, 14.4, 8.0, 13.3)
    arrow(ax, 10.2, 12.4, 9.6, 12.8, "config", fs=7)
    arrow(ax, 10.5, 10.9, 9.6, 8.9, "thresholds", fs=7, dashed=True)
    arrow(ax, cx, 12.3, cx, 11.3)
    arrow(ax, cx, 10.2, cx, 9.3)
    arrow(ax, cx, 8.1, cx, 7.3)
    arrow(ax, cx, 6.2, cx, 5.4)
    arrow(ax, 3.6, 4.8, 3.1, 4.8, "hold")
    arrow(ax, 9.6, 4.75, 10.2, 4.55)
    ax.add_patch(
        FancyArrowPatch(
            (1.25, 5.3), (1.25, 14.4), arrowstyle="-|>", mutation_scale=13, lw=1.2, color="black", linestyle="dashed"
        )
    )
    ax.text(0.72, 9.8, "suppress further escalation", fontsize=7.4, ha="center", va="center", rotation=90)
    ax.set_title("FIG. 1", fontsize=12, loc="left")
    save(fig, "coh_fig1_architecture.png", dpi=170)


def fig2_directive_directory():
    fig, ax = plt.subplots(figsize=(10.5, 7.8))
    ax.axis("off")
    ax.set_xlim(0, 13.5)
    ax.set_ylim(0, 9)
    box(
        ax,
        0.4,
        0.7,
        4.6,
        7.6,
        "COMMON DIRECTIVE\n\n- subject / topic\n- instruction\n- stance: require · forbid\n"
        "   · prefer · assert · guide\n- artifact class:\n   memory · skill · file\n"
        "- execution precedence\n- updated_at (staleness)\n- escalation count (memory)\n"
        "- confidence\n- topic embedding",
        "200",
        fs=8,
    )
    box(ax, 6.6, 7.5, 6.6, 0.95, "COHERENCE DIRECTORY ENTRY\nkey = topic semantic identity", "210", fs=8)
    box(
        ax,
        6.8,
        5.7,
        6.4,
        1.15,
        "SKILL directive · precedence 30 · STALE\n-> GOVERNING (executes at run time)",
        "220",
        fs=7.7,
    )
    box(ax, 6.8, 4.2, 6.4, 0.95, "FILE directive · precedence 20", "230", fs=7.8)
    box(ax, 6.8, 2.5, 6.4, 1.15, "MEMORY directive · precedence 10\nescalating (observed_count up)", "240", fs=7.7)
    box(ax, 6.8, 0.8, 6.4, 1.0, "contradiction / windup spans the\ntwo classes sharing this topic", None, fs=7.6)
    arrow(ax, 9.8, 7.5, 9.8, 6.85)
    ax.annotate("", xy=(6.3, 6.3), xytext=(6.3, 3.05), arrowprops=dict(arrowstyle="<->", lw=1.1))
    ax.text(5.95, 4.65, "higher\nexecution\nprecedence", fontsize=7.1, ha="center", va="center")
    ax.set_title("FIG. 2", fontsize=12, loc="left")
    save(fig, "coh_fig2_directive.png", dpi=180)


def fig3_contradiction():
    fig, ax = plt.subplots(figsize=(9, 11.5))
    ax.axis("off")
    ax.set_xlim(0, 9.6)
    ax.set_ylim(0, 13)
    cx = 3.3
    box(ax, 0.8, 11.5, 5.0, 0.85, "Project memory + skill + file\ninto common directives", "300", 7.7)
    box(ax, 0.8, 10.0, 5.0, 0.9, "Group directives by semantic identity\n(topic cosine >= threshold)", "305", 7.5)
    box(ax, 0.8, 8.6, 5.0, 0.8, "Take a directive pair across\ntwo artifact classes", "310", 7.6)
    diamond(ax, cx, 7.0, 2.5, 0.95, "Stances conflict?\n(require vs assert)", "320", 7.4)
    box(ax, 6.4, 6.55, 2.9, 0.9, "No incident\n(continue)", "325", 7.6)
    box(ax, 0.8, 4.9, 5.0, 0.95, "Attribute the higher-execution-\nprecedence directive as GOVERNING", "330", 7.5)
    box(ax, 0.8, 3.5, 5.0, 0.85, "Record CROSS_ARTIFACT_\nCONTRADICTION incident", "340", 7.5)
    box(ax, 0.8, 2.0, 5.0, 0.95, "Propose repair of the\ngoverning artifact", "350", 7.6)
    arrow(ax, cx, 11.5, cx, 10.9)
    arrow(ax, cx, 10.0, cx, 9.4)
    arrow(ax, cx, 8.6, cx, 7.95)
    arrow(ax, 5.8, 7.0, 6.4, 7.0, "no")
    arrow(ax, cx, 6.05, cx, 5.85, "yes")
    arrow(ax, cx, 4.9, cx, 4.35)
    arrow(ax, cx, 3.5, cx, 2.95)
    ax.set_title("FIG. 3", fontsize=12, loc="left")
    save(fig, "coh_fig3_contradiction.png", dpi=180)


def fig4_windup():
    fig, ax = plt.subplots(figsize=(9, 13))
    ax.axis("off")
    ax.set_xlim(0, 9.6)
    ax.set_ylim(0, 14)
    cx = 3.3
    box(ax, 0.7, 12.8, 5.2, 0.8, "For each behavioral memory directive", "400", 7.9)
    diamond(ax, cx, 11.4, 2.5, 0.85, "escalation count >= K cycles?", "410", 7.5)
    box(ax, 6.5, 10.95, 2.6, 0.8, "skip", "415", 7.8)
    diamond(
        ax,
        cx,
        9.0,
        2.7,
        1.05,
        "higher-precedence non-memory\nartifact on same subject,\nstaler than escalation?",
        "420",
        7.1,
    )
    box(ax, 6.5, 8.55, 2.6, 0.8, "skip", "425", 7.8)
    box(ax, 0.7, 6.2, 5.2, 1.05, "Raise ESCALATION_WINDUP incident\n-> name suspected GOVERNING artifact", "430", 7.6)
    box(ax, 0.7, 4.7, 5.2, 0.95, "Apply anti-windup HOLD\n(mark directive coherence_hold)", "440", 7.6)
    box(ax, 0.7, 3.0, 5.2, 1.15, "Subsequent reinforcement: record as\nevidence, FREEZE confidence", "450", 7.5)
    box(ax, 0.7, 1.5, 5.2, 0.95, "Propose repair of the governing artifact", "460", 7.6)
    arrow(ax, cx, 12.8, cx, 12.25)
    arrow(ax, 5.8, 11.4, 6.5, 11.4, "no")
    arrow(ax, cx, 10.55, cx, 10.05, "yes")
    arrow(ax, 6.0, 9.0, 6.5, 9.0, "no")
    arrow(ax, cx, 7.95, cx, 7.25, "yes")
    arrow(ax, cx, 6.2, cx, 5.65)
    arrow(ax, cx, 4.7, cx, 4.15)
    arrow(ax, cx, 3.0, cx, 2.45)
    ax.set_title("FIG. 4", fontsize=12, loc="left")
    save(fig, "coh_fig4_windup.png", dpi=170)


def fig5_failure_resolution():
    fig, ax = plt.subplots(figsize=(11.5, 9))
    ax.axis("off")
    ax.set_xlim(0, 14.5)
    ax.set_ylim(0, 12.2)
    # Top band: the feedback-futility loop ordinary dreaming cannot break.
    box(ax, 0.4, 8.3, 3.7, 1.0, "REPEATED USER FEEDBACK", "500", 8)
    box(ax, 0.4, 6.2, 3.7, 1.4, "MEMORY DIRECTIVE escalates\nobserved_count 1->2->3->4\nconfidence up", "510", 7.7)
    box(ax, 5.0, 6.2, 4.3, 1.4, "memory-vs-memory dreaming\nfinds NO CONFLICT\n-> escalation continues", "540", 7.7)
    box(ax, 5.0, 8.3, 4.3, 1.0, "STALE SELF-AUTHORED SKILL\n(precedence 30, updated last month)", "520", 7.5)
    box(ax, 10.3, 8.3, 3.8, 1.0, "GOVERNS behavior\nat execution time", "530", 7.8)
    box(ax, 10.3, 6.4, 3.8, 1.0, "BEHAVIOR UNCHANGED\n-> user repeats feedback", "535", 7.5)
    arrow(ax, 2.25, 8.3, 2.25, 7.6)
    arrow(ax, 4.1, 6.9, 5.0, 6.9)
    arrow(ax, 6.9, 8.3, 6.9, 7.6)
    arrow(ax, 9.3, 8.8, 10.3, 8.8)
    arrow(ax, 12.2, 8.3, 12.2, 7.4)
    # Dashed futility return, routed over the top so it crosses no box text.
    ax.add_patch(
        FancyArrowPatch(
            (12.2, 9.32),
            (2.25, 9.32),
            arrowstyle="-|>",
            mutation_scale=12,
            lw=1.1,
            color="black",
            linestyle="dashed",
            connectionstyle="arc3,rad=0.34",
        )
    )
    ax.text(7.2, 5.55, "futility loop: feedback never converges", fontsize=7.6, ha="center", style="italic")
    # Bottom band: the coherence cycle breaks the loop.
    box(
        ax,
        1.6,
        3.2,
        11.0,
        1.2,
        "COHERENCE ENGINE: escalation-windup detected -> attribute STALE SKILL\nas governing -> incident + anti-windup HOLD",
        "550",
        8,
    )
    box(ax, 1.6, 1.3, 5.1, 1.1, "ESCALATION ARRESTED\nconfidence frozen while repair proceeds", "560", 7.7)
    box(ax, 7.3, 1.3, 5.3, 1.1, "REPAIR RECEIPT\nincident -> artifact version digest -> receipt hash", "570", 7.5)
    arrow(ax, 7.1, 6.2, 7.1, 4.4)
    arrow(ax, 4.2, 3.2, 4.2, 2.4)
    arrow(ax, 10.0, 3.2, 10.0, 2.4)
    ax.set_title("FIG. 5", fontsize=12, loc="left")
    save(fig, "coh_fig5_failure_resolution.png", dpi=165)


if __name__ == "__main__":
    fig1_architecture()
    fig2_directive_directory()
    fig3_contradiction()
    fig4_windup()
    fig5_failure_resolution()
    print("wrote coherence figures to", OUT)
    for f in sorted(os.listdir(OUT)):
        if f.startswith("coh_"):
            print("  ", f)
