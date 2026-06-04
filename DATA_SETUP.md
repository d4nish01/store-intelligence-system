`# DATA_SETUP.md
# Store Intelligence — Real Data Integration Guide

This guide explains how to connect your actual store data (POS exports and
CCTV-derived event files) to the project.

---

## What you have vs what the project expects

### POS Transactions

| Your file | Project expects |
|-----------|----------------|
| `order_id` | `transaction_id` |
| `order_date` + `order_time` (separate) | `timestamp` (ISO-8601 combined) |
| `total_amount` | `basket_value_inr` |
| `store_id` = `ST1008` | any consistent store ID |
| one row per product line | one row per transaction |

### CCTV Events

| Your event type | Project event type |
|----------------|-------------------|
| `entry` | `ENTRY` |
| `exit` | `EXIT` |
| `zone_entered` | `ZONE_ENTER` |
| `zone_exited` | `ZONE_EXIT` |
| *(synthesised from zone_entered/exited pair)* | `ZONE_DWELL` |
| `queue_completed` | `BILLING_QUEUE_JOIN` |
| `queue_abandoned` | `BILLING_QUEUE_ABANDON` |

| Your field | Project field |
|-----------|--------------|
| `id_token` / `track_id` | `visitor_id` |
| `store_code` / `store_id` | `store_id` |
| `event_timestamp` / `event_time` / `queue_join_ts` | `timestamp` |
| `zone_id` | `zone_id` |
| `zone_name` | `metadata.sku_zone` |
| `wait_seconds × 1000` | `dwell_ms` |
| `queue_position_at_join` | `metadata.queue_depth` |

Fields your files contain that the project doesn't use:
`gender_pred`, `age_pred`, `age_bucket`, `is_face_hidden`, `group_id`,
`group_size`, `zone_hotspot_x/y`, `is_revenue_zone`, `zone_type`,
`brand_name`, `product_id`, `queue_served_ts`, `queue_exit_ts`.

---

## Step-by-step: loading your data

### Step 1 — Adapt and load POS transactions

```bash
# Convert your POS CSV to project format
# Replace STORE_BLR_002 with whatever store ID you want to use
python scripts/adapt_pos_csv.py \
  --input  data/POS_raw_sample.csv \
  --output data/pos_transactions.csv \
  --store-id STORE_BLR_002

# Load into the database
docker compose exec api python scripts/load_pos.py \
  --file data/pos_transactions.csv
```

This collapses multi-line orders (one row per product) into single transactions
and converts the `DD-MM-YYYY HH:MM:SS` format to ISO-8601.

### Step 2 — Adapt and load CCTV events

```bash
# Convert your raw events JSONL to project schema
# Map your store code(s) to your project store ID
python scripts/adapt_events_jsonl.py \
  --input  data/events_raw_sample.jsonl \
  --output data/sample_events.jsonl \
  --store-id-map "store_1076=STORE_BLR_002,ST1076=STORE_BLR_002"

# Replay into the running API
docker compose exec api python pipeline/replay.py \
  --events data/sample_events.jsonl \
  --api http://localhost:8000/events/ingest \
  --speed 999999
```

The adapter automatically synthesises `ZONE_DWELL` events from
`zone_entered`/`zone_exited` pairs — you don't need to add them manually.

### Step 3 — Verify

```bash
curl localhost:8000/health
curl localhost:8000/stores/STORE_BLR_002/metrics
curl localhost:8000/stores/STORE_BLR_002/funnel
```

---

## Store ID alignment

Your POS file uses `ST1008` and your events use `store_1076` / `ST1076`.
These appear to be **two different stores** — the events come from store 1076
and the POS comes from store 1008.

For the funnel to work correctly (matching billing-zone events to POS
transactions), both files must use the **same store ID**.

Options:
- If they ARE the same store, pick one ID and use `--store-id` on both adapters.
- If they are different stores, run the adapters with different target store IDs
  (e.g. `STORE_MUM_1076` and `STORE_BLR_1008`) and query each separately.

Example for two different stores:
```bash
python scripts/adapt_pos_csv.py \
  --input data/POS_raw_sample.csv \
  --output data/pos_transactions.csv \
  --store-id STORE_BLR_1008

python scripts/adapt_events_jsonl.py \
  --input data/events_raw_sample.jsonl \
  --output data/sample_events.jsonl \
  --store-id-map "store_1076=STORE_MUM_1076,ST1076=STORE_MUM_1076"

# Then query each separately:
curl localhost:8000/stores/STORE_MUM_1076/metrics
curl localhost:8000/stores/STORE_BLR_1008/metrics
```

---

## What to upload from your CCTV ZIP files

When you share the CCTV ZIP files, the project needs:

```
data/raw/store_001/
    entry 1.mp4          ← camera watching the entrance door (primary)
    entry 2.mp4          ← second camera at entrance (confirmation/backup)
    zone.mp4             ← camera covering the product shelves / floor
    billing_area.mp4     ← camera covering the checkout / billing queue

config/layouts/STORE_001.layout.json   ← zone polygon definitions
```

The **layout PNG** you mentioned is exactly what the calibration tool uses
to draw zone polygons. Run:

```bash
python tools/calibrate_zones.py \
  --image data/raw/store_001/store_layout.png \
  --store-id STORE_001 \
  --out config/layouts/STORE_001.layout.json
```

This opens an interactive window where you click to draw polygons over the
layout image to mark the entry line, product zones, and billing area.

If you don't have OpenCV window support (e.g. running on a server), use
`--template-only` to generate a JSON template and edit the coordinates
manually using pixel coordinates from the layout PNG.

---

## Pre-adapted files already in this repo

| File | Description |
|------|-------------|
| `data/POS_raw_sample.csv` | Original POS export (as provided) |
| `data/events_raw_sample.jsonl` | Original CCTV events (as provided) |
| `data/pos_transactions.csv` | Adapted POS — ready for `load_pos.py` |
| `data/sample_events.jsonl` | Adapted events — ready for `replay.py` |

