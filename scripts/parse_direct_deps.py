"""Parse direct dependency names from a pyproject.toml file.

Prints one lowercase package name per line.

Usage: python3 scripts/parse_direct_deps.py [path/to/pyproject.toml]
"""

import re
import sys
from pathlib import Path


def parse(path: str = "pyproject.toml") -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r"^dependencies\s*=\s*\[(.*?)\](?=\s*\n\s*\n|\s*\n\s*\[)", text, re.S | re.M)
    if not m:
        return []
    names: list[str] = []
    for raw in m.group(1).splitlines():
        line = raw.strip().strip(",").strip("\"'")
        if line and not line.startswith("#"):
            name = re.split(r"[>=<!\[;]", line)[0].strip().lower()
            if name:
                names.append(name)
    return names


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "pyproject.toml"
    for name in parse(path):
        print(name)
