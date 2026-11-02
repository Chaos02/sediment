<img src="assets/icons/sediment.svg" width="96" alt="Sediment icon">

# Sediment

[![Pipeline](https://gitlab.com/Chaos02-oss/sediment/badges/main/pipeline.svg)](https://gitlab.com/Chaos02-oss/sediment/-/pipelines)
[![Release](https://img.shields.io/gitlab/v/release/Chaos02-oss%2Fsediment?include_prereleases&sort=semver&label=release)](https://gitlab.com/Chaos02-oss/sediment/-/releases)
[![Issues on GitLab](https://img.shields.io/badge/issues-on%20GitLab-FC6D26?logo=gitlab)](https://gitlab.com/Chaos02-oss/sediment/-/issues)
[![HACS: custom repository](https://img.shields.io/badge/HACS-custom%20repository-41BDF5)](https://www.hacs.xyz/docs/faq/custom_repositories/)
[![License: MPL-2.0](https://img.shields.io/badge/license-MPL--2.0-blue)](LICENSE)

> [!IMPORTANT]
> **This project lives on GitLab: [gitlab.com/Chaos02-oss/sediment](https://gitlab.com/Chaos02-oss/sediment).**
> Issues, merge requests, CI and releases are all there.
> The GitHub repository is a read-only mirror. It exists only because the Home Assistant Marketplace (HACS) installs from GitHub.
> Pull requests on GitHub are not reviewed. Please open a merge request on GitLab instead.

Home Assistant keeps raw state history for 10 days and then deletes it. Sediment thins the old rows of the recorder's `states` table in place instead. It keeps spikes, steps, every state change and the edges of `unavailable` gaps. Flapping bursts shrink to their first and last change from tier 2 on, unless you turn that off. It removes the long stretches where nothing changed. History, logbook and energy keep working, because the data stays in the normal tables.

Status: beta. Tested with Home Assistant 2026.10 (recorder schema 53) on SQLite. MariaDB and PostgreSQL are untested.

## What it does

- Sediment thins in tiers by age. A bucket is 15 minutes up to 6 months, 1 hour up to 2 years, then 6 hours up to your history length.
- Each bucket keeps its first and last row, and its exact minimum and maximum if it moves. A persistent step keeps both rows of the jump.
- Counters keep both ends of every bucket and every reset, so deltas stay exact.
- Binary and enum entities keep every transition.
- Each entity has a profile: `keep`, `default`, `coarse` or `drop`. Rules assign it.
- Before you save, Sediment projects the database size from your recorder's real row rates. An optional size cap only warns. Sediment never thins newer data to fit it.
- Two sensors show the size: `sensor.sediment_db_size` and `sensor.sediment_projected_size`.

## Install

Add this repository to HACS as a custom repository, with pre-releases enabled. Or copy `custom_components/sediment` into your configuration directory. Restart, then add Sediment under Settings, Devices & services.

## Safety

- A new install starts in dry run. A dry run deletes nothing. It writes `sediment/last_report.json` to your configuration directory and shows a notification.
- Sediment changes only rows older than the raw window (10 days by default).
- The takeover is a checkbox in the "Purging and retention" step. With it ticked and dry run off, Sediment turns off the recorder's nightly purge. It also cleans up events, short-term statistics and recorder runs itself.
- Unticking the takeover asks for confirmation. Removing the integration hands purging back and tells you.
- At every start, Sediment checks the database (SQLite only for now), the recorder schema version and the internals it relies on. If anything differs, it leaves your data alone and raises a repair issue. If the takeover was already applied, it also keeps the recorder from purging.
- Add `recorder: auto_purge: false` to `configuration.yaml` as a safety net. If Sediment fails to load, the built-in purge deletes the history.
- Back up the database before the first real run.

## Limits

- Thinning loses detail between kept rows. At the 6 hour tier a bucket keeps one high and one low point, so smaller spikes are gone.
- Sediment does not touch long-term statistics.
- The recorder's include and exclude filters are YAML only. To remove an entity's old rows after the fact, use the `drop` profile.

## Use

The job runs nightly at 04:30. The service `sediment.run_now` runs it once. Details are in [docs/](docs/).

## License

MPL-2.0. See `LICENSE`.
