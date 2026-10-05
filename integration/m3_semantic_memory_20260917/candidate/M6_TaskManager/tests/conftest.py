"""pytest path setup for the M6 suite under the semantic candidate tree.

The semantic ``source_admission`` / ``command_validator`` import M3 modules
(``M3_KnowledgeGraph.src.*``), and the tests import M6 both as ``src.*`` and as
``M6_TaskManager.src.*``. That needs two roots on ``sys.path``:

* the candidate root (parent of ``M6_TaskManager``) for ``M3_KnowledgeGraph`` and
  ``M6_TaskManager`` package imports;
* ``M6_TaskManager`` itself for the bare ``src.*`` imports the tests use.

Without this the collection fails with ``No module named 'M3_KnowledgeGraph'``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent          # .../candidate/M6_TaskManager/tests
_M6 = _TESTS.parent                                # .../candidate/M6_TaskManager
_ROOT = _M6.parent                                 # .../candidate

for _path in (_ROOT, _M6):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
