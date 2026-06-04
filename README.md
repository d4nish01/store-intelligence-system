# Store Intelligence

> **Converting raw CCTV footage and POS records into offline store analytics — so Apex Retail's physical stores are no longer a blind spot.**

---

## Business Problem

Apex Retail has mature online analytics but zero visibility into what happens inside physical stores. Questions like "How many people entered today?", "Which product zones drove engagement?", "What fraction of visitors converted to a purchase?", and "Where are people abandoning the billing queue?" are currently unanswerable.

This system bridges that gap. It turns anonymous CCTV footage and POS transaction logs into a structured event stream, a queryable Intelligence API, live metrics, a conversion funnel, zone heatmaps, anomaly detection, and a real-time dashboard — all containerised and ready to run in minutes.

**North Star Metric:**

```
Offline Conversion Rate = Visitors who completed a purchase / Total unique visitors in session window
```

Every component in this system is designed to make that number measurable, explainable, and improvable.

---

## What the System Does

| Layer | Responsibility |
|---|---|
| **Detection pipeline** | Runs a YOLO-based person detector on CCTV clips. Tracks individuals across frames. Emits structured behavioral events. |
| **Event schema** | Eight canonical event types (ENTRY, EXIT, ZONE_ENTER, ZONE_EXIT, ZONE_DWELL, BILLING_QUEUE_JOIN, BILLING_QUEUE_ABANDON, REENTRY). No proprietary formats. |
| **Intelligence API** | FastAPI service. Accepts event ingestion, serves metrics, funnel, heatmap, anomalies, and health — one endpoint per concern. |
| **POS correlation** | Matches billing-zone sessions against POS transactions by store and time window to infer conversions. No customer ID required. |
| **Dashboard** | Live HTML dashboard at `/dashboard`. Polls all endpoints every 2 seconds. Feed status indicator, anomaly cards, conversion rate. |
| **Replay tool** | Injects pre-recorded or generated events into the API at configurable speed for testing and demo. |
| **Docker** | Single `docker compose up --build` starts API + database. Nothing to install beyond Docker. |

---

## Architecture Overview

```
CCTV Clips (mp4)
     │
     ▼
pipeline/process_store.py
  ├── detect.py        ← YOLO-based person detector (demo fallback)
  ├── tracker.py       ← Multi-object tracker (ByteTrack / centroid fallback)
  ├── geometry.py      ← Polygon zone assignment from layout JSON
  ├── staff.py         ← Staff heuristic (badge colour / re-entry pattern)
  ├── reid.py          ← Short-term identity manager (re-entry dedup)
  └── emit.py          ← Validates & emits events to JSONL or API
     │
     ▼ events.jsonl / POST /events/ingest
     │
     ▼
FastAPI Intelligence API
  ├── POST /events/ingest          ← Idempotent batch ingestion
  ├── GET  /stores/{id}/metrics    ← Conversion, queue, abandonment, …
  ├── GET  /stores/{id}/funnel     ← Entry → Zone → Queue → Purchase
  ├── GET  /stores/{id}/heatmap    ← Zone dwell heat by session
  ├── GET  /stores/{id}/anomalies  ← Rule-based anomaly log
  └── GET  /health                 ← DB + per-store feed status
     │
     ▼
app/dashboard.py → GET /dashboard?store_id=…
```

---

## Layout-Independent Multi-Store Design

The system is **layout-independent and multi-store ready**. Store-specific details — camera roles, entry lines, billing zones, product-zone polygons, open hours, and deduplication windows — are loaded from configuration files. The two provided stores are treated as sample inputs; additional stores can be onboarded by adding a new store block in `config/stores.yaml` and a matching layout JSON file.

```yaml
# config/stores.yaml (example)
stores:
  STORE_BLR_002:
    layout: config/layouts/STORE_BLR_002.layout.json
    open_hours: "10:00-22:00"
    timezone: "Asia/Kolkata"
    entry_cameras: [CAM_ENTRY_01, CAM_ENTRY_02]
    zone_camera:   CAM_ZONE_01
    billing_camera: CAM_BILLING_01
    dedup_window_s: 3
    reentry_window_s: 300

  STORE_MUM_001:
    layout: config/layouts/STORE_MUM_001.layout.json
    open_hours: "09:30-21:30"
    # ... and so on
```

No code changes are required to add STORE_003. Only a config entry and a layout JSON.

---

## Dataset Structure

```
data/
  raw/
    store_001/
      entry 1.mp4          ← Primary entry/exit camera
      entry 2.mp4          ← Secondary entry/exit (confirmation)
      zone.mp4             ← Main floor / product-zone camera
      billing_area.mp4     ← Billing counter / queue camera
      store 1 - layout.png ← Layout reference image
    store_002/
      entry 1.mp4
      entry 2.mp4
      zone.mp4
      billing_area.mp4
      store 2 - layout.png
  sample_events.jsonl      ← Pre-recorded events for replay
  pos_transactions.csv     ← POS data for purchase correlation
```

---

## Camera Role Mapping

| File | Role | Usage |
|---|---|---|
| `entry 1.mp4` | Primary entry/exit | All ENTRY and EXIT events originate here |
| `entry 2.mp4` | Secondary confirmation | Confirms group entry, resolves occlusion, raises single-camera confidence |
| `zone.mp4` | Product zone floor | ZONE_ENTER, ZONE_EXIT, ZONE_DWELL events |
| `billing_area.mp4` | Billing queue | BILLING_QUEUE_JOIN, BILLING_QUEUE_ABANDON events |

Camera roles are declared in `config/stores.yaml`. The pipeline reads roles from config, so the same code handles any camera layout.

---

## Two-Entry-Camera Deduplication

The dataset contains two entry cameras (`entry 1.mp4` and `entry 2.mp4`). Treating both independently would **inflate footfall** by double-counting visitors who appear in both feeds.

**Resolution strategy:**

- `entry 1` is the **primary source** of ENTRY and EXIT events.
- `entry 2` is used as **confirmation** — it raises confidence for solo entries and helps detect group entries where individuals in a cluster may be partially occluded from one angle.
- If both cameras detect an ENTRY or EXIT event for the same visitor within a configurable **2–4 second window**, the events are merged into a single visitor session.
- If only one camera detects the event (e.g. the visitor entered via the far lane), the event is still emitted but with **lower confidence**.
- The deduplication window is configurable per store in `config/stores.yaml` (`dedup_window_s`).

This approach avoids double-counting while preserving coverage quality and confidence metadata.

---

## Repository Structure

```
store-intelligence/
├── app/
│   ├── main.py              ← FastAPI app, route registration
│   ├── models.py            ← SQLAlchemy ORM models
│   ├── database.py          ← DB engine / session factory
│   ├── ingestion.py         ← POST /events/ingest handler
│   ├── metrics.py           ← GET /stores/{id}/metrics
│   ├── funnel.py            ← GET /stores/{id}/funnel
│   ├── heatmap.py           ← GET /stores/{id}/heatmap
│   ├── anomalies.py         ← GET /stores/{id}/anomalies
│   ├── health.py            ← GET /health
│   ├── dashboard.py         ← GET /dashboard (this file)
│   └── logging_config.py    ← Structured JSON logging
├── pipeline/
│   ├── process_store.py     ← Entry point: detect + emit for one store
│   ├── detect.py            ← YOLO detector wrapper (demo fallback)
│   ├── tracker.py           ← Multi-object tracker
│   ├── geometry.py          ← Polygon zone assignment
│   ├── staff.py             ← Staff heuristic filter
│   ├── reid.py              ← Re-identification / re-entry manager
│   ├── emit.py              ← Event schema validation + emit
│   └── replay.py            ← Replay JSONL events into API
├── config/
│   ├── stores.yaml          ← Per-store camera roles and config
│   └── layouts/             ← Per-store layout JSON with zone polygons
├── data/
│   ├── raw/                 ← CCTV clips (gitignored)
│   ├── sample_events.jsonl  ← Pre-recorded events
│   └── pos_transactions.csv ← POS data
├── scripts/
│   └── load_pos.py          ← POS CSV loader
├── tests/
│   ├── test_ingestion.py
│   ├── test_metrics.py
│   ├── test_funnel.py
│   ├── test_heatmap.py
│   ├── test_anomalies.py
│   ├── test_health.py
│   └── test_pipeline_schema.py
├── docs/
│   ├── screenshots/README.md
│   └── follow_up_answers.md
├── output/events/           ← Detection output (gitignored)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── pytest.ini
├── DESIGN.md
├── CHOICES.md
└── FINAL_CHECKLIST.md
```

---

## Five-Command Quickstart

```bash
# 1. Clone and enter project
git clone <repo-url>
cd store-intelligence

# 2. Build and start API + database
docker compose up --build

# 3. Load POS transactions (new terminal)
docker compose exec api python scripts/load_pos.py --file data/pos_transactions.csv

# 4. Replay sample events into the API
docker compose exec api python pipeline/replay.py \
  --events data/sample_events.jsonl \
  --api http://localhost:8000/events/ingest \
  --speed 999999

# 5. Query live metrics
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

That's it. The API is live at `http://localhost:8000`.

---

## API Endpoints

```bash
# Health (DB + per-store feed status)
curl http://localhost:8000/health

# Store metrics
curl http://localhost:8000/stores/STORE_BLR_002/metrics

# Conversion funnel
curl http://localhost:8000/stores/STORE_BLR_002/funnel

# Zone heatmap
curl http://localhost:8000/stores/STORE_BLR_002/heatmap

# Anomaly log
curl http://localhost:8000/stores/STORE_BLR_002/anomalies

# Ingest a batch of events
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '[{"event_id":"...","store_id":"STORE_BLR_002",...}]'
```

Interactive API docs: `http://localhost:8000/docs`

---

## Live Dashboard

```
http://localhost:8000/dashboard?store_id=STORE_BLR_002
```

The dashboard polls all endpoints every 2 seconds, displays conversion rate, queue depth, abandonment rate, visitor counts, latest anomaly with suggested action, and a `LIVE` / `STALE_FEED` indicator from the health endpoint.

---

## Running Detection on CCTV Clips

**Standard run (requires CCTV clips in `data/raw/`):**

```bash
python pipeline/process_store.py \
  --store-id STORE_001 \
  --input-dir data/raw/store_001 \
  --layout config/layouts/STORE_001.layout.json \
  --out output/events/STORE_001.events.jsonl
```

**Demo mode** (generates synthetic events without GPU/clips):

```bash
python pipeline/process_store.py \
  --store-id STORE_001 \
  --input-dir data/raw/store_001 \
  --layout config/layouts/STORE_001.layout.json \
  --out output/events/STORE_001.events.jsonl \
  --demo
```

**Replay generated events into the API:**

```bash
python pipeline/replay.py \
  --events output/events/STORE_001.events.jsonl \
  --api http://localhost:8000/events/ingest \
  --speed 999999
```

> **Note on the CCTV clips:** The dataset clips are not committed to the repository. Place them in `data/raw/store_001/` and `data/raw/store_002/` before running detection. Use `--demo` flag for API and dashboard testing without clips.

---

## Running Tests

```bash
# Inside Docker (recommended)
docker compose exec api pytest --cov=app --cov=pipeline

# Or locally with a virtualenv
pytest --cov=app --cov=pipeline
```

---

## Docker Reference

```bash
# Build and start
docker compose up --build

# Start in background
docker compose up -d --build

# Stop
docker compose down

# View logs
docker compose logs -f api

# Open a shell
docker compose exec api bash
```

The `data/` directory is mounted into the container, so clips and CSV files placed there are immediately available.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Port 8000 already in use` | Change the host port in `docker-compose.yml` (e.g. `"8001:8000"`) or stop the conflicting process. |
| `database is locked` | Stop any other process writing to the SQLite file. In Docker, only one container writes. |
| `ModuleNotFoundError: ultralytics` | Run `pip install ultralytics` or rebuild the Docker image. Weights are downloaded on first run. |
| `cv2 not found` | Run `pip install opencv-python-headless`. Already included in the Docker image. |
| `Layout file missing` | Ensure `config/layouts/STORE_001.layout.json` exists. The layout file maps zone polygons. |
| `Metrics return zeros` | No events have been ingested yet. Run the replay step first. |
| `STALE_FEED in health response` | The last event timestamp for the store is older than the staleness threshold. Run replay or check the detection pipeline. |
| `replay.py: Connection refused` | The API container is not running. Run `docker compose up` first. |
| `store_id not found in metrics` | The store_id in the request doesn't match any ingested events. Check spelling; default is `STORE_BLR_002`. |

---

## AI-Assisted Engineering Note

This project was built with AI-assisted engineering. AI tools were used for:

- Comparing detection model options (YOLOv8, YOLOv11, RT-DETR, MediaPipe) and selecting YOLO-based person detection as the practical default
- Generating pytest coverage scaffolding, which was then reviewed and extended with domain-specific edge cases
- Drafting schema decision rationale (e.g. why `session_seq` lives in `metadata`, why confidence is preserved rather than filtered, why there is no PURCHASE event type)
- Proposing production migration paths (PostgreSQL + Kafka) while keeping SQLite as the take-home default

All AI-suggested code and documentation was reviewed, verified against the actual implementation, and corrected where the suggestion did not match the contract.

---

## Submission Checklist

- [x] `docker compose up --build` starts the API
- [x] `POST /events/ingest` accepts batch events without 5xx
- [x] `GET /stores/STORE_BLR_002/metrics` returns valid JSON
- [x] `GET /stores/STORE_BLR_002/funnel` returns funnel steps
- [x] `GET /stores/STORE_BLR_002/heatmap` returns zone dwell data
- [x] `GET /stores/STORE_BLR_002/anomalies` returns anomaly list
- [x] `GET /health` returns DB and per-store feed status
- [x] `GET /dashboard?store_id=STORE_BLR_002` serves live dashboard
- [x] Detection pipeline with YOLO + demo fallback
- [x] Two-entry-camera deduplication documented and implemented
- [x] POS correlation for purchase inference
- [x] Staff excluded from metrics, stored for auditability
- [x] REENTRY event type handled without inflating unique visitors
- [x] All events validated against official 8-type schema
- [x] Idempotent ingestion (duplicate `event_id` does not create duplicate rows)
- [x] Tests with coverage reporting
- [x] `DESIGN.md` — non-trivial architecture document
- [x] `CHOICES.md` — engineering decision record
- [x] `FINAL_CHECKLIST.md` — submission gate
- [x] `docs/follow_up_answers.md` — reviewer Q&A
