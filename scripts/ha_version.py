#!/usr/bin/env python3
"""Print the Home Assistant version that requirements_test.txt pins (standard library only).

The pin is on pytest-homeassistant-custom-component, which depends on exactly one
Home Assistant release (`homeassistant==2026.10.4`). This reads that dependency from
the plugin's metadata on PyPI, so the hassfest job can check out the matching
home-assistant/core tag without a second pin to keep in sync.

Usage: ha_version.py [--file requirements_test.txt] [--plugin-version 0.13.375]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
import urllib.request

PLUGIN = "pytest-homeassistant-custom-component"
ROOT = Path(__file__).resolve().parent.parent


def plugin_version(requirements: Path) -> str:
    for line in requirements.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(rf"\s*{re.escape(PLUGIN)}\s*==\s*([^\s#;]+)\s*(#.*)?", line)
        if m:
            return m.group(1)
    raise SystemExit(f"{requirements} does not pin {PLUGIN}==<version>")


def ha_version(version: str) -> str:
    url = f"https://pypi.org/pypi/{PLUGIN}/{version}/json"
    last: Exception | None = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                info = json.load(resp)["info"]
            break
        except OSError as err:
            last = err
    else:
        raise SystemExit(f"cannot read {url}: {last}")
    for req in info.get("requires_dist") or []:
        m = re.fullmatch(r"homeassistant==([0-9][^\s;]*)\s*(;.*)?", req)
        if m:
            return m.group(1)
    raise SystemExit(f"{PLUGIN} {version} has no 'homeassistant==' requirement")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--file", type=Path, default=ROOT / "requirements_test.txt")
    p.add_argument("--plugin-version", help="skip the file, use this plugin version")
    args = p.parse_args()
    print(ha_version(args.plugin_version or plugin_version(args.file)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
