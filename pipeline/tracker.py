"""
pipeline/tracker.py

Responsibilities:
- Track persons across video frames.
- Prefers ByteTrack/DeepSORT if available.
- Falls back to centroid tracker.
- Outputs camera-local track_id with bbox history and confidence history.
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
import time

# ─── ByteTrack / SORT optional import ─────────────────────────────────────────

BYTETRACK_AVAILABLE = False
try:
    # ultralytics ships ByteTrack as built-in tracker
    # We rely on YOLO's track() method if available.
    # If using standalone boxmot / supervision, try here:
    from boxmot import ByteTracker as _ByteTracker
    BYTETRACK_AVAILABLE = True
except ImportError:
    pass


# ─── Data structures ───────────────────────────────────────────────────────────

@dataclass
class TrackState:
    """Represents a tracked person across frames."""
    track_id: int
    bbox: List[float]                  # Most recent [x1, y1, x2, y2]
    centroid: Tuple[float, float]      # Most recent centroid (cx, cy)
    bbox_history: List[List[float]] = field(default_factory=list)
    centroid_history: List[Tuple[float, float]] = field(default_factory=list)
    confidence_history: List[float] = field(default_factory=list)
    first_seen: float = 0.0            # timestamp_offset_sec
    last_seen: float = 0.0             # timestamp_offset_sec
    frames_seen: int = 0
    frames_missing: int = 0
    active: bool = True

    def update(self, bbox: List[float], confidence: float, timestamp: float):
        self.bbox = bbox
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        self.centroid = (cx, cy)
        self.bbox_history.append(bbox)
        self.centroid_history.append((cx, cy))
        self.confidence_history.append(confidence)
        self.last_seen = timestamp
        self.frames_seen += 1
        self.frames_missing = 0

    @property
    def avg_confidence(self) -> float:
        if not self.confidence_history:
            return 0.0
        return float(np.mean(self.confidence_history[-10:]))

    @property
    def stability_boost(self) -> float:
        """Small boost for tracks seen many frames, capped at 0.1."""
        return min(0.1, self.frames_seen * 0.002)

    @property
    def effective_confidence(self) -> float:
        return min(1.0, self.avg_confidence + self.stability_boost)


# ─── Centroid Tracker (fallback) ───────────────────────────────────────────────

class CentroidTracker:
    """
    Simple centroid-based tracker.
    Matches detections to existing tracks by nearest centroid distance.
    Expires tracks after max_missing_frames consecutive unmatched frames.
    """

    def __init__(
        self,
        max_distance: float = 80.0,
        max_missing_frames: int = 15,
        max_missing_sec: float = 2.0,
    ):
        self.max_distance = max_distance
        self.max_missing_frames = max_missing_frames
        self.max_missing_sec = max_missing_sec
        self._next_id = 1
        self.tracks: Dict[int, TrackState] = {}

    def update_tracks(
        self,
        detections: List[Dict[str, Any]],
        timestamp: float,
    ) -> List[TrackState]:
        """
        Match detections to existing tracks. Update or create tracks.
        Expire lost tracks.

        Args:
            detections: List of {"bbox": [x1,y1,x2,y2], "confidence": float}
            timestamp: Current frame timestamp (seconds from video start).

        Returns:
            List of currently active TrackState objects.
        """
        if not detections:
            # Age all tracks
            to_deactivate = []
            for tid, track in self.tracks.items():
                track.frames_missing += 1
                age_sec = timestamp - track.last_seen
                if (track.frames_missing > self.max_missing_frames
                        or age_sec > self.max_missing_sec):
                    to_deactivate.append(tid)
            for tid in to_deactivate:
                self.tracks[tid].active = False
            return [t for t in self.tracks.values() if t.active]

        det_centroids = [
            ((d["bbox"][0] + d["bbox"][2]) / 2,
             (d["bbox"][1] + d["bbox"][3]) / 2)
            for d in detections
        ]
        active_tracks = {tid: t for tid, t in self.tracks.items() if t.active}

        if not active_tracks:
            # All new tracks
            for i, det in enumerate(detections):
                self._create_track(det, det_centroids[i], timestamp)
            return list(self.tracks.values())

        # Build cost matrix (track x detection)
        track_ids = list(active_tracks.keys())
        track_centroids = [active_tracks[tid].centroid for tid in track_ids]

        cost = np.zeros((len(track_ids), len(det_centroids)))
        for ti, tc in enumerate(track_centroids):
            for di, dc in enumerate(det_centroids):
                cost[ti, di] = np.sqrt((tc[0] - dc[0])**2 + (tc[1] - dc[1])**2)

        # Greedy matching: assign nearest unmatched det to each track
        matched_tracks = set()
        matched_dets = set()

        # Sort by cost ascending
        pairs = sorted(
            [(cost[ti, di], ti, di)
             for ti in range(len(track_ids))
             for di in range(len(det_centroids))],
            key=lambda x: x[0]
        )

        for dist, ti, di in pairs:
            if ti in matched_tracks or di in matched_dets:
                continue
            if dist > self.max_distance:
                break
            tid = track_ids[ti]
            self.tracks[tid].update(
                detections[di]["bbox"],
                detections[di]["confidence"],
                timestamp,
            )
            matched_tracks.add(ti)
            matched_dets.add(di)

        # Unmatched tracks: age them
        to_deactivate = []
        for ti, tid in enumerate(track_ids):
            if ti not in matched_tracks:
                self.tracks[tid].frames_missing += 1
                age_sec = timestamp - self.tracks[tid].last_seen
                if (self.tracks[tid].frames_missing > self.max_missing_frames
                        or age_sec > self.max_missing_sec):
                    to_deactivate.append(tid)
        for tid in to_deactivate:
            self.tracks[tid].active = False

        # Unmatched detections: new tracks
        for di, det in enumerate(detections):
            if di not in matched_dets:
                self._create_track(det, det_centroids[di], timestamp)

        return [t for t in self.tracks.values() if t.active]

    def _create_track(self, det: Dict, centroid: Tuple, timestamp: float) -> TrackState:
        tid = self._next_id
        self._next_id += 1
        state = TrackState(
            track_id=tid,
            bbox=det["bbox"],
            centroid=centroid,
            first_seen=timestamp,
            last_seen=timestamp,
        )
        state.bbox_history.append(det["bbox"])
        state.centroid_history.append(centroid)
        state.confidence_history.append(det["confidence"])
        state.frames_seen = 1
        self.tracks[tid] = state
        return state

    def get_all_tracks(self) -> List[TrackState]:
        return list(self.tracks.values())


# ─── YOLO-native ByteTrack wrapper ─────────────────────────────────────────────

class YOLOByteTracker:
    """
    Wraps YOLO's built-in ByteTrack (via ultralytics model.track()).
    This is the preferred tracker when ultralytics is available.
    Falls back to CentroidTracker if not.
    """

    def __init__(self, model, max_missing_sec: float = 2.0):
        self.model = model
        self._track_states: Dict[int, TrackState] = {}
        self.max_missing_sec = max_missing_sec

    def update_tracks(
        self,
        frame: np.ndarray,
        timestamp: float,
        min_conf: float = 0.25,
    ) -> List[TrackState]:
        """
        Run model.track() on a frame and return TrackState list.
        """
        results = self.model.track(
            frame,
            persist=True,
            verbose=False,
            classes=[0],  # person only
            conf=min_conf,
        )
        active_ids = set()
        for result in results:
            if result.boxes is None:
                continue
            for box in result.boxes:
                if box.id is None:
                    continue
                tid = int(box.id[0])
                conf = float(box.conf[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                bbox = [x1, y1, x2, y2]
                active_ids.add(tid)

                if tid not in self._track_states:
                    cx = (x1 + x2) / 2
                    cy = (y1 + y2) / 2
                    state = TrackState(
                        track_id=tid,
                        bbox=bbox,
                        centroid=(cx, cy),
                        first_seen=timestamp,
                        last_seen=timestamp,
                    )
                    self._track_states[tid] = state

                self._track_states[tid].update(bbox, conf, timestamp)

        # Deactivate missing
        for tid, state in self._track_states.items():
            if tid not in active_ids:
                state.frames_missing += 1
                if (timestamp - state.last_seen) > self.max_missing_sec:
                    state.active = False
            else:
                state.active = True

        return [s for s in self._track_states.values() if s.active]

    def get_all_tracks(self) -> List[TrackState]:
        return list(self._track_states.values())


# ─── Factory ───────────────────────────────────────────────────────────────────

def build_tracker(model=None, settings: dict = None):
    """
    Return best available tracker.
    If model is provided and ultralytics available → YOLOByteTracker.
    Otherwise → CentroidTracker (fallback).
    """
    settings = settings or {}
    max_distance = settings.get("centroid_max_distance", 80.0)
    max_missing_frames = settings.get("centroid_max_missing_frames", 15)
    max_missing_sec = settings.get("track_max_missing_sec", 2.0)

    if model is not None:
        try:
            from ultralytics import YOLO
            return YOLOByteTracker(model, max_missing_sec=max_missing_sec)
        except ImportError:
            pass

    print("[tracker] Using fallback CentroidTracker.")
    return CentroidTracker(
        max_distance=max_distance,
        max_missing_frames=max_missing_frames,
        max_missing_sec=max_missing_sec,
    )
