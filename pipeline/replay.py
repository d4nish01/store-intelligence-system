"""
pipeline/replay.py

Replay JSONL events into the POST /events/ingest API endpoint.

Usage:
    # Normal speed (respects event timestamps)
    python pipeline/replay.py \\
        --events output/events/STORE_001.events.jsonl \\
        --api http://localhost:8000/events/ingest \\
        --speed 1

    # Fast replay (send as fast as possible)
    python pipeline/replay.py \\
        --events data/sample_events.jsonl \\
        --api http://localhost:8000/events/ingest \\
        --speed 999999
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests


# ─── Replay logic ──────────────────────────────────────────────────────────────

def load_events_jsonl(path: str) -> List[Dict[str, Any]]:
    """Load events from JSONL file."""
    events = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[replay] WARNING: Skipping invalid JSON on line {i}: {e}")
    return events


def _parse_event_ts(event: Dict[str, Any]) -> float:
    """Parse event timestamp to epoch seconds."""
    ts = event.get("timestamp", "")
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def send_batch(
    events: List[Dict[str, Any]],
    api_url: str,
    session: requests.Session,
    timeout: int = 30,
) -> Dict[str, int]:
    """
    Send a batch of events to POST /events/ingest.
    Returns summary dict: {accepted, duplicates, rejected, errors}.
    """
    summary = {"accepted": 0, "duplicates": 0, "rejected": 0, "errors": 0}

    try:
        resp = session.post(
            api_url,
            json={"events": events},
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code == 200:
            body = resp.json()
            # Parse backend response — adapt to actual response shape
            if isinstance(body, dict):
                summary["accepted"] += body.get("accepted", len(events))
                summary["duplicates"] += body.get("duplicates", 0)
                summary["rejected"] += body.get("rejected", 0)
            elif isinstance(body, list):
                summary["accepted"] += len(events)
        elif resp.status_code == 207:
            # Partial success
            body = resp.json()
            if isinstance(body, dict):
                summary["accepted"] += body.get("accepted", 0)
                summary["duplicates"] += body.get("duplicates", 0)
                summary["rejected"] += body.get("rejected", 0)
            else:
                summary["accepted"] += len(events)
        elif resp.status_code in (409,):
            # All duplicates
            summary["duplicates"] += len(events)
        elif resp.status_code in (422, 400):
            print(f"[replay] Batch rejected ({resp.status_code}): {resp.text[:200]}")
            summary["rejected"] += len(events)
        else:
            print(f"[replay] Unexpected status {resp.status_code}: {resp.text[:200]}")
            summary["errors"] += len(events)
    except requests.exceptions.ConnectionError:
        print(f"[replay] ERROR: Cannot connect to API at {api_url}")
        print("  Make sure the backend is running: docker compose up")
        summary["errors"] += len(events)
    except requests.exceptions.Timeout:
        print(f"[replay] ERROR: Request timed out")
        summary["errors"] += len(events)
    except Exception as e:
        print(f"[replay] ERROR sending batch: {e}")
        summary["errors"] += len(events)

    return summary


def replay_events(
    events: List[Dict[str, Any]],
    api_url: str,
    speed: float = 1.0,
    batch_size: int = 50,
    max_batch_size: int = 500,
) -> Dict[str, int]:
    """
    Replay events to API, respecting timestamps at the given speed multiplier.

    Args:
        events: Sorted list of event dicts.
        api_url: POST endpoint URL.
        speed: Replay speed multiplier. 1.0 = real time, 999999 = as fast as possible.
        batch_size: Default batch size.
        max_batch_size: Maximum batch size (capped).

    Returns:
        Summary dict: {accepted, duplicates, rejected, errors, total_sent}
    """
    batch_size = min(batch_size, max_batch_size)
    total = {"accepted": 0, "duplicates": 0, "rejected": 0, "errors": 0, "total_sent": 0}

    if not events:
        print("[replay] No events to replay.")
        return total

    fast_mode = speed >= 1000.0
    session = requests.Session()

    print(f"[replay] Replaying {len(events)} events to {api_url}")
    print(f"[replay] Speed: {'FAST (no timing)' if fast_mode else f'{speed}x'}, batch_size={batch_size}")

    if fast_mode:
        # Send in batches as fast as possible
        batch: List[Dict[str, Any]] = []
        for i, event in enumerate(events):
            batch.append(event)
            if len(batch) >= batch_size:
                result = send_batch(batch, api_url, session)
                for k in total:
                    if k in result:
                        total[k] += result[k]
                total["total_sent"] += len(batch)
                print(f"[replay] Sent {total['total_sent']}/{len(events)} events...", end="\r")
                batch = []

        if batch:
            result = send_batch(batch, api_url, session)
            for k in total:
                if k in result:
                    total[k] += result[k]
            total["total_sent"] += len(batch)

    else:
        # Time-aware replay: wait proportionally between event groups
        if not events:
            return total

        first_ts = _parse_event_ts(events[0])
        replay_start_real = time.time()

        batch: List[Dict[str, Any]] = []
        batch_target_ts = _parse_event_ts(events[0])

        for event in events:
            event_ts = _parse_event_ts(event)

            # If we have a batch and new event is in a different time window, flush
            if batch and (event_ts - batch_target_ts) > 1.0:
                # Wait for correct replay time
                elapsed_real = time.time() - replay_start_real
                elapsed_event = (batch_target_ts - first_ts) / speed
                sleep_time = elapsed_event - elapsed_real
                if sleep_time > 0:
                    time.sleep(sleep_time)

                result = send_batch(batch, api_url, session)
                for k in total:
                    if k in result:
                        total[k] += result[k]
                total["total_sent"] += len(batch)
                print(f"[replay] Sent {total['total_sent']}/{len(events)} events...", end="\r")
                batch = []
                batch_target_ts = event_ts

            batch.append(event)
            if len(batch) >= batch_size:
                # Flush large batch immediately
                result = send_batch(batch, api_url, session)
                for k in total:
                    if k in result:
                        total[k] += result[k]
                total["total_sent"] += len(batch)
                print(f"[replay] Sent {total['total_sent']}/{len(events)} events...", end="\r")
                batch = []
                batch_target_ts = event_ts

        if batch:
            result = send_batch(batch, api_url, session)
            for k in total:
                if k in result:
                    total[k] += result[k]
            total["total_sent"] += len(batch)

    print()  # Newline after \r progress
    return total


# ─── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Replay JSONL events to the Store Intelligence API."
    )
    parser.add_argument(
        "--events", required=True,
        help="Path to JSONL events file",
    )
    parser.add_argument(
        "--api", default="http://localhost:8000/events/ingest",
        help="API endpoint URL (default: http://localhost:8000/events/ingest)",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="Replay speed multiplier (1=realtime, 999999=fast)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="Events per API batch (default: 50, max: 500)",
    )
    args = parser.parse_args()

    if not __import__("os").path.exists(args.events):
        print(f"[replay] ERROR: Events file not found: {args.events}")
        sys.exit(1)

    events = load_events_jsonl(args.events)
    if not events:
        print("[replay] No events loaded. Exiting.")
        sys.exit(0)

    # Sort by timestamp
    events.sort(key=lambda e: e.get("timestamp", ""))

    result = replay_events(
        events=events,
        api_url=args.api,
        speed=args.speed,
        batch_size=min(args.batch_size, 500),
    )

    print("\n" + "="*40)
    print("  Replay Summary")
    print("="*40)
    print(f"  Total sent:   {result.get('total_sent', 0)}")
    print(f"  Accepted:     {result.get('accepted', 0)}")
    print(f"  Duplicates:   {result.get('duplicates', 0)}")
    print(f"  Rejected:     {result.get('rejected', 0)}")
    print(f"  Errors:       {result.get('errors', 0)}")
    print("="*40)

    if result.get("errors", 0) > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
