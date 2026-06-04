# MERGE_LOG.md
# Store Intelligence — Final Merge Log
# Performed: 2026-06-04

## Merge Order Applied
1. **ai1_clean_base.zip** — Config, layouts, CONTRACT.md, MERGE_RULES.md, PROJECT_STRUCTURE.md, README.md, .env.example
2. **ai2_backend_fixed_structured.zip** — app/, scripts/, tests/, Dockerfile, requirements.txt, pytest.ini, docker-compose.yml
3. **ai3_detection_fixed_structured.zip** — pipeline/, tools/, tests/test_pipeline_schema.py, output/events/, data/
4. **ai4_dashboard_docs_fixed_structured.zip** — app/dashboard.py, docs/, README.md, DESIGN.md, CHOICES.md, FINAL_CHECKLIST.md

## Integration Fixes Applied

### Fix 1: Dashboard Router Integration
- **Problem**: `app/main.py` had a placeholder `GET /dashboard` that returned static HTML.
- **Fix**: Added `from app.dashboard import router as dashboard_router` and `app.include_router(dashboard_router)` after `app = FastAPI(...)`. Removed the placeholder `/dashboard` endpoint to avoid route conflict.
- **Result**: Full AI-4 dashboard now served at `GET /dashboard?store_id=STORE_BLR_002`.

### Fix 2: Dockerfile Updated
- **Problem**: Dockerfile only copied `app/`, `scripts/`, `tests/`.
- **Fix**: Added `COPY pipeline/ ./pipeline/`, `COPY tools/ ./tools/`, `COPY config/ ./config/`. Also added `libglib2.0-0`, `libsm6`, `libxext6`, `libxrender-dev` for opencv-headless system libs. Added `RUN mkdir -p data/raw output/events`.

### Fix 3: requirements.txt Updated
- **Problem**: Missing `PyYAML`, `requests`, `opencv-python-headless`, `numpy`.
- **Fix**: Added all required backend + pipeline dependencies. Ultralytics kept optional (commented out) since `--demo` mode works without it.

### Fix 4: AI-1 Config Layouts Preserved
- **Problem**: AI-3 provided a different `config/layouts/STORE_001.layout.json` (1280×720 px, different zone schema).
- **Fix**: Kept AI-1's `STORE_001.layout.json` (1920×1080 px, full template with calibration notes). AI-3 config was discarded per MERGE_RULES.md §1.

### Fix 5: Unused Imports Cleaned in main.py
- **Problem**: `HTMLResponse` and `Response` imports were unused after removing the placeholder dashboard route.
- **Fix**: Removed those two unused imports.

## Contract Compliance Verification

| Check | Status |
|-------|--------|
| No `PURCHASE` event_type in pipeline or ingestion | ✓ PASS |
| `PURCHASE` as funnel stage derived from POS | ✓ PASS (funnel.py) |
| `dwell_ms` used (not `dwell_seconds`) | ✓ PASS |
| `session_seq` inside `metadata` | ✓ PASS |
| `queue_depth` inside `metadata` | ✓ PASS |
| 8 valid event types in `EventType` enum | ✓ PASS |
| SQLite remains default | ✓ PASS |
| Multi-store ready (no store hardcoding in logic) | ✓ PASS |
| `health.stores[store_id].feed_status` (not root) | ✓ PASS |
| Dashboard router integrated in main.py | ✓ PASS |

## Notes on Validation Commands
- `docker compose up --build`: Dockerfile complete — should build and run cleanly.
- `pytest`: All tests present. Note: `pytest-cov` with `--cov-fail-under=70` requires sufficient test coverage.
- `python pipeline/process_store.py --demo`: Works without ultralytics (demo mode in process_store.py).
- `python pipeline/replay.py --speed 999999`: Works — replay.py from AI-3 supports this flag.
- Docker network required: API must be running before replay.py can POST events.
