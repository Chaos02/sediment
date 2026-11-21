"""The import file: its header, its content hash and the path rule."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
from typing import Any

from .const import (
    IMPORT_DIR,
)

_LOGGER = logging.getLogger(__name__)


FORMAT = "sediment-import"


FORMAT_VERSION = 1


HASHED_META = ("source_schema", "profile_signature", "profile_mode", "profile_map_hash")


class ImportFileError(Exception):
    """The file is not a usable import database."""


# ---------------------------------------------------------------- the file
def open_import(path: str) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def content_hash(conn: sqlite3.Connection, extra: dict[str, Any]) -> str:
    """Hash of everything the import writes, as in the tool (the two must agree).

    Every column of every row, the attribute text, the entity classes and profiles, the
    per-day widths, and the manifest fields that decide how the file is treated.
    """
    h = hashlib.sha256()
    h.update((json.dumps(extra, sort_keys=True) + "\n").encode())

    def feed(rows: Any) -> None:
        for row in rows:
            h.update(("\x1f".join(repr(x) for x in row) + "\n").encode())

    feed(
        conn.execute(
            "SELECT s.row_id, s.entity_id, s.ts, s.state, s.last_changed_ts, s.last_reported_ts,"
            " s.old_row_id, s.origin_idx, a.shared_attrs FROM states s"
            " LEFT JOIN attributes a ON a.attr_id = s.attr_id"
            " ORDER BY s.entity_id, s.ts, s.row_id"
        )
    )
    h.update(b"--days\n")
    feed(conn.execute("SELECT day, profile, width FROM days ORDER BY day, profile"))
    h.update(b"--entities\n")
    feed(conn.execute("SELECT entity_id, class, profile FROM entities ORDER BY entity_id"))
    return h.hexdigest()


def read_header(path: str) -> dict[str, Any]:
    """Validate the file and return its manifest plus time span. Runs in an executor."""
    if not os.path.isfile(path):
        raise ImportFileError("the file does not exist")
    try:
        conn = open_import(path)
        try:
            meta = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
            if meta.get("format") != FORMAT or meta.get("version") != FORMAT_VERSION:
                raise ImportFileError(
                    "not a Sediment import file, or a version this release cannot read"
                )
            lo, hi, n = conn.execute("SELECT MIN(ts), MAX(ts), COUNT(*) FROM states").fetchone()
            extra = {k: meta.get(k) for k in HASHED_META}
            if meta.get("content_hash") != content_hash(conn, extra):
                raise ImportFileError("the content hash does not match, the file is damaged")
            days = [list(r) for r in conn.execute("SELECT day, profile, width FROM days")]
            profiles = dict(conn.execute("SELECT entity_id, profile FROM entities"))
        finally:
            conn.close()
    except sqlite3.Error as err:
        raise ImportFileError(f"not a readable import database ({err})") from err
    meta.update(
        {
            "first_ts": lo,
            "last_ts": hi,
            "rows_in_file": n,
            "days": days,
            "entity_profiles": profiles,
        }
    )
    return meta


def check_import_path(config_dir: str, is_allowed: Any, path: str) -> str | None:
    """Why this path may not be used, or None. Blocking: runs in an executor.

    Files belong in `<config>/sediment/`. Anything under `<config>/www` is refused because
    that folder is served to the network without a login.
    """
    real = os.path.realpath(path)
    www = os.path.realpath(os.path.join(config_dir, "www"))
    if real == www or real.startswith(www + os.sep):
        return (
            "files under <config>/www are served over the network without a login. "
            "Put the import file in <config>/sediment/ instead"
        )
    own = os.path.realpath(os.path.join(config_dir, IMPORT_DIR))
    if real == own or real.startswith(own + os.sep) or is_allowed(path):
        return None
    return (
        "this path is not allowed. Put the file in <config>/sediment/ "
        "(or in a folder listed in allowlist_external_dirs)"
    )
