"""Build a persistent Memotron graph populated with demo simulation data.

The generated SQLite file is intended for the interactive memory graph browser:

  uv run examples/memory_graph_demo.py
  uv run memotron-admin-server --graph-path .memotron/memory_graph_demo.sqlite --scope customer:wdw:pinnacle-events
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from fleet_demo_web import SCOPE, build_timeline

DEFAULT_GRAPH_PATH = Path(".memotron/memory_graph_demo.sqlite")


async def build_demo_graph(graph_path: Path) -> None:
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    if graph_path.exists():
        graph_path.unlink()
    steps, meta = await build_timeline(graph_path=graph_path)
    print("Memotron memory graph demo populated.")
    print(f"  graph_path      : {graph_path}")
    print(f"  default_scope   : {SCOPE.key}")
    print(f"  steps captured  : {len(steps)}")
    print(f"  dream distilled : {meta['dream_distilled']} observations -> 1 rollup")
    print()
    print("Serve it with:")
    print(f"  uv run memotron-admin-server --graph-path {graph_path} --scope {SCOPE.key}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Populate a persistent memory graph demo SQLite file.")
    parser.add_argument(
        "--graph-path",
        default=str(DEFAULT_GRAPH_PATH),
        help=f"SQLite graph path to create. Existing generated file is replaced. Default: {DEFAULT_GRAPH_PATH}",
    )
    args = parser.parse_args()
    asyncio.run(build_demo_graph(Path(args.graph_path).expanduser()))


if __name__ == "__main__":
    main()
