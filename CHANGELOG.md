# Changelog

Notable changes land here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- Config flow and options flow: history length (1, 2, 5, 10 years or custom days), Sediment resolution (Fine, Standard, Coarse, Custom tiers), optional size cap, profile rules, and a "Purging and retention" step.
- Live size projection in the flow, from the recorder's real row rates and the built-in reference kept shares. Above the cap the flow offers a coarser oldest tier. It never thins newer data to fit.
- Sensors `sensor.sediment_db_size` and `sensor.sediment_projected_size`.
- Codec: bucket extremes, persistent steps and swinging door trending for numeric series, exact bucket ends and resets for counters, every transition for enum and binary series with optional flapping collapse, edges of `unavailable` gaps, attribute driven series for `climate`.
- Few-level numeric series use the enum rule only when their levels are many sensor quanta apart. Resolution flicker such as 0.00 and 0.01 goes through the numeric rule.
- Profiles `keep`, `default`, `coarse` and `drop`, assigned by entity id pattern, domain, device class or integration, with a per-rule flapping override.
- Recorder takeover (opt in): turns off the recorder's own purge and aligns its keep days with the history length. Once applied, it's remembered (`owned_retention`), because the thinned history depends on it. Reloads and restarts keep it. Switching dry run on only pauses thinning. Unticking asks for confirmation. Removing the integration hands purging back with a notification.
- Compatibility guard at every start and before every job: SQLite only, schema allow-list, recorder attributes and task signatures. A mismatch never hands purging back. If the takeover was never applied, the recorder stays untouched. If it was, Sediment keeps the recorder from purging. Both raise a repair issue.
- Repair issue with a safety net for a failed load: set `recorder: auto_purge: false` in `configuration.yaml`.
- Nightly job at 04:30 on the recorder thread, in time-boxed batches of whole entity-days, backing off while the recorder queue is longer than 1000. It thins the tiers, applies `drop` and the horizon, and with the takeover on removes old events, short-term statistics, recorder runs, unused attributes and unused entity ids and event types, never earlier than the recorder's own `keep_days` would have. Each step runs even if an earlier one failed, and the cleanup loops until its backlog is gone. Repack on the second Sunday of the month. Entity-days with more than 200,000 rows are processed in time slices.
- Failed entity-days are quarantined (transient database errors only after three failures), listed in the report and released with `sediment.clear_quarantine`. Three failed nightly jobs in a row raise a repair issue.
- When the profile rules, resolution or presets change, entities whose profile differs have their old days thinned again. A transient change of an entity's metadata does not.
- Ownership of the retention is also inferred from the data (old states with the takeover ticked), for protection only.
- Disabling the integration keeps the history protected until the next restart and says so.
- A job that backs off no longer blocks unloading. Unloading gives up after 30 seconds.
- Dry run, on by default for new installs: computes everything, writes `sediment/last_report.json`, raises a notification, deletes nothing. Its measured kept shares replace the reference defaults in the projection.
- Service `sediment.run_now` with a `dry_run` field.
- History importer: `tools/merge_history.py` merges recorder snapshots (read only, overlaps removed, gaps kept, thinned with the integration's codec by each day's age) into an import file, and the service `sediment.import_history` inserts it, entity by entity, only below the oldest live row. Dry run by default; refuses another schema, a non-SQLite database and an import the recorder's purge would delete. See `docs/importing.md`.
- Service `sediment.export_profile_map` and `merge_history.py --profile-map`, so the import knows the profiles that only Home Assistant sees. Without a map, entities whose rules the tool cannot evaluate are kept in full. The import marks days as thinned only for a matching map.
- The importer remembers the bound for entities without live rows per file (an interrupted import converges on re-run), seals every written column with the content hash, rejects impossible rows and unusable chain links, skips rows the nightly job would delete anyway, and accepts files from `<config>/sediment/` (never from `<config>/www`).
- Repair issue `retention_inferred` when old states make Sediment hold the recorder's purge back without a stored takeover.
- English and German translations.
