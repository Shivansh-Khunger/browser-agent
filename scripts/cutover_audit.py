"""Fail when removed runtime surface or unpinned runtime dependencies return."""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCANNED = (
    ROOT / "browser_agent",
    ROOT / "tests",
    ROOT / "README.md",
    ROOT / ".env.example",
    ROOT / "pyproject.toml",
    ROOT / "uv.lock",
)
FORBIDDEN = ("camou" + "fox", "play" + "wright")


def files() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for item in SCANNED:
        if item.is_dir():
            found.extend(
                path
                for path in item.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            )
        else:
            found.append(item)
    return found


def main() -> int:
    failures: list[str] = []
    for path in files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        lowered = text.casefold()
        for forbidden in FORBIDDEN:
            if forbidden in lowered:
                failures.append(f"{path.relative_to(ROOT)}: contains removed runtime name")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if not re.search(r'"nodriver==[0-9]+\.[0-9]+\.[0-9]+"', project):
        failures.append("pyproject.toml: nodriver must use exact version pin")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"cutover audit passed ({len(files())} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
