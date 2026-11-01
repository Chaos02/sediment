#!/usr/bin/env python3
"""Build sediment.zip, the manual-install archive (standard library only).

The archive holds the contents of custom_components/sediment at its root, the
layout HACS expects from a zip release and the one to unpack into
<config>/custom_components/sediment. Only files tracked by git go in. Names are
sorted and timestamps are fixed to the commit time, so the same commit gives the
same bytes (needed to rerun a release job safely).
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys
import zipfile

import release_tools as rt


def tracked_files(src: Path) -> list[Path]:
    result = rt.git("ls-files", "-z", "--", str(src.relative_to(rt.ROOT)), check=False)
    if result.returncode == 0 and result.stdout:
        names = [n for n in result.stdout.split("\0") if n]
        return sorted(rt.ROOT / n for n in names if (rt.ROOT / n).is_file())
    # Not a git checkout: take everything but caches.
    return sorted(
        f
        for f in src.rglob("*")
        if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"
    )


def timestamp() -> tuple[int, int, int, int, int, int]:
    import time

    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch is None:
        out = rt.git("log", "-1", "--format=%ct", check=False)
        epoch = out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else "315532800"
    t = time.gmtime(max(int(epoch), 315532800))  # ZIP cannot hold dates before 1980
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec - t.tm_sec % 2)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", type=Path, default=rt.INTEGRATION_DIR)
    p.add_argument("--out", type=Path, default=rt.ROOT / "dist" / rt.ZIP_NAME)
    args = p.parse_args()

    src = args.src.resolve()
    files = tracked_files(src)
    if not any(f.name == "manifest.json" and f.parent == src for f in files):
        print(f"REFUSING: {src}/manifest.json is not among the files to pack")
        return 1
    stamp = timestamp()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            info = zipfile.ZipInfo(f.relative_to(src).as_posix(), date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3
            zf.writestr(info, f.read_bytes(), compresslevel=9)
    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
    print(f"{args.out}: {len(files)} files, {args.out.stat().st_size} bytes")
    print(f"sha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
