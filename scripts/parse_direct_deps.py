"""Parse direct dependency names from a pyproject.toml file.

Prints one lowercase package name per line.

Usage: python3 scripts/parse_direct_deps.py [path/to/pyproject.toml]
"""

import re
import sys


def parse(path: str = "pyproject.toml") -> list[str]:
    with open(path) as f:
        text = f.read()
    m = re.search(
        r"^dependencies\s*=\s*\[(.*?)\](?=\s*\n\s*\n|\s*\n\s*\[)", text, re.S | re.M
    )
    if not m:
        return []
    names: list[str] = []
    for line in m.group(1).splitlines():
        line = line.strip().strip(",").strip("\"'")
        if line and not line.startswith("#"):
            name = re.split(r"[>=<!\[;]", line)[0].strip().lower()
            if name:
                names.append(name)
    return names


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "pyproject.toml"
    for name in parse(path):
        print(name)
