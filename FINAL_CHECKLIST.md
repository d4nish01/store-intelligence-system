# FINAL_CHECKLIST.md — Submission Gate

Run through every item below before submitting. Do not submit with any `[ ]` unchecked unless accompanied by a documented known limitation in DESIGN.md.

---

## 1. Acceptance Gate Checklist

- [ ] `docker compose up --build` starts the API without errors
- [ ] API is reachable at `http://localhost:8000`
- [ ] README explains the detection pipeline
- [ ] `POST /events/ingest` accepts a valid event batch without 5xx
- [ ] `GET /stores/STORE_BLR_002/metrics` returns valid JSON
- [ ] `DESIGN.md` exists and is non-trivial (architecture, data flow, design decisions)
- [ ] `CHOICES.md` exists and is non-trivial (model choice, schema design, storage decision)

---

## 2. API Checklist

- [ ] `POST /events/ingest` — idempotent on `event_id`; duplicate event does not create a duplicate row
- [ ] `POST /events/ingest` — invalid `event_type` returns structured error, not 5xx
- [ ] `POST /events/ingest` — partial batch failure returns `accepted` + `rejected` counts
- [ ] `GET /stores/{store_id}/metrics` — returns `conversion_rate`, `unique_visitors`, `purchases`, `queue_depth`, `abandonment_rate`, `total_entries`, `total_exits`
- [ ] `GET /stores/{store_id}/funnel` — returns four steps: Entry, Zone Visit, Billing Queue, Purchase (with counts and drop-off)
- [ ] `GET /stores/{store_id}/heatmap` — returns zone-level dwell aggregation
- [ ] `GET /stores/{store_id}/anomalies` — returns list with `anomaly_type`, `severity`, `detected_at`
- [ ] `GET /health` — returns `status`, `db`, and per-store `stores[store_id].feed_status`
- [ ] `GET /dashboard` — serves HTML; no 5xx
- [ ] `GET /docs` — FastAPI OpenAPI spec renders
- [ ] All endpoints return `Content-Type: application/json` (except `/dashboard`)
- [ ] Staff sessions (`is_staff=True`) are excluded from all customer metrics
- [ ] Metrics return `0` or `0.0` for stores with no events, not 500 errors

---

## 3. Detection Checklist

- [ ] `pipeline/process_store.py --demo` generates events without CCTV clips and without GPU
- [ ] Generated events pass schema validation in `emit.py`
- [ ] `event_type` values in generated events are within the 8 allowed types only
- [ ] `confidence` is between 0 and 1
- [ ] `session_seq` is inside `metadata`, not at root level
- [ ] `queue_depth` is inside `metadata`
- [ ] `sku_zone` is inside `metadata`
- [ ] `dwell_ms` is used (not `dwell_seconds`)
- [ ] No `PURCHASE` event type is emitted by the pipeline
- [ ] Two-entry-camera deduplication is active: `entry 1` is primary, `entry 2` is confirmation
- [ ] Staff events are stored with `is_staff=True` and excluded from customer metrics
- [ ] REENTRY events reuse the same `visitor_id` rather than creating a new unique visitor

---

## 4. Dashboard Checklist

- [ ] `http://localhost:8000/dashboard?store_id=STORE_BLR_002` loads without error
- [ ] Dashboard updates every 2 seconds (visible countdown ticker)
- [ ] Conversion rate card displays correct value from `/metrics`
- [ ] Queue depth card updates live
- [ ] Abandonment rate card updates live
- [ ] `LIVE` / `STALE_FEED` indicator reads from `health.stores[storeId].feed_status` (not root `feed_status`)
- [ ] Latest anomaly card shows `anomaly_type`, `severity`, and suggested action
- [ ] Store ID input field is functional — changing store ID and clicking APPLY switches data
- [ ] Error banner appears when API is unreachable
- [ ] Dashboard does not depend on any CDN for core functionality
- [ ] Store ID is not hardcoded; default is `STORE_BLR_002` but any store can be entered

---

## 5. Documentation Checklist

- [ ] `README.md` — five-command quickstart works exactly as written
- [ ] `README.md` — camera role mapping explains two entry cameras
- [ ] `README.md` — two-entry deduplication explained
- [ ] `README.md` — layout-independent multi-store design explained
- [ ] `README.md` — troubleshooting section covers common failure modes
- [ ] `README.md` — AI-assisted engineering note included
- [ ] `DESIGN.md` — AI-Assisted Decisions section has three concrete examples
- [ ] `DESIGN.md` — architecture text diagram included
- [ ] `DESIGN.md` — POS correlation limitation documented
- [ ] `DESIGN.md` — 40-store migration path documented
- [ ] `CHOICES.md` — Decision 1: Model choice (YOLO vs RT-DETR vs others)
- [ ] `CHOICES.md` — Decision 2: Event schema (no PURCHASE type, schema rationale)
- [ ] `CHOICES.md` — Decision 3: Storage (SQLite vs Postgres, migration path)
- [ ] `CHOICES.md` — Dataset-specific: two entry cameras
- [ ] `CHOICES.md` — Layout-independent design
- [ ] `docs/follow_up_answers.md` — 10 likely reviewer questions answered
- [ ] `docs/screenshots/README.md` — screenshot instructions included
- [ ] No documentation claims a feature that is not implemented in code
- [ ] No `PURCHASE` event type appears anywhere in documentation or code

---

## 6. Testing Checklist

- [ ] `pytest` runs without import errors
- [ ] `test_ingestion.py` — tests valid batch, duplicate idempotency, invalid event_type rejection
- [ ] `test_metrics.py` — tests staff exclusion, zero-visitor store, conversion rate
- [ ] `test_funnel.py` — tests funnel steps and drop-off logic
- [ ] `test_heatmap.py` — tests zone aggregation
- [ ] `test_anomalies.py` — tests anomaly detection rules
- [ ] `test_health.py` — tests feed_status (LIVE vs STALE)
- [ ] `test_pipeline_schema.py` — tests event schema validation
- [ ] All tests pass: `pytest --cov=app --cov=pipeline`
- [ ] Coverage report generated
- [ ] Each test file has a specific AI prompt block (not identical boilerplate)

---

## 7. Docker Checklist

- [ ] `Dockerfile` builds without error
- [ ] `docker compose up --build` starts in < 2 minutes on a standard laptop
- [ ] `docker compose exec api pytest` runs tests inside the container
- [ ] `docker compose exec api python pipeline/replay.py ...` works inside the container
- [ ] `data/` directory is mounted (clips and CSV accessible inside container)
- [ ] Container does not run as root (optional but preferred)
- [ ] Health check in `docker-compose.yml` reports healthy

---

## 8. Edge-Case Checklist

- [ ] Store with zero events returns `0` metrics, not 500
- [ ] Store with only staff events returns `0` customer metrics
- [ ] Repeated REENTRY events do not inflate unique visitor count
- [ ] Duplicate `event_id` in batch returns success (idempotent), not error
- [ ] `event_type: "PURCHASE"` is rejected with a structured error
- [ ] `confidence: 1.5` is rejected with validation error
- [ ] `dwell_ms: null` is accepted (not all events have dwell)
- [ ] `session_seq` at root level is rejected or normalised into metadata
- [ ] `queue_depth` at root level is rejected or normalised into metadata

---

## 9. Reviewer Follow-Up Readiness

- [ ] Can explain YOLO choice vs RT-DETR in 30 seconds
- [ ] Can explain why there is no PURCHASE event type
- [ ] Can explain two-entry-camera deduplication algorithm
- [ ] Can explain POS correlation method and its limitations
- [ ] Can explain what breaks first at 40 stores and the migration path
- [ ] Can explain why VLM was not used for zone classification
- [ ] Can explain how staff are excluded from metrics
- [ ] Can explain how REENTRY is handled without inflating unique visitors
- [ ] Can explain confidence score preservation design choice
- [ ] Can explain the layout-independent multi-store design
- [ ] `docs/follow_up_answers.md` is complete and reviewed

---

## 10. Final Commands — Run Before Submission

Run these in order and confirm each succeeds:

```bash
# 1. Clean build
docker compose down
docker compose up --build

# 2. Health check
curl http://localhost:8000/health

# 3. Replay sample events
python pipeline/replay.py \
  --events data/sample_events.jsonl \
  --api http://localhost:8000/events/ingest \
  --speed 999999

# 4. Verify metrics
curl http://localhost:8000/stores/STORE_BLR_002/metrics

# 5. Verify funnel
curl http://localhost:8000/stores/STORE_BLR_002/funnel

# 6. Verify heatmap
curl http://localhost:8000/stores/STORE_BLR_002/heatmap

# 7. Verify anomalies
curl http://localhost:8000/stores/STORE_BLR_002/anomalies

# 8. Run tests with coverage
docker compose exec api pytest --cov=app --cov=pipeline

# 9. Demo detection (no clips required)
python pipeline/process_store.py \
  --store-id STORE_001 \
  --input-dir data/raw/store_001 \
  --layout config/layouts/STORE_001.layout.json \
  --out output/events/STORE_001.events.jsonl \
  --demo

# 10. Replay generated detection events
python pipeline/replay.py \
  --events output/events/STORE_001.events.jsonl \
  --api http://localhost:8000/events/ingest \
  --speed 999999

# 11. Open dashboard in browser
# http://localhost:8000/dashboard?store_id=STORE_BLR_002
```

All commands must complete without 5xx errors before submission.

---

*This checklist should be reviewed by the submitter and initialled/committed to the repo before the final push.*
