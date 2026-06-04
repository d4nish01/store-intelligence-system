#!/usr/bin/env python3
"""
scripts/adapt_pos_csv.py

Adapts a real-world Purplle/retail POS CSV export into the format
expected by scripts/load_pos.py.

Real CSV columns:
    order_id, order_date, order_time, store_id, product_id, brand_name, total_amount

Output CSV columns (project format):
    transaction_id, store_id, timestamp, basket_value_inr

Usage:
    python scripts/adapt_pos_csv.py \
        --input  data/POS_-_sample_transactions.csv \
        --output data/pos_transactions.csv \
        --store-id ST1008

Notes:
    - order_date format: DD-MM-YYYY  (e.g. 10-04-2026)
    - order_time format: HH:MM:SS
    - One row per product line — multiple rows with the same order_id
      are aggregated into a single transaction (basket = sum of total_amount).
    - --store-id overrides the store_id column if you want to normalise
      it to the project's naming convention (e.g. STORE_BLR_002).
      Leave blank to keep the original store_id value.
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime


def parse_date(order_date: str, order_time: str) -> str:
    """Convert DD-MM-YYYY + HH:MM:SS to ISO-8601 UTC string."""
    combined = f"{order_date.strip()} {order_time.strip()}"
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%m-%d-%Y %H:%M:%S"):
        try:
            dt = datetime.strptime(combined, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date/time: '{order_date}' / '{order_time}'")


def adapt(input_path: str, output_path: str, store_id_override: str = "") -> None:
    if not os.path.exists(input_path):
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("ERROR: Input CSV is empty.", file=sys.stderr)
        sys.exit(1)

    # Detect columns (case-insensitive)
    col = {k.strip().lower(): k for k in rows[0].keys()}

    def get(row, *names):
        for n in names:
            if n in col:
                return row[col[n]].strip()
        return ""

    # Group by order_id → sum basket value, keep earliest timestamp
    baskets: dict = defaultdict(lambda: {"total": 0.0, "ts": "", "store": ""})
    skipped = 0
    for row in rows:
        order_id = get(row, "order_id", "transaction_id", "id")
        if not order_id:
            skipped += 1
            continue
        amount_raw = get(row, "total_amount", "amount", "basket_value_inr", "price")
        try:
            amount = float(amount_raw) if amount_raw else 0.0
        except ValueError:
            amount = 0.0

        order_date = get(row, "order_date", "date")
        order_time = get(row, "order_time", "time")
        try:
            ts = parse_date(order_date, order_time)
        except ValueError as e:
            print(f"  WARN: Skipping row {order_id}: {e}")
            skipped += 1
            continue

        store = store_id_override or get(row, "store_id", "store_code")
        if not baskets[order_id]["ts"]:
            baskets[order_id]["ts"] = ts
        if not baskets[order_id]["store"]:
            baskets[order_id]["store"] = store
        baskets[order_id]["total"] += amount

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    written = 0
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["transaction_id", "store_id", "timestamp", "basket_value_inr"])
        for order_id, basket in sorted(baskets.items(), key=lambda x: x[1]["ts"]):
            writer.writerow([
                order_id,
                basket["store"],
                basket["ts"],
                round(basket["total"], 2),
            ])
            written += 1

    print(f"POS adapter complete:")
    print(f"  Input rows  : {len(rows)}")
    print(f"  Skipped     : {skipped}")
    print(f"  Transactions: {written}  (unique order_ids, baskets aggregated)")
    print(f"  Output      : {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Adapt real POS CSV to project format")
    parser.add_argument("--input",    required=True, help="Path to raw POS CSV")
    parser.add_argument("--output",   default="data/pos_transactions.csv",
                        help="Output path (default: data/pos_transactions.csv)")
    parser.add_argument("--store-id", default="",
                        help="Override store_id (e.g. STORE_BLR_002). Leave blank to keep original.")
    args = parser.parse_args()
    adapt(args.input, args.output, args.store_id)
