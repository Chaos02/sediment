#!/usr/bin/env python3
"""Merge recorder database snapshots into one thinned import database.

Offline tool for `sediment.import_history`. It needs Python 3.14 or newer (the
integration's code uses its syntax) and nothing else: no Home Assistant, no third party packages. It uses the integration's own codec and
profile code, so the import arrives thinned exactly as the integration would have thinned
it, to the tier each day's age calls for.

    python tools/merge_history.py OUT.db SNAPSHOT1.db SNAPSHOT2.db ... --now 2026-11-01

Every snapshot is opened read-only with `immutable=1` and must be recorder schema 53. A
snapshot taken from a running recorder may have a `-wal` file next to it; `immutable`
ignores that file, so checkpoint the snapshot first or copy it to a place where the
recorder is stopped.

What it does:

* merges the `states` of all snapshots per entity, dropping rows that appear in two
  overlapping snapshots (same entity id and `last_updated_ts`) and preferring the copy
  whose `old_state_id` still points at a row of its own snapshot;
* keeps gaps as gaps: the periods the snapshots cover are written to the manifest, the
  stretches between them are listed as gaps, and no row is invented;
* thins every entity-day with the integration's codec and the tier its age calls for,
  counted from `--now`; days beyond the history length are dropped;
* writes the import database and `<OUT>.manifest.json`.

Events, statistics and context ids are not imported. Statistics are already in the live
database and the events of an old period are not worth their size.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib
from itertools import pairwise
import json
import os
from pathlib import Path
import sqlite3
import sys
import types
from typing import Any

FORMAT = "sediment-import"
FORMAT_VERSION = 1
SUPPORTED_SCHEMA = 53
DAY = 86400.0
PERIOD_TOLERANCE = 3600.0  # snapshots this close (or overlapping) form one period
MIN_PYTHON = (3, 14)


def load_core() -> tuple[Any, Any]:
    """Load codec.py and profiles.py without importing the Home Assistant package."""
    root = Path(__file__).resolve().parent.parent
    pkg = types.ModuleType("_sediment_core")
    pkg.__path__ = [str(root / "custom_components" / "sediment")]
    sys.modules["_sediment_core"] = pkg
    codec = importlib.import_module("_sediment_core.codec")
    profiles = importlib.import_module("_sediment_core.profiles")
    return codec, profiles


def parse_now(text: str) -> float:
    """`YYYY-MM-DD` (midnight UTC) or an ISO date time (UTC unless it has an offset)."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # noqa: UP017
    return dt.timestamp()


HASHED_META = ("source_schema", "profile_signature", "profile_mode", "profile_map_hash")


def content_hash(conn: sqlite3.Connection, extra: dict[str, Any]) -> str:
    """Hash of everything the import writes: the importer's identity key and its seal.

    Every column of every row, the attribute text, the entity classes and profiles, the
    per-day widths and the manifest fields that decide how the file is treated. The importer
    has the same function and refuses a file whose hash differs.
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


# ---------------------------------------------------------------- sources
class Source:
    """One recorder snapshot, read only."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.name = os.path.basename(path)
        uri = Path(path).resolve().as_uri() + "?mode=ro&immutable=1"
        self.conn = sqlite3.connect(uri, uri=True)
        wal = path + "-wal"
        self.wal_ignored = os.path.exists(wal) and os.path.getsize(wal) > 0
        row = self.conn.execute(
            "SELECT schema_version FROM schema_changes ORDER BY change_id DESC LIMIT 1"
        ).fetchone()
        self.schema = row[0] if row else None
        if self.schema != SUPPORTED_SCHEMA:
            raise SystemExit(
                f"{self.name}: recorder schema {self.schema}, only {SUPPORTED_SCHEMA} is supported"
            )
        self.mids = {
            eid: mid
            for mid, eid in self.conn.execute("SELECT metadata_id, entity_id FROM states_meta")
        }
        lo, hi, n = self.conn.execute(
            "SELECT MIN(last_updated_ts), MAX(last_updated_ts), COUNT(*) FROM states"
        ).fetchone()
        self.first, self.last, self.rows = lo, hi, n
        self._attrs: dict[int, str | None] = {}

    def entity_rows(self, entity_id: str) -> list[tuple]:
        """(ts, state, last_changed_ts, last_reported_ts, chain_valid, attr_json, origin)."""
        mid = self.mids.get(entity_id)
        if mid is None:
            return []
        out = []
        seen: set[int] = set()
        cur = self.conn.execute(
            "SELECT state_id, state, last_updated_ts, last_changed_ts, last_reported_ts,"
            " old_state_id, attributes_id, origin_idx FROM states WHERE metadata_id = ?"
            " AND last_updated_ts IS NOT NULL ORDER BY last_updated_ts, state_id",
            (mid,),
        )
        for sid, state, ts, changed, reported, old, aid, origin in cur:
            valid = old is not None and old in seen
            seen.add(sid)
            out.append((ts, state, changed, reported, valid, self.attrs(aid), origin))
        return out

    def attrs(self, attr_id: int | None) -> str | None:
        if attr_id is None:
            return None
        if len(self._attrs) > 50_000:
            self._attrs.clear()
        if attr_id not in self._attrs:
            row = self.conn.execute(
                "SELECT shared_attrs FROM state_attributes WHERE attributes_id = ?", (attr_id,)
            ).fetchone()
            self._attrs[attr_id] = row[0] if row else None
        return self._attrs[attr_id]


def build_periods(sources: list[Source]) -> list[dict]:
    """Merge the time spans the snapshots cover; what lies between them is a gap."""
    spans = sorted(
        (s.first, s.last, s.name) for s in sources if s.first is not None and s.last is not None
    )
    periods: list[dict] = []
    for first, last, name in spans:
        if periods and first <= periods[-1]["end"] + PERIOD_TOLERANCE:
            periods[-1]["end"] = max(periods[-1]["end"], last)
            periods[-1]["sources"].append(name)
        else:
            periods.append({"start": first, "end": last, "sources": [name]})
    return periods


# ---------------------------------------------------------------- merging one entity
def merge_entity(sources: list[Source], entity_id: str, stats: dict) -> list[tuple]:
    """Rows of all snapshots, one per `last_updated_ts`, sorted by time."""
    best: dict[float, tuple] = {}
    for src in sources:
        for row in src.entity_rows(entity_id):
            ts = row[0]
            have = best.get(ts)
            if have is None:
                best[ts] = row
                continue
            stats["duplicates"] += 1
            if row[4] and not have[4]:  # prefer the copy with a valid old_state_id chain
                best[ts] = row
                stats["chain_preferred"] += 1
    return [best[t] for t in sorted(best)]


# ---------------------------------------------------------------- thinning
def component_kind(codec: Any, raw: list) -> str:
    """continuous, or enum (also for few-level numeric components), for an attribute."""
    nn = [x for x in raw if x is not None]
    num = [codec.to_float(x) if not isinstance(x, (str, bool)) else None for x in nn]
    isnum = sum(1 for x in num if x is not None)
    if not nn or isnum < 0.9 * len(nn) or isnum == 0:
        return "enum"
    levels = {x for x in num if x is not None}
    if len(levels) <= codec.FEWLEVEL_MAX_LEVELS and codec.levels_far_apart(
        [str(x) for x in nn], levels
    ):
        return "enum"
    return "continuous"


class Thinner:
    """Thin merged rows with the integration's codec, day by day, like the nightly job."""

    def __init__(self, codec: Any, profiles: Any, args: argparse.Namespace, now: float) -> None:
        self.codec, self.profiles, self.now = codec, profiles, now
        self.raw_days = float(args.raw_days)
        self.history_days = float(args.history_days)
        self.flap_default = bool(args.flapping)
        text = Path(args.rules).read_text(encoding="utf-8") if args.rules else ""
        self.rules_text = text
        self.rules, errors = profiles.parse_rules(text)
        if errors:
            raise SystemExit("rules: " + "; ".join(errors))
        custom = json.loads(args.custom_tiers) if args.custom_tiers else None
        self.resolution, self.custom = args.resolution, custom
        self.coarse_horizon = float(args.coarse_horizon)
        self.schedules = profiles.build_schedules(
            args.resolution, custom, self.raw_days, self.history_days, self.coarse_horizon
        )
        self.signature = profiles.profile_signature(
            text, args.resolution, custom, self.flap_default, int(self.coarse_horizon)
        )
        self.days: dict[tuple[int, str], float] = {}
        # a profile map written by `sediment.export_profile_map` knows what this tool cannot
        self.profile_mode = "rules"
        self.map_hash: str | None = None
        self.assignments: dict[str, tuple[str, bool | None]] = {}
        self.undecidable = 0
        if getattr(args, "profile_map", None):
            self._load_map(Path(args.profile_map))

    def _load_map(self, path: Path) -> None:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("format") != "sediment-profile-map" or doc.get("version") != 1:
            raise SystemExit("--profile-map: not a Sediment profile map")
        assignments = {
            eid: (v["profile"], v.get("flap")) for eid, v in doc.get("entities", {}).items()
        }
        if self.profiles.profile_map_hash(doc["signature"], assignments) != doc.get("map_hash"):
            raise SystemExit("--profile-map: the map hash does not match, the file was changed")
        self.profile_mode = "map"
        self.assignments = assignments
        self.map_hash = doc["map_hash"]
        self.signature = doc["signature"]

    def entity_profile(self, entity_id: str, attrs: dict) -> tuple[str, bool | None]:
        """Profile and flapping rule. Without a map, what the rules need but the snapshot
        cannot show (an `integration:` rule, a device class that is not in the attributes)
        makes the entity `keep`: thinning it wrongly would lose data."""
        if entity_id in self.assignments:
            return self.assignments[entity_id]
        meta = self.profiles.EntityMeta(
            device_class=attrs.get("device_class"), state_class=attrs.get("state_class")
        )
        for rule in self.rules:
            if rule.kind in ("entity", "domain") or (
                rule.kind == "device_class" and meta.device_class is not None
            ):
                if self.profiles.rule_hits(rule, entity_id, meta):
                    return rule.profile, rule.flap
            else:  # integration, or a device class the snapshot does not show
                self.undecidable += 1
                return "keep", None
        return "default", None

    def thin(
        self, entity_id: str, rows: list[tuple], periods: list[dict]
    ) -> tuple[list[tuple[int, int]], str, str]:
        """Return ([(row index, period index)] to keep, class, profile)."""
        codec = self.codec
        attrs_json = next((r[5] for r in reversed(rows) if r[5]), None)
        try:
            last_attrs = json.loads(attrs_json) if attrs_json else {}
        except ValueError:
            last_attrs = {}
        profile, flap = self.entity_profile(entity_id, last_attrs)
        flap = self.flap_default if flap is None else flap
        t = [r[0] for r in rows]
        s = ["None" if r[1] is None else r[1] for r in rows]
        domain = entity_id.split(".", 1)[0]
        cls = codec.classify(domain, s, last_attrs.get("state_class"))
        starts = [p["start"] for p in periods]

        def period_of(ts: float) -> int:
            return max(0, bisect_right(starts, ts) - 1)

        if profile == "drop":
            return [], cls, profile
        v = codec.numeric_view(s) if cls in ("continuous", "counter") else None
        params = codec.series_params(v) if cls == "continuous" else {"q": 0, "sigma": 0, "R": 0}
        comps = comp_params = None
        if cls == "attribute":
            comps, comp_params = self._attribute_components(domain, rows, s)
        sched = self.schedules.get(profile)
        kept: list[tuple[int, int]] = []
        # rows grouped by period, then by UTC day
        i = 0
        n = len(rows)
        while i < n:
            pi = period_of(t[i])
            pend = i
            while pend < n and period_of(t[pend]) == pi:
                pend += 1
            day_start = i
            while day_start < pend:
                day = int(t[day_start] // DAY)
                day_end = day_start
                while day_end < pend and int(t[day_end] // DAY) == day:
                    day_end += 1
                age = (self.now - (day + 1) * DAY) / DAY
                if profile == "keep" or sched is None:
                    k = -1 if age < self.history_days else None
                else:
                    k = sched.target_index(age)
                if k is None:
                    pass  # beyond the history length
                elif k < 0:
                    kept.extend((j, pi) for j in range(day_start, day_end))
                else:
                    width = sched.tiers[k].cfg.bucket
                    key = (day, profile)
                    self.days[key] = max(self.days.get(key, 0.0), width)
                    cfgs = [sched.tiers[x].cfg for x in range(k + 1)]
                    pos = self._chain(
                        cls,
                        t,
                        v,
                        s,
                        params,
                        comps,
                        comp_params,
                        cfgs,
                        flap,
                        i,
                        pend,
                        day_start,
                        day_end,
                    )
                    kept.extend((j, pi) for j in pos)
                day_start = day_end
            i = pend
        return kept, cls, profile

    def _chain(
        self, cls, t, v, s, params, comps, comp_params, cfgs, flap, pstart, pend, a, b
    ) -> list[int]:
        """Row indices (in the merged list) kept for the day rows [a, b)."""
        codec = self.codec
        lo = a - 1 if a > pstart else a
        hi = b + 1 if b < pend else b
        seq_t = t[lo:hi]
        off = a - lo
        n_day = b - a
        tend = t[b] if b < pend else t[pend - 1]
        # the chain always starts at tier 1, so relative and absolute tier index agree and
        # flapping collapse applies from the second tier on, as in the nightly job
        flap_from = 1 if flap else None
        if cls == "attribute":
            sub = [
                (name, kind, None if num is None else num[lo:hi], st[lo:hi])
                for name, kind, num, st in comps
            ]
            out = codec.thin_attribute_chain(
                seq_t, s[lo:hi], sub, comp_params, cfgs, tend, flap_from=flap_from
            )
            final = set(out[-1])
        else:
            chain = codec.run_chain(
                cls,
                seq_t,
                None if v is None else v[lo:hi],
                s[lo:hi],
                params,
                cfgs,
                tend,
                flap_from=flap_from,
            )
            final = set(chain[-1][0])
        if a > pstart:
            final.add(0)
        if b < pend:
            final.add(len(seq_t) - 1)
        return [lo + p for p in range(off, off + n_day) if p in final]

    def _attribute_components(self, domain: str, rows: list[tuple], s: list[str]):
        codec = self.codec
        attrs = []
        for r in rows:
            try:
                attrs.append(json.loads(r[5]) if r[5] else {})
            except ValueError:
                attrs.append({})
        comps: list[tuple] = [("state", "enum", None, s)]
        params: list[dict | None] = [None]
        for name in codec.ATTR_DRIVEN[domain]:
            raw = [
                None if (st in codec.GAP_STATES) else a.get(name)
                for st, a in zip(s, attrs, strict=True)
            ]
            kind = component_kind(codec, raw)
            strs = [str(x) for x in raw]
            if kind == "continuous":
                num = [codec.to_float(x) if not isinstance(x, (str, bool)) else None for x in raw]
                comps.append((name, kind, num, strs))
                params.append(codec.series_params(num))
            else:
                comps.append((name, "enum", None, strs))
                params.append(None)
        return comps, params


# ---------------------------------------------------------------- output
SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE entities (entity_id TEXT PRIMARY KEY, class TEXT, profile TEXT);
CREATE TABLE attributes (attr_id INTEGER PRIMARY KEY, shared_attrs TEXT NOT NULL UNIQUE);
CREATE TABLE states (
    row_id INTEGER PRIMARY KEY,
    entity_id TEXT NOT NULL,
    period INTEGER NOT NULL,
    ts REAL NOT NULL,
    state TEXT,
    last_changed_ts REAL,
    last_reported_ts REAL,
    old_row_id INTEGER,
    attr_id INTEGER,
    origin_idx INTEGER
);
CREATE INDEX states_entity_ts ON states (entity_id, ts);
CREATE TABLE days (day INTEGER NOT NULL, profile TEXT NOT NULL, width REAL NOT NULL,
                   PRIMARY KEY (day, profile));
"""


def merge(out_path: str, source_paths: list[str], args: argparse.Namespace, log=print) -> dict:
    """Do the whole job. Returns the manifest."""
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(
            "Python 3.14 or newer is needed. Home Assistant's container image has it, see "
            "docs/importing.md"
        )
    codec, profiles = load_core()
    now = parse_now(args.now)
    sources = [Source(p) for p in source_paths]
    for src in sources:
        if src.wal_ignored:
            log(f"warning: {src.name} has a -wal file, which immutable=1 ignores")
        if src.first is None:
            log(f"warning: {src.name} has no states")
    periods = build_periods(sources)
    thinner = Thinner(codec, profiles, args, now)
    if os.path.exists(out_path):
        os.remove(out_path)
    out = sqlite3.connect(out_path)
    out.executescript(SCHEMA)
    attr_ids: dict[str, int] = {}
    stats = {"duplicates": 0, "chain_preferred": 0}
    raw_by_period = [0] * len(periods)
    kept_by_period = [0] * len(periods)
    by_class: dict[str, int] = defaultdict(int)
    by_profile: dict[str, int] = defaultdict(int)
    entity_ids = sorted({e for src in sources for e in src.mids})
    next_row = 1
    for n_ent, eid in enumerate(entity_ids, 1):
        rows = merge_entity(sources, eid, stats)
        if not rows:
            continue
        starts = [p["start"] for p in periods]
        for r in rows:
            raw_by_period[max(0, bisect_right(starts, r[0]) - 1)] += 1
        kept, cls, profile = thinner.thin(eid, rows, periods)
        out.execute("INSERT INTO entities VALUES (?, ?, ?)", (eid, cls, profile))
        by_class[cls] += len(kept)
        by_profile[profile] += len(kept)
        ids: dict[int, int] = {}  # row index -> row_id
        last_by_period: dict[int, int] = {}
        batch = []
        for j, pi in kept:
            ts, state, changed, reported, _valid, attrs_json, origin = rows[j]
            aid = None
            if attrs_json is not None:
                aid = attr_ids.get(attrs_json)
                if aid is None:
                    aid = attr_ids[attrs_json] = len(attr_ids) + 1
                    out.execute("INSERT INTO attributes VALUES (?, ?)", (aid, attrs_json))
            row_id = next_row
            next_row += 1
            ids[j] = row_id
            prev = last_by_period.get(pi)
            # HA's own NULL pointers stay NULL; otherwise point at the previous kept row
            chained = prev if (prev is not None and _source_chained(rows, j)) else None
            last_by_period[pi] = row_id
            batch.append((row_id, eid, pi, ts, state, changed, reported, chained, aid, origin))
            kept_by_period[pi] += 1
        out.executemany("INSERT INTO states VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
        if n_ent % 500 == 0:
            log(f"{n_ent}/{len(entity_ids)} entities")
    out.executemany(
        "INSERT INTO days VALUES (?, ?, ?)",
        [(d, p, w) for (d, p), w in sorted(thinner.days.items())],
    )
    out.commit()
    extra = {
        "source_schema": SUPPORTED_SCHEMA,
        "profile_signature": thinner.signature,
        "profile_mode": thinner.profile_mode,
        "profile_map_hash": thinner.map_hash,
    }
    digest = content_hash(out, extra)
    if thinner.undecidable:
        log(
            f"warning: {thinner.undecidable} entities depend on metadata this tool cannot see "
            "(an integration: rule or a device class that is not in the attributes). They are "
            "kept in full. Run the service sediment.export_profile_map and pass the file with "
            "--profile-map to thin them."
        )
    gaps = [
        {"start": a["end"], "end": b["start"], "seconds": b["start"] - a["end"]}
        for a, b in pairwise(periods)
    ]
    manifest = {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "source_schema": SUPPORTED_SCHEMA,
        "content_hash": digest,
        "now": now,
        "profile_signature": thinner.signature,
        "profile_mode": thinner.profile_mode,
        "profile_map_hash": thinner.map_hash,
        "undecidable_entities": thinner.undecidable,
        "history_days": thinner.history_days,
        "raw_days": thinner.raw_days,
        "resolution": args.resolution,
        "sources": [
            {"name": s.name, "first": s.first, "last": s.last, "rows": s.rows} for s in sources
        ],
        "periods": [
            {
                "start": p["start"],
                "end": p["end"],
                "sources": p["sources"],
                "rows_merged": raw_by_period[i],
                "rows_kept": kept_by_period[i],
            }
            for i, p in enumerate(periods)
        ],
        "gaps": gaps,
        "entities": len(entity_ids),
        "rows_kept": sum(kept_by_period),
        "rows_merged": sum(raw_by_period),
        "duplicates_dropped": stats["duplicates"],
        "chain_preferred": stats["chain_preferred"],
        "rows_by_class": dict(by_class),
        "rows_by_profile": dict(by_profile),
    }
    for key, value in manifest.items():
        out.execute("INSERT INTO meta VALUES (?, ?)", (key, json.dumps(value)))
    out.commit()
    out.close()
    manifest_path = out_path + ".manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def _source_chained(rows: list[tuple], j: int) -> bool:
    """HA stored a pointer for this row (not NULL), in the snapshot it was taken from."""
    return bool(rows[j][4])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("out", help="import database to write")
    ap.add_argument("snapshots", nargs="+", help="recorder databases (schema 53)")
    ap.add_argument(
        "--now", required=True, help="date the tiers are counted from, YYYY-MM-DD (UTC)"
    )
    ap.add_argument("--history-days", type=int, default=1830)
    ap.add_argument("--raw-days", type=int, default=10)
    ap.add_argument(
        "--resolution", default="standard", choices=["fine", "standard", "coarse", "custom"]
    )
    ap.add_argument(
        "--custom-tiers", help='JSON, e.g. [{"bucket_min": 15, "until_days": 100}, ...]'
    )
    ap.add_argument("--coarse-horizon", type=int, default=365)
    ap.add_argument("--rules", help="profile rules file, same syntax as in the options")
    ap.add_argument(
        "--profile-map",
        help="profile map from the service sediment.export_profile_map (replaces --rules for "
        "every entity in it and lets the importer mark the days as thinned)",
    )
    ap.add_argument(
        "--flapping",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="collapse flapping from tier 2 on (default on)",
    )
    args = ap.parse_args(argv)
    manifest = merge(args.out, args.snapshots, args)
    print(
        json.dumps(
            {k: manifest[k] for k in ("rows_merged", "rows_kept", "entities", "gaps")}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
