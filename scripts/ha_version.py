#!/usr/bin/env python3
"""Print the Home Assistant version that requirements_ci.txt pins (standard library only).

The hassfest job checks out the matching home-assistant/core tag.

Usage: ha_version.py [--file requirements_ci.txt]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent


def ha_version(requirements: Path) -> str:
    for line in requirements.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"\s*homeassistant\s*==\s*([^\s#;]+)\s*(#.*)?", line)
        if m:
            return m.group(1)
    raise SystemExit(f"{requirements} does not pin homeassistant==<version>")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", type=Path, default=ROOT / "requirements_ci.txt")
    args = p.parse_args()
    print(ha_version(args.file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
