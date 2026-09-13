"""Generate patent-style line drawings (FIG. A1-A3) for the Memotron
counsel-facing addendum, which is drafted outside this repository (patent
strategy notes are deliberately not kept in shared git history).
Black-and-white schematics sharing the visual language of the two
provisionals' figures (via _draw.py). These cover the addendum's net-new edges
that the existing receipts/coherence figure sets do not draw:

  FIG. A1  Erasure-surviving replay via hash-over-ciphertext.
  FIG. A2  The quadruple-bound, re-executable erasure certificate.
  FIG. A3  One proof substrate + cross-family mutual reinforcement.

Run (no project dependency pollution):
    uv run --with matplotlib --with numpy python patent/make_add_figures.py
Output: patent/figures/.
"""

from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from _draw import OUT, arrow, box, diamond, save
from matplotlib.patches import FancyArrowPatch


def figA1_erasure_surviving_replay():
    fig, ax = plt.subplots(figsize=(11.5, 9.5))
    ax.axis("off")
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 11.5)
    # Two planes.
    box(
        ax,
        0.4,
        9.4,
        5.7,
        1.5,
        "CONTENT PLANE\nfact · object · source_text · entity names ·\n"
        "embedding vectors\nAES-256-GCM ciphertext under per-scope DEK",
        "A110",
        7.4,
    )
    box(
        ax,
        7.4,
        9.4,
        6.2,
        1.5,
        "LINEAGE PLANE\nstructural ids · predicates · timestamps ·\n"
        "status / supersession · hash-chained receipts ·\n"
        "blind-index tokens (keyed HMAC)",
        "A120",
        7.4,
    )
    # Commitments computed over the STORED representation.
    box(
        ax,
        1.9,
        7.4,
        10.2,
        1.15,
        "COMMITMENTS OVER THE STORED REPRESENTATION\n"
        "payload digests · chain links · Merkle root · graph STATE hashes"
        "  —  all computed over ciphertext / keyed commitment",
        "A130",
        7.6,
    )
    arrow(ax, 3.2, 9.4, 4.5, 8.55)
    arrow(ax, 10.5, 9.4, 9.5, 8.55)
    # The erasure event.
    box(ax, 4.4, 5.6, 5.2, 1.05, "DESTROY PER-SCOPE DEK\nCRYPTO_SHRED_KEY_DESTROYED · receipted", "A140", 7.8)
    arrow(ax, 7.0, 7.4, 7.0, 6.65)
    # Outcomes.
    box(ax, 0.4, 3.4, 5.7, 1.4, "CONTENT PLANE\n-> [CRYPTO-SHREDDED]\npermanently unrecoverable", "A150", 7.8)
    box(
        ax,
        7.4,
        3.0,
        6.2,
        1.9,
        "LINEAGE PLANE STILL VERIFIES\n"
        "(a) chain verifies against Merkle root\n"
        "(b) model-free replay reconstructs structural STATE\n"
        "(c) blind index still deduplicates / seeks",
        "A160",
        7.6,
    )
    arrow(ax, 5.6, 5.6, 3.2, 4.8, "content")
    arrow(ax, 8.4, 5.6, 10.0, 4.9, "lineage")
    # Prior-art contrast (dashed).
    ax.add_patch(
        FancyArrowPatch(
            (7.0, 3.0), (7.0, 2.15), arrowstyle="-|>", mutation_scale=12, lw=1.1, color="black", linestyle="dashed"
        )
    )
    box(
        ax,
        0.4,
        0.7,
        13.2,
        1.35,
        "PRIOR ART (commitments over plaintext): only event ordering or"
        " inclusion / consistency proofs survive key destruction\n"
        "— state reconstruction does NOT  (VeritasChain VCP; Codenotary"
        " US12,530,685 B2).  Here: STATE reconstruction survives.",
        "A170",
        7.6,
    )
    ax.set_title("FIG. A1", fontsize=12, loc="left")
    save(fig, "add_figA1_erasure_surviving_replay.png", dpi=150)


def figA2_erasure_certificate():
    fig, ax = plt.subplots(figsize=(8.9, 12.5))
    ax.axis("off")
    ax.set_xlim(-1.3, 10.5)
    ax.set_ylim(0, 14.5)
    box(ax, 0.8, 13.1, 7.2, 0.95, "LEG (i)  receipted key destruction", "A210", 8)
    box(
        ax,
        0.8,
        11.5,
        7.2,
        1.15,
        "LEG (ii)  ciphertext-only sweep over enumerated stores\n-> name any plaintext remnant by store · row · field",
        "A220",
        7.8,
    )
    box(
        ax,
        0.8,
        9.9,
        7.2,
        1.15,
        "LEG (iii)  post-destruction Merkle-root chain verification\nof every run that touched the scope",
        "A230",
        7.8,
    )
    box(
        ax,
        0.8,
        8.3,
        7.2,
        1.15,
        "LEG (iv)  post-destruction byte-replay\nof every checkpointed mutating run",
        "A240",
        7.8,
    )
    diamond(ax, 4.4, 6.6, 3.1, 0.85, "any leg fails ?", "A250", 8)
    box(ax, 8.4, 6.05, 2.0, 1.2, "WITHHOLD\ncertificate\n(fail-closed)", "A255", 7.4)
    box(
        ax,
        0.8,
        4.2,
        7.2,
        1.2,
        "ERASURE CERTIFICATE (canonical)\ndigest appended to chain — ERASURE_CERTIFICATE_ISSUED",
        "A260",
        7.8,
    )
    box(
        ax,
        0.8,
        2.3,
        7.2,
        1.2,
        "verify_erasure: RE-EXECUTE legs (i)-(iv)\nagainst the live store on demand",
        "A270",
        7.8,
    )
    # Flow arrows.
    arrow(ax, 4.4, 13.1, 4.4, 12.65)
    arrow(ax, 4.4, 11.5, 4.4, 11.05)
    arrow(ax, 4.4, 9.9, 4.4, 9.45)
    arrow(ax, 4.4, 8.3, 4.4, 7.5)
    arrow(ax, 7.5, 6.6, 8.4, 6.6)
    ax.text(7.95, 6.82, "yes", fontsize=7.5, ha="center", va="bottom")
    arrow(ax, 4.4, 5.75, 4.4, 5.4)
    ax.text(4.62, 5.56, "no", fontsize=7.5, ha="left", va="center")
    arrow(ax, 4.4, 4.2, 4.4, 3.5)
    # Re-executable loop: down the left margin from A270 back up to A210 (dashed).
    ax.add_patch(
        FancyArrowPatch(
            (0.8, 2.9),
            (0.8, 13.55),
            arrowstyle="-|>",
            mutation_scale=12,
            lw=1.1,
            color="black",
            linestyle="dashed",
            connectionstyle="arc3,rad=-0.30",
        )
    )
    ax.text(-0.95, 8.2, "re-executable", fontsize=7.5, rotation=90, ha="center", va="center")
    ax.set_title("FIG. A2", fontsize=12, loc="left")
    save(fig, "add_figA2_erasure_certificate.png", dpi=150)


def figA3_one_substrate():
    fig, ax = plt.subplots(figsize=(11.5, 8.5))
    ax.axis("off")
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 10)
    # Central ledger.
    box(ax, 4.9, 4.1, 4.2, 1.8, "ONE HASH-CHAINED,\nMERKLE-COMMITTED\nRECEIPT LEDGER", "A300", 8.2)
    # Four surrounding roles.
    box(
        ax,
        0.3,
        7.9,
        4.3,
        1.7,
        "GOVERN what to remember\nMotive · effective-policy digest\n(inheritance-flattened)",
        "A310",
        7.6,
    )
    box(
        ax,
        9.4,
        7.7,
        4.3,
        1.9,
        "PROVE why remembered / rejected\ncandidate + decision receipts ·\nnegative space · byte replay",
        "A320",
        7.6,
    )
    box(
        ax,
        0.3,
        0.4,
        4.7,
        2.0,
        "DETECT non-memory governance\ncoherence incidents\n(COHERENCE_INCIDENT_RECORDED)\n+ anti-windup hold",
        "A330",
        7.6,
    )
    box(ax, 9.4, 0.5, 4.3, 1.8, "PROVE what was forgotten\ncrypto-shred · four-leg\nerasure certificate", "A340", 7.6)
    # Connections to the ledger.
    arrow(ax, 4.6, 8.2, 5.6, 5.9)  # govern -> ledger
    arrow(ax, 9.4, 8.3, 8.4, 5.9)  # prove-why -> ledger
    arrow(ax, 5.0, 1.9, 5.9, 4.1)  # detect -> ledger
    arrow(ax, 9.4, 1.7, 8.4, 4.3)  # forget -> ledger
    ax.text(
        3.15,
        3.05,
        "same ledger ->\ninherits replay +\nerasure-surviving\nverifiability",
        fontsize=7.2,
        ha="left",
        va="center",
        style="italic",
    )
    ax.set_title("FIG. A3", fontsize=12, loc="left")
    save(fig, "add_figA3_one_substrate.png", dpi=150)


if __name__ == "__main__":
    figA1_erasure_surviving_replay()
    figA2_erasure_certificate()
    figA3_one_substrate()
    print("wrote addendum figures to", OUT)
    for f in sorted(os.listdir(OUT)):
        if f.startswith("add_fig"):
            print("  ", f)
