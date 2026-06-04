# CONTRACT.md
# Store Intelligence System — Integration Contract v1.1
# This document defines the integration contract for the detection pipeline, backend API, dashboard, data schema, and runtime behavior.
# Any schema, endpoint, or configuration change should be reviewed and versioned before submission.

---

## 1. PROJECT GOAL

Convert raw anonymised CCTV footage + POS transactions into structured retail analytics:
- Customer visit counts, conversion rate, dwell time
- Funnel analysis (Entry → Zone → Billing → Purchase)
- Zone heatmaps and anomaly detection
- FastAPI intelligence API + live dashboard

**North Star Metric:**
```
Offline Store Conversion Rate = Visitors who completed purchase / Total unique visitors in session window
```
Every architectural decision must support this metric.

---

## 2. LAYOUT-INDEPENDENT MULTI-STORE DESIGN

### MANDATORY RULE
The system MUST work for any store by supplying:
- `store_id`
- A block in `config/stores.yaml`
- `config/layouts/{store_id}.layout.json`
- Camera videos in the correct directory
- POS transactions CSV

### FORBIDDEN
- Hardcoding `STORE_001` or `STORE_002` anywhere in application code
- Hardcoding camera filenames in pipeline or backend code
- Hardcoding zone names, polygons, or thresholds in Python code

### CORRECT PATTERN
All store-specific data must be loaded from:
```
config/stores.yaml                        → per-store camera mapping, windows, directories
config/layouts/{store_id}.layout.json     → polygons, entry lines, zone labels
```

To add `STORE_003`: add one block in `config/stores.yaml` + one layout file at
`config/layouts/STORE_003.layout.json`. Zero application code changes required.

---

## 3. DATASET STRUCTURE

Each store dataset contains exactly:
```
billing_area.mp4       → Billing counter / queue area camera
entry 1.mp4            → Primary entry/exit camera
entry 2.mp4            → Secondary entry/exit confirmation camera
zone.mp4               → Main floor / product-zone camera
{store_id}-layout.png  → Store layout reference image
```

Mapped to `data/raw/{store_id_lowercase}/` directory.

---

## 4. CAMERA ROLE MAPPING

| Role Constant            | File              | Purpose                                      |
|--------------------------|-------------------|----------------------------------------------|
| `entry_exit_primary`     | entry 1.mp4       | Authoritative ENTRY/EXIT source              |
| `entry_exit_secondary`   | entry 2.mp4       | Confirmation only; deduplicated against primary |
| `billing`                | billing_area.mp4  | Queue depth, billing events                  |
| `main_floor_zone`        | zone.mp4          | Zone enter/exit/dwell events                 |

Camera IDs (e.g. `CAM_ENTRY_01`) are defined in `config/stores.yaml`, not in code.
Role strings above are frozen and must not be changed.

---

## 5. EVENT SCHEMA (FROZEN)

All events produced by the pipeline and accepted by the API MUST conform exactly to this schema.
Field names, types, and required/optional status are frozen. No module may alter them.

```json
{
  "event_id":   "uuid-v4",
  "store_id":   "STORE_BLR_002",
  "camera_id":  "CAM_ENTRY_01",
  "visitor_id": "VIS_c8a2f1",
  "event_type": "ZONE_DWELL",
  "timestamp":  "2026-03-03T14:22:10Z",
  "zone_id":    "SKINCARE",
  "dwell_ms":   8400,
  "is_staff":   false,
  "confidence": 0.91,
  "metadata": {
    "queue_depth":  null,
    "sku_zone":     "MOISTURISER",
    "session_seq":  5
  }
}
```

### Field Rules

| Field                   | Rule                                                                                    |
|-------------------------|-----------------------------------------------------------------------------------------|
| `event_id`              | UUID v4. Must be unique globally. Used for idempotent ingest.                           |
| `store_id`              | Dynamic from config. Never hardcoded.                                                   |
| `camera_id`             | From `config/stores.yaml` camera block.                                                 |
| `visitor_id`            | Session/visitor token. Stable across a single visit session.                            |
| `event_type`            | Must be one of the frozen event type strings (Section 6).                               |
| `timestamp`             | ISO-8601 UTC. Format: `YYYY-MM-DDTHH:MM:SSZ`                                           |
| `zone_id`               | `null` for ENTRY, EXIT, REENTRY events. Required for zone/billing events.               |
| `dwell_ms`              | `0` for instantaneous events (ENTRY, EXIT, ZONE_ENTER, ZONE_EXIT, REENTRY).            |
| `is_staff`              | Always present. `true` or `false`. Never `null`.                                        |
| `confidence`            | Always present. Float `0.0–1.0`. Never `null`.                                          |
| `metadata.queue_depth`  | Required (non-null integer) for BILLING_QUEUE_JOIN and BILLING_QUEUE_ABANDON.          |
| `metadata.sku_zone`     | From layout zone label if available. `null` otherwise.                                  |
| `metadata.session_seq`  | Integer ≥ 1. Starts at 1 per session. Monotonically increasing within a session.       |

### Low-Confidence Events
Events with `confidence` below `detection.min_person_confidence` in `config/settings.yaml` MUST NOT be silently dropped.
They MUST be emitted with the actual low confidence value and stored in the database.
They are excluded from metrics at query time, not at ingest time.

---

## 6. EVENT TYPES (FROZEN)

Only these eight strings are valid values for `event_type`. Any other string MUST be rejected at ingest.

### `ENTRY`
Visitor crosses entry threshold inbound. Starts a new session unless this is a re-entry.
- `zone_id`: `null`
- `dwell_ms`: `0`
- `metadata.session_seq`: `1`

### `EXIT`
Visitor crosses entry threshold outbound. Closes or pauses the session.
- `zone_id`: `null`
- `dwell_ms`: `0`

### `ZONE_ENTER`
Visitor enters a named product/store zone.
- `zone_id`: required, non-null
- `dwell_ms`: `0`

### `ZONE_EXIT`
Visitor leaves a named product/store zone.
- `zone_id`: required, non-null
- `dwell_ms`: `0`

### `ZONE_DWELL`
Visitor remains in zone continuously for ≥30 seconds. Emitted every 30 seconds of continued
dwell (interval configurable via `config/settings.yaml → session.dwell_emit_interval_seconds`).
- `zone_id`: required, non-null
- `dwell_ms`: cumulative dwell in milliseconds at time of emit

### `BILLING_QUEUE_JOIN`
Visitor enters billing zone while `queue_depth > 0`.
- `zone_id`: billing zone id
- `metadata.queue_depth`: required, integer ≥ 1

### `BILLING_QUEUE_ABANDON`
Visitor leaves billing zone before a matching POS transaction occurs within the correlation window.
- `zone_id`: billing zone id
- `metadata.queue_depth`: depth at time of abandonment

### `REENTRY`
Same physical visitor detected crossing entry inbound after a prior EXIT event within the
re-entry window.
- Must NOT increment unique visitor count.
- Must NOT open a new funnel session.
- `zone_id`: `null`
- `dwell_ms`: `0`

---

## 7. API ENDPOINT CONTRACT (FROZEN)

Endpoint paths are frozen. backend module must implement exactly these paths with no prefix, versioning, or rename.

---

### `POST /events/ingest`

**Request body:**
```json
{
  "events": [ { ...event_schema... } ]
}
```
- Max batch size: 500 (configurable via `config/settings.yaml → ingestion.max_batch_size`)
- Validates each event against the frozen schema
- Deduplicates by `event_id` (idempotent — same event_id submitted twice = one record)
- Partial success allowed — valid events are accepted even if others in the batch are rejected
- No raw Python stack traces in error responses

**Success response:**
```json
{
  "accepted": 48,
  "duplicates": 2,
  "rejected": 1,
  "errors": [
    {
      "index": 12,
      "event_id": "optional-if-parseable",
      "field": "timestamp",
      "message": "Invalid ISO timestamp"
    }
  ]
}
```

---

### `GET /stores/{id}/metrics`

**Response:**
```json
{
  "store_id": "STORE_001",
  "unique_visitors": 0,
  "conversion_rate": 0.0,
  "avg_dwell_per_zone": {},
  "current_queue_depth": 0,
  "abandonment_rate": 0.0,
  "total_entries": 0,
  "total_exits": 0,
  "purchases": 0,
  "last_updated": "2026-03-03T14:22:10Z"
}
```

**Rules:**
- Exclude all records where `is_staff = true`
- `unique_visitors`: count distinct `visitor_id` values that have an ENTRY event, excluding REENTRY from the count
- `conversion_rate`: `purchases / unique_visitors` — return `0.0` if `unique_visitors = 0` (never divide-by-zero)
- `avg_dwell_per_zone`: dict keyed by `zone_id`, values in milliseconds
- `current_queue_depth`: latest `queue_depth` from most recent BILLING_QUEUE_JOIN event
- `abandonment_rate`: `BILLING_QUEUE_ABANDON sessions / BILLING_QUEUE_JOIN sessions` — return `0.0` if denominator is 0
- REENTRY events must not inflate `unique_visitors`
- If store has no events: return all-zero response with HTTP 200, not 404 or 500

---

### `GET /stores/{id}/funnel`

Funnel stages in order:
1. `entry` — sessions with at least one ENTRY event
2. `zone_visit` — sessions with at least one ZONE_ENTER event
3. `billing_queue` — sessions with at least one BILLING_QUEUE_JOIN event
4. `purchase` — sessions matched to a POS transaction

**Rules:**
- Session-based counting, not raw event counting
- Staff excluded (`is_staff = false`)
- REENTRY not double-counted as new session
- Each stage is a subset of the previous stage

**Response:**
```json
{
  "store_id": "STORE_001",
  "funnel": [
    { "stage": "entry",         "sessions": 120 },
    { "stage": "zone_visit",    "sessions": 98  },
    { "stage": "billing_queue", "sessions": 45  },
    { "stage": "purchase",      "sessions": 30  }
  ],
  "conversion_rate": 0.25
}
```

---

### `GET /stores/{id}/heatmap`

**Response:**
```json
{
  "store_id": "STORE_001",
  "zones": [
    {
      "zone_id": "PRODUCT_ZONE_A",
      "visit_count": 87,
      "avg_dwell_ms": 45000,
      "normalized_score": 72,
      "data_confidence": "HIGH"
    }
  ]
}
```

**Rules:**
- `data_confidence = "LOW"` if fewer than 20 customer sessions visited the zone (threshold in `config/settings.yaml → anomalies.heatmap_low_confidence_sessions`)
- `normalized_score`: 0–100, relative to the zone with max `visit_count` in that store
- Staff excluded

---

### `GET /stores/{id}/anomalies`

**Anomaly types:**

| Type                   | Trigger                                                                  | Severity   |
|------------------------|--------------------------------------------------------------------------|------------|
| `BILLING_QUEUE_SPIKE`  | `queue_depth` exceeds `anomalies.queue_spike_depth` threshold            | `WARN`     |
| `CONVERSION_DROP`      | Conversion rate drops >30% vs prior comparison window                    | `CRITICAL` |
| `DEAD_ZONE`            | No zone events for a product zone in `anomalies.dead_zone_minutes`       | `INFO`     |
| `STALE_FEED`           | No events ingested for a store in `anomalies.stale_feed_minutes`         | `WARN`     |

**Response:**
```json
{
  "store_id": "STORE_001",
  "anomalies": [
    {
      "type": "BILLING_QUEUE_SPIKE",
      "severity": "WARN",
      "message": "Queue depth reached 7 at CAM_BILLING_01",
      "suggested_action": "Open additional billing counter",
      "detected_at": "2026-03-03T14:22:10Z"
    }
  ]
}
```

---

### `GET /health`

**Response:**
```json
{
  "status": "ok",
  "database": "connected",
  "last_event_per_store": {
    "STORE_001": "2026-03-03T14:22:10Z",
    "STORE_002": null
  },
  "feed_status": {
    "STORE_001": "LIVE",
    "STORE_002": "STALE_FEED"
  },
  "version": "1.0.0",
  "uptime_seconds": 3600
}
```

---

### `GET /dashboard`

Returns HTML dashboard page. Owned and implemented by dashboard/documentation module.
backend module must provide a stub returning HTTP 501 so the app starts without dashboard/documentation module's work.

---

## 8. DATABASE SCHEMA CONTRACT

### Default Implementation: SQLite
**SQLite is the default database for take-home portability.**
backend module must implement using SQLite via SQLAlchemy.
Do not require PostgreSQL to run the project.

PostgreSQL may be used as a production migration path and should be noted in `CHOICES.md`,
but the schema must work correctly on SQLite from day one.

### Implementation-Neutral Type Mapping

| Logical Type         | SQLite Implementation         | PostgreSQL Migration Path   |
|----------------------|-------------------------------|-----------------------------|
| timestamp/datetime   | `TEXT` (ISO-8601 UTC string)  | `TIMESTAMPTZ`               |
| metadata JSON        | `TEXT` (JSON-serialised string) | `JSONB`                   |
| current timestamp    | `CURRENT_TIMESTAMP`           | `NOW()`                     |
| boolean              | `INTEGER` (0/1)               | `BOOLEAN`                   |
| primary key          | `TEXT`                        | `TEXT`                      |

backend module should use SQLAlchemy column types that map correctly to both SQLite and PostgreSQL
(e.g. `sa.Text`, `sa.DateTime`, `sa.Boolean`, `sa.Integer`).
Do not write raw `TIMESTAMPTZ` or `JSONB` DDL as mandatory implementation types.

---

### Table: `events`

```
Table: events
Columns:
  event_id       TEXT, PRIMARY KEY
  store_id       TEXT, NOT NULL
  camera_id      TEXT, NOT NULL
  visitor_id     TEXT, NOT NULL
  event_type     TEXT, NOT NULL
  timestamp      TEXT, NOT NULL   (ISO-8601 UTC string)
  zone_id        TEXT, NULLABLE
  dwell_ms       INTEGER, NOT NULL, DEFAULT 0
  is_staff       INTEGER, NOT NULL  (0 = false, 1 = true)
  confidence     REAL, NOT NULL
  metadata_json  TEXT, NULLABLE   (JSON-serialised string)
  created_at     TEXT, NOT NULL   (ISO-8601 UTC string, default current timestamp)

Indexes:
  idx_events_store_time  ON (store_id, timestamp)
  idx_events_visitor     ON (visitor_id)
  idx_events_type        ON (event_type)
```

---

### Table: `pos_transactions`

```
Table: pos_transactions
Columns:
  transaction_id    TEXT, PRIMARY KEY
  store_id          TEXT, NOT NULL
  timestamp         TEXT, NOT NULL   (ISO-8601 UTC string)
  basket_value_inr  REAL, NULLABLE

Indexes:
  idx_pos_store_time  ON (store_id, timestamp)
```

---

### Table: `visitor_sessions`

```
Table: visitor_sessions
Columns:
  session_id      TEXT, PRIMARY KEY
  store_id        TEXT, NOT NULL
  visitor_id      TEXT, NOT NULL
  first_seen      TEXT, NULLABLE   (ISO-8601 UTC string)
  last_seen       TEXT, NULLABLE   (ISO-8601 UTC string)
  has_entry       INTEGER, NOT NULL, DEFAULT 0
  has_zone_visit  INTEGER, NOT NULL, DEFAULT 0
  has_billing     INTEGER, NOT NULL, DEFAULT 0
  has_purchase    INTEGER, NOT NULL, DEFAULT 0
  is_staff        INTEGER, NOT NULL, DEFAULT 0
  reentry_count   INTEGER, NOT NULL, DEFAULT 0

Indexes:
  idx_sessions_store    ON (store_id)
  idx_sessions_visitor  ON (visitor_id)
```

**Note:** backend module may compute sessions dynamically from the `events` table or maintain
`visitor_sessions` as a materialised table. Either is acceptable provided all API
responses reflect session-level logic as defined in this contract.

---

## 9. POS CORRELATION CONTRACT

### CSV Format (frozen column names)
```
store_id,transaction_id,timestamp,basket_value_inr
STORE_BLR_002,TXN_00441,2026-03-03T14:38:12Z,1240.00
```

### Correlation Rule
A visitor session is counted as **converted** if:
1. The session includes a `BILLING_QUEUE_JOIN` or any event placing the visitor in the billing zone
2. The visitor's last billing zone event timestamp falls within **5 minutes before** a POS
   transaction `timestamp` for the same `store_id`
3. The session has not already been matched to another transaction

### Tie-breaking
If multiple sessions match a single transaction, choose the session whose last billing zone event
is most recent (closest in time before the transaction).

This is a heuristic. Limitations and trade-offs must be documented in `CHOICES.md` by dashboard/documentation module.

---

## 10. VISITOR AND SESSION LOGIC

- A **session** begins with an ENTRY event for a `visitor_id`
- A session ends when EXIT is detected, or after `reentry_window_minutes` of inactivity
- `session_seq` in metadata increments for every event within a session, starting at 1
- `visitor_id` is the cross-camera re-identification token. The pipeline (detection pipeline module) is responsible for maintaining this token consistently across cameras
- Two detections from `entry_exit_primary` and `entry_exit_secondary` within `dedup_window_seconds` for the same crossing direction = one visitor event (see Rule 13 below)

---

## 11. STAFF EXCLUSION RULE

- Staff are identified by the pipeline (vest colour, badge, movement pattern — detection pipeline module implementation detail)
- All staff events MUST be stored with `is_staff = 1` (true)
- Staff events are NOT excluded at ingest — they are stored normally
- All metric, funnel, heatmap, and anomaly queries MUST filter `WHERE is_staff = 0`
- Staff presence must be auditable in raw event queries but invisible in all computed customer metrics

---

## 12. RE-ENTRY RULE

- If a visitor (same `visitor_id`) crosses entry inbound after a prior EXIT event, and the time since EXIT is less than `reentry_window_minutes`, emit `REENTRY` event — not `ENTRY`
- Re-entry window is configurable per store in `config/stores.yaml → reentry_window_minutes` (system default: 30)
- If the gap since EXIT exceeds the re-entry window, treat as a new unique visitor and emit `ENTRY`
- `REENTRY` events must NOT increment `unique_visitors` in any metric
- `REENTRY` sessions must NOT open a new row in the conversion funnel

---

## 13. TWO-ENTRY-CAMERA DEDUPLICATION RULE

This rule prevents double-counting visitors seen by both entry cameras.

**Camera roles:**
- `CAM_ENTRY_01` → role `entry_exit_primary` — authoritative source
- `CAM_ENTRY_02` → role `entry_exit_secondary` — confirmation only

**Merge condition:**
If `entry_exit_primary` and `entry_exit_secondary` both detect a crossing in the **same direction**
within `dedup_window_seconds` (default: 4 seconds per `config/stores.yaml`), they represent the
**same physical person** and must be merged into a single event.

**Merge behaviour:**
- `camera_id` and `timestamp`: taken from the primary camera (`entry_exit_primary`)
- `confidence`: `max(confidence_primary, confidence_secondary)`
- Secondary detection: discarded (not emitted separately)

**Partial detection — secondary only:**
If only `entry_exit_secondary` sees the crossing (primary missed), emit the event with:
- `camera_id = CAM_ENTRY_02`
- `confidence = original_confidence * 0.85` (confidence penalty for unconfirmed crossing)

**Partial detection — primary only:**
If only `entry_exit_primary` sees the crossing, emit normally with no penalty.

**No overlap:**
Crossings separated by more than `dedup_window_seconds` are independent events (different people).

---

## 14. DETECTION PIPELINE RESPONSIBILITIES (detection pipeline module)

detection pipeline module owns `pipeline/`. It must:

1. Load store config from `config/stores.yaml` by `store_id` argument
2. Load layout from `config/layouts/{store_id}.layout.json`
3. Process each camera video according to its role
4. Run person detection (YOLO or equivalent) per frame
5. Track persons across frames (ByteTrack or equivalent)
6. Apply staff exclusion logic
7. Apply entry line crossing detection per layout `entry_lines`
8. Apply polygon containment for zone events per layout `zones`
9. Apply two-entry-camera deduplication rule (Section 13)
10. Apply re-entry rule (Section 12)
11. Emit events conforming exactly to the frozen event schema (Section 5)
12. Write events to `output/events/{store_id}_{timestamp}.jsonl`
13. Optionally POST events to `POST /events/ingest` in real-time

**detection pipeline module must NOT:**
- Hardcode store IDs, zone names, camera filenames, or polygon coordinates
- Modify the event schema
- Change API endpoint paths
- Depend on PostgreSQL or any database directly

---

## 15. BACKEND RESPONSIBILITIES (backend module)

backend module owns `app/`. It must:

1. Implement all frozen API endpoints (Section 7) exactly
2. Implement database schema (Section 8) using SQLite + SQLAlchemy
3. Implement POS correlation logic (Section 9)
4. Implement session logic (Section 10)
5. Apply staff exclusion in all metric queries (Section 11)
6. Apply re-entry dedup in unique visitor counts (Section 12)
7. Implement anomaly detection (Section 7, anomalies endpoint)
8. Write `scripts/init_db.py` to create all tables
9. Write `scripts/load_pos.py` to import `data/pos_transactions.csv`
10. Write `scripts/generate_demo_events.py` for testing
11. Provide a stub for `app/dashboard.py` returning HTTP 501

**backend module must NOT:**
- Require PostgreSQL to run the project
- Use PostgreSQL-specific raw DDL (`TIMESTAMPTZ`, `JSONB`, `NOW()`) as mandatory types
- Change frozen endpoint paths
- Rename database columns
- Skip staff filtering in any metric query
- Return 500 on empty-store queries

---

## 16. DASHBOARD RESPONSIBILITIES (dashboard/documentation module)

dashboard/documentation module owns `app/dashboard.py` and `docs/`. It must:

1. Implement `GET /dashboard` returning a live HTML page
2. Display real-time metrics from the API endpoints (no direct database access)
3. Show funnel visualisation
4. Show zone heatmap
5. Show anomaly alerts
6. Write `DESIGN.md`, `CHOICES.md`, `FINAL_CHECKLIST.md`
7. Complete `README.md` replacing all `[PLACEHOLDER]` sections

**dashboard/documentation module must NOT:**
- Modify `app/models.py` event schema fields
- Modify API endpoint paths or response shapes
- Modify database schema or query logic

---

## 17. TEST RESPONSIBILITIES

**backend module tests** (in `tests/`):
- `test_ingestion.py` — batch ingest, dedup by event_id, partial failure, schema rejection
- `test_metrics.py` — conversion rate, staff exclusion, zero-visitor edge case, zero-division safety
- `test_funnel.py` — funnel stage counts, re-entry not double-counted, staff excluded
- `test_heatmap.py` — score normalisation, LOW confidence flag for sparse zones
- `test_anomalies.py` — each anomaly type triggers at correct threshold
- `test_health.py` — database connected, LIVE vs STALE_FEED logic

**detection pipeline module tests:**
- `test_pipeline_schema.py` — every emitted event from the pipeline matches the frozen schema in Section 5

---

## 18. INTEGRATION RULES

1. detection pipeline module pipeline JSONL output must be valid input to `POST /events/ingest`
2. backend module backend must accept any valid event regardless of `store_id`
3. dashboard/documentation module dashboard must consume backend module API endpoints only — no direct database access
4. `pipeline/replay.py` reads `output/events/*.jsonl` and POSTs to `/events/ingest` for demo
5. All timestamps system-wide must be UTC ISO-8601 format: `YYYY-MM-DDTHH:MM:SSZ`
6. All monetary values are in INR stored as REAL/NUMERIC
7. The database is the single source of truth; the pipeline writes to it via the API only

---

## 19. WHAT OTHER MODULES MAY NOT CHANGE

The following are frozen by contract owner. backend module, detection pipeline module, and dashboard/documentation module may not alter these:

| Item                             | Defined In                        |
|----------------------------------|-----------------------------------|
| Event schema field names & types | Section 5 of this document        |
| Event type strings (8 values)    | Section 6 of this document        |
| API endpoint paths (7 paths)     | Section 7 of this document        |
| API response field names         | Section 7 of this document        |
| Database table names & columns   | Section 8 of this document        |
| POS CSV column names             | Section 9 of this document        |
| Camera role strings (4 values)   | Section 4 of this document        |
| Config file locations            | `config/stores.yaml`, `config/layouts/` |
| Two-entry-camera dedup logic     | Section 13 of this document       |
| Staff exclusion rule             | Section 11 of this document       |
| Re-entry rule                    | Section 12 of this document       |

---

## 20. EDGE CASE HANDLING (MANDATORY)

All modules must handle these without crashing:

| Case                        | Required Behaviour                                                        |
|-----------------------------|---------------------------------------------------------------------------|
| Group entry                 | Count individuals (bounding boxes), not groups.                           |
| Staff movement              | Store with `is_staff = 1`. Exclude from all metrics.                      |
| Re-entry                    | Emit `REENTRY`. Do not increment unique visitors.                         |
| Partial occlusion           | Lower confidence value. Do not drop event.                                |
| Billing queue buildup       | Track `queue_depth`. Emit anomaly if above threshold.                     |
| Billing queue abandonment   | Emit `BILLING_QUEUE_ABANDON` if no POS match within window.               |
| Empty store                 | Return all-zero metrics with HTTP 200. Never 404 or 500.                  |
| Camera overlap (entry 1+2)  | Apply dedup rule (Section 13). Single event emitted.                      |
| Zero purchases              | `conversion_rate = 0.0`. Never raise ZeroDivisionError.                   |
| All-staff footage           | Customer metrics return zero. Staff events stored with `is_staff = 1`.    |

---

*CONTRACT.md — Version 1.1.0*
*Change from v1.0: SQLite as default DB; implementation-neutral schema types; clarified config file paths.*
*Schema, endpoint, and configuration changes should be reviewed and versioned before submission.*
