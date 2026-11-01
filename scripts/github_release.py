#!/usr/bin/env python3
"""Create or update the GitHub Release for a tag (standard library only).

GitHub is a write-only shadow of the GitLab project. The GitLab push mirror
carries the tag; this script waits until it arrived, then creates the release
through the API (a pre-release for -beta.N tags) and attaches the zip.

It is safe to run again: an existing release is updated, an asset that is
already there with the same content is kept, a different one is replaced.

The token is read from GITHUB_RELEASE_TOKEN (fine-grained PAT, Contents read
and write, this one repository). It is never printed. With --dry-run the
script needs no token, makes no request and prints the calls it would make.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import release_tools as rt

API = "https://api.github.com"
DEFAULT_REPO = "Chaos02/sediment"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class Client:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self._token = token

    def call(
        self,
        method: str,
        url: str,
        payload: dict | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> dict | list | None:
        if not url.startswith("http"):
            url = self.base + url
        body = json.dumps(payload).encode() if payload is not None else data
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "sediment-release-job",
        }
        if body is not None:
            headers["Content-Type"] = content_type or "application/json"
        last: Exception | None = None
        for attempt in range(4):
            req = urllib.request.Request(url, data=body, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as err:
                text = err.read().decode("utf-8", "replace")[:500]
                if err.code < 500:
                    raise ApiError(err.code, text) from None
                last = ApiError(err.code, text)
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                last = err
            time.sleep(2 * (attempt + 1))
        if isinstance(last, ApiError):
            raise last
        raise ApiError(599, f"no answer from {urllib.parse.urlsplit(url).netloc}: {last}")


def tag_commit(client: Client, repo: str, tag: str) -> str | None:
    """Commit the mirror's tag points at, or None while the tag is missing."""
    try:
        ref = client.call("GET", f"/repos/{repo}/git/ref/tags/{tag}")
    except ApiError as err:
        if err.status == 404:
            return None
        raise
    obj = ref["object"]
    for _ in range(5):  # an annotated (signed) tag points at a tag object first
        if obj["type"] == "commit":
            return obj["sha"]
        if obj["type"] != "tag":
            raise ApiError(0, f"tag {tag} points at a {obj['type']}")
        obj = client.call("GET", f"/repos/{repo}/git/tags/{obj['sha']}")["object"]
    raise ApiError(0, f"tag {tag} is nested too deep")


def wait_for_tag(
    client: Client, repo: str, tag: str, sha: str, timeout: int, interval: int
) -> None:
    print(f"--- wait for the mirror to have {tag} (up to {timeout} s)")
    deadline = time.monotonic() + timeout
    while True:
        try:
            found = tag_commit(client, repo, tag)
        except ApiError as err:
            if err.status in (401, 403):
                raise rt.Refusal(
                    f"GitHub refused the token ({err}). Check GITHUB_RELEASE_TOKEN: "
                    f"Contents read and write on {repo}, not expired."
                ) from None
            if err.status < 500:
                raise
            print(f"GitHub does not answer properly ({err}), trying again")
            found = None
        if found is not None:
            if found != sha:
                raise rt.Refusal(
                    f"the mirror has {tag} at {found}, this pipeline built {sha}. "
                    "Refusing to attach a release to a different commit."
                )
            print(f"the mirror has {tag} at {sha}")
            return
        if time.monotonic() >= deadline:
            raise rt.Refusal(
                f"{repo} did not get the tag {tag} within {timeout} s. Check the push "
                "mirror in GitLab (Settings, Repository, Mirroring repositories), then "
                "run this job again."
            )
        print(f"not there yet, next check in {interval} s")
        time.sleep(interval)


def release_payload(tag: str, notes: str, prerelease: bool) -> dict:
    return {
        "tag_name": tag,
        "name": tag,
        "body": notes,
        "draft": False,
        "prerelease": prerelease,
        # A beta must never become the "latest" release that stable users follow.
        "make_latest": "false" if prerelease else "true",
    }


def upload_url(release: dict, name: str) -> str:
    return release["upload_url"].split("{", 1)[0] + "?name=" + urllib.parse.quote(name)


def ensure_release(client: Client, repo: str, payload: dict) -> dict:
    tag = payload["tag_name"]
    try:
        existing = client.call("GET", f"/repos/{repo}/releases/tags/{tag}")
    except ApiError as err:
        if err.status != 404:
            raise
        existing = None
    if existing is None:
        print(f"--- create the release {tag} (pre-release: {payload['prerelease']})")
        return client.call("POST", f"/repos/{repo}/releases", payload)
    print(f"--- the release {tag} exists (id {existing['id']}), updating it")
    return client.call("PATCH", f"/repos/{repo}/releases/{existing['id']}", payload)


def ensure_asset(client: Client, repo: str, release: dict, path: Path) -> None:
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    print(f"--- asset {path.name}: {len(data)} bytes, sha256 {digest}")
    for asset in release.get("assets") or []:
        if asset["name"] != path.name:
            continue
        same_size = asset.get("size") == len(data)
        remote = asset.get("digest")  # "sha256:<hex>" on current GitHub, may be absent
        same_digest = remote is None or remote == f"sha256:{digest}"
        if same_size and same_digest and asset.get("state", "uploaded") == "uploaded":
            print("already attached with the same content, keeping it")
            return
        print("attached with other content or unfinished, replacing it")
        client.call("DELETE", f"/repos/{repo}/releases/assets/{asset['id']}")
    client.call("POST", upload_url(release, path.name), data=data, content_type="application/zip")
    print("uploaded")


def print_dry_run(args: argparse.Namespace, payload: dict, zip_path: Path) -> None:
    repo, tag, base = args.repo, payload["tag_name"], args.api_url
    size = zip_path.stat().st_size if zip_path.exists() else "not built yet"
    shown = {**payload, "body": f"<{len(payload['body'].splitlines())} lines of notes>"}
    print("DRY RUN: no token is read and no request is made. The calls would be:")
    print(f"  GET    {base}/repos/{repo}/git/ref/tags/{tag}")
    print(f"           again every {args.interval} s until it exists, at most {args.timeout} s")
    print("           (an annotated tag adds GET .../git/tags/<sha>)")
    print(f"           the tag must point at {args.sha or '$CI_COMMIT_SHA'}")
    print(f"  GET    {base}/repos/{repo}/releases/tags/{tag}")
    print(f"           404: POST {base}/repos/{repo}/releases")
    print(f"           else: PATCH {base}/repos/{repo}/releases/<id>")
    print(f"           payload {json.dumps(shown)}")
    print(
        f"  POST   <upload_url of the release>?name={zip_path.name}  ({size} bytes, application/zip)"
    )
    print("           an asset of that name is kept when size and sha256 match,")
    print(f"           else DELETE {base}/repos/{repo}/releases/assets/<id> first")
    print("--- release notes:")
    print(payload["body"])


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--tag", default=os.environ.get("CI_COMMIT_TAG"), help="default: $CI_COMMIT_TAG")
    p.add_argument("--sha", default=os.environ.get("CI_COMMIT_SHA"), help="default: $CI_COMMIT_SHA")
    p.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name (default {DEFAULT_REPO})")
    p.add_argument("--zip", type=Path, default=rt.ROOT / "dist" / rt.ZIP_NAME, dest="zip_path")
    p.add_argument("--notes-file", type=Path, help="release notes (default: from CHANGELOG.md)")
    p.add_argument(
        "--canonical-url",
        default=os.environ.get("CI_PROJECT_URL"),
        help="GitLab project URL, named in the release text (default: $CI_PROJECT_URL)",
    )
    p.add_argument("--timeout", type=int, default=1200, help="seconds to wait for the mirror")
    p.add_argument("--interval", type=int, default=15, help="seconds between checks")
    p.add_argument("--api-url", default=API, help=argparse.SUPPRESS)  # for tests
    p.add_argument("--dry-run", action="store_true", help="print the API calls, make none")
    args = p.parse_args()

    if not args.tag:
        print("REFUSING: no tag given (set --tag or run in a tag pipeline)")
        return 1
    try:
        version, prerelease = rt.parse_tag(args.tag)
        if args.notes_file:
            notes = args.notes_file.read_text(encoding="utf-8").strip()
        else:
            notes = rt.release_notes(version, prerelease)
        if args.canonical_url:
            notes += (
                "\n\n---\nThis release is a copy. The project lives on GitLab: "
                f"{args.canonical_url}/-/releases/{args.tag}"
            )
        payload = release_payload(args.tag, notes, prerelease)

        if args.dry_run:
            print_dry_run(args, payload, args.zip_path)
            return 0

        token = os.environ.get("GITHUB_RELEASE_TOKEN")
        if not token:
            raise rt.Refusal(
                "GITHUB_RELEASE_TOKEN is not set. It is a protected, masked variable "
                "with the environment scope 'release', so only a pipeline for a "
                "protected tag sees it. See docs/releasing.md."
            )
        if not args.sha:
            raise rt.Refusal("no commit given (set --sha or $CI_COMMIT_SHA)")
        if not args.zip_path.is_file():
            raise rt.Refusal(
                f"{args.zip_path} does not exist, build it first (scripts/build_zip.py)"
            )

        client = Client(args.api_url, token)
        wait_for_tag(client, args.repo, args.tag, args.sha, args.timeout, args.interval)
        release = ensure_release(client, args.repo, payload)
        ensure_asset(client, args.repo, release, args.zip_path)
        print(f"released {args.tag}: {release.get('html_url', '')}")
    except rt.Refusal as err:
        print(f"REFUSING: {err}")
        return 1
    except ApiError as err:
        print(f"GitHub API error: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
