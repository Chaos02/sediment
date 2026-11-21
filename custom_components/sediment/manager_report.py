"""The report of a run: counters, repair issue for failing runs, file and notification."""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
import os
from typing import Any

from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from . import solver
from .const import (
    DOMAIN,
    ISSUE_JOB_FAILING,
    JOB_FAIL_ISSUE_AFTER,
    REPORT_FILE,
)
from .issues import delete_issues

_LOGGER = logging.getLogger(__name__)


TOP_ENTITIES = 25


class ReportMixin:
    """Report building and writing, for the manager."""

    def _note_result(self, errors: list[str]) -> None:
        """Count consecutive failed jobs; three in a row raise a repair issue."""
        if not errors:
            self.data["failed_jobs"] = 0
            delete_issues(self.hass, ISSUE_JOB_FAILING)
            return
        self.data["failed_jobs"] = self.data.get("failed_jobs", 0) + 1
        if self.data["failed_jobs"] >= JOB_FAIL_ISSUE_AFTER and self.settings.takeover:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_JOB_FAILING,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_JOB_FAILING,
                translation_placeholders={
                    "count": str(self.data["failed_jobs"]),
                    "errors": "; ".join(errors[:3]),
                },
            )

    def _make_report(self, job: Any, dry: bool, seconds: float, waited: float) -> dict[str, Any]:
        st = job.stats
        stats = asdict(st)
        top = sorted(st.top_entities.items(), key=lambda kv: -kv[1][0])[:TOP_ENTITIES]
        stats["top_entities"] = {eid: {"raw_rows": v[0], "kept_rows": v[1]} for eid, v in top}
        measured = solver.measured_shares_from_counts(st.share_counts)
        days = max(1, st.days_simulated)
        report: dict[str, Any] = {
            "version": 1,
            "generated": dt_util.utcnow().isoformat(),
            "mode": "dry_run" if dry else "run",
            "seconds": round(seconds, 1),
            "backoff_seconds": waited,
            "settings": {
                "history_days": self.settings.history_days,
                "resolution": self.settings.resolution,
                "raw_days": self.settings.raw_days,
                "takeover": self.settings.takeover,
                "takeover_applied": bool(self.takeover and self.takeover.active),
                "flapping_collapse": self.settings.flap_collapse,
                "size_cap_gb": self.settings.size_cap_gb,
            },
            "stats": stats,
            "quarantine": dict(list(self.data.get("quarantine", {}).items())[:50]),
            "measured_kept_share": measured,
            "raw_rows_per_day_by_class_and_profile": {
                k: round(v / days, 1) for k, v in st.raw_rows_by_class_profile.items()
            },
        }
        if self.data.get("projected_gb") is not None:
            report["projected_gb"] = self.data["projected_gb"]
        return report

    @staticmethod
    def _notification_text(report: dict[str, Any]) -> str:
        st = report["stats"]
        lines = [
            f"Simulated {st['days_simulated']} day(s), {st['entity_days']} entity-days, "
            f"{int(st['rows_seen']):,} rows. Nothing was thinned and no entity was dropped.",
        ]
        if st.get("housekeeping"):
            lines.append(
                "The takeover is applied, so the history length and the cleanup of events, "
                f"short-term statistics and runs did run: {st['horizon_rows']:,} old state "
                f"rows, {st['events_deleted']:,} events and {st['short_term_deleted']:,} "
                "short-term statistics were removed."
            )
        shares = report.get("measured_kept_share") or {}
        for cls, by_bucket in shares.items():
            parts = ", ".join(
                f"{float(b) / 60:g} min: {s * 100:.1f} %"
                for b, s in sorted(by_bucket.items(), key=lambda kv: float(kv[0]))
            )
            lines.append(f"Kept share of {cls} rows: {parts}")
        if st["dropped_rows"]:
            lines.append(f"{st['dropped_rows']:,} rows of drop entities would be deleted.")
        if st.get("reprofiled_entities"):
            lines.append(
                f"{st['reprofiled_entities']} entities changed profile since the last run; "
                "their old days would be thinned again."
            )
        lines.append("Full report: sediment/last_report.json in the config directory.")
        return "\n\n".join(lines)

    def _write_report(self, report: dict[str, Any]) -> None:
        path = self.hass.config.path(REPORT_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        os.replace(tmp, path)
