"""Make the shipped ``scripts/ledger.py`` importable as ``import ledger``."""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ is not a package: put it at the FRONT of sys.path so `import ledger` resolves
# to the shipped module in this worktree, never to something else on the box.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
