# DESIGN.md
# Store Intelligence System — Architecture and Design Document
# This document describes the system architecture, implementation choices, data flow, and runtime behavior.
# Features not yet implemented are marked [NOT IMPLEMENTED] or [FUTURE WORK].

---

## 1. Overview

Store Intelligence converts raw anonymised CCTV footage and point-of-sale transaction records
into structured retail analytics. The system answers the question an online analytics platforma
cannot answer for physical stores: *who walked in, what did they look at, how long did they stay,
and did they buy?*

The output is a FastAPI service exposing structured metrics, a conversion funnel, zone heatmaps,
anomaly alerts, and a live dashboard — all derived from a stream of behavioural events emitted
by a computer vision detection pipeline.

**North Star Metric:**
```
Offline Store Conversion Rate = Visitors who completed a purchase
                                ─────────────────────────────────
                                Total unique visitors in session window
```

Every component in the system is designed to compute this metric accurately, handling staff
exclusion, visitor re-entry, camera overlap, and POS correlation.

---

## 2. System Goals

| Goal | Requirement |
|------|-------------|
| Accurate footfall | Deduplicate two entry cameras; exclude staff; handle re-entry |
| Conversion rate | Correlate billing zone events with POS transactions |
| Zone intelligence | Track dwell time per product zone per session |
| Queue management | Detect billing queue depth and abandonment |
| Anomaly alerting | Detect spikes, drops, dead zones, and stale feeds |
| Layout independence | Any store onboardable via config; no code changes |
| Take-home portability | SQLite default; Docker compose; no external dependencies |
| Production readiness | Event schema frozen; idempotent ingest; structured errors |

---

## 3. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│  INPUT LAYER                                                        │
│                                                                     │
│  data/raw/{store_id}/                                               │
│  ├── entry 1.mp4       (primary entry/exit)                        │
│  ├── entry 2.mp4       (secondary confirmation)                    │
│  ├── billing_area.mp4  (queue + checkout)                          │
│  └── zone.mp4          (product floor)                             │
│                                                                     │
│  data/pos_transactions.csv  (store_id, txn_id, timestamp, value)   │
│  config/stores.yaml         (camera roles, windows)                │
│  config/layouts/{id}.json   (polygons, entry lines, zones)         │
└────────────────────────┬────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  DETECTION PIPELINE  pipeline/                                      │
│                                                                     │
│  process_store.py  ──► detect.py   (YOLO person detection)         │
│                    ──► tracker.py  (ByteTrack multi-object)        │
│                    ──► reid.py     (cross-camera visitor_id)       │
│                    ──► staff.py    (staff classification)          │
│                    ──► geometry.py (line crossing + polygon zones) │
│                    ──► emit.py     (event serialisation)           │
│                                                                     │
│  Output: output/events/{store_id}_{ts}.jsonl                       │
└────────────────────────┬────────────────────────────────────────────┘
                         │  POST /events/ingest  (batched JSONL)
                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  FASTAPI BACKEND  app/                                              │
│                                                                     │
│  POST /events/ingest    → validate, dedup, store                   │
│  GET  /stores/{id}/metrics   → conversion rate, dwell, queues      │
│  GET  /stores/{id}/funnel    → 4-stage session funnel              │
│  GET  /stores/{id}/heatmap   → zone visit heatmap                  │
│  GET  /stores/{id}/anomalies → spike / drop / dead zone / stale    │
│  GET  /health           → DB + feed status per store               │
│  GET  /dashboard        → live HTML dashboard                      │
│                                                                     │
│  Database: store_intelligence.db  (SQLite via SQLAlchemy)          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 4. Data Flow

```
1. Operator places videos in data/raw/{store_id}/
2. Operator runs: python tools/calibrate_zones.py --store-id STORE_001
   → Draws zone polygons + entry lines on real video frame
   → Writes config/layouts/STORE_001.layout.json

3. Pipeline: python pipeline/process_store.py --store-id STORE_001
   → Loads config/stores.yaml and config/layouts/STORE_001.layout.json
   → Processes all 4 camera feeds per role
   → Emits events to output/events/STORE_001_{ts}.jsonl

4. Replay (or live streaming):
   python pipeline/replay.py --events output/events/STORE_001_*.jsonl
                              --api http://localhost:8000/events/ingest
   → POSTs event batches to the API

5. API ingests events:
   → Validates schema
   → Deduplicates by event_id
   → Writes to SQLite events table
   → Updates visitor_sessions

6. POS load (one-time):
   python scripts/load_pos.py
   → Imports data/pos_transactions.csv into pos_transactions table

7. Metrics are computed on demand from stored events + POS data
   → No pre-aggregation required at this scale
   → [FUTURE WORK] Materialised views for high-throughput production

8. Dashboard polls /stores/{id}/metrics, /funnel, /heatmap, /anomalies
   → Renders live view
```

---

## 5. Layout-Independent Multi-Store Design

The system is layout-independent and multi-store ready. Store-specific details such as camera
roles, entry lines, billing zones, product-zone polygons, open hours, and deduplication windows
are loaded from configuration files. The two provided stores are treated as sample inputs;
additional stores can be onboarded by adding a new store block in `config/stores.yaml` and a
matching layout JSON file in `config/layouts/`.

**No application code changes are needed to add a new store.**

| What varies per store      | Where it lives                                  |
|----------------------------|-------------------------------------------------|
| Camera filenames and roles | `config/stores.yaml` → `cameras` block         |
| Dedup / re-entry windows   | `config/stores.yaml` → timing fields           |
| Zone polygon coordinates   | `config/layouts/{STORE_ID}.layout.json`        |
| Entry line positions        | `config/layouts/{STORE_ID}.layout.json`        |
| SKU zone labels            | `config/layouts/{STORE_ID}.layout.json`        |
| Store open/close hours     | `config/layouts/{STORE_ID}.layout.json`        |

What does NOT vary: event schema, API paths, database schema, detection algorithm, business logic.

---

## 6. Store Configuration

`config/stores.yaml` is the per-store registry. Each store block defines:

```yaml
STORE_001:
  display_name: "Apex Retail — Store 001"
  raw_dir: "data/raw/store_001"
  layout_file: "config/layouts/STORE_001.layout.json"
  timezone: "Asia/Kolkata"
  dedup_window_seconds: 4
  reentry_window_minutes: 30
  billing_pos_window_minutes: 5
  stale_feed_minutes: 10
  cameras:
    CAM_ENTRY_01:
      filename: "entry 1.mp4"
      role: "entry_exit_primary"
    CAM_ENTRY_02:
      filename: "entry 2.mp4"
      role: "entry_exit_secondary"
    CAM_BILLING_01:
      filename: "billing_area.mp4"
      role: "billing"
    CAM_ZONE_01:
      filename: "zone.mp4"
      role: "main_floor_zone"
```

The pipeline loads this config by `store_id` argument. Camera filenames, roles, and all timing
windows come from here at runtime.

---

## 7. Layout Configuration

`config/layouts/{STORE_ID}.layout.json` defines the spatial geometry of each store:

- **zones**: polygon coordinates (in video pixel space) for each named zone, with the camera
  that covers it and the `sku_zone` label from the store layout
- **entry_lines**: line segments for ENTRY/EXIT crossing detection, with inbound direction
- **camera_coverage**: mapping of camera IDs to the zone IDs they observe

Each zone polygon is in the coordinate space of its camera. A visitor is classified as being in
a zone if their bounding box centroid falls inside the polygon.

`tools/calibrate_zones.py` provides an interactive UI to draw these polygons on a real video
frame so coordinates match the actual footage.

---

## 8. Camera Role Mapping

| Role String              | File              | Function                                            |
|--------------------------|-------------------|-----------------------------------------------------|
| `entry_exit_primary`     | entry 1.mp4       | Authoritative ENTRY and EXIT events                 |
| `entry_exit_secondary`   | entry 2.mp4       | Confirmation; merged with primary within dedup window |
| `billing`                | billing_area.mp4  | Queue depth tracking, billing zone containment       |
| `main_floor_zone`        | zone.mp4          | Zone enter/exit/dwell events for product areas       |

Role strings are frozen in CONTRACT.md §4.

---

## 9. Two-Entry-Camera Deduplication

The provided dataset includes two entry cameras covering the same physical threshold. Treating
both as independent sources would double-count every visitor.

**Algorithm:**

```
For each detected crossing on entry_exit_secondary camera:
  Look for a crossing on entry_exit_primary camera
  in the same direction (ENTRY or EXIT)
  within dedup_window_seconds (default 4s).

  If match found:
    → Emit one event using primary camera_id and primary timestamp
    → confidence = max(primary_confidence, secondary_confidence)
    → Discard secondary detection

  If no match (primary missed):
    → Emit event using secondary camera_id
    → confidence = original_confidence × 0.85  (confidence penalty)

  If only primary detected (normal case):
    → Emit event normally, no penalty
```

This handles group entry, partial occlusion, and camera angle differences without introducing
false negatives or false positives at the threshold.

The dedup window is configurable per store via `config/stores.yaml → dedup_window_seconds`.

---

## 10. Detection Pipeline

The pipeline processes each camera role independently, then coordinates results across cameras
for a given store.

```
pipeline/process_store.py
  ├── Loads config/stores.yaml[store_id]
  ├── Loads config/layouts/{store_id}.layout.json
  │
  ├── For each camera:
  │   ├── detect.py     → YOLOv8 person detection, per-frame bounding boxes + confidence
  │   ├── tracker.py    → ByteTrack, assigns track_id stable across frames
  │   ├── staff.py      → Classify track as staff or customer
  │   └── geometry.py   → Entry line crossing / polygon containment
  │
  ├── reid.py           → Cross-camera visitor_id unification
  │                        (appearance embedding similarity across CAMs)
  │
  └── emit.py           → Build frozen event JSON per CONTRACT.md §5
                          Apply dedup logic for entry cameras
                          Apply re-entry logic
                          Write to output/events/
```

**Camera processing per role:**
- `entry_exit_primary` / `secondary`: line-crossing detection → ENTRY / EXIT / REENTRY events
- `billing`: polygon containment + queue depth counting → BILLING_QUEUE_JOIN / ABANDON
- `main_floor_zone`: polygon containment per zone → ZONE_ENTER / ZONE_EXIT / ZONE_DWELL

---

## 11. Tracking and Visitor Identity

**Intra-camera tracking:** ByteTrack assigns a `track_id` per camera feed. Track IDs are stable
across frames within a single video but have no meaning across cameras.

**Cross-camera re-identification:** `reid.py` produces a `visitor_id` token that is consistent
across cameras within the same store session. The approach: appearance feature embeddings are
compared when a person exits one camera's field of view and appears in another. If similarity
exceeds a threshold within a time window, they share a `visitor_id`.

**`visitor_id` format:** `VIS_{6-char hex}` — derived from re-id embedding hash, not from
tracking IDs. This makes it stable and anonymised.

**Session continuity:** All events from one physical person's single store visit share one
`visitor_id`. `session_seq` inside `metadata` increments per event within the session.

---

## 12. Staff Detection Strategy

Staff are excluded from all customer metrics but their events are preserved for audit.

**Detection approach (implemented in `pipeline/staff.py`):**
1. Vest colour heuristic: staff wearing high-visibility or branded-colour vests are classified
   by HSV colour range on the upper body bounding box region
2. Movement pattern: staff typically traverse the full store repeatedly; customers have
   distinct entry → browse → exit trajectories
3. Manual seeding (fallback): known staff track IDs can be seeded for short clips

**Result:** `is_staff = 1` on stored events. All metric queries apply `WHERE is_staff = 0`.

**Limitation:** Imperfect in stores without staff uniforms. See CHOICES.md.

---

## 13. Event Schema Design

The event schema is frozen and identical for every store, every camera, and every event type.
See CONTRACT.md §5 for the authoritative schema.

**Key design decisions:**
- `event_id` is UUID v4 for global uniqueness and idempotent ingest
- `store_id` and `camera_id` are dynamic from config, never hardcoded
- `visitor_id` is the cross-camera session token, not a camera-local track ID
- `confidence` is always present; low-confidence events are stored, not dropped
- `is_staff` is always present; never null
- `dwell_ms` is in milliseconds (not seconds) for precision
- `metadata.session_seq` tracks event order within a session
- `metadata.queue_depth` is required for billing queue events
- `metadata.sku_zone` comes from the layout JSON zone label

**There is no `PURCHASE` event type.** Purchase is derived from POS transaction correlation
with billing zone events. This is a deliberate architectural choice: the CCTV system cannot
observe a completed transaction — only the presence of a visitor in the billing area.

---

## 14. Event Stream and Replay

**Pipeline output:** `output/events/{STORE_ID}_{YYYYMMDDTHHMMSSZ}.jsonl`
Each line is one complete JSON event conforming to the frozen schema.

**Replay script:** `pipeline/replay.py`
```bash
python pipeline/replay.py \
  --events output/events/STORE_001_*.jsonl \
  --api http://localhost:8000/events/ingest \
  --speed 999999   # 999999 = as fast as possible (no real-time throttle)
```

This replays pre-generated events into the API, enabling:
- Demo without re-running the full CV pipeline
- CI/CD integration testing
- Performance testing of the ingest endpoint

---

## 15. Backend API Design

The API is a FastAPI application. All route paths are frozen in CONTRACT.md §7.

**Design principles:**
- Stateless request handling; all state in SQLite
- Idempotent ingest: same `event_id` submitted twice = one row (dedup enforced at ingest)
- Partial success on batch ingest: valid events accepted even if others in the batch fail
- Structured error responses: no raw stack traces; `field` and `message` per rejected event
- Empty store returns zero-value metrics (HTTP 200), never 404 or 500
- Staff filtered at query time, not at ingest time (preserve audit trail)
- Zero-division safety in all rate calculations

**Pydantic models** in `app/models.py` enforce the frozen schema at the HTTP boundary.
Any event that fails Pydantic validation is rejected with a structured error.

---

## 16. Database Design

**Default: SQLite**, file at `store_intelligence.db` in the project root.
Created by `scripts/init_db.py`. Gitignored.

SQLAlchemy ORM abstracts the database engine. The schema uses engine-neutral column types
so the same models work on both SQLite and PostgreSQL without modification.

Three tables (column names frozen — see CONTRACT.md §8):

| Table               | Purpose                                               |
|---------------------|-------------------------------------------------------|
| `events`            | All ingested behavioural events, one row per event    |
| `pos_transactions`  | Imported POS records for conversion correlation       |
| `visitor_sessions`  | Session-level aggregation per visitor per store visit |

All timestamps stored as ISO-8601 UTC text strings in SQLite.
`metadata_json` stored as JSON-serialised text in SQLite; migrates to JSONB in PostgreSQL.

---

## 17. Session Logic

A **session** is one uninterrupted store visit by one physical person.

```
Session lifecycle:
  ENTRY event received
    → New session created (or REENTRY if within reentry_window_minutes)
    → session_seq = 1

  Subsequent events in same visit
    → session_seq increments
    → visitor_sessions.last_seen updated
    → has_zone_visit, has_billing flags updated

  EXIT event received
    → Session paused (not yet closed)
    → Timer starts for reentry_window_minutes

  If visitor returns within reentry_window_minutes:
    → REENTRY event emitted (not ENTRY)
    → Same session continued; reentry_count incremented
    → Unique visitor count NOT incremented

  If visitor does not return within reentry_window_minutes:
    → Session closed
    → Next detection = new session + new unique visitor
```

---

## 18. POS Correlation

Purchase attribution connects a visitor session to a POS transaction using temporal proximity.

**Algorithm:**
```
For each POS transaction T at timestamp T.ts for store S:

  Find all visitor sessions in store S where:
    - has_billing = true
    - last billing zone event timestamp is within
      billing_pos_window_minutes (default 5) BEFORE T.ts
    - session not already matched to another transaction

  If multiple sessions match:
    → Choose the one whose last billing event is most recent
      (closest in time before T.ts)

  If match found:
    → Mark session has_purchase = true
    → Session counts toward conversion numerator
```

**Limitations:**
- Heuristic: no customer identifier links CCTV to POS
- Multi-person queue: the closest-session tie-break may occasionally mis-attribute
- Documented in CHOICES.md

---

## 19. Metrics Computation

All metrics computed on demand from the `events` and `visitor_sessions` tables.

```
unique_visitors    = COUNT DISTINCT visitor_id WHERE event_type = 'ENTRY' AND is_staff = 0
                     (REENTRY events excluded from this count)

conversion_rate    = has_purchase sessions / unique_visitor sessions
                     (0.0 if unique_visitors = 0)

avg_dwell_per_zone = AVG(dwell_ms) per zone_id WHERE event_type IN ('ZONE_DWELL') AND is_staff = 0

current_queue_depth = latest metadata.queue_depth from BILLING_QUEUE_JOIN events

abandonment_rate   = BILLING_QUEUE_ABANDON sessions / BILLING_QUEUE_JOIN sessions
                     (0.0 if denominator = 0)
```

---

## 20. Funnel Computation

The conversion funnel is session-based. Each stage is a subset of the previous.

```
Stage 1 — Entry:
  Sessions with at least one ENTRY event AND is_staff = 0
  (REENTRY does not open a new funnel row)

Stage 2 — Zone Visit:
  Of above, sessions with at least one ZONE_ENTER event

Stage 3 — Billing Queue:
  Of above, sessions with at least one BILLING_QUEUE_JOIN event

Stage 4 — Purchase:
  Of above, sessions with has_purchase = true (set by POS correlation)
```

`conversion_rate` in the funnel response = Stage 4 / Stage 1.

---

## 21. Heatmap Computation

The zone heatmap shows relative visitor engagement across product zones.

```
For each zone_id in the store layout:
  visit_count    = COUNT DISTINCT visitor_id with ZONE_ENTER in this zone (is_staff = 0)
  avg_dwell_ms   = AVG dwell_ms of ZONE_DWELL events in this zone (is_staff = 0)
  normalized_score = (visit_count / max_visit_count_across_all_zones) × 100
  data_confidence = "LOW" if visit_count < heatmap_low_confidence_sessions (default 20)
                    "HIGH" otherwise
```

The `normalized_score` is 0–100 and shows which zones attract the most visitors relative to
the busiest zone. It does not represent an absolute engagement percentage.

---

## 22. Anomaly Detection

Four anomaly types are detected on demand when `GET /stores/{id}/anomalies` is called:

| Anomaly               | Detection Logic                                                    |
|-----------------------|--------------------------------------------------------------------|
| `BILLING_QUEUE_SPIKE` | Latest `queue_depth` from events > `queue_spike_depth` threshold   |
| `CONVERSION_DROP`     | Current window conversion rate < (prior window rate × (1 − drop_threshold)) |
| `DEAD_ZONE`           | A product zone with no ZONE_ENTER events in `dead_zone_minutes`    |
| `STALE_FEED`          | No events of any type for this store in `stale_feed_minutes`       |

Thresholds are centralized in `config/settings.yaml` where practical, with conservative fallbacks used to keep the take-home demo portable.

Each anomaly response includes `severity`, `message`, `suggested_action`, and `detected_at`.

---

## 23. Health and Stale Feed Detection

`GET /health` returns the service status, database connectivity, and per-store feed freshness.

```
feed_status per store:
  LIVE       → at least one event ingested within stale_feed_minutes
  STALE_FEED → no events in stale_feed_minutes window

database:
  connected  → SQLAlchemy can execute a test query
  error      → connection failed (reports error type, not stack trace)
```

This endpoint is used in integration verification (see MERGE_RULES.md Rule 11).

---

## 24. Dashboard Design

`GET /dashboard` returns a single HTML page served by FastAPI.

The dashboard polls the API endpoints on a refresh interval and renders:
- Current conversion rate (large number, prominent)
- Funnel chart (4-stage bar or Sankey)
- Zone heatmap (colour-coded grid or layout overlay)
- Active anomaly alerts (severity-badged list)
- Queue depth indicator (live)
- Last updated timestamp per store

The dashboard is a server-rendered HTML page with embedded JavaScript for polling.
It consumes only the public API endpoints — no direct database access.

Implemented in `app/dashboard.py`.

---

## 25. Production Readiness

**What is production-ready in this submission:**
- Frozen event schema and API contracts
- Idempotent ingest with structured error responses
- Staff exclusion applied consistently in all queries
- Zero-division safety in all rate computations
- Config-driven multi-store onboarding
- Structured JSON logging
- Docker Compose for containerised deployment
- Pytest test suite covering ingestion, metrics, funnel, heatmap, anomaly, health, and schema behavior

**What requires work before production at scale:**
- SQLite → PostgreSQL migration (single-file DB does not support concurrent writers at scale)
- Background aggregation workers (on-demand query computation does not scale to 40 stores)
- Redis/Kafka for pipeline stream buffering (current replay is file-based)
- Authentication on the API (no auth in take-home version)
- GPU inference server for detection pipeline (currently single-process)

---

## 26. Known Limitations

| Limitation                        | Impact                                         | Mitigation                              |
|-----------------------------------|------------------------------------------------|-----------------------------------------|
| POS correlation is heuristic      | Occasional mis-attribution in busy queues      | Closest-session tie-break; document in CHOICES.md |
| Staff detection heuristic         | May miss staff without uniforms                | Manual seed list fallback               |
| Cross-camera re-id is approximate | visitor_id may split or merge across cameras   | Confidence penalty; session aggregation absorbs some errors |
| SQLite concurrent writes          | Bottleneck under simultaneous store pipelines  | PostgreSQL migration path documented    |
| No real-time streaming            | Pipeline is batch/replay, not live frame-by-frame ingest | Kafka integration as future work |
| Polygon calibration is manual     | Requires operator time per store               | calibrate_zones.py interactive tool     |
| layout.png not parsed             | Layout image not used in detection             | Used only as operator reference         |

---

## 27. Migration Path for 40 Stores

Current architecture scales to approximately 5–10 simultaneous store pipelines on a single
server before hitting SQLite write contention and single-process detection limits.

**To scale to 40 stores:**

```
Phase 1 — Database (stores 1–10)
  Replace SQLite with PostgreSQL.
  No schema changes needed (SQLAlchemy handles this).
  Connection pool via pgbouncer.

Phase 2 — Ingest throughput (stores 10–20)
  Add Redis Streams or Kafka between pipeline and API.
  Pipeline emits to stream; background workers consume and write to DB.
  POST /events/ingest becomes a stream producer endpoint.

Phase 3 — Metrics aggregation (stores 20–40)
  Add background aggregation workers that pre-compute
  hourly/daily metrics into a summary table.
  API reads from summary table for metrics endpoints.
  On-demand computation retained for real-time queries.

Phase 4 — Detection scale-out
  GPU inference server (Triton or TorchServe) shared across stores.
  Pipeline workers are thin clients sending frames to inference server.
  One pipeline process per store, inference shared.
```

The config-driven design means the application layer requires no changes across all phases —
only the infrastructure layer scales.

**Adding STORE_003 at any phase:**
```bash
# 1. Add store block to config/stores.yaml
# 2. Create config/layouts/STORE_003.layout.json
# 3. Calibrate polygons on real footage
python tools/calibrate_zones.py --store-id STORE_003
# 4. Place videos in data/raw/store_003/
# 5. Run pipeline
python pipeline/process_store.py --store-id STORE_003
# Zero code changes at any scale phase.
```

---

## 28. AI-Assisted Engineering Decisions

This section documents places where AI-assisted engineering was used to compare options, stress-test assumptions, and improve implementation quality. Final decisions were reviewed, integrated, and validated manually.

---

### Decision 1: Detection Model Selection

**Question:** Which person detection model to use for CCTV footage processing?

**Options evaluated:**

| Model         | Speed     | Accuracy  | Deployment complexity | Notes                                  |
|---------------|-----------|-----------|-----------------------|----------------------------------------|
| YOLOv8        | Very fast | Good      | Low                   | Mature, large community, easy pip install |
| YOLOv11       | Fast      | Good+     | Low                   | Newer, minor accuracy improvement      |
| RT-DETR       | Moderate  | Very good | Moderate              | Transformer-based; heavier to deploy   |
| MediaPipe     | Fast      | Moderate  | Very low              | Designed for mobile; less accurate on CCTV overhead angles |
| VLM (CLIP etc.)| Slow    | Good for classification | High | Not designed for frame-by-frame detection |

**AI-assisted suggestion:** RT-DETR for highest accuracy; YOLOv8 for practical take-home deployment.

**Final decision:** YOLO-based detector (YOLOv8 or YOLOv11).

**Rationale:**
- CCTV person detection does not require state-of-the-art object classification accuracy; it
  requires fast, reliable bounding box detection of a single class (person)
- YOLO is significantly easier to install, run, and explain in a take-home evaluation
- ByteTrack integration is well-documented with YOLO
- The detection module (`pipeline/detect.py`) is designed as a replaceable wrapper; RT-DETR
  can be substituted without changing the rest of the pipeline

---

### Decision 2: Zone Classification Design

**Question:** How should the system determine which product zone a visitor is in?

**AI-assisted suggestion:** Use a Vision-Language Model (e.g. CLIP or GPT-4V) to classify zone content
from frame crops.

**Analysis:**
- VLMs are useful for *assisted calibration* — identifying what product category is on a shelf
  from a photograph when setting up a new store
- VLMs are not suited for *live inference* — they are slow, expensive per call, non-deterministic,
  and introduce an external API dependency

**Final decision:** Polygon-based zone containment from `config/layouts/{store_id}.layout.json`

**Rationale:**
- Deterministic: same frame produces same zone classification every time (testable)
- Explainable: polygon coordinates are auditable; the system can explain why a visitor was
  classified as being in zone X
- Cheap: a point-in-polygon check is O(n) with n = polygon vertices, negligible CPU cost
- Layout-independent: zone labels and polygons are per-store config, not trained weights
- No external dependency: no API calls, no model download required for zone classification

**VLM future use:** A VLM could accelerate the calibration step — helping an operator identify
zone labels from the layout image. This is `[FUTURE WORK]` in `tools/calibrate_zones.py`.

---

### Decision 3: Storage and Backend Architecture

**Question:** What database and messaging infrastructure to use?

**AI-assisted production suggestion:** PostgreSQL for persistence, Redis Streams for event buffering,
Kafka for high-throughput multi-store ingestion, background workers for metric aggregation.

**Analysis:**
- For a take-home hackathon evaluation, a PostgreSQL + Kafka stack requires significant
  infrastructure setup, is harder to run with a single `docker compose up`, and adds
  complexity that obscures the core analytics logic
- The evaluation criteria include correctness, completeness, and code quality — not
  infrastructure complexity

**Final decision:** SQLite via SQLAlchemy for take-home submission.

**Rationale:**
- SQLite runs with zero configuration; the database file is created by `python scripts/init_db.py`
- SQLAlchemy abstracts the engine; migrating to PostgreSQL requires one environment variable change
- No separate database container needed in Docker Compose (simplifies the evaluator's setup)
- The schema, queries, and application logic are identical on SQLite and PostgreSQL

**Migration path to production:** Documented in Section 27 above. The config-driven multi-store
design and SQLAlchemy abstraction make this a infrastructure swap, not a code rewrite.

---

*DESIGN.md — Version 1.0.0*
*For integration contract, see CONTRACT.md. For trade-off rationale, see CHOICES.md.*
