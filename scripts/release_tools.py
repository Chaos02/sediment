"""Shared helpers for the release scripts (standard library only).

Tag shapes: ``vX.Y.Z`` is a final release, ``vX.Y.Z-beta.N`` a pre-release.
The manifest version uses the same shape without the ``v``.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = ROOT / "custom_components" / "sediment"
MANIFEST = INTEGRATION_DIR / "manifest.json"
CHANGELOG = ROOT / "CHANGELOG.md"
ZIP_NAME = "sediment.zip"

_TAG = re.compile(r"v(\d+\.\d+\.\d+)(?:-beta\.(\d+))?")
# Spellings of a beta that the manifest may use: 1.2.3-beta.1, 1.2.3b1, 1.2.3-b1 ...
_VERSION = re.compile(r"v?(\d+\.\d+\.\d+)(?:[-.]?(?:beta|b)[-.]?(\d+))?")


class Refusal(Exception):
    """A guard failed. The message says why."""


def parse_tag(tag: str) -> tuple[str, bool]:
    """Return (version, is_prerelease) for a release tag, or raise Refusal."""
    m = _TAG.fullmatch(tag)
    if not m:
        raise Refusal(f"{tag!r} is not vX.Y.Z or vX.Y.Z-beta.N")
    return normalize_version(tag), m.group(2) is not None


def normalize_version(raw: str) -> str:
    """Return X.Y.Z or X.Y.Z-beta.N for any accepted spelling, or raise Refusal."""
    m = _VERSION.fullmatch(raw.strip())
    if not m:
        raise Refusal(f"version {raw!r} is not X.Y.Z or X.Y.Z-beta.N")
    base, beta = m.groups()
    return base if beta is None else f"{base}-beta.{int(beta)}"


def manifest_version(path: Path = MANIFEST) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise Refusal(f"cannot read {path}: {err}") from err
    raw = data.get("version")
    if not isinstance(raw, str):
        raise Refusal(f"{path} has no version string")
    return normalize_version(raw)


_REF_DEFINITION = re.compile(r"^\[[^\]]+\]:\s")


def _heading_key(line: str) -> str:
    """Version (or the word 'unreleased') named by a '## ' changelog heading."""
    title = line[3:].strip().replace("[", "").replace("]", "")
    first = title.split()[0] if title.split() else ""
    return first.lstrip("v").lower()


def changelog_section(text: str, key: str) -> str:
    """Body of the '## <key>' section ('' when absent). key is a version or 'unreleased'."""
    out: list[str] = []
    found = False
    fenced = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            if found:
                break
            if _heading_key(line) == key.lower():
                found = True
            continue
        if found:
            out.append(line)
    while out and (not out[-1].strip() or _REF_DEFINITION.match(out[-1])):
        out.pop()
    return "\n".join(out).strip()


def release_notes(version: str, prerelease: bool, changelog: Path = CHANGELOG) -> str:
    """Notes for the release.

    Finals need their own section and are refused without one. Betas use the
    Unreleased section, or a short note when it is empty.
    """
    try:
        text = changelog.read_text(encoding="utf-8")
    except OSError as err:
        raise Refusal(f"cannot read {changelog}: {err}") from err
    if not prerelease:
        notes = changelog_section(text, version)
        if not notes:
            raise Refusal(f"CHANGELOG.md has no '## {version}' section, or it is empty")
        return notes
    notes = changelog_section(text, "unreleased")
    if notes:
        return notes
    return f"Beta build {version}. See CHANGELOG.md for the changes so far."


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=check,
    )
