"""
pipeline/process_store.py

Main CLI entrypoint for the Store Intelligence Detection Pipeline.

Usage:
    python pipeline/process_store.py \\
        --store-id STORE_001 \\
        --input-dir data/raw/store_001 \\
        --layout config/layouts/STORE_001.layout.json \\
        --out output/events/STORE_001.events.jsonl

Options:
    --model         YOLO model name (default: yolov8n.pt)
    --frame-skip    Process every Nth frame (default: 5)
    --demo          Generate realistic demo events without real YOLO
    --start-time    Video wall-clock start time ISO-8601 (default: now)
    --config        Path to stores.yaml (default: config/stores.yaml)
    --settings      Path to settings.yaml (default: config/settings.yaml)
    --min-conf      Minimum detection confidence (default: from settings or 0.25)
"""

from pathlib import Path
import argparse
import json
import os
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Add project root to path so imports work when run as script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.emit import (
    create_event,
    epoch_to_iso,
    read_events_jsonl,
    reset_session_seq,
    utc_now_iso,
    validate_event,
    write_events_jsonl,
    ALLOWED_EVENT_TYPES,
)
from pipeline.geometry import (
    bbox_centroid,
    check_entry_crossing,
    get_zone_for_point,
    is_billing_zone,
)
from pipeline.reid import (
    VisitorIdentityManager,
    merge_entry_events,
    _parse_ts,
)
from pipeline.staff import build_staff_classifier


# ─── Constants ─────────────────────────────────────────────────────────────────

ZONE_DWELL_INTERVAL_SEC = 30.0      # Emit ZONE_DWELL every N seconds of continuous dwell
BILLING_ABANDON_TIMEOUT_SEC = 300.0  # 5 min: if no POS corr after leaving billing → abandon


# ─── Config loading ─────────────────────────────────────────────────────────────

def load_settings(settings_path: str = "config/settings.yaml") -> Dict[str, Any]:
    """Load settings.yaml. Returns empty dict if file missing."""
    if not os.path.exists(settings_path):
        return {}
    with open(settings_path, "r") as f:
        return yaml.safe_load(f) or {}


def load_stores_config(config_path: str = "config/stores.yaml") -> Dict[str, Any]:
    """Load stores.yaml. Returns empty dict if file missing."""
    if not os.path.exists(config_path):
        return {}
    with open(config_path, "r") as f:
        return yaml.safe_load(f) or {}


def load_layout(layout_path: str) -> Dict[str, Any]:
    """Load layout JSON for a store."""
    if not os.path.exists(layout_path):
        print(
            f"\n[process_store] ERROR: Layout file not found: {layout_path}\n"
            f"  Run tools/calibrate_zones.py to create one:\n"
            f"    python tools/calibrate_zones.py --image <layout_image.png> "
            f"--store-id <STORE_ID> --out {layout_path}\n"
        )
        sys.exit(1)
    with open(layout_path, "r") as f:
        return json.load(f)


# ─── Camera file detection ──────────────────────────────────────────────────────

CAMERA_ROLE_PATTERNS = {
    "billing": ("billing_area", "billing"),
    "entry_exit_primary": ("entry 1", "entry1", "entry_1", "entry_primary"),
    "entry_exit_secondary": ("entry 2", "entry2", "entry_2", "entry_secondary"),
    "main_floor_zone": ("zone", "floor", "main"),
}

CAMERA_ID_BY_ROLE = {
    "billing": "CAM_BILLING_01",
    "entry_exit_primary": "CAM_ENTRY_01",
    "entry_exit_secondary": "CAM_ENTRY_02",
    "main_floor_zone": "CAM_ZONE_01",
}


def detect_camera_role(filename: str) -> Optional[str]:
    """Infer camera role from filename."""
    name_lower = filename.lower()
    for role, patterns in CAMERA_ROLE_PATTERNS.items():
        for pat in patterns:
            if pat in name_lower:
                return role
    return None


def discover_cameras(input_dir: Path, store_cfg: dict) -> list[dict]:
    input_dir = Path(input_dir)
    """Discover camera files from config/stores.yaml.

    Supports both formats:
    1) cameras as dict:
       CAM_ENTRY_01:
         filename: "entry 1.mp4"
         role: "entry_exit_primary"

    2) cameras as list:
       - camera_id: CAM_ENTRY_01
         filename: "entry 1.mp4"
         role: "entry_exit_primary"
    """
    cameras = []
    configured = store_cfg.get("cameras", {})

    if isinstance(configured, dict):
        iterable = []
        for camera_id, cam_cfg in configured.items():
            if isinstance(cam_cfg, dict):
                item = dict(cam_cfg)
                item.setdefault("camera_id", camera_id)
            else:
                item = {"camera_id": camera_id, "filename": str(cam_cfg)}
            iterable.append(item)
    elif isinstance(configured, list):
        iterable = configured
    else:
        iterable = []

    for cam in iterable:
        if isinstance(cam, str):
            cam = {"camera_id": cam, "filename": cam}

        camera_id = cam.get("camera_id") or cam.get("id") or cam.get("name")
        filename = cam.get("filename", "")
        role = cam.get("role", "")

        video_path = input_dir / filename

        if not filename or not video_path.exists():
            print(f"[process_store] WARNING: Configured camera file not found: {video_path}")
            continue

        cameras.append({
            "camera_id": camera_id,
            "filename": filename,
            "path": str(video_path),
            "video_path": str(video_path),
            "role": role,
            "description": cam.get("description", ""),
        })

    return cameras

def parse_start_time(start_time_str: Optional[str]) -> float:
    """Parse ISO-8601 start time to epoch seconds. Defaults to now."""
    if not start_time_str:
        return datetime.now(timezone.utc).timestamp()
    ts = start_time_str
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).timestamp()


# ─── Per-camera processing ──────────────────────────────────────────────────────

def process_camera(
    video_path: str,
    camera_id: str,
    role: str,
    store_id: str,
    layout: Dict[str, Any],
    identity_mgr: VisitorIdentityManager,
    start_epoch: float,
    model=None,
    frame_skip: int = 5,
    min_conf: float = 0.25,
    settings: Dict[str, Any] = None,
    demo_mode: bool = False,
) -> List[Dict[str, Any]]:
    """
    Process a single camera video and return raw events (unsorted).
    """
    settings = settings or {}
    zones = layout.get("zones", [])
    entry_lines = layout.get("entry_lines", [])

    # Staff classifier for this camera
    staff_clf = build_staff_classifier(
        store_id=store_id,
        camera_id=camera_id,
        settings=settings,
    )

    # Track state for event generation
    # track_id → {zone, zone_entry_ts, last_dwell_ts, prev_centroid, billing_entry_ts, visitor_id}
    track_state: Dict[int, Dict[str, Any]] = {}

    # Queue depth in billing zone
    billing_tracks: Dict[int, float] = {}  # track_id → entry_ts

    events: List[Dict[str, Any]] = []

    def get_or_init_state(tid: int) -> Dict[str, Any]:
        if tid not in track_state:
            track_state[tid] = {
                "zone": None,
                "zone_entry_ts": None,
                "last_dwell_emit_ts": None,
                "prev_centroid": None,
                "billing_entry_ts": None,
                "visitor_id": None,
                "entered_store": False,
                "exited_store": False,
            }
        return track_state[tid]

    def emit(event_type, track_id, ts_epoch, **kwargs) -> Optional[Dict[str, Any]]:
        """Helper: create and append event for a track."""
        state = get_or_init_state(track_id)
        vid = state.get("visitor_id")
        if not vid:
            return None

        is_staff = staff_clf.is_likely_staff(str(track_id))
        base_conf = kwargs.pop("confidence", min_conf)

        try:
            ev = create_event(
                store_id=store_id,
                camera_id=camera_id,
                visitor_id=vid,
                event_type=event_type,
                timestamp=epoch_to_iso(ts_epoch),
                is_staff=is_staff,
                confidence=base_conf,
                **kwargs,
            )
            events.append(ev)
            return ev
        except ValueError as e:
            print(f"[process_store] Event creation error: {e}")
            return None

    # ── Load detector and tracker ──
    from pipeline.tracker import CentroidTracker

    tracker = CentroidTracker(
        max_distance=settings.get("centroid_max_distance", 80.0),
        max_missing_frames=settings.get("centroid_max_missing_frames", 15),
        max_missing_sec=settings.get("track_max_missing_sec", 2.5),
    )

    # ── Detect frame iterator ──
    if demo_mode:
        from pipeline.detect import iter_demo_detections
        frame_iter = iter_demo_detections(
            duration_sec=settings.get("demo_duration_sec", 300.0),
            frame_skip=frame_skip,
            num_people=settings.get("demo_num_people", 3),
        )
    else:
        from pipeline.detect import iter_video_detections
        try:
            frame_iter = iter_video_detections(
                video_path=video_path,
                model=model,
                frame_skip=frame_skip,
                min_conf=min_conf,
                video_start_time_sec=0.0,
            )
        except IOError as e:
            print(f"[process_store] ERROR: {e}")
            return events

    # ── Main processing loop ──
    for frame_data in frame_iter:
        ts_offset = frame_data["timestamp_offset_sec"]
        ts_epoch = start_epoch + ts_offset
        detections = frame_data["detections"]

        active_tracks = tracker.update_tracks(detections, ts_offset)

        # Update billing queue count
        current_billing_track_ids = set()

        for track in active_tracks:
            tid = track.track_id
            state = get_or_init_state(tid)
            centroid = bbox_centroid(track.bbox)
            prev_centroid = state["prev_centroid"]
            conf = track.effective_confidence

            # ── Assign visitor_id on first appearance ──
            if state["visitor_id"] is None:
                vid = identity_mgr.assign_visitor_id(
                    camera_id=camera_id,
                    local_track_id=tid,
                    timestamp=ts_epoch,
                    bbox=track.bbox,
                    role=role,
                )
                state["visitor_id"] = vid

            # ── Staff feature update ──
            current_zone = state["zone"]
            is_billing = is_billing_zone({"zone_id": current_zone or "", "zone_type": ""})
            staff_clf.update_staff_features(
                track_id=str(tid),
                zone_id=current_zone,
                timestamp=ts_offset,
                is_billing=is_billing,
            )

            # ── ENTRY / EXIT from entry cameras ──
            if role in ("entry_exit_primary", "entry_exit_secondary") and entry_lines:
                if prev_centroid is not None:
                    crossing = check_entry_crossing(prev_centroid, centroid, entry_lines)
                    if crossing:
                        direction = crossing["direction"]
                        entry_line_id = crossing["line_id"]

                        if direction == "inbound" and not state["entered_store"]:
                            # Check if this is a re-entry
                            is_reentry = identity_mgr.detect_reentry(
                                state["visitor_id"], ts_epoch
                            )
                            if is_reentry:
                                emit(
                                    "REENTRY", tid, ts_epoch,
                                    zone_id=None, dwell_ms=0, confidence=conf,
                                    extra_metadata={"entry_line_id": entry_line_id},
                                )
                            else:
                                emit(
                                    "ENTRY", tid, ts_epoch,
                                    zone_id=None, dwell_ms=0, confidence=conf,
                                    extra_metadata={"entry_line_id": entry_line_id},
                                )
                                identity_mgr.record_entry(state["visitor_id"], ts_epoch)
                            state["entered_store"] = True
                            state["exited_store"] = False

                        elif direction == "outbound" and state["entered_store"]:
                            emit(
                                "EXIT", tid, ts_epoch,
                                zone_id=None, dwell_ms=0, confidence=conf,
                                extra_metadata={"entry_line_id": entry_line_id},
                            )
                            identity_mgr.record_exit(state["visitor_id"], ts_epoch)
                            state["exited_store"] = True
                            state["entered_store"] = False

            # ── ZONE_ENTER / ZONE_EXIT / ZONE_DWELL ──
            if role in ("main_floor_zone", "billing"):
                zone_obj = get_zone_for_point(centroid, zones, camera_id=camera_id)
                new_zone_id = zone_obj["zone_id"] if zone_obj else None
                sku = zone_obj.get("sku_zone") if zone_obj else None
                is_bill = is_billing_zone(zone_obj) if zone_obj else False

                if new_zone_id != state["zone"]:
                    # Zone exit from previous zone
                    if state["zone"] is not None:
                        old_zone_entry_ts = state["zone_entry_ts"] or ts_epoch
                        dwell_duration_ms = int((ts_epoch - old_zone_entry_ts) * 1000)

                        # BILLING_QUEUE_ABANDON heuristic
                        if is_billing_zone({"zone_id": state["zone"], "zone_type": ""}):
                            billing_entry = state.get("billing_entry_ts")
                            if billing_entry is not None:
                                billing_tracks.pop(tid, None)
                                # POS correlation would happen in backend;
                                # emit candidate abandon with reduced confidence
                                abandon_conf = max(0.3, conf - 0.2)
                                emit(
                                    "BILLING_QUEUE_ABANDON", tid, ts_epoch,
                                    zone_id=state["zone"], dwell_ms=dwell_duration_ms,
                                    confidence=abandon_conf,
                                    queue_depth=len(billing_tracks),
                                    sku_zone=None,
                                    # NOTE: Final POS correlation is backend's responsibility.
                                    # We emit candidate; backend will resolve against POS data.
                                    extra_metadata={"candidate_abandon": True},
                                )
                                state["billing_entry_ts"] = None

                        emit(
                            "ZONE_EXIT", tid, ts_epoch,
                            zone_id=state["zone"], dwell_ms=dwell_duration_ms,
                            confidence=conf, sku_zone=sku,
                        )

                    # Zone enter into new zone
                    if new_zone_id is not None:
                        emit(
                            "ZONE_ENTER", tid, ts_epoch,
                            zone_id=new_zone_id, dwell_ms=0, confidence=conf, sku_zone=sku,
                        )

                        if is_bill:
                            # Billing queue join if queue already has people
                            current_queue = len(billing_tracks)
                            if current_queue > 0:
                                emit(
                                    "BILLING_QUEUE_JOIN", tid, ts_epoch,
                                    zone_id=new_zone_id, dwell_ms=0,
                                    confidence=conf,
                                    queue_depth=current_queue,
                                )
                            billing_tracks[tid] = ts_epoch
                            state["billing_entry_ts"] = ts_epoch

                    state["zone"] = new_zone_id
                    state["zone_entry_ts"] = ts_epoch
                    state["last_dwell_emit_ts"] = ts_epoch

                else:
                    # Still in same zone → check ZONE_DWELL
                    if new_zone_id is not None:
                        last_dwell = state.get("last_dwell_emit_ts") or ts_epoch
                        elapsed_since_dwell = ts_epoch - last_dwell
                        if elapsed_since_dwell >= ZONE_DWELL_INTERVAL_SEC:
                            total_dwell_ms = int((ts_epoch - state["zone_entry_ts"]) * 1000)
                            emit(
                                "ZONE_DWELL", tid, ts_epoch,
                                zone_id=new_zone_id, dwell_ms=total_dwell_ms,
                                confidence=conf, sku_zone=sku,
                            )
                            state["last_dwell_emit_ts"] = ts_epoch

                if is_bill:
                    current_billing_track_ids.add(tid)

            state["prev_centroid"] = centroid

        # Update billing tracks: remove tracks that are no longer active
        active_tids = {t.track_id for t in active_tracks}
        for gone_tid in list(billing_tracks.keys()):
            if gone_tid not in active_tids:
                billing_tracks.pop(gone_tid, None)

    print(f"[process_store] Camera {camera_id} ({role}): {len(events)} raw events")
    return events


# ─── Demo event generator ───────────────────────────────────────────────────────

def generate_demo_events(
    store_id: str,
    layout: Dict[str, Any],
    cameras: List[Dict[str, Any]],
    start_epoch: float,
    num_visitors: int = 5,
) -> List[Dict[str, Any]]:
    """
    Generate realistic demo events without any real video or YOLO.
    Useful for testing integration with the backend.
    """
    import random
    rng = random.Random(42)

    zones = layout.get("zones", [])
    product_zones = [z for z in zones if z.get("zone_type", "product") not in ("billing", "entry")]
    billing_zones = [z for z in zones if is_billing_zone(z)]

    billing_zone_id = billing_zones[0]["zone_id"] if billing_zones else "BILLING"
    billing_sku = billing_zones[0].get("sku_zone") if billing_zones else None

    entry_camera_id = "CAM_ENTRY_01"
    for cam in cameras:
        if cam["role"] == "entry_exit_primary":
            entry_camera_id = cam["camera_id"]
            break

    zone_camera_id = "CAM_ZONE_01"
    for cam in cameras:
        if cam["role"] == "main_floor_zone":
            zone_camera_id = cam["camera_id"]
            break

    billing_camera_id = "CAM_BILLING_01"
    for cam in cameras:
        if cam["role"] == "billing":
            billing_camera_id = cam["camera_id"]
            break

    events = []
    reset_session_seq()

    for i in range(num_visitors):
        vid = f"VIS_{uuid.uuid4().hex[:6]}"
        is_staff = (i == 0)  # First visitor is staff for demo
        entry_offset = rng.uniform(30, 120) * (i + 1)
        ts = start_epoch + entry_offset

        # ENTRY
        events.append(create_event(
            store_id=store_id,
            camera_id=entry_camera_id,
            visitor_id=vid,
            event_type="ENTRY",
            timestamp=epoch_to_iso(ts),
            zone_id=None,
            dwell_ms=0,
            is_staff=is_staff,
            confidence=round(rng.uniform(0.7, 0.95), 4),
        ))
        ts += rng.uniform(10, 30)

        # Visit 1-3 product zones
        num_zones = rng.randint(1, min(3, len(product_zones) or 1))
        for zi in range(num_zones):
            if not product_zones:
                break
            zone = rng.choice(product_zones)
            zone_id = zone["zone_id"]
            sku = zone.get("sku_zone")

            events.append(create_event(
                store_id=store_id,
                camera_id=zone_camera_id,
                visitor_id=vid,
                event_type="ZONE_ENTER",
                timestamp=epoch_to_iso(ts),
                zone_id=zone_id,
                dwell_ms=0,
                is_staff=is_staff,
                confidence=round(rng.uniform(0.65, 0.92), 4),
                sku_zone=sku,
            ))
            ts += rng.uniform(5, 20)

            dwell_sec = rng.uniform(20, 120)
            if dwell_sec >= 30:
                events.append(create_event(
                    store_id=store_id,
                    camera_id=zone_camera_id,
                    visitor_id=vid,
                    event_type="ZONE_DWELL",
                    timestamp=epoch_to_iso(ts + 30),
                    zone_id=zone_id,
                    dwell_ms=30000,
                    is_staff=is_staff,
                    confidence=round(rng.uniform(0.65, 0.92), 4),
                    sku_zone=sku,
                ))

            ts += dwell_sec
            events.append(create_event(
                store_id=store_id,
                camera_id=zone_camera_id,
                visitor_id=vid,
                event_type="ZONE_EXIT",
                timestamp=epoch_to_iso(ts),
                zone_id=zone_id,
                dwell_ms=int(dwell_sec * 1000),
                is_staff=is_staff,
                confidence=round(rng.uniform(0.65, 0.92), 4),
                sku_zone=sku,
            ))
            ts += rng.uniform(5, 15)

        # Billing visit (70% of non-staff)
        if not is_staff and rng.random() < 0.7:
            queue_depth = rng.randint(0, 4)
            events.append(create_event(
                store_id=store_id,
                camera_id=billing_camera_id,
                visitor_id=vid,
                event_type="ZONE_ENTER",
                timestamp=epoch_to_iso(ts),
                zone_id=billing_zone_id,
                dwell_ms=0,
                is_staff=False,
                confidence=round(rng.uniform(0.7, 0.95), 4),
            ))
            ts += rng.uniform(5, 10)

            if queue_depth > 0:
                events.append(create_event(
                    store_id=store_id,
                    camera_id=billing_camera_id,
                    visitor_id=vid,
                    event_type="BILLING_QUEUE_JOIN",
                    timestamp=epoch_to_iso(ts),
                    zone_id=billing_zone_id,
                    dwell_ms=0,
                    is_staff=False,
                    confidence=round(rng.uniform(0.7, 0.95), 4),
                    queue_depth=queue_depth,
                ))

            # 20% chance of abandon
            if rng.random() < 0.2:
                ts += rng.uniform(10, 60)
                events.append(create_event(
                    store_id=store_id,
                    camera_id=billing_camera_id,
                    visitor_id=vid,
                    event_type="BILLING_QUEUE_ABANDON",
                    timestamp=epoch_to_iso(ts),
                    zone_id=billing_zone_id,
                    dwell_ms=int(rng.uniform(10, 60) * 1000),
                    is_staff=False,
                    confidence=round(rng.uniform(0.45, 0.7), 4),
                    queue_depth=queue_depth,
                ))
            else:
                ts += rng.uniform(30, 120)
                events.append(create_event(
                    store_id=store_id,
                    camera_id=billing_camera_id,
                    visitor_id=vid,
                    event_type="ZONE_EXIT",
                    timestamp=epoch_to_iso(ts),
                    zone_id=billing_zone_id,
                    dwell_ms=int(rng.uniform(30, 120) * 1000),
                    is_staff=False,
                    confidence=round(rng.uniform(0.7, 0.92), 4),
                ))

        # EXIT
        ts += rng.uniform(10, 30)
        events.append(create_event(
            store_id=store_id,
            camera_id=entry_camera_id,
            visitor_id=vid,
            event_type="EXIT",
            timestamp=epoch_to_iso(ts),
            zone_id=None,
            dwell_ms=0,
            is_staff=is_staff,
            confidence=round(rng.uniform(0.7, 0.95), 4),
        ))

    # Sort by timestamp
    events.sort(key=lambda e: e["timestamp"])
    return events


# ─── Main pipeline ──────────────────────────────────────────────────────────────

def run_pipeline(args) -> int:
    """
    Main pipeline execution. Returns count of events written.
    """
    store_id = args.store_id
    input_dir = args.input_dir
    layout_path = args.layout
    output_path = args.out
    demo_mode = args.demo
    frame_skip = args.frame_skip
    start_time_str = args.start_time
    min_conf_arg = args.min_conf

    print(f"\n{'='*60}")
    print(f"  Store Intelligence Pipeline")
    print(f"  Store: {store_id}")
    print(f"  Input: {input_dir}")
    print(f"  Layout: {layout_path}")
    print(f"  Output: {output_path}")
    print(f"  Demo mode: {demo_mode}")
    print(f"{'='*60}\n")

    # Load configs
    settings = load_settings(args.settings)
    stores_cfg = load_stores_config(args.config)
    store_cfg = stores_cfg.get("stores", {}).get(store_id, {})

    # Load layout
    layout = load_layout(layout_path)

    # Discover cameras
    if not os.path.exists(input_dir) and not demo_mode:
        print(f"[process_store] ERROR: Input directory not found: {input_dir}")
        sys.exit(1)

    cameras = []
    if os.path.exists(input_dir):
        cameras = discover_cameras(input_dir, store_cfg)

    if not cameras and not demo_mode:
        print("[process_store] No cameras found and not in demo mode. Exiting.")
        sys.exit(1)

    if not cameras and demo_mode:
        # Synthesize camera list for demo
        cameras = [
            {"video_path": None, "camera_id": "CAM_ENTRY_01", "role": "entry_exit_primary"},
            {"video_path": None, "camera_id": "CAM_ENTRY_02", "role": "entry_exit_secondary"},
            {"video_path": None, "camera_id": "CAM_ZONE_01", "role": "main_floor_zone"},
            {"video_path": None, "camera_id": "CAM_BILLING_01", "role": "billing"},
        ]

    # Parse start time
    start_epoch = parse_start_time(start_time_str)

    # Min confidence
    min_conf = min_conf_arg or settings.get("min_confidence", 0.25)

    # Shared identity manager
    identity_mgr = VisitorIdentityManager(
        store_id=store_id,
        reentry_window_sec=settings.get("reentry_window_sec", 3600.0),
    )

    all_events: List[Dict[str, Any]] = []

    if demo_mode:
        print("[process_store] Running in DEMO mode — generating synthetic events.")
        all_events = generate_demo_events(
            store_id=store_id,
            layout=layout,
            cameras=cameras,
            start_epoch=start_epoch,
            num_visitors=settings.get("demo_num_visitors", 8),
        )
    else:
        # Load YOLO model
        model = None
        model_name = args.model or settings.get("yolo_model", "yolov8n.pt")
        try:
            from pipeline.detect import load_detector
            model = load_detector(model_name)
        except ImportError:
            print(
                f"\n[process_store] YOLO not available.\n"
                f"  Install with: pip install ultralytics\n"
                f"  Or use --demo flag to run without a real model.\n"
            )
            sys.exit(1)

        # Process each camera
        for cam in cameras:
            cam_events = process_camera(
                video_path=cam["video_path"],
                camera_id=cam["camera_id"],
                role=cam["role"],
                store_id=store_id,
                layout=layout,
                identity_mgr=identity_mgr,
                start_epoch=start_epoch,
                model=model,
                frame_skip=frame_skip,
                min_conf=float(min_conf),
                settings=settings,
                demo_mode=False,
            )
            all_events.extend(cam_events)

    # ── Merge entry-camera duplicates ──
    primary_cam = next(
        (c["camera_id"] for c in cameras if c["role"] == "entry_exit_primary"),
        "CAM_ENTRY_01",
    )
    secondary_cam = next(
        (c["camera_id"] for c in cameras if c["role"] == "entry_exit_secondary"),
        "CAM_ENTRY_02",
    )

    all_events = merge_entry_events(
        all_events,
        primary_camera_id=primary_cam,
        secondary_camera_id=secondary_cam,
        dedup_window_seconds=settings.get("entry_dedup_window_sec", 4.0),
        identity_manager=identity_mgr,
    )

    # ── Sort by timestamp ──
    all_events.sort(key=lambda e: e.get("timestamp", ""))

    # ── Write output ──
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    count = write_events_jsonl(all_events, output_path)

    print(f"\n[process_store] Done. {count} events written to {output_path}")
    return count


# ─── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Store Intelligence Detection Pipeline — Process one store's CCTV footage."
    )
    parser.add_argument("--store-id", required=True, help="Store identifier, e.g. STORE_001")
    parser.add_argument("--input-dir", required=True, help="Directory containing camera video files")
    parser.add_argument("--layout", required=True, help="Path to store layout JSON file")
    parser.add_argument("--out", required=True, help="Output JSONL file path")
    parser.add_argument("--model", default=None, help="YOLO model filename (default: yolov8n.pt)")
    parser.add_argument("--frame-skip", type=int, default=5, help="Process every Nth frame (default: 5)")
    parser.add_argument("--demo", action="store_true", help="Generate demo events without real YOLO")
    parser.add_argument("--start-time", default=None, help="Video wall-clock start time ISO-8601")
    parser.add_argument("--config", default="config/stores.yaml", help="Path to stores.yaml")
    parser.add_argument("--settings", default="config/settings.yaml", help="Path to settings.yaml")
    parser.add_argument("--min-conf", type=float, default=None, help="Minimum detection confidence")

    args = parser.parse_args()
    run_pipeline(args)


if __name__ == "__main__":
    main()
