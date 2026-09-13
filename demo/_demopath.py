"""Import FIRST in every demo entry script. Nothing else belongs in here.

``demo/inspect.py`` is the demo's "show me the graph" command, and that name
shadows the standard library's ``inspect`` module: Python puts a script's own
directory at ``sys.path[0]``, so any other script in ``demo/`` would import
``demo/inspect.py`` when a dependency (pydantic -> typing_extensions) asks for
``inspect``, and blow up with::

    AttributeError: module 'inspect' has no attribute 'signature'

This module removes ``demo/`` from the FRONT of ``sys.path``, pins the real
stdlib ``inspect`` into ``sys.modules``, then re-appends ``demo/`` at the END
so ``import _demo`` still resolves while stdlib names always win.
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

sys.path[:] = [entry for entry in sys.path if os.path.abspath(entry or os.getcwd()) != _HERE]

import inspect as _stdlib_inspect  # noqa: E402,F401  (pin the real module)

if _HERE not in sys.path:
    sys.path.append(_HERE)
