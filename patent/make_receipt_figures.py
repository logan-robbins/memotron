"""Generate patent-style line drawings (FIG. 1-7) for the replay-receipts
provisional. Black-and-white schematics with reference numerals matching the
specification text. Output: patent/figures/.

Run (no project dependency pollution):
    uv run --with matplotlib --with numpy python patent/make_receipt_figures.py
"""

from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from _draw import OUT, arrow, box, diamond, flow, save
from matplotlib.patches import FancyArrowPatch


def fig1_architecture():
    fig, ax = plt.subplots(figsize=(9, 11))
    ax.axis("off")
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 16)
    # Main vertical formation pipeline (left/center column).
    box(ax, 1.0, 14.7, 6.2, 0.9, "AGENT / APPLICATION", "100")
    box(ax, 1.0, 13.4, 6.2, 0.9, "EVIDENCE EPISODE (tenant · scope · agent)", "105")
    box(ax, 1.0, 12.1, 6.2, 0.9, "EVIDENCE-DIGEST MODULE", "110")
    box(ax, 1.0, 10.7, 6.2, 1.0, "CANDIDATE EXTRACTION\n(prompt · model · profile snapshot)", "125")
    box(ax, 1.0, 9.4, 6.2, 0.9, "CANDIDATE RECEIPTS", "130")
    box(ax, 1.0, 7.9, 6.2, 1.1, "POLICY GATES\ntype · salience · governance · trust · dedup · supersede", "135", 8)
    box(ax, 1.0, 6.6, 6.2, 0.9, "DECISION RECEIPTS", "140")
    box(ax, 1.0, 5.0, 6.2, 1.2, "GRAPH MUTATION\nTYPED TEMPORAL GRAPH\nworking tier + full evidence tier", "145", 8)
    # Policy source feeding the resolver.
    box(ax, 8.4, 13.4, 4.0, 1.0, "CONTROL PLANE +\nMEMORYBANK (policy source)", "115", 8)
    box(ax, 8.4, 11.9, 4.0, 1.1, "MOTIVE RESOLVER\n-> policy version digest\n+ effective policy digest", "120", 8)
    # Hashing / commitment column (right of the gates).
    box(ax, 8.4, 8.7, 4.0, 0.9, "GRAPH STATE HASH", "150", 8)
    box(ax, 8.4, 7.3, 4.0, 0.9, "RECEIPT HASH CHAIN", "155", 8)
    box(ax, 8.4, 5.9, 4.0, 0.9, "RUN MERKLE CHECKPOINT", "160", 8)
    # Downstream proof/report consumers.
    box(ax, 0.2, 2.9, 2.4, 1.0, "REPLAY ENGINE\n-> replay proof", "165", 7.6)
    box(ax, 2.9, 2.9, 2.4, 1.0, "COUNTERFACTUAL\nEVALUATOR", "170", 7.6)
    box(ax, 5.6, 2.9, 2.2, 1.0, "NEGATIVE-SPACE\nQUERY", "175", 7.6)
    box(ax, 8.1, 2.9, 2.2, 1.0, "POLICY CERT +\nREGRESSION GATE", "180", 7.2)
    box(ax, 10.6, 2.9, 2.2, 1.0, "MEMORY BOM\n(run report)", "190", 7.4)
    box(
        ax,
        3.4,
        1.1,
        6.0,
        0.9,
        "GOVERNANCE CERTIFICATION BUNDLE\nBOM + replay + negative space + policy + erasure",
        "198",
        6.8,
    )

    arrow(ax, 4.1, 14.7, 4.1, 14.3)
    arrow(ax, 4.1, 13.4, 4.1, 13.0)
    arrow(ax, 4.1, 12.1, 4.1, 11.7)
    arrow(ax, 4.1, 10.7, 4.1, 10.3)
    arrow(ax, 4.1, 9.4, 4.1, 9.0)
    arrow(ax, 4.1, 7.9, 4.1, 7.5)
    arrow(ax, 4.1, 6.6, 4.1, 6.2)
    arrow(ax, 10.4, 13.4, 10.4, 13.0)  # source -> resolver
    arrow(ax, 8.4, 12.4, 7.2, 11.2, "policy digests")  # resolver -> gates
    arrow(ax, 7.4, 5.9, 8.4, 9.0)  # mutation -> state hash (before/after)
    arrow(ax, 8.4, 8.7, 8.4, 8.2)  # state hash -> chain
    arrow(ax, 7.2, 6.9, 8.4, 7.6)  # decision receipts -> chain
    arrow(ax, 10.4, 7.3, 10.4, 6.8)  # chain -> checkpoint
    ax.text(7.55, 8.1, "before /\nafter", fontsize=7, ha="left", va="center")
    ax.text(7.35, 7.45, "receipts", fontsize=7, ha="left", va="center")
    # Consumers hang off the bottom of the pipeline / checkpoint (no long crossings).
    arrow(ax, 1.4, 5.0, 1.4, 3.9)  # pipeline -> replay
    arrow(ax, 4.1, 5.0, 4.1, 3.9)  # pipeline -> counterfactual
    arrow(ax, 8.9, 5.9, 6.7, 3.9)  # checkpoint -> neg-space
    arrow(ax, 10.4, 5.9, 9.2, 3.9)  # checkpoint -> policy gate
    arrow(ax, 11.5, 5.9, 11.7, 3.9)  # checkpoint -> BOM
    arrow(ax, 4.1, 2.9, 5.5, 2.0)  # counterfactual -> bundle
    arrow(ax, 6.7, 2.9, 6.2, 2.0)  # negative-space -> bundle
    arrow(ax, 9.2, 2.9, 7.2, 2.0)  # policy gate -> bundle
    arrow(ax, 11.7, 2.9, 8.0, 2.0)  # BOM -> bundle
    ax.set_title("FIG. 1", fontsize=12, loc="left")
    save(fig, "fig1_architecture.png", dpi=150)


def fig2_receipt():
    fig, ax = plt.subplots(figsize=(9, 7))
    ax.axis("off")
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 10)
    # Inputs bound into a receipt.
    inputs = [
        ("CANDIDATE DIGEST", "210"),
        ("EPISODE DIGEST", "212"),
        ("MOTIVE VERSION DIGEST", "214"),
        ("EFFECTIVE POLICY DIGEST", "216"),
        ("GOVERNANCE POLICY DIGEST", "218"),
    ]
    y = 9.0
    for txt, num in inputs:
        box(ax, 0.4, y, 3.3, 0.62, txt, num, fs=7.5)
        arrow(ax, 3.7, y + 0.31, 4.6, 6.2)
        y -= 0.74
    # Central receipt record.
    box(
        ax,
        4.6,
        5.2,
        4.6,
        2.0,
        "RECEIPT RECORD\nschema_version · decision_type\n· decision reason\n"
        "· salience/threshold · dedup/score\n· graph state hash before/after (150)",
        "200",
        fs=7.6,
    )
    box(ax, 9.7, 6.6, 3.0, 0.9, "DECISION RESULT +\ngraph pointer / reason", "220", 7.6)
    arrow(ax, 9.2, 6.4, 9.7, 7.0)
    # Hash chain of three receipts.
    cy = 3.0
    for i, x in enumerate([1.0, 5.2, 9.4]):
        box(ax, x, cy, 3.0, 1.1, "prev_receipt_hash (230)\nreceipt_hash (232)", f"R{i}", fs=7.5)
        if i > 0:
            arrow(ax, x, cy + 0.55, x - 1.2, cy + 0.55)
    arrow(ax, 6.9, 5.2, 6.7, 4.1, "canonicalize + hash")
    # Merkle root + run checkpoint.
    box(
        ax,
        4.6,
        0.4,
        4.6,
        1.0,
        "RUN CHECKPOINT (240): first/last hash,\ncount, merkle_root (250), graph hashes",
        None,
        fs=7.5,
    )
    for x in [2.5, 6.7, 10.9]:
        arrow(ax, x, cy, 6.9, 1.45)
    ax.text(1.6, 2.05, "MERKLE ROOT\n(250)", ha="center", fontsize=8, style="italic")
    ax.set_title("FIG. 2", fontsize=12, loc="left")
    save(fig, "fig2_receipt.png")


def fig3_formation():
    fig, ax = plt.subplots(figsize=(7.5, 12))
    ax.axis("off")
    ax.set_xlim(0, 8.4)
    ax.set_ylim(0, 13)
    steps = [
        ("Receive episode in tenant / scope", "300"),
        ("Compute immutable evidence digest", "305"),
        ("Resolve Motive + policy; compute version digests", "310"),
        ("Extract candidates (recorded model snapshot)", "315"),
        ("Record a candidate receipt for each candidate", "320"),
        ("Resolve memory type + truth key", "325"),
        ("Apply governance / trust gates (receipt each)", "330"),
        ("Apply Motive type + salience gates (receipt each)", "335"),
        ("Dedup / supersede vs active rows (record score)", "340"),
        ("Create / reinforce / supersede / reject mutation\n(receipt + graph hashes)", "345"),
        ("Link hash chain + compute run Merkle checkpoint", "350"),
        ("Mark run replay-verifiable iff graph hash matches", "355"),
    ]
    flow(ax, steps, x=0.4, w=7.4, top=11.6, gap=0.98, h=0.72, fs=7.6)
    ax.set_title("FIG. 3", fontsize=12, loc="left")
    save(fig, "fig3_formation.png")


def fig4_replay():
    fig, ax = plt.subplots(figsize=(8.5, 12))
    ax.axis("off")
    ax.set_xlim(0, 10.5)
    ax.set_ylim(0, 14)
    box(ax, 1.0, 12.8, 6.9, 0.8, "Select run / scope / episode / receipt range", "400", 8)
    box(ax, 1.0, 11.6, 6.9, 0.8, "Verify receipt chain + run Merkle root", "410", 8)
    box(ax, 1.0, 10.4, 6.9, 0.8, "Load evidence / candidate / policy / decision receipts", "420", 7.6)
    diamond(ax, 4.45, 9.1, 3.15, 0.75, "Required artifact missing ?", "430", 7.8)
    box(ax, 7.95, 8.45, 2.3, 1.3, "FAIL CLOSED\nmissing artifact", "435", 7.4)
    box(ax, 1.0, 7.45, 6.9, 0.85, "Apply decision receipts in event order from base", "440", 7.6)
    box(ax, 1.0, 6.15, 6.9, 0.85, "Recompute UUIDs, status, demotion, supersede, hash", "450", 7.4)
    diamond(ax, 4.45, 4.75, 3.15, 0.75, "Reconstructed hash == checkpoint ?", "460", 7.6)
    box(ax, 7.95, 4.1, 2.3, 1.3, "FAIL CLOSED\ncheckpoint mismatch", "480", 7.2)
    diamond(ax, 4.45, 2.9, 3.15, 0.75, "Latest receipted hash == live graph ?", "465", 7.4)
    box(ax, 1.0, 0.55, 3.2, 0.95, "REPLAY PROOF +\nNO-SILENT-MUTATION PASS", "470", 7.1)
    box(ax, 5.4, 0.55, 3.2, 0.95, "FAIL CLOSED\nlive-state mismatch", "485", 7.2)
    arrow(ax, 4.45, 12.8, 4.45, 12.4)
    arrow(ax, 4.45, 11.6, 4.45, 11.2)
    arrow(ax, 4.45, 10.4, 4.45, 9.85)
    arrow(ax, 7.6, 9.1, 7.95, 9.1, "yes")
    arrow(ax, 4.45, 8.35, 4.45, 8.3, "no")
    arrow(ax, 4.45, 7.45, 4.45, 7.0)
    arrow(ax, 4.45, 6.15, 4.45, 5.5)
    arrow(ax, 7.6, 4.75, 7.95, 4.75, "no")
    arrow(ax, 4.45, 4.0, 4.45, 3.65, "yes")
    arrow(ax, 3.0, 2.25, 2.6, 1.5, "yes")
    arrow(ax, 5.9, 2.25, 6.8, 1.5, "no")
    ax.set_title("FIG. 4", fontsize=12, loc="left")
    save(fig, "fig4_replay.png")


def fig5_counterfactual():
    fig, ax = plt.subplots(figsize=(9, 7))
    ax.axis("off")
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 9)
    box(ax, 0.6, 7.4, 4.4, 1.1, "RECORDED CANDIDATE RECEIPTS\n+ EVIDENCE (from run)", "500", 8)
    box(ax, 6.4, 7.4, 4.4, 1.1, "ALTERNATE MOTIVE + VERSION\n+ EFFECTIVE POLICY", "510", 8)
    box(
        ax, 3.4, 5.4, 6.2, 1.1, "APPLY ALTERNATE POLICY\ntype · salience · dedup · governance · theme rules", "520", 7.8
    )
    box(
        ax,
        3.4,
        3.4,
        6.2,
        1.1,
        "ISOLATED MEMORY DELTA\naccept · reject · transform · reinforce ·\nsupersede · demote · prune",
        "530",
        7.8,
    )
    box(ax, 3.4, 1.6, 6.2, 0.9, "COMPARE TO PRODUCTION DELTA", "540", 8)
    box(ax, 3.4, 0.2, 6.2, 0.85, "COUNTERFACTUAL REPORT (+ optional receipts)", "550", 8)
    box(ax, 10.4, 3.2, 2.4, 1.2, "PRODUCTION\nGRAPH\nUNCHANGED", "560", 8)
    arrow(ax, 2.8, 7.4, 5.4, 6.5)
    arrow(ax, 8.6, 7.4, 7.0, 6.5)
    arrow(ax, 6.5, 5.4, 6.5, 4.5)
    arrow(ax, 6.5, 3.4, 6.5, 2.5)
    arrow(ax, 6.5, 1.6, 6.5, 1.05)
    ax.add_patch(
        FancyArrowPatch(
            (9.6, 2.0), (10.6, 3.2), arrowstyle="-|>", mutation_scale=13, lw=1.2, color="black", linestyle="dashed"
        )
    )
    ax.text(10.0, 2.4, "no\nmutation", fontsize=7.5, ha="left", va="center")
    ax.set_title("FIG. 5", fontsize=12, loc="left")
    save(fig, "fig5_counterfactual.png")


def fig6_negative_space():
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.axis("off")
    ax.set_xlim(0, 9)
    ax.set_ylim(0, 11)
    box(ax, 1.2, 9.4, 6.6, 0.9, "QUERY: scope / time / episode / Motive / type / reason", "600", 7.8)
    box(
        ax,
        1.2,
        7.7,
        6.6,
        1.1,
        "SELECT NON-MATERIALIZING RECEIPTS\nfiltered · gated · redacted · reinforced ·\nsuperseded · demoted · pruned",
        "610",
        7.6,
    )
    box(
        ax,
        1.2,
        5.9,
        6.6,
        1.1,
        "JOIN candidate digest + evidence pointer\n+ policy digest + Motive digest + reason",
        "620",
        7.6,
    )
    box(ax, 1.2, 4.4, 6.6, 0.9, "REDACTION FILTER (policy-safe)", "630", 8)
    box(ax, 1.2, 2.8, 6.6, 0.9, "REDACTED EXPLANATION\n(operator / auditor / user)", "640", 8)
    box(
        ax,
        0.6,
        0.8,
        7.8,
        1.1,
        'e.g. "directive extracted but not persisted:\nMotive allowed only requirement memory"',
        None,
        fs=7.6,
    )
    arrow(ax, 4.5, 9.4, 4.5, 8.8)
    arrow(ax, 4.5, 7.7, 4.5, 7.0)
    arrow(ax, 4.5, 5.9, 4.5, 5.3)
    arrow(ax, 4.5, 4.4, 4.5, 3.7)
    arrow(ax, 4.5, 2.8, 4.5, 1.9)
    ax.set_title("FIG. 6", fontsize=12, loc="left")
    save(fig, "fig6_negative_space.png")


def fig7_certification():
    fig, ax = plt.subplots(figsize=(7.5, 14))
    ax.axis("off")
    ax.set_xlim(0, 9)
    ax.set_ylim(-1.0, 16)
    steps = [
        ("FIXTURE CORPUS\nsensitive · irrelevant · contradictory ·\nlow-salience · high-value", "700"),
        ("RUN FORMATION UNDER THE MOTIVE", "710"),
        ("RECORD RECEIPTS FOR EVERY CANDIDATE + DECISION", "720"),
        ("VERIFY RECEIPT CHAIN + GRAPH STATE HASH", "730"),
        (
            "COMPUTE METRICS\ndisallowed-type · PII persistence · unexplained ·\n"
            "dup rate · supersession · context-visible · demoted · budget",
            "740",
        ),
        (
            "EMIT CERTIFICATE\nMotive digest · corpus digest · Merkle root ·\nmetrics · failing receipts · PASS/FAIL",
            "750",
        ),
        ("COMPARE AGAINST BASELINE RUN\naccepted-by-both · original-only · alternate-only", "760"),
        ("POLICY REGRESSION GATE\npreserve allowed baseline · block disallowed · no new accepts", "770"),
        ("GOVERNANCE BUNDLE DIGEST\ncertificate + comparison + replay + BOM", "780"),
    ]
    y = 14.6
    centers = []
    for txt, num in steps:
        h = 1.15 if "\n" in txt and txt.count("\n") >= 2 else 0.9
        box(ax, 0.6, y - h, 7.8, h, txt, num, fs=7.6)
        centers.append((4.5, y - h, y))
        y -= h + 0.7
    for i in range(len(centers) - 1):
        arrow(ax, 4.5, centers[i][1], 4.5, centers[i + 1][2])
    ax.set_title("FIG. 7", fontsize=12, loc="left")
    save(fig, "fig7_certification.png")


def fig8_bundle():
    fig, ax = plt.subplots(figsize=(11, 8.5))
    ax.axis("off")
    ax.set_xlim(0, 14.5)
    ax.set_ylim(0, 10.5)
    inputs = [
        (8.8, "MEMORY BOM\npolicy · evidence · candidates\nMerkle root · graph hashes", "810"),
        (7.45, "BYTE REPLAY PROOF\nchain + checkpoint + live state", "820"),
        (6.10, "NO-SILENT-MUTATION\nscope continuity proof", "830"),
        (4.75, "NEGATIVE SPACE\nobserved but absent", "840"),
        (3.40, "POLICY CERT +\nREGRESSION GATE", "850"),
        (2.05, "COHERENCE INCIDENTS +\nREPAIR RECEIPTS", "860"),
        (0.70, "ERASURE CERTIFICATE\n(optional digest)", "870"),
    ]
    for y, text, num in inputs:
        box(ax, 0.6, y, 4.0, 0.9, text, num, fs=7.0)
        arrow(ax, 4.6, y + 0.45, 5.6, 5.25)
    box(
        ax,
        5.6,
        4.3,
        4.0,
        1.9,
        "GOVERNANCE CERTIFICATION BUNDLE\ncanonicalize selected proofs\nscope · run · policy · coherence · erasure",
        "800",
        7.0,
    )
    box(ax, 10.7, 4.6, 3.2, 1.25, "CANONICAL\nBUNDLE DIGEST", "890", 7.3)
    box(ax, 10.7, 2.4, 3.2, 1.1, "ARCHIVE /\nDISCLOSE / AUDIT", None, 7.4)
    arrow(ax, 9.6, 5.25, 10.7, 5.25)
    arrow(ax, 12.3, 4.6, 12.3, 3.5)
    ax.set_title("FIG. 8", fontsize=12, loc="left")
    save(fig, "fig8_governance_bundle.png")


if __name__ == "__main__":
    fig1_architecture()
    fig2_receipt()
    fig3_formation()
    fig4_replay()
    fig5_counterfactual()
    fig6_negative_space()
    fig7_certification()
    fig8_bundle()
    print("wrote figures to", OUT)
    for f in sorted(os.listdir(OUT)):
        print("  ", f)
