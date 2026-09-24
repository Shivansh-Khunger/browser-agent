from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_removed_runtime_surface_does_not_return() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "cutover_audit.py")],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
