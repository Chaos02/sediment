#!/usr/bin/env python3
"""Publish the zip and create the GitLab release for a tag (standard library only).

Runs in the tag pipeline and authenticates with the CI job token.

1. Uploads the zip to the project's generic package registry
   (sediment/<version>/sediment.zip). A file already published with the same
   content is skipped; one with DIFFERENT content stops the job, because a
   version must never change after it was published.
2. Creates the GitLab release with the notes and a link to that file. An
   existing release is left as it is.

With --dry-run nothing is requested and the calls are printed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import release_tools as rt


def request(
    method: str, url: str, token: str, body: bytes | None = None, content_type: str | None = None
) -> tuple[int, bytes]:
    headers = {"JOB-TOKEN": token, "User-Agent": "sediment-release-job"}
    if content_type:
        headers["Content-Type"] = content_type
    last: Exception | None = None
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            data = err.read()
            if err.code < 500:
                return err.code, data
            last = RuntimeError(f"HTTP {err.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            last = err
        time.sleep(2 * (attempt + 1))
    raise rt.Refusal(f"{method} {url} failed: {last}")


def description(notes: str, version: str, prerelease: bool, project_url: str) -> str:
    head = ""
    if prerelease:
        head = (
            "**Pre-release.** Turn on pre-releases for Sediment in HACS to receive it. "
            "Back up your database before the first real run.\n\n"
        )
    return (
        f"{head}{notes}\n\n### Install\n\n"
        "With HACS (Marketplace in Home Assistant 2026.11 and later): add "
        "`Chaos02/sediment` as a custom repository and download it.\n\n"
        f"By hand: unpack `sediment.zip` into `<config>/custom_components/sediment`. "
        f"More in [README]({project_url}/-/blob/v{version}/README.md)."
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--tag", default=os.environ.get("CI_COMMIT_TAG"), help="default: $CI_COMMIT_TAG")
    p.add_argument("--zip", type=Path, default=rt.ROOT / "dist" / rt.ZIP_NAME, dest="zip_path")
    p.add_argument("--notes-file", type=Path, help="release notes (default: from CHANGELOG.md)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.tag:
        print("REFUSING: no tag given (set --tag or run in a tag pipeline)")
        return 1
    api = os.environ.get("CI_API_V4_URL", "https://gitlab.com/api/v4")
    project_id = os.environ.get("CI_PROJECT_ID", "<project id>")
    project_url = os.environ.get("CI_PROJECT_URL", "https://gitlab.com/Chaos02-oss/sediment")
    try:
        version, prerelease = rt.parse_tag(args.tag)
        notes = (
            args.notes_file.read_text(encoding="utf-8").strip()
            if args.notes_file
            else rt.release_notes(version, prerelease)
        )
        name = args.zip_path.name
        base = f"{api}/projects/{project_id}/packages/generic/sediment/{version}"
        file_url = f"{base}/{name}"
        release = {
            "name": args.tag,
            "tag_name": args.tag,
            "description": description(notes, version, prerelease, project_url),
            "assets": {
                "links": [
                    {
                        "name": name,
                        "url": file_url,
                        "direct_asset_path": f"/{name}",
                        "link_type": "package",
                    }
                ]
            },
        }
        releases_url = f"{api}/projects/{project_id}/releases"

        if args.dry_run:
            size = args.zip_path.stat().st_size if args.zip_path.exists() else "not built yet"
            print("DRY RUN: no request is made. The calls would be:")
            print(f"  GET    {file_url}")
            print(f"           404: PUT the file ({size} bytes)")
            print("           200 with the same content: skip   200 with other content: refuse")
            print(f"  GET    {releases_url}/{urllib.parse.quote(args.tag)}")
            print(f"           404: POST {releases_url}   200: leave it")
            print(f"           payload {json.dumps({**release, 'description': '<see below>'})}")
            print("--- description:")
            print(release["description"])
            return 0

        token = os.environ.get("CI_JOB_TOKEN")
        if not token:
            raise rt.Refusal("CI_JOB_TOKEN is not set (this runs in a GitLab job)")
        if not args.zip_path.is_file():
            raise rt.Refusal(
                f"{args.zip_path} does not exist, build it first (scripts/build_zip.py)"
            )
        data = args.zip_path.read_bytes()

        print(f"--- upload to the generic package registry: {file_url}")
        status, published = request("GET", file_url, token)
        if status == 404:
            status, body = request("PUT", file_url, token, data, "application/octet-stream")
            if status not in (200, 201):
                raise rt.Refusal(f"upload failed, HTTP {status}: {body[:300]!r}")
            print("uploaded")
        elif status == 200:
            if published != data:
                raise rt.Refusal(
                    f"{name} is already published for {version} with different content. "
                    "A published version never changes: release the next one."
                )
            print("already published, same content")
        else:
            raise rt.Refusal(f"HTTP {status} when checking {file_url}")

        print("--- create the release")
        status, _ = request("GET", f"{releases_url}/{urllib.parse.quote(args.tag)}", token)
        if status == 200:
            print(f"the release {args.tag} exists already, leaving it as it is")
        elif status == 404:
            status, body = request(
                "POST", releases_url, token, json.dumps(release).encode(), "application/json"
            )
            if status not in (200, 201):
                raise rt.Refusal(f"creating the release failed, HTTP {status}: {body[:300]!r}")
            print("created")
        else:
            raise rt.Refusal(f"HTTP {status} when checking for the release {args.tag}")
        print(f"released {args.tag}: {project_url}/-/releases/{args.tag}")
    except rt.Refusal as err:
        print(f"REFUSING: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
