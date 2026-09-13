"""Shared patent-style line-drawing primitives.

Black-and-white schematic helpers (rounded boxes, decision diamonds, arrows, a
vertical flow stacker) used by every figure generator in this directory so the
drawings share one visual language and one implementation. Imported by
make_receipt_figures.py and make_coherence_figures.py; output goes to
patent/figures/.
"""

from __future__ import annotations

import os
from itertools import pairwise

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figures")
os.makedirs(OUT, exist_ok=True)

BW = dict(facecolor="white", edgecolor="black", linewidth=1.4)


def box(ax, x, y, w, h, text, num=None, fs=8.5):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02", **BW, mutation_scale=8))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs)
    if num:
        ax.text(x + w - 0.04, y + h - 0.04, num, ha="right", va="top", fontsize=7.5, style="italic")


def diamond(ax, cx, cy, hw, hh, text, num=None, fs=8.5):
    ax.add_patch(plt.Polygon([(cx, cy + hh), (cx + hw, cy), (cx, cy - hh), (cx - hw, cy)], **BW))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=fs)
    if num:
        ax.text(cx + hw - 0.05, cy + hh - 0.2, num, ha="right", va="top", fontsize=7.5, style="italic")


def arrow(ax, x1, y1, x2, y2, text=None, fs=7.5, dx=0.06, dashed=False):
    ax.add_patch(
        FancyArrowPatch(
            (x1, y1),
            (x2, y2),
            arrowstyle="-|>",
            mutation_scale=13,
            lw=1.2,
            color="black",
            linestyle="dashed" if dashed else "solid",
        )
    )
    if text:
        ax.text((x1 + x2) / 2 + dx, (y1 + y2) / 2, text, ha="left", va="center", fontsize=fs)


def flow(ax, steps, x=1.0, w=6.4, top=11.3, gap=1.55, h=1.0, fs=8):
    """Stack labelled boxes top-to-bottom and connect with downward arrows."""
    y = top
    centers = []
    for txt, num in steps:
        box(ax, x, y, w, h, txt, num, fs=fs)
        centers.append((x + w / 2, y))
        y -= gap
    for (cx, cy0), (_, cy1) in pairwise(centers):
        arrow(ax, cx, cy0, cx, cy1 + h)
    return centers


def save(fig, name, dpi=200):
    fig.savefig(os.path.join(OUT, name), dpi=dpi, bbox_inches="tight")
    plt.close(fig)
