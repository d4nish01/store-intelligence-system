# CHOICES.md — Engineering Decisions and Trade-offs

These are the non-obvious decisions I made during the build, written down so a reviewer can
understand the reasoning without having to guess. Some of these I went back and forth on;
some were obvious from the start. I've tried to be honest about both.

---

## Decision 1: Detection Model

I looked at four realistic options before picking one.

**YOLOv8 / YOLOv11** — what I went with. Fast, well-documented, single `pip install`, and
the pre-trained weights cover person detection well out of the box. I've used it before which
helped.

**RT-DETR** — transformer-based, genuinely better accuracy in dense multi-person scenes. I
considered this seriously. The reason I didn't go with it: it's noticeably heavier to run and
the setup is more involved. For a take-home where the reviewer needs to reproduce this on their
own machine, adding that friction felt like the wrong trade. I documented it as the obvious
production upgrade path instead.

**MediaPipe** — designed for frontal face and pose detection. Overhead and angled CCTV cameras
are not what it was optimised for. Accuracy degrades meaningfully in this setup.

**VLMs (CLIP, GPT-4V etc.)** — I looked at this briefly for zone classification specifically,
not detection. For detection it's not even worth considering — too slow for per-frame inference.
For zone classification I explain why I rejected it in Decision 5 below.

The detection module lives entirely in `pipeline/detect.py`. I wrote it as a replaceable
wrapper on purpose — swapping to RT-DETR for a production deployment is a one-file change.

**Honest trade-offs I accepted:** YOLO can occasionally split a single person into two tracks
in crowded frames. I handle this at the tracker and re-id level rather than at detection. Low
confidence detections get stored with their actual confidence value rather than being dropped —
that way the threshold can be adjusted later without losing data.

---

## Decision 2: Event Schema

Most of the schema follows the challenge contract directly. A few choices are worth explaining.

**`event_id` as UUID v4** — The ingest endpoint is idempotent. Submitting the same event
twice should produce one database row, not two. UUID v4 gives global uniqueness without a
central counter, and the pipeline can generate IDs offline before the API is even running.

**`visitor_id` as a pipeline-generated token, not a database ID** — The backend never
generates `visitor_id`. It comes from the re-id module and is an anonymised token. I kept
PII handling in the pipeline layer and out of the API layer intentionally.

**`session_seq` inside `metadata`** — It's operational metadata, useful for debugging but
not a business metric. Keeping it in `metadata` matches the contract spec and keeps the root
schema clean. The backend denormalises it to a column for query speed but the wire format
stays consistent.

**`confidence` always present, never null** — I wanted the option to filter by confidence
at query time. You can't do that if confidence is sometimes missing. It also lets you monitor
camera health — a store where average confidence is dropping might have a dirty lens or
changed lighting conditions.

**Low-confidence events stored, not dropped** — This was a deliberate call. Dropping events
at ingest is irreversible. If I later decided to lower the confidence threshold (maybe the
store has challenging lighting), I'd have no way to recover the dropped events. Storing
everything and filtering at query time gives flexibility. The threshold lives in
`config/settings.yaml` and can be changed without a deployment.

**Staff events stored with `is_staff = True`, not discarded** — Same reasoning. I want the
audit trail. If the staff classifier is wrong about a particular track, I can see the event
and re-classify retroactively. Every metric query applies `WHERE is_staff = False` at the
SQL layer so it can't accidentally be omitted.

**`queue_depth` in `metadata`** — Queue depth at a specific moment is not derivable from
other events after the fact. It has to be captured at the moment of BILLING_QUEUE_JOIN or
BILLING_QUEUE_ABANDON and stored. I put it in `metadata` to keep the root schema tidy.

**`sku_zone` in `metadata`** — Links a zone event to the product category label from the
store layout. This makes "which SKU category had the highest dwell time?" a straightforward
query without needing to join back to the layout config file at query time.

**REENTRY as its own event type, not just another ENTRY** — If REENTRY used the same
`ENTRY` event type, every re-entering visitor would increment unique_visitors. The funnel
would show double entries for customers who stepped outside briefly. Having a distinct event
type means the metrics logic can handle the two cases differently with a simple
`event_type IN ('ENTRY')` vs `event_type IN ('ENTRY', 'REENTRY')` choice.

**No PURCHASE event type** — The camera cannot observe whether a payment completed. A
visitor standing at the billing counter might abandon without paying. Purchase is inferred
from POS correlation, not directly observed. Adding a PURCHASE event type would blur that
distinction in a way I think is misleading. The funnel's purchase stage is computed from POS
data, which is the right source of truth for that signal.

---

## Decision 3: Storage and API Architecture

I'll be straightforward about this: SQLite is not the right choice for 40 stores in production.
I picked it anyway and here's why.

For a take-home evaluation, the evaluator runs `docker compose up` and the system works. No
separate database container, no credentials to configure, no connection strings to fiddle with.
The database is a file. That's a real advantage in a demo context.

The reason SQLite doesn't hurt here: the demo workload is two stores, a few hundred to a few
thousand events replayed from a file. SQLite handles that without breaking a sweat.

The reason I'm confident it migrates cleanly: I used SQLAlchemy throughout. The models,
queries, and session management are all ORM-based. Switching to PostgreSQL is changing one
line in `.env`:

```
DATABASE_URL=postgresql://user:pass@localhost:5432/store_intelligence
```

Nothing else changes. I verified this mentally while writing the models — I deliberately
avoided any SQLite-specific syntax.

**Other options I considered:**

Redis Streams — good for real-time event buffering, wrong for this demo. Adds a Redis
container and makes the setup more complex without any benefit at demo scale.

Kafka — I understand why you'd want it for a real 40-store deployment. For a take-home it
would bury the actual analytics logic under infrastructure setup. Not the right trade.

In-memory only — simplest possible, but the evaluator needs to replay events once and then
query metrics multiple times without replaying again. In-memory loses everything on restart.

**Idempotency** — the pipeline may crash and replay. The replay script may be run twice by
mistake. Without deduplication on `event_id`, any of those scenarios inflates every metric.
The `events.event_id` primary key enforces deduplication at the database layer so the ingest
endpoint can be called any number of times with the same event and produce one row.

**Structured error responses** — when a batch of 50 events has 3 malformed ones, the caller
needs to know which 3 failed and why, not just that "something went wrong". The ingest
response returns `accepted`, `duplicates`, `rejected`, and an `errors` list with index,
field, and message for each failure. This makes debugging pipeline output practical.

**The migration plan for production** is in DESIGN.md §27. Short version: PostgreSQL first,
then Redis/Kafka for event buffering, then background aggregation workers for metric
pre-computation. The config-driven store design means none of those infrastructure changes
touch the business logic layer.

---

## Two Entry Cameras — Dataset-Specific Decision

When I opened the dataset I expected three video files per store (entry, zone, billing).
There were four — two entry cameras.

My first instinct was to check if one was redundant. Looking at the layout images it became
clear they're both covering the same physical threshold from slightly different angles. That
means if I treat them as independent event sources, every visitor gets an ENTRY event from
each camera. Footfall doubles. Conversion rate halves. That's a fundamental error, not a
minor one.

The solution: `entry 1.mp4` is the authoritative primary source for all ENTRY and EXIT events.
`entry 2.mp4` is a confirmation camera — it never independently emits an ENTRY or EXIT.

When `entry 1` detects a crossing, I check whether `entry 2` also detected one in the same
direction within `dedup_window_seconds` (default 4 seconds). If yes, I merge them into one
event using the primary camera's timestamp and take the higher confidence. If only `entry 1`
fires (normal case), I emit normally. If somehow only `entry 2` fires and primary missed
completely, I emit with a 0.85× confidence penalty.

The 4-second window is conservative. For a narrow single-door entry you could tighten it to
2 seconds. The risk of widening it is merging two different people who entered in quick
succession — that's worse than not merging, so I left it conservative.

The side benefit of the two-camera design: group entries and partial occlusions on one angle
are caught by the other. The secondary camera isn't just a dedup tool, it's also a coverage
backup.

Evidence it's working in the screenshots: `CAM_ENTRY_02: 0 raw events` — the secondary camera
produced zero independent events. That's the correct behaviour.

---

## Layout-Independent Design

Early in the project I made a rule: if adding STORE_003 requires changing a Python file,
something is wrong.

The reason is practical. A system that works for exactly two specific stores isn't a retail
analytics platform — it's a two-store script. The whole point of building this system is that
it generalises.

Every store-specific value is in config:

`config/stores.yaml` — camera IDs and filenames, camera roles, raw video directory, layout
file path, and all the timing windows (dedup window, re-entry window, POS correlation window,
stale feed threshold).

`config/layouts/{STORE_ID}.layout.json` — zone polygon coordinates in camera pixel space,
entry line coordinates and inbound direction, SKU zone labels, camera coverage mapping,
and store open/close hours.

The pipeline loads them at runtime using `store_id` as the key:

```python
stores = yaml.safe_load(open("config/stores.yaml"))["stores"]
store_cfg = stores[store_id]
layout = json.load(open(store_cfg["layout_file"]))
```

The code has no idea whether it's processing STORE_001 or STORE_042. It just loads the right
config and runs.

To add STORE_003 in practice: add one block in `stores.yaml`, create a layout JSON (I included
STORE_001 as a template to copy), run `tools/calibrate_zones.py` to draw the polygons on the
actual camera frames, drop the videos in the right directory. That's it. I tested this
reasoning during development and verified it while writing the merge rules — if adding a store
requires a code change, that's a compliance failure.

---

## POS Correlation Heuristic

This was the decision I was least satisfied with, but I think it's also the most honest one.

There is no clean way to match a POS transaction to a specific visitor when the CCTV and POS
systems share no common identifier. No loyalty card, no face recognition, no basket scan
event. The only linking signals are: same store, overlapping time, and visitor was near the
billing counter.

The heuristic: for each POS transaction, find the most recent unmatched `BILLING_QUEUE_JOIN`
event in the same store within 5 minutes before the transaction timestamp. That session gets
credited as a conversion.

5 minutes covers most real checkout interactions (scan items, pay, get receipt) while being
tight enough not to credit someone who was near the billing area 20 minutes earlier.

If multiple sessions match (several people at the counter simultaneously), I pick the one
whose last billing event is closest in time to the transaction. That's a reasonable tiebreak
but it's not perfect — in a high-traffic peak hour it occasionally credits the wrong session.

I'm documenting this limitation clearly rather than glossing over it. For aggregate store-level
conversion rates over a full day, individual mis-attributions are a small percentage of total
transactions and roughly cancel out. For individual session-level attribution they're a real
limitation.

The correct fix requires a shared identifier: a basket scan event, an NFC tap, a loyalty card
swipe. None of those are available from CCTV alone. This is documented as a known limitation
in DESIGN.md rather than as a feature.

The window is configurable per store in `config/stores.yaml → billing_pos_window_minutes`.
Stores with long checkout queues (lots of complex basket interactions) may need 7–10 minutes.
Stores with simple self-checkout setups could tighten it to 3 minutes.