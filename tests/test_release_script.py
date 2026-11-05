"""scripts/github_release.py against a fake GitHub, with and without immutable releases."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import github_release  # noqa: E402

REPO = "Chaos02/sediment"
TAG = "v0.1.0-beta.3"
SHA = "a" * 40
ZIP = b"PK-the-zip"


class FakeGitHub:
    """Just enough of the API. Drafts hide from the tag endpoint, as on GitHub."""

    def __init__(self, immutable_setting: bool) -> None:
        self.immutable_setting = immutable_setting
        self.releases: list[dict] = []
        self.log: list[tuple[str, str]] = []
        self.next_id = 1
        self.base = ""

    def mutations(self) -> list[tuple[str, str]]:
        return [entry for entry in self.log if entry[0] != "GET"]

    def add_release(self, *, draft: bool, immutable: bool, assets: list[bytes]) -> dict:
        rel = {
            "id": self.next_id,
            "tag_name": TAG,
            "draft": draft,
            "prerelease": True,
            "immutable": immutable,
            "assets": [],
        }
        self.next_id += 1
        self.releases.append(rel)
        for data in assets:
            self.add_asset(rel, data)
        return rel

    def add_asset(self, rel: dict, data: bytes) -> None:
        rel["assets"].append(
            {
                "id": 100 * rel["id"] + len(rel["assets"]),
                "name": "sediment.zip",
                "size": len(data),
                "state": "uploaded",
            }
        )

    def view(self, rel: dict) -> dict:
        return {
            **rel,
            "html_url": f"https://example.invalid/{rel['id']}",
            "upload_url": f"{self.base}/upload/{rel['id']}{{?name,label}}",
        }

    def find(self, rel_id: int) -> dict | None:
        return next((r for r in self.releases if r["id"] == rel_id), None)


def make_handler(fake: FakeGitHub):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def reply(self, status: int, body=None) -> None:
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def handle_any(self, method: str) -> None:
            parts = urlsplit(self.path)
            path = parts.path
            fake.log.append((method, path))
            raw = self.body()
            locked = lambda rel: rel is not None and not rel["draft"] and rel["immutable"]  # noqa: E731
            if method == "GET" and path == f"/repos/{REPO}/git/ref/tags/{TAG}":
                return self.reply(200, {"object": {"type": "commit", "sha": SHA}})
            if method == "GET" and path == f"/repos/{REPO}/releases/tags/{TAG}":
                for rel in fake.releases:
                    if not rel["draft"]:
                        return self.reply(200, fake.view(rel))
                return self.reply(404, {"message": "Not Found"})
            if method == "GET" and path == f"/repos/{REPO}/releases":
                return self.reply(200, [fake.view(r) for r in fake.releases])
            if method == "POST" and path == f"/repos/{REPO}/releases":
                payload = json.loads(raw)
                rel = fake.add_release(draft=payload["draft"], immutable=False, assets=[])
                if not rel["draft"]:
                    rel["immutable"] = fake.immutable_setting
                return self.reply(201, fake.view(rel))
            if method == "PATCH" and path.startswith(f"/repos/{REPO}/releases/"):
                rel = fake.find(int(path.rsplit("/", 1)[1]))
                if rel is None or locked(rel):
                    return self.reply(422, {"message": "release is immutable"})
                payload = json.loads(raw)
                if rel["draft"] and not payload["draft"]:
                    rel["immutable"] = fake.immutable_setting
                rel["draft"] = payload["draft"]
                return self.reply(200, fake.view(rel))
            if method == "POST" and path.startswith("/upload/"):
                rel = fake.find(int(path.rsplit("/", 1)[1]))
                assert parse_qs(parts.query)["name"] == ["sediment.zip"]
                if rel is None or locked(rel):
                    return self.reply(422, {"message": "release is immutable"})
                fake.add_asset(rel, raw)
                return self.reply(201, {"name": "sediment.zip"})
            if method == "DELETE" and "/releases/assets/" in path:
                asset_id = int(path.rsplit("/", 1)[1])
                for rel in fake.releases:
                    if any(a["id"] == asset_id for a in rel["assets"]):
                        if locked(rel):
                            return self.reply(422, {"message": "release is immutable"})
                        rel["assets"] = [a for a in rel["assets"] if a["id"] != asset_id]
                        return self.reply(204)
                return self.reply(404, {"message": "Not Found"})
            return self.reply(404, {"message": f"unexpected {method} {path}"})

        def do_GET(self) -> None:
            self.handle_any("GET")

        def do_POST(self) -> None:
            self.handle_any("POST")

        def do_PATCH(self) -> None:
            self.handle_any("PATCH")

        def do_DELETE(self) -> None:
            self.handle_any("DELETE")

    return Handler


@pytest.fixture
def serve(socket_enabled):
    started: list[tuple[ThreadingHTTPServer, threading.Thread]] = []

    def start(immutable_setting: bool) -> FakeGitHub:
        fake = FakeGitHub(immutable_setting)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fake))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        fake.base = f"http://127.0.0.1:{server.server_address[1]}"
        return fake

    yield start
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run(fake: FakeGitHub, tmp_path: Path, monkeypatch, data: bytes = ZIP) -> int:
    zip_path = tmp_path / "sediment.zip"
    zip_path.write_bytes(data)
    notes = tmp_path / "notes.md"
    notes.write_text("Notes.", encoding="utf-8")
    monkeypatch.setenv("GITHUB_RELEASE_TOKEN", "t")
    monkeypatch.delenv("CI_PROJECT_URL", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "github_release.py", "--tag", TAG, "--sha", SHA, "--repo", REPO,
            "--zip", str(zip_path), "--notes-file", str(notes), "--api-url", fake.base,
            "--timeout", "5", "--interval", "1",
        ],
    )  # fmt: skip
    return github_release.main()


def test_new_release_is_a_draft_until_the_zip_is_attached(serve, tmp_path, monkeypatch) -> None:
    fake = serve(immutable_setting=True)
    assert run(fake, tmp_path, monkeypatch) == 0
    (rel,) = fake.releases
    assert not rel["draft"] and rel["immutable"]
    assert [a["name"] for a in rel["assets"]] == ["sediment.zip"]
    # create, then upload, then publish: the only order an immutable release allows
    assert [m for m, _ in fake.mutations()] == ["POST", "POST", "PATCH"]
    assert fake.mutations()[1][1].startswith("/upload/")


def test_rerun_after_success_changes_nothing(serve, tmp_path, monkeypatch) -> None:
    fake = serve(immutable_setting=True)
    assert run(fake, tmp_path, monkeypatch) == 0
    before = len(fake.mutations())
    assert run(fake, tmp_path, monkeypatch) == 0
    assert len(fake.mutations()) == before


def test_draft_left_by_an_earlier_run_is_finished(serve, tmp_path, monkeypatch) -> None:
    fake = serve(immutable_setting=True)
    fake.add_release(draft=True, immutable=False, assets=[ZIP])
    assert run(fake, tmp_path, monkeypatch) == 0
    (rel,) = fake.releases  # found through the list, not duplicated
    assert not rel["draft"] and len(rel["assets"]) == 1
    assert [m for m, _ in fake.mutations()] == ["PATCH", "PATCH"]  # no second upload


def test_immutable_release_with_another_zip_is_refused(
    serve, tmp_path, monkeypatch, capsys
) -> None:
    fake = serve(immutable_setting=True)
    fake.add_release(draft=False, immutable=True, assets=[b"another"])
    assert run(fake, tmp_path, monkeypatch) == 1
    assert "immutable" in capsys.readouterr().out
    assert not fake.mutations()


def test_published_release_without_immutability_is_still_updated(
    serve, tmp_path, monkeypatch
) -> None:
    fake = serve(immutable_setting=False)
    fake.add_release(draft=False, immutable=False, assets=[b"old"])
    assert run(fake, tmp_path, monkeypatch) == 0
    (rel,) = fake.releases
    assert len(rel["assets"]) == 1 and rel["assets"][0]["size"] == len(ZIP)
