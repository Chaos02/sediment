# Configuration

Go to Settings, Devices & services, Sediment, Configure. The same steps appear when you add the integration.

## History length

Pick 1, 2, 5 or 10 years, or a number of days. When Sediment owns purging, it deletes rows older than this. It thins newer data and never deletes it early.

## Sediment resolution

| Resolution | Tier 1 | Tier 2 | Tier 3 |
| --- | --- | --- | --- |
| Fine | 15 minutes to 1 year | 1 hour to 3 years | 6 hours |
| Standard | 15 minutes to 6 months | 1 hour to 2 years | 6 hours |
| Coarse | 1 hour to 6 months | 6 hours to 2 years | 1 day |
| Custom | your bucket and end age | your bucket and end age | your bucket |

The optional size cap is a guard. If the projection is above the cap, the flow offers to make the oldest tier one step coarser and recalculates. The steps are 15 minutes, 1 hour, 6 hours and 1 day. The newer tiers stay as they are.

## Profiles

Write one rule per line. The first match wins: `selector = profile [flap|noflap]`.

- Selectors: an entity id with `*` and `?`, `domain:sensor`, `device_class:temperature`, `integration:name`.
- Profiles: `keep` (never thinned, kept to the history length), `default`, `coarse` (1 hour to 90 days, then 6 hours, history capped at one year) and `drop` (kept for the raw window only).
- `flap` or `noflap` overrides the global flapping collapse for the matched entities.
- Lines that start with `#` are comments. Entities without a rule are `default`.

Example:

```
# noisy host metrics do not need fine resolution
sensor.host_* = coarse
integration:my_proxy = coarse
# never thin the energy meter
sensor.house_energy = keep
# keep every change of this flapping device
device_tracker.printer = default noflap
```

## Purging and retention

The step shows the recorder's current values. Ticking "Sediment takes over purging" does this at every start:

1. checks the recorder (see [recorder.md](recorder.md));
2. sets `auto_purge` to off and `keep_days` to the history length on the recorder instance;
3. remembers that the takeover is applied (thinned history now depends on it).

The recorder's original values come back only on purpose. That happens when you untick the box and confirm, or when you remove the integration. Disabling the integration keeps the takeover until Home Assistant restarts. The options flow warns that the built-in purge then deletes everything older than the recorder's configured `keep_days` at its next nightly run. A notification says the same. Reloading or restarting Sediment keeps the takeover. For the safety net in `configuration.yaml`, see [recorder.md](recorder.md).

The raw window defaults to the recorder's current `keep_days`. Without the takeover, the recorder deletes states older than its own `keep_days` before Sediment can thin them. So the takeover is what makes Sediment useful.

Dry run is on until you untick it. Before the takeover has ever been applied, a dry run only checks it (a failed check raises the repair issue), and the recorder keeps purging as before. Once the takeover is applied, switching dry run on again only pauses thinning. The takeover stays. The history length (never shorter than the last real run's) and the cleanup keep running, because they do what the recorder's purge would have done. `drop` rules and the shorter `coarse` horizon are only counted in a dry run.

The cleanup of events, short-term statistics and runs never deletes earlier than the recorder's own `keep_days` would have. If the raw window is longer, the raw window applies.

## Service

`sediment.run_now` has an optional field `dry_run`. Without the field it uses the setting from the options.

`sediment.export_profile_map` writes `<config>/sediment/profile_map.json` for the merge tool (see [importing.md](importing.md)).

`sediment.import_history` (fields `path` and `dry_run`, a dry run by default) brings older history back from a file made by `tools/merge_history.py`. See [importing.md](importing.md).

`sediment.clear_quarantine` makes Sediment thin again the entity-days that it skipped after errors (see [how-it-works.md](how-it-works.md)).

## Sensors

- `sensor.sediment_db_size`: size of the recorder database file and its WAL, in GB. SQLite only.
- `sensor.sediment_projected_size`: the projection at the end of the history length.
