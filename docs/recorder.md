# Recorder compatibility and who owns purging

Sediment writes to tables it does not own, so it runs only against recorder code it was tested with. Once you tick the takeover, it also decides when old history is deleted.

## The guard

At every start with the takeover ticked, and before every job, `compat.check_compat` checks:

- the database is SQLite (Sediment refuses other databases for now; a dry run, which only reads, still works);
- the recorder schema version is in the allow-list (53);
- the recorder instance has `keep_days`, `auto_purge`, `auto_repack`, `backlog`, `max_bind_vars`, `queue_task`, `get_session` and the cache managers Sediment evicts from;
- `keep_days` is an integer and `auto_purge` a bool, and the recorder has no legacy event-linked state rows;
- the recorder's nightly task still reads `auto_purge` and `keep_days` when it runs;
- `RecorderTask` is a dataclass whose `run` takes `(self, instance)`, and `PurgeTask` has the fields `purge_before`, `repack`, `apply_filter`;
- `session_scope` and `repack_database` exist.

What a failed check does depends on whether Sediment has already taken over:

| Situation | Result |
| --- | --- |
| Takeover never applied | Sediment takes nothing over and leaves the recorder untouched. A repair issue lists the reasons. |
| Takeover already applied (thinned history exists) | Sediment thins and deletes nothing. It keeps the recorder from purging: it sets `auto_purge` to off (if that attribute still exists). It raises `keep_days` to the largest of its configured value, the history length, and the age of your oldest row plus a day. So even a `recorder.purge` call without `keep_days` cannot wipe the history. A repair issue says so. |

At that moment, handing purging back to the built-in purge would delete the thinned history. So a failed check never does that.

## What the takeover changes

The takeover changes two attributes on the recorder instance, in memory only: `auto_purge` and `keep_days`. Sediment writes them to no file. After a restart the recorder has its configured values, and Sediment sets them again when it loads. Sediment remembers in its own storage that the takeover is applied (`owned_retention`).

While the takeover is applied:

- Reloading or restarting Sediment changes nothing. The next setup applies the takeover again.
- Switching dry run on pauses thinning only. The history length (never shorter than the one of the last real run) and the cleanup of events, short-term statistics and recorder runs keep running, because they do what the recorder's own purge would have done. `drop` rules and the shorter horizon of `coarse` entities are only counted during a dry run, so you can test new rules.
- Unticking the box in the options asks for confirmation. The built-in purge then deletes everything older than the recorder's configured `keep_days` at its next nightly run.
- Removing the integration hands purging back and posts a notification that says the same.
- Disabling the integration hands nothing back. The recorder keeps Sediment's settings until Home Assistant restarts, and a notification says so. After a restart the recorder uses its configured values, which is why the safety net below matters. Enabling the integration again applies the takeover as before, also with dry run on.

## Ownership is inferred from the data too

Sediment stores in its own storage that the takeover is applied. If that storage is lost (a restore from backup, a deleted file), the data still shows it. Suppose the takeover is ticked and states are older than the recorder's configured `keep_days` plus two days. Then something other than the recorder's purge has kept them. Sediment treats the retention as owned *for protection only*. It keeps the recorder from purging, exactly as for a failed guard. It does nothing that deletes.

## Safety net for a failed load

Sediment can protect your history only while it runs. If it does not load at all (a failed update, a missing file), the recorder starts with your configuration and runs its own purge. To rule that out, add this to `configuration.yaml`:

```yaml
recorder:
  auto_purge: false
```

Sediment raises a repair issue if the takeover is ticked and the recorder started with `auto_purge` on. With the line above, the recorder never purges on its own, and only Sediment deletes old data.

## Writing to the database

Everything runs on the recorder thread as `RecorderTask`s, the way the recorder's own purge does. Sediment deletes only rows older than the raw window. It uses the same cache evictions as the recorder's purge. See [how-it-works.md](how-it-works.md).
