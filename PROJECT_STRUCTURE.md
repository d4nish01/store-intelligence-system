# PROJECT_STRUCTURE.md
# Store Intelligence System — Repository Structure Guide v1.1
# This document describes the repository layout, module boundaries, configuration locations, and runtime files.

---

## Full Repository Tree

```
store-intelligence/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── models.py
│   ├── database.py
│   ├── ingestion.py
│   ├── metrics.py
│   ├── funnel.py
│   ├── heatmap.py
│   ├── anomalies.py
│   ├── health.py
│   ├── logging_config.py
│   └── dashboard.py
│
├── pipeline/
│   ├── __init__.py
│   ├── detect.py
│   ├── tracker.py
│   ├── geometry.py
│   ├── staff.py
│   ├── reid.py
│   ├── emit.py
│   ├── process_store.py
│   ├── replay.py
│   └── run.sh
│
├── scripts/
│   ├── init_db.py
│   ├── load_pos.py
│   └── generate_demo_events.py
│
├── tools/
│   └── calibrate_zones.py
│
├── config/                                ← ALL config lives here. No config at root.
│   ├── stores.yaml                        ← Per-store camera mapping, windows, directories
│   ├── settings.yaml                      ← System-wide thresholds and settings
│   └── layouts/                           ← One layout JSON per store
│       ├── STORE_001.layout.json
│       └── STORE_002.layout.json
│
├── data/
│   ├── raw/
│   │   ├── store_001/
│   │   │   ├── billing_area.mp4
│   │   │   ├── entry 1.mp4
│   │   │   ├── entry 2.mp4
│   │   │   └── zone.mp4
│   │   └── store_002/
│   │       ├── billing_area.mp4
│   │       ├── entry 1.mp4
│   │       ├── entry 2.mp4
│   │       └── zone.mp4
│   ├── sample_events.jsonl
│   └── pos_transactions.csv
│
├── output/
│   └── events/                            ← Pipeline writes {store_id}_{ts}.jsonl here
│
├── tests/
│   ├── test_ingestion.py
│   ├── test_metrics.py
│   ├── test_funnel.py
│   ├── test_heatmap.py
│   ├── test_anomalies.py
│   ├── test_health.py
│   └── test_pipeline_schema.py
│
├── docs/
│   └── screenshots/
│
├── CONTRACT.md                            ← Integration contract and schema reference.
├── PROJECT_STRUCTURE.md                   ← Repository structure reference.
├── MERGE_RULES.md                         ← Merge and conflict-resolution reference.
├── DESIGN.md                              ← Architecture narrative.
├── CHOICES.md                             ← Implementation trade-offs and rationale.
├── README.md                              ← Final setup and usage guide.
├── FINAL_CHECKLIST.md                     ← Pre-submission checklist.
├── docker-compose.yml                     ← Backend/API runtime file.
├── Dockerfile                             ← Backend/API runtime file.
├── requirements.txt                       ← Backend/API runtime file.
├── pytest.ini                             ← Backend/API runtime file.
├── store_intelligence.db                  ← SQLite database file (gitignored, created at runtime)
└── .env.example                           ← Environment variable template.
```

---

## Config Directory — Critical Note for All Agents

**All configuration files live inside `config/`. Nothing at the project root.**

```
config/stores.yaml                     ← Read by the backend and detection pipeline
config/settings.yaml                   ← Read by the backend and detection pipeline
config/layouts/STORE_001.layout.json   ← Read by: pipeline (pipeline)
config/layouts/STORE_002.layout.json   ← Read by: pipeline (pipeline)
config/layouts/{STORE_ID}.layout.json  ← Pattern for future stores
```

Recommended loading pattern:
```python
import yaml, json, os

# Load stores config
with open("config/stores.yaml") as f:
    stores = yaml.safe_load(f)["stores"]

store_cfg = stores[store_id]          # Dynamic — never hardcode store_id
layout_path = store_cfg["layout_file"]  # e.g. "config/layouts/STORE_001.layout.json"

with open(layout_path) as f:
    layout = json.load(f)
```

---

## Folder and File Descriptions

---

### `/app/` — FastAPI Backend
**Purpose:** FastAPI application serving all intelligence API endpoints.
**Database:** SQLite via SQLAlchemy. File: `store_intelligence.db` at project root (gitignored).

| File               | Purpose                                                                      |
|--------------------|------------------------------------------------------------------------------|
| `__init__.py`      | Package init                                                                 |
| `main.py`          | FastAPI app factory, router registration, startup/shutdown hooks             |
| `models.py`        | Pydantic models for events, API requests, API responses. **SCHEMA FROZEN per CONTRACT.md §5.** |
| `database.py`      | SQLAlchemy engine + session factory. Default: `sqlite:///./store_intelligence.db` |
| `ingestion.py`     | `POST /events/ingest` — validation, dedup by `event_id`, batch write         |
| `metrics.py`       | `GET /stores/{id}/metrics` — conversion rate, dwell, queue depth             |
| `funnel.py`        | `GET /stores/{id}/funnel` — four-stage session funnel                        |
| `heatmap.py`       | `GET /stores/{id}/heatmap` — zone visit counts and dwell scores              |
| `anomalies.py`     | `GET /stores/{id}/anomalies` — anomaly detection against threshold configs   |
| `health.py`        | `GET /health` — service, DB status, feed staleness per store                 |
| `logging_config.py`| Structured JSON logging setup                                                |
| `dashboard.py`     | `GET /dashboard` — live HTML dashboard served by the API.    |    |

**Note on `database.py`:**
```python
# Correct default — SQLite, take-home portable
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./store_intelligence.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
```
The `connect_args` flag is required for SQLite + FastAPI threading. Do not use PostgreSQL-only
connection args as defaults.

---

### `/pipeline/` — Computer Vision Detection Pipeline
**Purpose:** Interactive utilities for per-store calibration.
**Purpose:** Processes CCTV video, runs detection + tracking, emits structured events.

| File               | Purpose                                                                      |
|--------------------|------------------------------------------------------------------------------|
| `__init__.py`      | Package init                                                                 |
| `detect.py`        | Person detection wrapper (YOLO or equivalent). Returns bounding boxes + confidence per frame |
| `tracker.py`       | Multi-object tracker (ByteTrack or equivalent). Assigns stable track IDs across frames |
| `geometry.py`      | Polygon containment, entry line crossing, direction detection. Reads all geometry from layout JSON at `config/layouts/{store_id}.layout.json` |
| `staff.py`         | Staff classification (vest colour, badge, movement heuristic)                |
| `reid.py`          | Cross-camera re-identification — produces stable `visitor_id` tokens         |
| `emit.py`          | Event serialiser: tracking output → frozen event JSON per CONTRACT.md §5. Handles `session_seq`, dedup window, re-entry logic |
| `process_store.py` | Main entry point. Accepts `--store-id` arg. Loads `config/stores.yaml` and `config/layouts/{store_id}.layout.json`. Orchestrates all cameras. |
| `replay.py`        | Reads `output/events/*.jsonl`, POSTs to `POST /events/ingest`. Used for demo and CI. |
| `run.sh`           | Shell wrapper for `process_store.py`                                         |

**Note on the pipeline:**
- `process_store.py` must accept `--store-id STORE_001` and derive ALL paths dynamically from config
- Layout file is always at: `config/layouts/{store_id}.layout.json` — never hardcoded
- Do not import from `app/` — pipeline is a separate process

---

### `/scripts/` — Utility Scripts
**Purpose:** Database initialization, data loading, and demo event generation.
**Purpose:** DB initialisation, data loading, demo generation.

| File                       | Purpose                                                           |
|----------------------------|-------------------------------------------------------------------|
| `init_db.py`               | Creates all three tables per CONTRACT.md §8. Uses SQLite default. |
| `load_pos.py`              | Imports `data/pos_transactions.csv` into `pos_transactions` table |
| `generate_demo_events.py`  | Generates synthetic events matching CONTRACT.md §5 schema for testing |

**Note on utility scripts:** `init_db.py` must use the same `DATABASE_URL` env var as `app/database.py`.
Do not hardcode a separate DB path.

---

### `/tools/` — Developer Calibration Tools
**Purpose:** Interactive utilities for per-store calibration.

| File                  | Purpose                                                                |
|-----------------------|------------------------------------------------------------------------|
| `calibrate_zones.py`  | Interactive tool to draw zone polygons and entry lines on a real video frame. Accepts `--store-id` and writes output to `config/layouts/{store_id}.layout.json`. |

---

### `/config/` — System Configuration
**Purpose:** Single source of truth for store-specific and system-wide configuration.
**Purpose:** Single source of truth for store-specific and system-wide configuration.

| File                            | Read By         | Purpose                                      |
|---------------------------------|-----------------|----------------------------------------------|
| `stores.yaml`                   | backend, pipeline      | Per-store camera mapping, directories, timing windows |
| `settings.yaml`                 | backend, pipeline      | System-wide thresholds, batch sizes, detection settings |
| `layouts/STORE_001.layout.json` | pipeline            | Zone polygons, entry lines, camera coverage for STORE_001 |
| `layouts/STORE_002.layout.json` | pipeline            | Zone polygons, entry lines, camera coverage for STORE_002 |

**Configuration rules:**
- Avoid hardcoding values that already exist in these configuration files
- To add STORE_003: add one block to `config/stores.yaml` + create `config/layouts/STORE_003.layout.json`
- Zero Python code changes required for new stores

---

### `/data/` — Input Data (Read-Only for Pipeline)
**Module:** N/A (operator provides videos; backend reads POS CSV)

| Path                        | Purpose                                                       |
|-----------------------------|---------------------------------------------------------------|
| `raw/store_001/`            | CCTV videos for Store 001 (gitignored)                        |
| `raw/store_002/`            | CCTV videos for Store 002 (gitignored)                        |
| `sample_events.jsonl`       | Pre-generated events for API testing (committed)              |
| `pos_transactions.csv`      | POS transaction data (committed or gitignored depending on sensitivity) |

---

### `/output/events/` — Pipeline Output
**Purpose:** Interactive utilities for per-store calibration. (writes); pipeline `replay.py` and backend tests (read)
**Naming convention:** `{STORE_ID}_{YYYYMMDDTHHMMSSZ}.jsonl`
Example: `STORE_001_20260303T142210Z.jsonl`

---

### `/tests/` — Test Suite
**Purpose:** Database initialization, data loading, and demo event generation. (all except `test_pipeline_schema.py`), pipeline (`test_pipeline_schema.py`)

| File                       | Module | Coverage                                              |
|----------------------------|-------|-------------------------------------------------------|
| `test_ingestion.py`        | backend  | Batch ingest, dedup, partial failure, schema rejection |
| `test_metrics.py`          | backend  | Conversion rate, staff exclusion, zero-visitor safety  |
| `test_funnel.py`           | backend  | Stage counts, re-entry dedup, staff exclusion          |
| `test_heatmap.py`          | backend  | Score normalisation, LOW confidence flag               |
| `test_anomalies.py`        | backend  | All four anomaly types at correct thresholds           |
| `test_health.py`           | backend  | DB connected status, LIVE vs STALE_FEED                |
| `test_pipeline_schema.py`  | pipeline  | All emitted events match CONTRACT.md §5 schema exactly |

---

### Root-Level Files

| File                   | Module | Purpose                                                    |
|------------------------|-------|------------------------------------------------------------|
| `CONTRACT.md`          | Reference | Integration contract and schema reference.                                           |
| `PROJECT_STRUCTURE.md` | Reference | Repository structure guide.                                                 |
| `MERGE_RULES.md`       | Reference | Conflict-resolution and merge guidance.                                 |
| `DESIGN.md`            | Docs | Architecture decisions narrative.                          |
| `CHOICES.md`           | Docs | Heuristic trade-offs and justifications.                   |
| `README.md`            | Docs | Final setup and usage guide.                      |
| `FINAL_CHECKLIST.md`   | Docs | Pre-submission verification checklist.                     |
| `docker-compose.yml`   | Runtime | Compose file for API service.        |
| `Dockerfile`           | Runtime | API container image.                                       |
| `requirements.txt`     | Runtime | Python dependencies.                                       |
| `pytest.ini`           | Tests | Pytest configuration.                                      |
| `store_intelligence.db`| Runtime | SQLite DB file. Created by `scripts/init_db.py`. Gitignored. |
| `.env.example`         | Runtime | Environment variable template.          |

---

*PROJECT_STRUCTURE.md — Version 1.1.0*
*Change from v1.0: Explicit config path guidance; SQLite DB file noted; code pattern for loading config added.*
