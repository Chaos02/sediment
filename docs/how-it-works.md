# How Sediment works

## What is thinned

Sediment works on the recorder's `states` table. One unit of work is one entity on one UTC day. Sediment touches a day only if the day ends before the raw window (10 days by default). So it never affects rows the recorder is still writing, or the recorder's own caches.

Each day gets the tier that matches its age. Tiers follow age, not a share of the history length. When you lengthen the history, recent data keeps its resolution.

| Tier (Standard) | Age | Bucket |
| --- | --- | --- |
| Raw | up to the raw window | none |
| 1 | to 6 months | 15 minutes |
| 2 | to 2 years | 1 hour |
| 3 | to the history length | 6 hours |

When a day that is already at tier 1 gets older, Sediment thins it again with the next tier's buckets. It works on the rows that survived. Each tier is a subset of the one before.

## What each entity class keeps

- Continuous (most sensors): the first and last row of every bucket. If the bucket moved by more than the tolerance E, it also keeps its exact minimum and maximum. A jump larger than max(3E, 5 sigma) that lasts a quarter bucket keeps both rows. A swinging door pass over these candidate points then removes the rows that a straight line within E explains. E comes from the series: the smallest step (quantum), the noise (sigma) and the robust range.
- Counters (`state_class` `total` or `total_increasing`): the first and last value of every bucket, the rows around every reset, and nothing for buckets that did not change. Deltas stay exact.
- Enum and binary: every row whose state differs from the row before. Sediment removes same-state rows (attribute changes only). With flapping collapse on, a burst of four or more changes, each within 60 seconds of the last, keeps its first and last change. This starts at tier 2.
- Few-level numbers (at most three distinct values, such as 0 and 800): the enum rule applies, but only if the closest two levels are more than five sensor quanta apart. Otherwise the numeric rule applies. That suits a sensor that flickers between 0.00 and 0.01.
- `climate`: the state plus `current_temperature`, `temperature` and `hvac_action`. A row stays if any component keeps it.
- Gaps: a run of `unavailable` or `unknown` rows longer than five minutes keeps its first row, the last valid row before it and the first valid row after it.
- The first and last row of an entity's whole series always stay.

Guarantee, as measured on a reference database: at the first tier, Sediment keeps the exact extremes of every active bucket. Every sample of a flat bucket is within 2E of the reconstruction. At later tiers the guarantee applies to the previous tier's output, so errors can add up.

## Keeping the database valid

- If a kept row's `old_state_id` pointed at a deleted row, Sediment moves it to the previous kept row of the same entity. A NULL that Home Assistant wrote stays NULL.
- If a row outside the day points at a row that Sediment would delete, Sediment moves the pointer to the previous kept row. If that row lies inside the raw window, Sediment keeps the pointed-at row instead. So rows in the raw window stay untouched.
- Sediment keeps the last row of a day when the entity's next row lies inside the raw window.
- Sediment deletes attribute rows only when no `states` row references them. It also evicts the recorder's id caches, as the recorder's own purge does.
- Each entity-day is one transaction. A step stops at its first error and rolls that entity-day back. Sediment retries a transient database error (locked, busy) on the next run. It skips the entity-day only after three such failures. Any other error skips it at once. The rows stay as they are, and the report lists the skipped entity-days under `quarantine`. Three failed nightly jobs in a row raise a repair issue.
- Before Sediment deletes a chunk, it clears the `old_state_id` of the rows in that chunk. So no chunk can strand a reference.

## The nightly job

At 04:30 the job runs these steps, in this order:

1. Thin the tiers. Thin again the old days of entities whose profile changed.
2. Delete the rows of `drop` entities that are older than the raw window.
3. With the takeover: delete rows older than the history length. Then delete events, short-term statistics, recorder runs, unused attributes, and entity ids and event types that nothing uses any more.

The cleanup deletes only what is older than the larger of the raw window and the recorder's own `keep_days`. It loops until its backlog is gone. A failing step does not stop the later steps. So the history length and the cleanup run even when thinning fails. On the second Sunday of the month, Sediment repacks the database, if the recorder's own `auto_repack` was on.

The job runs as recorder tasks. Each task works for about a quarter of a second and finishes whole entity-days. Between tasks, the job waits while the recorder's queue holds more than 1000 items.

Sediment processes an entity-day with more than 200,000 rows in time slices. A slice is a multiple of the widest bucket, so one unit cannot hold the recorder thread for long. If the bucket is a whole day or wider, Sediment cannot slice it. It skips the entity-day and lists it under `oversize` in the report.

Sediment keeps its progress in `.storage/sediment`:

- the tier already applied to each day;
- the class and tolerance of each entity (refreshed weekly, with the last known `state_class`);
- the entity-day where the job stopped;
- the profile of every entity that is not `default`.

### When a profile changes

Sediment stores the profile of every entity that is not `default`. It also stores a hash of everything that assigns profiles: the rules text, the resolution and its custom tiers, the flapping default and the coarse horizon. When that hash changes, Sediment thins again the entities whose profile differs. It starts at the first tier, on the rows that survived. So a coarser profile takes effect on history that is already thinned. A finer profile cannot bring back rows that an earlier, coarser profile deleted. It only affects data that is not thinned yet.

A profile that changes without a rule change never triggers this. An entity keeps its stored profile as long as the rules stay the same. So an entity whose device class is unknown for one night (its integration has not started, or it is unavailable) does not move to `default` and does not get thinned. If the rules change while an entity's metadata is missing, the more conservative of the stored and the new profile applies. Within one run, Sediment never thins a day twice for the same profile.

## Dry run

A dry run reads the last ten complete days. It runs the full tier chain on every entity-day in memory. It counts, per entity class and bucket size, how many rows it would keep. These shares replace the built-in reference numbers in the size projection. It also counts what a real run would delete for `drop` and the history length. A dry run writes nothing to the database. The report is `sediment/last_report.json` in the configuration directory.
