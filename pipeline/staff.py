"""
pipeline/staff.py

Responsibilities:
- Classify tracks as likely staff using duration, movement, and zone-pattern heuristics.
- Optional manual override via config/staff_overrides.yaml.
- Staff events are still emitted; is_staff=True lets backend exclude from metrics.
"""

import os
import yaml
from collections import defaultdict
from typing import Dict, List, Set, Optional, Any


# ─── Staff feature accumulator ─────────────────────────────────────────────────

class StaffFeatureAccumulator:
    """Tracks features per track_id to classify staff vs customer."""

    def __init__(
        self,
        staff_duration_threshold_sec: float = 3600.0,   # 1 hour+
        billing_zone_visits_threshold: int = 5,
        zone_transition_threshold: int = 15,
        time_window_count_threshold: int = 8,
        manual_staff_ids: Optional[Set[str]] = None,
    ):
        """
        Args:
            staff_duration_threshold_sec: Track present longer than this → staff candidate.
            billing_zone_visits_threshold: Visits billing zone this many times → staff.
            zone_transition_threshold: Total zone transitions → staff candidate.
            time_window_count_threshold: Active across this many 5-min windows → staff.
            manual_staff_ids: Set of track_ids manually marked as staff.
        """
        self.staff_duration_threshold_sec = staff_duration_threshold_sec
        self.billing_zone_visits_threshold = billing_zone_visits_threshold
        self.zone_transition_threshold = zone_transition_threshold
        self.time_window_count_threshold = time_window_count_threshold
        self.manual_staff_ids: Set[str] = manual_staff_ids or set()

        # Per-track state
        self._first_seen: Dict[str, float] = {}
        self._last_seen: Dict[str, float] = {}
        self._zone_visits: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._zone_transitions: Dict[str, int] = defaultdict(int)
        self._billing_visits: Dict[str, int] = defaultdict(int)
        self._prev_zone: Dict[str, Optional[str]] = {}
        self._active_time_windows: Dict[str, Set[int]] = defaultdict(set)
        self._staff_cache: Dict[str, Optional[bool]] = {}

    def update_staff_features(
        self,
        track_id: str,
        zone_id: Optional[str],
        timestamp: float,
        is_billing: bool = False,
    ):
        """
        Update heuristic features for a track.
        Call this every frame or zone-change event.

        Args:
            track_id: Camera-local or global track identifier.
            zone_id: Current zone_id or None.
            timestamp: Current timestamp in seconds from video start.
            is_billing: Whether current zone is a billing zone.
        """
        # Invalidate staff cache for this track
        self._staff_cache.pop(track_id, None)

        if track_id not in self._first_seen:
            self._first_seen[track_id] = timestamp
        self._last_seen[track_id] = timestamp

        # 5-minute window buckets
        window = int(timestamp // 300)
        self._active_time_windows[track_id].add(window)

        # Zone tracking
        if zone_id:
            self._zone_visits[track_id][zone_id] += 1
            prev = self._prev_zone.get(track_id)
            if prev is not None and prev != zone_id:
                self._zone_transitions[track_id] += 1
            self._prev_zone[track_id] = zone_id

        if is_billing:
            self._billing_visits[track_id] += 1

    def is_likely_staff(self, track_id: str) -> bool:
        """
        Returns True if track is likely a staff member.
        Result is cached until next update_staff_features call.
        """
        if track_id in self.manual_staff_ids:
            return True

        if track_id in self._staff_cache and self._staff_cache[track_id] is not None:
            return self._staff_cache[track_id]

        result = self._classify(track_id)
        self._staff_cache[track_id] = result
        return result

    def _classify(self, track_id: str) -> bool:
        first = self._first_seen.get(track_id, 0)
        last = self._last_seen.get(track_id, 0)
        duration_sec = last - first

        # Heuristic 1: Very long presence
        if duration_sec >= self.staff_duration_threshold_sec:
            return True

        # Heuristic 2: Many billing zone visits
        billing_count = self._billing_visits.get(track_id, 0)
        if billing_count >= self.billing_zone_visits_threshold:
            return True

        # Heuristic 3: Many zone transitions
        transitions = self._zone_transitions.get(track_id, 0)
        if transitions >= self.zone_transition_threshold:
            return True

        # Heuristic 4: Active across many time windows
        windows = len(self._active_time_windows.get(track_id, set()))
        if windows >= self.time_window_count_threshold:
            return True

        return False

    def staff_confidence(self, track_id: str) -> float:
        """
        Returns a confidence score (0–1) for how likely the track is staff.
        Combines multiple heuristic signals.
        """
        if track_id in self.manual_staff_ids:
            return 1.0

        score = 0.0
        first = self._first_seen.get(track_id, 0)
        last = self._last_seen.get(track_id, 0)
        duration_sec = last - first

        # Duration signal
        if duration_sec > 0:
            dur_score = min(1.0, duration_sec / self.staff_duration_threshold_sec)
            score += dur_score * 0.4

        # Billing visits signal
        billing_count = self._billing_visits.get(track_id, 0)
        billing_score = min(1.0, billing_count / self.billing_zone_visits_threshold)
        score += billing_score * 0.3

        # Zone transitions signal
        transitions = self._zone_transitions.get(track_id, 0)
        trans_score = min(1.0, transitions / self.zone_transition_threshold)
        score += trans_score * 0.2

        # Time window signal
        windows = len(self._active_time_windows.get(track_id, set()))
        win_score = min(1.0, windows / self.time_window_count_threshold)
        score += win_score * 0.1

        return round(min(1.0, score), 4)

    def get_all_staff_ids(self) -> Set[str]:
        """Return set of track_ids classified as staff."""
        return {tid for tid in self._first_seen if self.is_likely_staff(tid)}


# ─── Config loader ─────────────────────────────────────────────────────────────

def load_staff_overrides(
    store_id: str,
    camera_id: Optional[str] = None,
    overrides_path: str = "config/staff_overrides.yaml",
) -> Set[str]:
    """
    Load manually specified staff track IDs from config.
    Returns empty set if file missing or store not configured.

    File format:
      STORE_001:
        CAM_ZONE_01:
          staff_track_ids: [42, 17]
    """
    if not os.path.exists(overrides_path):
        return set()

    try:
        with open(overrides_path, "r") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[staff] Warning: Could not load staff_overrides.yaml: {e}")
        return set()

    store_data = data.get(store_id, {})
    ids: Set[str] = set()

    if camera_id:
        cam_data = store_data.get(camera_id, {})
        raw_ids = cam_data.get("staff_track_ids", [])
        ids.update(str(i) for i in raw_ids)
    else:
        # Load from all cameras for this store
        for cam, cam_data in store_data.items():
            raw_ids = cam_data.get("staff_track_ids", [])
            ids.update(str(i) for i in raw_ids)

    return ids


def build_staff_classifier(
    store_id: str,
    camera_id: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    overrides_path: str = "config/staff_overrides.yaml",
) -> StaffFeatureAccumulator:
    """
    Factory: build a StaffFeatureAccumulator with settings and manual overrides.
    """
    settings = settings or {}
    manual_ids = load_staff_overrides(store_id, camera_id, overrides_path)

    return StaffFeatureAccumulator(
        staff_duration_threshold_sec=settings.get("staff_duration_threshold_sec", 3600.0),
        billing_zone_visits_threshold=settings.get("staff_billing_visits_threshold", 5),
        zone_transition_threshold=settings.get("staff_zone_transitions_threshold", 15),
        time_window_count_threshold=settings.get("staff_time_windows_threshold", 8),
        manual_staff_ids=manual_ids,
    )
