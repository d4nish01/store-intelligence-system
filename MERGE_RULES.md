# MERGE_RULES.md
# Store Intelligence System — Multi-Agent Merge Rules v1.1
# AI-1 OWNED | DO NOT MODIFY WITHOUT AI-1 APPROVAL

These rules govern how conflicts between AI agents are resolved at integration time.
Read this before merging any branch.

---

## Priority Hierarchy

When any two files or agents conflict, resolution follows this strict priority order:

```
CONTRACT.md                        (highest authority — always wins)
    ↓
config/stores.yaml
config/settings.yaml               (AI-1 config — wins over implementation defaults)
config/layouts/*.layout.json
    ↓
Module owner AI                    (per ownership table in CONTRACT.md §14–§16)
    ↓
Integration tests in tests/        (must pass — they are the final functional arbiter)
```

---

## Rule 1 — CONTRACT.md Wins All Conflicts

If any file produced by AI-2, AI-3, or AI-4 conflicts with CONTRACT.md:
- The non-CONTRACT file must be corrected to match
- CONTRACT.md is never edited to match a conflicting implementation
- Escalate conflict immediately; do not silently deviate from the contract

---

## Rule 2 — Event Schema Is Frozen

`app/models.py` Pydantic event model must match CONTRACT.md §5 exactly:
- Field names: unchanged
- Field types: unchanged
- Required vs optional: unchanged
- `event_type` enum values: exactly the 8 strings in CONTRACT.md §6

If AI-2 or AI-3 needs internal processing fields, use a separate internal model class.
The serialised event JSON leaving the system must match the contract exactly.

---

## Rule 3 — API Endpoint Paths Are Frozen

No agent may rename, version, add a prefix to, or restructure these paths:
```
POST /events/ingest
GET  /stores/{id}/metrics
GET  /stores/{id}/funnel
GET  /stores/{id}/heatmap
GET  /stores/{id}/anomalies
GET  /health
GET  /dashboard
```
Path changes require AI-1 approval and a CONTRACT.md version bump.

---

## Rule 4 — Database Schema Column Names Are Frozen

AI-2's `scripts/init_db.py` must produce exactly the three tables with the exact column names
defined in CONTRACT.md §8. Column names and table names may not be renamed.

Additional nullable columns may be added only if they do not affect any existing query.

SQLite must work without PostgreSQL. Do not write raw `TIMESTAMPTZ`, `JSONB`, or
PostgreSQL-specific DDL as the mandatory default implementation.

---

## Rule 5 — Detection Output Adapts to API, Not the Reverse

If AI-3 pipeline produces events that do not match CONTRACT.md §5, AI-3 must fix `pipeline/emit.py`.
AI-2 backend must never add special-case parsing to handle malformed pipeline output.
The contract is the adapter layer.

---

## Rule 6 — Config File Paths Are Canonical

The following paths are canonical and must not be moved or renamed:
```
config/stores.yaml
config/settings.yaml
config/layouts/STORE_001.layout.json
config/layouts/STORE_002.layout.json
config/layouts/{STORE_ID}.layout.json   ← pattern for future stores
```

No agent may place configuration files at the project root or in `app/`, `pipeline/`, or `data/`.
All code must construct layout paths as:
```python
store_cfg["layout_file"]   # value from config/stores.yaml, e.g. "config/layouts/STORE_001.layout.json"
```

---

## Rule 7 — Documentation Must Describe Actual Code

AI-4's `DESIGN.md`, `CHOICES.md`, and `README.md` must describe the system as actually built.
Features planned but not implemented must be clearly marked `[NOT IMPLEMENTED]` or `[FUTURE WORK]`.
No aspirational documentation that misrepresents the submission state.

---

## Rule 8 — No Store Hardcoding in Application Code

No agent may hardcode `STORE_001`, `STORE_002`, camera filenames, zone polygon coordinates,
or zone names in any `.py`, `.sh`, or `.html` application file.

**Test:** If adding `STORE_003` requires any change to a Python file, the implementation is non-compliant.

All store-specific values are loaded at runtime from:
- `config/stores.yaml`
- `config/layouts/{store_id}.layout.json`

---

## Rule 9 — SQLite Is the Default Database

AI-2 must implement and test against SQLite.
`scripts/init_db.py` must run and create all tables using only SQLite.
`docker-compose.yml` must not require a separate database service by default.

If AI-2 adds optional PostgreSQL support, it must be gated behind an environment variable
and must not be required for `pytest` to pass.

---

## Rule 10 — Module Ownership Resolves Peer Conflicts

If two agents both modify a file owned by one of them:
- The owner's version takes precedence
- The non-owner must rebase onto the owner's version
- `app/dashboard.py` belongs to AI-4; AI-2 provides only the HTTP 501 stub

---

## Rule 11 — Final Integration Verification

Before submission, all of the following must succeed with exit code 0:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Initialise the SQLite database
python scripts/init_db.py

# 3. Load POS data
python scripts/load_pos.py

# 4. Start API (or via Docker)
docker compose up --build -d

# 5. Health check — must return {"status": "ok", ...}
curl -f http://localhost:8000/health

# 6. Replay sample events into the API
python pipeline/replay.py \
  --events data/sample_events.jsonl \
  --api http://localhost:8000/events/ingest \
  --speed 999999

# 7. Verify metrics for both stores
curl http://localhost:8000/stores/STORE_001/metrics
curl http://localhost:8000/stores/STORE_002/metrics

# 8. Run full test suite — all tests must pass or be marked skip with reason
pytest
```

---

## Rule 12 — Partial Implementation Is Acceptable; Silent Failure Is Not

If a module is incomplete:
- The API must still start and respond
- Unimplemented endpoints return HTTP 501 with `{"detail": "Not yet implemented"}`
- Tests for unimplemented features are marked `@pytest.mark.skip(reason="...")` with a reason
- No unhandled exceptions or raw stack traces exposed in API responses

---

## Rule 13 — Timestamps Are Always UTC ISO-8601

All timestamps written to the database, emitted in events, or returned in API responses must be UTC.
Format: `YYYY-MM-DDTHH:MM:SSZ`
No local time. No naive datetimes in business logic. No Unix epoch integers in API responses.

---

## Rule 14 — Staff Filter Is Non-Negotiable in Every Metric Query

Every metric, funnel, heatmap, and anomaly query must filter `WHERE is_staff = 0` (SQLite boolean).
A query that forgets this filter is a correctness bug, not a feature gap.

---

## Rule 15 — Zero-Division Safety Is Mandatory

These computations must never raise `ZeroDivisionError`:

| Computation                                    | Safe Return Value |
|------------------------------------------------|-------------------|
| `conversion_rate = purchases / unique_visitors` | `0.0` if `unique_visitors = 0` |
| `abandonment_rate = abandons / joins`           | `0.0` if `joins = 0` |
| `normalized_score` in heatmap                  | `0` if `max_visits = 0` |

---

## Pre-Merge Checklist

Run through this before every integration merge:

- [ ] `CONTRACT.md` is unchanged from AI-1 v1.1 (check git diff)
- [ ] `config/stores.yaml` is at `config/stores.yaml` (not at root)
- [ ] `config/settings.yaml` is at `config/settings.yaml` (not at root)
- [ ] `config/layouts/STORE_001.layout.json` exists and is valid JSON
- [ ] `config/layouts/STORE_002.layout.json` exists and is valid JSON
- [ ] `app/models.py` event schema matches CONTRACT.md §5 field-for-field
- [ ] All 7 API endpoint paths are present and responding
- [ ] `scripts/init_db.py` creates exactly 3 tables with correct column names on SQLite
- [ ] `app/database.py` default connection string is SQLite (`sqlite:///./store_intelligence.db`)
- [ ] No `STORE_001` or `STORE_002` string literal in any `.py` application file
- [ ] Staff filter (`is_staff = 0`) present in all metric/funnel/heatmap queries
- [ ] All zero-division guards in place
- [ ] `pytest` passes (skips are acceptable; failures are not)
- [ ] `docker compose up --build` succeeds
- [ ] `GET /health` returns HTTP 200 with `{"status": "ok"}`
- [ ] `GET /stores/STORE_001/metrics` returns HTTP 200 (may be all-zero)
- [ ] `GET /stores/STORE_002/metrics` returns HTTP 200 (may be all-zero)

---

*MERGE_RULES.md — Version 1.1.0 — AI-1 Lead Architect*
*Change from v1.0: Added Rule 6 (canonical config paths), Rule 9 (SQLite default), expanded checklist.*
