"""
pipeline/detect.py

Responsibilities:
- Load video frames.
- Run YOLO person detection (or fallback centroid simulation).
- Return per-frame detections: frame_index, timestamp_offset_sec, bbox, confidence.

Gracefully handles missing ultralytics package.
"""

import cv2
import numpy as np
import sys
from typing import Generator, List, Dict, Any, Optional

# ─── YOLO availability check ───────────────────────────────────────────────────

YOLO_AVAILABLE = False
_yolo_import_error = None

try:
    from ultralytics import YOLO as _YOLO
    YOLO_AVAILABLE = True
except ImportError as e:
    _yolo_import_error = e


def _check_yolo():
    if not YOLO_AVAILABLE:
        print(
            "\n[detect.py] ERROR: ultralytics package is not installed.\n"
            "  Install it with:  pip install ultralytics\n"
            "  Then re-run the pipeline.\n"
            "  Alternatively, use --demo flag to run without a real model.\n"
        )
        raise ImportError(
            "ultralytics not installed. Run: pip install ultralytics"
        ) from _yolo_import_error


# ─── Public API ────────────────────────────────────────────────────────────────

def load_detector(model_name: str = "yolov8n.pt"):
    """
    Load a YOLO model by name.
    Returns model object.
    Raises clear ImportError if ultralytics is missing.
    """
    _check_yolo()
    print(f"[detect] Loading YOLO model: {model_name}")
    model = _YOLO(model_name)
    return model


def detect_people_in_frame(
    model,
    frame: np.ndarray,
    min_conf: float = 0.25,
) -> List[Dict[str, Any]]:
    """
    Run detection on a single BGR frame.
    Returns list of dicts:
      {bbox: [x1,y1,x2,y2], confidence: float, class_name: "person"}
    Only person class (class_id=0 in COCO) is returned.
    """
    results = model(frame, verbose=False)
    detections = []
    for result in results:
        boxes = result.boxes
        if boxes is None:
            continue
        for box in boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            # COCO class 0 = person
            if cls_id != 0:
                continue
            if conf < min_conf:
                continue
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            detections.append({
                "bbox": [x1, y1, x2, y2],
                "confidence": conf,
                "class_name": "person",
            })
    return detections


def iter_video_detections(
    video_path: str,
    model,
    frame_skip: int = 5,
    min_conf: float = 0.25,
    video_start_time_sec: float = 0.0,
) -> Generator[Dict[str, Any], None, None]:
    """
    Iterate over frames of a video, yielding detection batches.

    Yields dict per processed frame:
    {
        "frame_index": int,
        "timestamp_offset_sec": float,
        "detections": [{"bbox": [...], "confidence": float, "class_name": "person"}]
    }

    Args:
        video_path: Path to video file.
        model: Loaded YOLO model.
        frame_skip: Process every Nth frame (1 = every frame).
        min_conf: Minimum detection confidence.
        video_start_time_sec: Offset added to frame timestamps (for wall-clock alignment).
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[detect] ERROR: Cannot open video: {video_path}")
        raise IOError(f"Cannot open video file: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_index = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_index % frame_skip == 0:
                timestamp_offset_sec = video_start_time_sec + (frame_index / fps)
                detections = detect_people_in_frame(model, frame, min_conf=min_conf)
                yield {
                    "frame_index": frame_index,
                    "timestamp_offset_sec": timestamp_offset_sec,
                    "detections": detections,
                }

            frame_index += 1
    finally:
        cap.release()


# ─── Demo / simulation mode ────────────────────────────────────────────────────

def iter_demo_detections(
    duration_sec: float = 300.0,
    frame_skip: int = 5,
    fps: float = 25.0,
    num_people: int = 3,
    rng_seed: int = 42,
) -> Generator[Dict[str, Any], None, None]:
    """
    Generates synthetic detections for demo/testing without a real video or YOLO.

    People move in simple random-walk paths within a 1280x720 frame.
    """
    rng = np.random.default_rng(rng_seed)
    total_frames = int(duration_sec * fps)

    # Initialize positions for each synthetic person
    positions = rng.uniform(100, 1100, size=(num_people, 2))  # (x_center, y_center)
    velocities = rng.uniform(-3, 3, size=(num_people, 2))
    active = [True] * num_people
    activation_times = rng.uniform(0, duration_sec * 0.3, size=num_people)
    exit_times = rng.uniform(duration_sec * 0.5, duration_sec, size=num_people)

    for frame_index in range(0, total_frames, frame_skip):
        timestamp_offset_sec = frame_index / fps
        detections = []

        for i in range(num_people):
            if timestamp_offset_sec < activation_times[i]:
                continue
            if timestamp_offset_sec > exit_times[i]:
                continue

            # Random walk
            positions[i] += velocities[i]
            # Bounce off walls
            for dim, limit in enumerate([1280, 720]):
                if positions[i][dim] < 50 or positions[i][dim] > limit - 50:
                    velocities[i][dim] *= -1
                    positions[i][dim] = np.clip(positions[i][dim], 50, limit - 50)

            # Add velocity jitter
            velocities[i] += rng.uniform(-0.5, 0.5, size=2)
            velocities[i] = np.clip(velocities[i], -8, 8)

            cx, cy = positions[i]
            w, h = 60, 160
            x1 = max(0, cx - w / 2)
            y1 = max(0, cy - h / 2)
            x2 = min(1280, cx + w / 2)
            y2 = min(720, cy + h / 2)

            conf = float(rng.uniform(0.55, 0.97))
            detections.append({
                "bbox": [x1, y1, x2, y2],
                "confidence": conf,
                "class_name": "person",
            })

        yield {
            "frame_index": frame_index,
            "timestamp_offset_sec": timestamp_offset_sec,
            "detections": detections,
        }
