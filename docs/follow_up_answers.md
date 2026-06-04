# Reviewer Follow-Up Q&A

These are answers to questions I expect reviewers to ask based on the design choices I made.
Writing them down also helped me think through edge cases I hadn't fully considered during the build.

---

## Q1: Why did you choose YOLO for detection?

Honestly, the decision wasn't complicated. For person detection on fixed-camera CCTV footage
you don't need a state-of-the-art model — you need something that reliably fires on upright
human silhouettes from an overhead or angled view, runs fast enough to process video in
reasonable time, and doesn't require a complex setup to evaluate.

YOLO hits all three. `pip install ultralytics`, point it at a video, you get bounding boxes.
I've used it before and the documentation is good. For the specific task of detecting people
(not fine-grained re-id, just "is there a person here and where") it's more than sufficient.

I looked at RT-DETR — it does get better accuracy on dense crowd benchmarks, and I noted it
in CHOICES.md as the production upgrade path. But it's noticeably heavier and the setup is
more involved. For a take-home project where the reviewer needs to run it locally, YOLO is
just the right call.

I kept the detector in its own file (`pipeline/detect.py`) specifically so I could swap it
out later without touching anything else. That felt like the honest tradeoff: practical now,
upgradeable later.

I also looked at VLMs briefly for zone classification. That idea didn't make it past the
first few minutes of thinking — too slow, too expensive, non-deterministic. Polygon containment
checks are microseconds and always produce the same answer. Not even a close comparison.

---

## Q2: What happens under occlusion?

A few layers handle this, and I want to be honest about where it still breaks.

At the **tracker level**, ByteTrack keeps a "lost track" buffer — if a bounding box disappears
for a few frames (someone walks behind a shelf unit, briefly obscured) the track ID stays
alive and reattaches when the person reappears. This handles the common case and stops a 2-second
occlusion from generating a phantom EXIT + ENTRY pair.

At the **event level**, if the confidence drops because the detector is looking at a partial
silhouette, that confidence value goes on the emitted event and gets stored. I made the choice
early on not to silently drop low-confidence events — you can always filter downstream, but
you can't recover a dropped event. The backend keeps them; metric queries can apply a confidence
threshold if needed.

For the **entry cameras specifically**, the two-camera design helps here. If someone is partially
occluded from `entry 1`, `entry 2` from a different angle often catches them. That's actually
one of the real reasons I was glad the dataset had two entry cameras rather than being annoyed
by the deduplication complexity.

Where it genuinely breaks: long occlusions (someone hidden for 10+ seconds). The tracker drops
the track, a new ID gets assigned when they reappear. The re-id module tries to match
appearances across this gap but it's not perfect. Those cases show up as slightly inflated
visitor counts and lower-confidence events. I documented this in DESIGN.md as a known
limitation rather than pretending it doesn't happen.

---

## Q3: Why two entry cameras and how do you handle them?

When I first opened the dataset I expected three camera files (entry, zone, billing) based on
the problem statement. There were four — two entry cameras. My first reaction was to figure
out if this was intentional or if one of them was redundant.

After looking at the layouts it made sense: two cameras at the same threshold from slightly
different angles. The practical implication is obvious — if you treat both as independent
ENTRY event sources, every visitor gets double-counted, your footfall is 2× what it should be,
and your conversion rate is halved. That's not a subtle error, it's a fundamental one.

So the design I went with: `entry 1` is the authoritative source for all ENTRY and EXIT events.
`entry 2` is a confirmation camera only — it never independently emits an ENTRY or EXIT.

When `entry 1` detects a crossing, I check whether `entry 2` also detected one in the same
direction within `dedup_window_seconds` (4 seconds by default, configurable per store).
If it did, I take the max confidence of the two and emit one event. If only `entry 1` fired,
I emit with the original confidence. If somehow only `entry 2` fired (edge case — primary
missed it completely), I emit with a reduced confidence to signal lower certainty.

The 4-second window is conservative. For a narrow entry you could tighten it to 2 seconds.
The risk of making it too wide is accidentally merging two different people who entered in
quick succession — that's worse than not merging at all, so I erred on the side of caution.

This also means the anomaly you see in the screenshots — `CAM_ENTRY_02: 0 raw events` — is
correct. The secondary camera produced 0 independent events, which is exactly the expected
behaviour.

---

## Q4: What breaks first at 40 stores?

I thought about this a lot because the SQLite choice only makes sense if I understand where
it stops being viable.

**First thing to break: SQLite write contention.** SQLite uses file-level locking. With 40
store pipelines each flushing event batches every few seconds, you'll start seeing write
queue latency almost immediately and actual errors at higher throughput. The fix is PostgreSQL,
and because I used SQLAlchemy throughout, it's literally a connection string change in
`app/database.py`. I tested this mentally while writing the ORM models — nothing in the
business logic layer touches anything SQLite-specific.

**Second: on-demand metric computation.** Right now `GET /stores/{id}/metrics` runs aggregate
queries over the full events table every time it's called. That's fine for one store with
a few thousand events. At 40 stores each accumulating millions of events over months, those
queries get slow. The fix is background aggregation workers that pre-compute hourly summaries
and write them to a `metrics_summary` table. The live tail (last 5 minutes) stays real-time;
historical data comes from the summary.

**Third: single-process detection.** 40 stores means 40 simultaneous video processing jobs.
Even if you have GPU capacity, managing 40 Python processes gets complicated fast. The cleaner
architecture is a centralised inference server (Triton or TorchServe) that the pipeline
workers talk to over gRPC. Frame batches go in, bounding boxes come out — shared GPU, thin
client processes.

**Fourth: the FastAPI process itself.** It'll get saturated under concurrent load from 40
stores' worth of dashboards and pipelines. Horizontal scaling behind a load balancer handles
this, but you need to move the SQLite file out first.

The good news is none of these infrastructure changes touch the business logic. The config-driven,
store-agnostic design means the application layer is already written for 40 stores — the
infrastructure just needs to catch up.

---

## Q5: Why not use a VLM for zone classification?

I genuinely considered it for about 10 minutes at the start of the project. The idea was:
instead of drawing polygons on a camera frame, you just ask a VLM "what zone is this person
standing in?" and let it use the visual context of the shelves around them.

The problems became obvious pretty quickly:

Speed is the killer. A VLM inference call is hundreds of milliseconds. At 30fps with even
3-4 tracked people per frame, you're looking at 10+ seconds of inference per second of video.
That's not workable.

Cost matters too. If you're using an API-based VLM, processing hours of CCTV footage costs
real money per call. And even self-hosted VLMs need significant GPU memory.

Non-determinism is actually what killed it for me more than performance. If the same frame
crop can produce different zone labels on different calls, your metrics aren't reproducible.
"Zone A had 47 dwell events" needs to mean the same thing every time you run the query.
A polygon check always gives the same answer.

Where I do think VLMs are genuinely useful in this project: during the initial setup of
`tools/calibrate_zones.py`. An operator uploads the store layout image, a VLM helps them
identify what the zones are by looking at the shelving, and suggests initial polygon
coordinates. That's an assisted calibration workflow — offline, occasional, tolerates latency.
I flagged that as future work in DESIGN.md.

---

## Q6: How is POS matched to a visitor without a customer ID?

This was the trickiest design problem in the whole project because there's no clean solution —
there's no shared identifier between the CCTV system and the POS system. No loyalty card ID,
no face recognition, nothing. Just timestamps and store ID.

The approach I went with: for each POS transaction, find the most recent unmatched
`BILLING_QUEUE_JOIN` event in the same store that happened within 5 minutes before the
transaction timestamp. That session gets credited as a conversion.

5 minutes is the window from the contract. It covers most real checkout interactions (scan
items, pay, get receipt) while being tight enough to not accidentally credit someone who was
at the billing area 20 minutes earlier.

If multiple sessions match (high queue depth — several people at the counter simultaneously),
I take the one whose last billing event is closest to the transaction timestamp. That's a
reasonable tiebreak but it's not perfect.

I want to be upfront about where this breaks: in high-traffic periods with multiple simultaneous
checkouts, the attribution is noisy. Two people finish checkout at the same time, one
transaction matches the wrong session. For aggregate store-level conversion rates over a full
day, these errors are small and roughly cancel out. For individual session-level attribution
they're real. I documented this clearly in CHOICES.md rather than pretending the heuristic
is more reliable than it is.

The correct fix requires a shared identifier — a basket scan event, an NFC tap, a loyalty
card swipe. None of those are available from CCTV alone.

---

## Q7: How are staff excluded?

Staff events are stored with `is_staff = True` and then excluded from every metric query
with a `WHERE is_staff = False` filter. The storage decision was deliberate — I didn't want
to throw away staff events because they're useful for debugging the heuristic and for
understanding store operational patterns. The exclusion happens at query time, not at ingest.

The detection heuristic in `pipeline/staff.py` has two signals:

Vest colour: the upper-body region of the bounding box is checked in HSV colour space. If
the dominant colour matches the configured staff uniform range (set in `stores.yaml`), the
track is flagged. This works well when staff wear distinctive clothing.

Movement pattern: staff move through the full store continuously and purposefully. Customers
browse and dwell. A track that covers a large spatial area at consistent speed over multiple
minutes without pausing at products is likely staff. I combined this with the colour check
to reduce false positives.

The honest limitation: stores where staff wear plain civilian clothes will have lower
classification accuracy. The movement pattern helps but it's not a reliable signal on its
own for shorter clips. If a staff member stands at a fixed location restocking for a long
stretch, they might look like a very engaged customer. These edge cases are logged with lower
confidence rather than silently misclassified.

---

## Q8: How is re-entry handled?

When a visitor exits and re-enters within `reentry_window_minutes` (default 30 minutes,
configurable per store), the system emits a `REENTRY` event instead of a second `ENTRY` event.
The session continues, `session_seq` increments, and the unique visitor count doesn't go up.

The reason this matters: without re-entry logic, a customer who steps outside to take a call
and comes back gets counted as two unique visitors. That inflates the denominator of your
conversion rate and makes the funnel show false double-entries. In a store with regular
re-entry patterns (near a car park entrance, for example), this would meaningfully corrupt
your metrics.

The identity manager in `pipeline/reid.py` handles the matching. When a new ENTRY is detected,
it checks whether there's a recent EXIT for the same `visitor_id` within the window. If yes:
REENTRY. If no (or if the identity was lost and re-id failed to match): new ENTRY.

Where it breaks: if the re-id module fails to reunify the visitor's identity between the exit
and re-entry — for example if their appearance changed significantly or the exit was on a
different camera — they'll be treated as a new visitor. This is a known limitation, documented
in DESIGN.md. The 30-minute window is long enough to cover most realistic re-entry scenarios
while being short enough that a genuine new visit the next day isn't accidentally merged.

---

## Q9: How is the system layout-independent?

No store ID, zone name, camera filename, or polygon coordinate appears in any Python file.
Every store-specific value is in config.

`config/stores.yaml` has one block per store: camera IDs, their filenames, their roles,
the path to that store's layout JSON, and all the timing windows (dedup, re-entry, POS
correlation, stale feed threshold).

`config/layouts/{STORE_ID}.layout.json` has the zone polygons in camera pixel coordinates,
the entry line segment and inbound direction, the SKU zone labels, and the open/close hours.

The pipeline loads them at runtime:

```python
stores = yaml.safe_load(open("config/stores.yaml"))["stores"]
store_cfg = stores[store_id]
layout = json.load(open(store_cfg["layout_file"]))
```

`store_id` comes from the CLI argument. The code never knows whether it's running for STORE_001
or STORE_042 — it just loads the right config and processes accordingly.

To add STORE_003: one new block in `stores.yaml`, one new layout JSON, run
`tools/calibrate_zones.py` to draw the polygons on the actual camera frames. No Python
changes. I tested this mentally during development and also checked it explicitly when writing
the MERGE_RULES — if adding a store requires a code change, something is wrong.

---

## Q10: What if detection is imperfect?

Detection is always imperfect. I designed for that from the start rather than assuming clean
input.

At the **event level**: confidence is preserved on every event. Nothing is dropped because the
confidence is low. You can always filter later; you can't recover a dropped event. This means
the backend occasionally stores events with 0.3 confidence from a half-occluded detection —
that's the right behaviour. Metric queries filter by confidence at read time if needed.

At the **session level**: a visitor who gets their track split into two (due to a long
occlusion or an ID switch) shows up as two sessions with low event counts. These sessions
typically don't reach the billing stage and don't meaningfully affect conversion rate. They
do slightly inflate unique visitor counts, which is a known limitation.

At the **ingest level**: the API validates every event against the Pydantic schema. Malformed
events from a buggy pipeline run get rejected with a structured error, not silently stored.
Idempotency on `event_id` means replaying a batch twice doesn't double-count.

The overall design philosophy is: degrade gracefully. A bad frame produces a low-confidence
event. A bad clip produces some fragmented sessions. A bad pipeline run produces rejected
events with clear error messages. None of these crash the API or corrupt the aggregate metrics
by more than a few percent.

The bigger risk is systematic errors — like the staff classifier misclassifying a staff member
as a customer for the whole day. That's why staff events are stored with `is_staff` rather
than just dropped, and why the confidence field is preserved so you can audit after the fact.