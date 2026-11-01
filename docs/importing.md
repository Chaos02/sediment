# Importing older history

If you have copies of the recorder database from earlier dates (backups, file system snapshots), Sediment can bring their history back. It takes two steps. An offline tool merges and thins the copies into one import file. Then the service `sediment.import_history` inserts that file into the live database.

Sediment adds only history that is older than what the live database holds. Nothing that is already recorded changes.

## What you need

- Copies of the recorder database. Each is a normal Home Assistant recorder SQLite file of schema 53. Each copy holds only the days it recorded (10 by default).
- Python 3.10 or newer on any machine. The tool needs no Home Assistant and no extra packages. It reads the integration's `codec.py` and `profiles.py` from this repository. Run it from a checkout (or a release folder) of Sediment.
- On the Home Assistant side: the takeover ticked and dry run off (see [recorder.md](recorder.md)). Without that, the recorder's own purge deletes rows older than its keep days at the next nightly run. So the service refuses.

If a copy has a `-wal` file next to it, checkpoint it first, or copy it from a stopped recorder. The tool opens every copy read-only with `immutable=1`. That mode ignores the `-wal` file, and the tool prints a warning.

## Step 0: export the profile map

The tool runs outside Home Assistant. It cannot see what only Home Assistant knows: which integration an entity belongs to, or a device class that comes from the entity registry. Have Home Assistant write that down first:

```yaml
action: sediment.export_profile_map
```

This writes `<config>/sediment/profile_map.json`: every entity with its resolved profile, plus the profile signature and a hash of the map. Copy the file next to the tool. Pass it with `--profile-map`.

Without a map, the tool applies your rules as far as it can see them (entity id, domain, a device class that is in the attributes). If it cannot evaluate an entity's rules (an `integration:` rule, or a device class that is not in the attributes), it treats the entity as `keep`. The import then carries that entity in full, and the tool says how many there are. That is safe but large. The importer also does not mark the days as thinned (see below).

## Step 1: merge and thin

```
python tools/merge_history.py import.db copy-a.db copy-b.db copy-c.db --now 2026-11-01
```

`--now` is the date the tiers count from, normally today. These options are useful:

| Option | Default | Meaning |
| --- | --- | --- |
| `--history-days` | 1830 | rows older than this are removed |
| `--raw-days` | 10 | rows younger than this stay raw |
| `--resolution` | standard | `fine`, `standard`, `coarse` or `custom` (with `--custom-tiers`) |
| `--profile-map FILE` | none | the map from `sediment.export_profile_map`; replaces the rules for every entity in it |
| `--rules FILE` | none | profile rules, same syntax as in the options (used for entities that are not in the map) |
| `--flapping` / `--no-flapping` | on | flapping collapse |
| `--coarse-horizon` | 365 | days kept for `coarse` entities |

Use the same values as in your Sediment options. If they differ, the importer notices (see below). It then leaves the thinning of those days to the nightly job.

What the tool does:

- Two copies usually share days. The tool keeps rows with the same entity id and `last_updated_ts` once. It prefers the copy whose `old_state_id` still points at a row of its own copy.
- The tool merges the time spans that the copies cover into periods. It lists the stretches between periods as gaps in the manifest. It invents no row. It thins each period on its own, so the first and last row of a period always stay.
- The tool runs every entity-day through the same codec and tiers as the nightly job, by its age from `--now`. So the import arrives as thin as it would have become. The tool removes days beyond the history length and leaves out `drop` entities. `keep` entities stay raw.
- `import.db.manifest.json` lists the sources, the periods covered, the gaps, the row counts per period, whether the tool used a profile map, the profile signature and a content hash. The hash covers every value the import writes, so the importer refuses a changed file.

The tool does not import events, statistics (the live database already has the hourly statistics) and context ids.

## Step 2: import

Put `import.db` in **`<config>/sediment/`**. That folder is always allowed. Another folder must be in `allowlist_external_dirs`. The service refuses a file under `<config>/www`, because Home Assistant serves that folder over the network without a login. Then call the service. Start with the default, a dry run:

```yaml
action: sediment.import_history
data:
  path: /config/sediment/import.db
  dry_run: true
```

The response and `sediment/last_import.json` in the configuration directory report, per period and per entity, how many rows the import would insert and how many it skips. Then run it for real with `dry_run: false`. Take a backup of the database first.

How the importer inserts:

- Per entity, it inserts only rows strictly older than that entity's oldest live row. An entity with no live rows gets the oldest live row of any entity as its bound. The importer measures that bound once for the file and remembers it. So running the import again after an interruption ends with the same rows as one clean run. The importer skips rows at or after the bound and counts them.
- It does not insert rows that the next nightly run would delete anyway. It counts them as `skipped_would_be_deleted`. These are rows beyond the history length, rows of a `coarse` entity beyond its horizon, and rows of a `drop` entity beyond the raw window. This uses the profiles as they are now, in a dry run too.
- It trusts nothing beyond the content hash. It rejects rows with a missing or non-finite time, or with `last_changed_ts` after `last_updated_ts` (`rejected_rows`). It keeps an `old_state_id` link only if the link points at an earlier row of the same entity. Otherwise the link becomes NULL (`chains_rejected`). It stores a state longer than 255 characters as NULL, as Home Assistant does (`states_too_long`).
- It matches entities to `states_meta` by `entity_id`. It creates a missing one through the recorder's own cache, so the recorder keeps using the same id.
- It matches attributes by Home Assistant's hash and text, and reuses them.
- It chains `old_state_id` inside the imported period. The first live row keeps the pointer Home Assistant wrote. A NULL stays NULL.
- Each entity is one transaction on the recorder thread, in batches, with the same back-off as the nightly job. You can simply start a stopped import again.
- When the takeover is applied, it does not import rows older than the history length, because the nightly job would delete them again.
- Afterwards, it reloads the recorder's oldest-timestamp cache.

## Doing it twice

Importing the same file again inserts nothing. Sediment remembers the content hash. Even without it, nothing is older than the oldest live row any more. A second file for an earlier period works. A second file that fills a gap between two imported periods does not work. The importer skips rows after the bound, because filling gaps means inserting among live rows.

## After the import

The importer records which imported days are already thinned, so the nightly job does not thin them again. It does that only if all three of these hold: the file was made with a profile map, the map's signature is the current one, and every entity in the file still has the profile it was thinned with. Otherwise (no map, other settings, an entity that changed profile) the importer leaves the days unmarked. The report says `thin_again` and why. The nightly job then thins them with the current settings. That is safe: thinning a thinned day changes it very little. A finer profile cannot bring back rows the tool already removed.

## Refusals

The service refuses, and writes nothing, if:

- the file was made from a recorder schema other than 53, is not an import file, or is damaged (the content hash does not match);
- the database is not SQLite;
- the recorder check fails (see [recorder.md](recorder.md));
- the recorder's own purge is on and would delete the imported rows at its next run: turn the takeover on first;
- the path is not in `<config>/sediment/` or an allowed folder, or is under `<config>/www`;
- a Sediment job or another import is running.

A dry run reports these as `problems` instead of failing.
