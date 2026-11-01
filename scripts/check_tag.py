#!/usr/bin/env python3
"""Guards for a release tag (standard library only).

Checks, in order:
  1. the tag is vX.Y.Z or vX.Y.Z-beta.N
  2. the tag version equals the version in manifest.json
  3. the tag commit is on the main branch
  4. for a final release, CHANGELOG.md has a section for the version

With --notes-out the release notes are written to that file.
Exit status 0 means every guard passed, 1 means the tag is refused.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

import release_tools as rt


def check_on_main(sha: str, main_branch: str, main_ref: str, fetch: bool) -> None:
    if fetch:
        fetched = rt.git(
            "fetch",
            "-q",
            "origin",
            f"+refs/heads/{main_branch}:refs/remotes/{main_ref}",
            check=False,
        )
        if fetched.returncode != 0:
            raise rt.Refusal(f"cannot fetch {main_branch} from origin: {fetched.stderr.strip()}")
    known = rt.git("rev-parse", "--verify", "--quiet", f"{main_ref}^{{commit}}", check=False)
    if known.returncode != 0:
        raise rt.Refusal(
            f"{main_ref} does not exist here, cannot tell if the tag is on {main_branch}"
        )
    result = rt.git("merge-base", "--is-ancestor", sha, main_ref, check=False)
    if result.returncode == 1:
        raise rt.Refusal(f"the tag points at a commit that is not on {main_branch}")
    if result.returncode != 0:
        raise rt.Refusal(f"git merge-base failed: {result.stderr.strip()}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--tag", default=os.environ.get("CI_COMMIT_TAG"), help="default: $CI_COMMIT_TAG")
    p.add_argument("--sha", default=os.environ.get("CI_COMMIT_SHA"), help="default: $CI_COMMIT_SHA")
    p.add_argument("--main-branch", default=os.environ.get("CI_DEFAULT_BRANCH", "main"))
    p.add_argument("--main-ref", help="ref to test against (default: origin/<main branch>)")
    p.add_argument("--no-fetch", action="store_true", help="do not fetch the main branch first")
    p.add_argument("--skip-ancestry", action="store_true", help="skip guard 3 (local tests only)")
    p.add_argument("--manifest", type=Path, default=rt.MANIFEST)
    p.add_argument("--changelog", type=Path, default=rt.CHANGELOG)
    p.add_argument("--notes-out", type=Path, help="write the release notes here")
    args = p.parse_args()

    if not args.tag:
        print("REFUSING: no tag given (set --tag or run in a tag pipeline)")
        return 1
    main_ref = args.main_ref or f"origin/{args.main_branch}"
    try:
        print("--- guard: tag shape")
        version, prerelease = rt.parse_tag(args.tag)
        print(f"version {version}, pre-release: {int(prerelease)}")

        print("--- guard: tag matches manifest.json")
        manifest = rt.manifest_version(args.manifest)
        if manifest != version:
            raise rt.Refusal(f"manifest.json says {manifest}, the tag says {version}")

        if args.skip_ancestry:
            print(f"--- guard: tag commit is on {args.main_branch} (SKIPPED by --skip-ancestry)")
        else:
            print(f"--- guard: tag commit is on {args.main_branch}")
            sha = args.sha or rt.git("rev-parse", "HEAD").stdout.strip()
            check_on_main(sha, args.main_branch, main_ref, fetch=not args.no_fetch)

        if prerelease:
            print("--- CHANGELOG.md: pre-release, using the Unreleased section")
        else:
            print(f"--- guard: CHANGELOG.md has a section for {version}")
        notes = rt.release_notes(version, prerelease, args.changelog)
    except (rt.Refusal, subprocess.SubprocessError) as err:
        print(f"REFUSING: {err}")
        return 1

    if args.notes_out:
        args.notes_out.write_text(notes + "\n", encoding="utf-8")
        print(f"release notes written to {args.notes_out} ({len(notes.splitlines())} lines)")
    print(f"tag {args.tag} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
