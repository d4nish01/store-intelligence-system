#!/usr/bin/env python3
"""Load POS transactions from a CSV file into the database."""
import sys
import os
import argparse
import csv
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import init_db, SessionLocal, PosTransaction


def parse_timestamp(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse timestamp: {s!r}")


def load_pos(filepath: str):
    init_db()
    db = SessionLocal()
    loaded = 0
    duplicates = 0
    failed = 0

    try:
        with open(filepath, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                tx_id = row.get("transaction_id", "").strip()
                store_id = row.get("store_id", "").strip()
                ts_raw = row.get("timestamp", "").strip()
                basket_raw = row.get("basket_value_inr", "0").strip()

                if not tx_id or not store_id or not ts_raw:
                    print(f"  SKIP row missing required fields: {row}")
                    failed += 1
                    continue

                # Dedup check
                existing = db.get(PosTransaction, tx_id)
                if existing:
                    duplicates += 1
                    continue

                try:
                    ts = parse_timestamp(ts_raw)
                    basket = float(basket_raw) if basket_raw else 0.0
                except (ValueError, TypeError) as exc:
                    print(f"  FAIL row {tx_id}: {exc}")
                    failed += 1
                    continue

                db.add(PosTransaction(
                    transaction_id=tx_id,
                    store_id=store_id,
                    timestamp=ts,
                    basket_value_inr=basket,
                ))
                loaded += 1

        db.commit()
    except FileNotFoundError:
        print(f"✗ File not found: {filepath}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()

    print(f"POS load complete:")
    print(f"  loaded:     {loaded}")
    print(f"  duplicates: {duplicates}")
    print(f"  failed:     {failed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Load POS transactions from CSV")
    parser.add_argument("--file", required=True, help="Path to CSV file")
    args = parser.parse_args()
    load_pos(args.file)
