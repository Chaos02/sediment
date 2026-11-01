#!/usr/bin/env python3
"""Offline stand-in for the HACS repository validation (standard library only).

The real check (the hacs/action container, ghcr.io/hacs/action) reads the
repository through the GitHub API and only validates public GitHub repositories.
GitLab CI cannot give it that, so this script checks the same file rules
against the working tree:

  hacs.json          valid JSON, 'name' present, only keys HACS knows, zip_release
                     needs filename (HACS: hacsjson)
  integration        exactly one directory under custom_components (HACS: layout)
  manifest.json      codeowners, documentation, domain, issue_tracker, name and
                     version present and well formed (HACS: integration_manifest)
  brand/             icon.png (and icon@2x.png) present (HACS: brands). A warning
                     until the icons land; an error with --require-brand.
  README             a README or info file exists (HACS: information)
  LICENSE            present (warning only; the default list wants one)

The checks that need GitHub itself (issues enabled, repository description,
topics, archived, license detection) are not run here.

Exit status 1 when an error was found. Warnings never fail the job.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import struct
import sys
from urllib.parse import urlparse

# Keys of HACS_MANIFEST_JSON_SCHEMA (custom_components/hacs/utils/validate.py).
HACS_JSON_KEYS = {
    "content_in_root": bool,
    "country": (str, list),
    "filename": str,
    "hacs": str,
    "hide_default_branch": bool,
    "homeassistant": str,
    "persistent_directory": str,
    "render_readme": bool,
    "zip_release": bool,
    "name": str,
}
MANIFEST_REQUIRED = ("codeowners", "documentation", "domain", "issue_tracker", "name", "version")
README_NAMES = {"readme", "readme.md", "info", "info.md"}
VERSION = re.compile(r"\d+(\.\d+)*([-.]?(a|b|rc|alpha|beta|dev)[-.]?\d*)?")
ICONS = {"icon.png": 256, "icon@2x.png": 512}


class Report:
    def __init__(self) -> None:
        self.errors = 0

    def ok(self, msg: str) -> None:
        print(f"ok       {msg}")

    def warn(self, msg: str) -> None:
        print(f"WARNING  {msg}")

    def error(self, msg: str) -> None:
        self.errors += 1
        print(f"ERROR    {msg}")


def load_json(path: Path, rep: Report) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        rep.error(f"{path.name} is missing")
        return None
    except ValueError as err:
        rep.error(f"{path.name} is not valid JSON: {err}")
        return None
    if not isinstance(data, dict):
        rep.error(f"{path.name} must hold a JSON object")
        return None
    return data


def check_hacs_json(root: Path, rep: Report) -> None:
    data = load_json(root / "hacs.json", rep)
    if data is None:
        return
    before = rep.errors
    if not isinstance(data.get("name"), str) or not data.get("name"):
        rep.error("hacs.json: 'name' is required and must be a string")
    for key, value in data.items():
        if key not in HACS_JSON_KEYS:
            rep.error(f"hacs.json: unknown key {key!r}")
        elif not isinstance(value, HACS_JSON_KEYS[key]) or (
            key != "country" and HACS_JSON_KEYS[key] is str and not value
        ):
            rep.error(f"hacs.json: {key!r} has the wrong type")
    if data.get("zip_release") and not data.get("filename"):
        rep.error("hacs.json: zip_release is true, but filename is not set")
    if data.get("filename") and not data.get("zip_release"):
        rep.warn("hacs.json: filename is set but zip_release is not, HACS ignores the file name")
    ha = data.get("homeassistant")
    if isinstance(ha, str) and not VERSION.fullmatch(ha):
        rep.error(f"hacs.json: homeassistant {ha!r} is not a version")
    if rep.errors == before:
        mode = "zip release" if data.get("zip_release") else "installs from the tag, no zip asset"
        rep.ok(f"hacs.json ({mode})")


def check_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    u = urlparse(value)
    return u.scheme in ("http", "https") and bool(u.netloc)


def check_integration(root: Path, rep: Report, require_brand: bool) -> None:
    base = root / "custom_components"
    dirs = (
        sorted(d for d in base.iterdir() if d.is_dir() and d.name != "__pycache__")
        if base.is_dir()
        else []
    )
    if len(dirs) != 1:
        rep.error(
            f"custom_components must hold exactly one integration, found {[d.name for d in dirs]}"
        )
        return
    integ = dirs[0]
    rep.ok(f"one integration: custom_components/{integ.name}")
    stray = [f.name for f in base.iterdir() if f.is_file() and f.name != "__init__.py"]
    if stray:
        rep.warn(f"custom_components holds loose files: {stray}")

    manifest = load_json(integ / "manifest.json", rep)
    if manifest is not None:
        before = rep.errors
        for key in MANIFEST_REQUIRED:
            if key not in manifest:
                rep.error(f"manifest.json: {key!r} is missing")
        if "codeowners" in manifest and not isinstance(manifest["codeowners"], list):
            rep.error("manifest.json: codeowners must be a list")
        for key in ("documentation", "issue_tracker"):
            if key in manifest and not check_url(manifest[key]):
                rep.error(f"manifest.json: {key} must be an http(s) URL")
        for key in ("domain", "name"):
            if key in manifest and not (isinstance(manifest[key], str) and manifest[key]):
                rep.error(f"manifest.json: {key} must be a non-empty string")
        if manifest.get("domain") and manifest["domain"] != integ.name:
            rep.error(
                f"manifest.json: domain {manifest['domain']!r} differs from the folder {integ.name!r}"
            )
        version = manifest.get("version")
        if "version" in manifest and not (isinstance(version, str) and VERSION.fullmatch(version)):
            rep.error(f"manifest.json: version {version!r} is not a version string")
        if rep.errors == before:
            rep.ok(f"manifest.json (version {version})")

    check_brand(integ / "brand", rep, require_brand)


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def check_brand(brand: Path, rep: Report, require: bool) -> None:
    problem = rep.error if require else rep.warn
    if not (brand / "icon.png").is_file():
        problem(
            f"{brand.parent.name}/brand/icon.png is missing (HACS needs it, or an entry in "
            "home-assistant/brands; Home Assistant 2026.3 and later read it from the folder)"
        )
        return
    before = rep.errors
    for name, side in ICONS.items():
        f = brand / name
        if not f.is_file():
            rep.warn(f"brand/{name} is missing")
            continue
        size = png_size(f)
        if size is None:
            rep.error(f"brand/{name} is not a PNG file")
        elif size != (side, side):
            rep.warn(f"brand/{name} is {size[0]}x{size[1]}, expected {side}x{side}")
    if rep.errors == before:
        rep.ok("brand/icon.png")


def check_readme_license(root: Path, rep: Report) -> None:
    names = {f.name.lower() for f in root.iterdir() if f.is_file()}
    if names & README_NAMES:
        rep.ok("README")
    else:
        rep.error("no README.md or info.md in the repository root")
    if any(n == "license" or n.startswith("license.") for n in names):
        rep.ok("LICENSE")
    else:
        rep.warn("no LICENSE file (the HACS default list requires a license)")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    p.add_argument(
        "--require-brand",
        action="store_true",
        help="a missing brand/icon.png is an error, not a warning",
    )
    args = p.parse_args()

    rep = Report()
    check_hacs_json(args.root, rep)
    check_integration(args.root, rep, args.require_brand)
    check_readme_license(args.root, rep)
    if rep.errors:
        print(f"\n{rep.errors} error(s)")
        return 1
    print("\nHACS file checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
