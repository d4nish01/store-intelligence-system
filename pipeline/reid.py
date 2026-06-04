"""
pipeline/reid.py

Responsibilities:
- Convert camera-local track IDs to global visitor_id tokens.
- Merge duplicate ENTRY/EXIT events from CAM_ENTRY_01 and CAM_ENTRY_02.
- Detect re-entry (same visitor after EXIT).
- Prevent double-counting.

Key rule:
  If CAM_ENTRY_01 and CAM_ENTRY_02 produce ENTRY/EXIT of same direction
  within dedup_window_seconds → merge into one event (prefer primary camera).
  If secondary camera confirms → boost confidence.
"""

import hashlib
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

# ─── Visitor ID generation ─────────────────────────────────────────────────────

def _make_visitor_id(store_id: str, local_key: str) -> str:
    """
    Generate a short, stable visitor ID from store+local key.
    Format: VIS_{6-char hex}
    """
    raw = f"{store_id}:{local_key}"
    digest = hashlib.md5(raw.encode()).hexdigest()[:6]
    return f"VIS_{digest}"


# ─── Session tracker ───────────────────────────────────────────────────────────

class VisitorSession:
    """Tracks state of one visitor session."""

    def __init__(self, visitor_id: str, store_id: str):
        self.visitor_id = visitor_id
        self.store_id = store_id
        self.session_seq = 0
        self.is_inside = False
        self.last_exit_ts: Optional[float] = None    # epoch seconds
        self.entry_count = 0
        self.exit_count = 0
        self.reentry_count = 0

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq


# ─── Identity manager ──────────────────────────────────────────────────────────

class VisitorIdentityManager:
    """
    Manages visitor identity across cameras within a store.

    - Converts (camera_id, local_track_id) → global visitor_id.
    - Tracks session state (entry/exit, re-entry).
    - Provides session_seq counter.
    """

    def __init__(
        self,
        store_id: str,
        reentry_window_sec: float = 3600.0,  # 1 hour: within this, same person → REENTRY
    ):
        self.store_id = store_id
        self.reentry_window_sec = reentry_window_sec

        # (camera_id, local_track_id) → visitor_id
        self._local_to_visitor: Dict[Tuple[str, int], str] = {}

        # visitor_id → VisitorSession
        self._sessions: Dict[str, VisitorSession] = {}

        # Appearance fingerprint → visitor_id (future: appearance-based ReID)
        # For now we rely on cross-entry-camera dedup
        self._entry_merge_buffer: List[Dict[str, Any]] = []

    def assign_visitor_id(
        self,
        camera_id: str,
        local_track_id: int,
        timestamp: float,          # seconds since video start (used internally)
        bbox: Optional[List[float]] = None,
        role: str = "main_floor_zone",
    ) -> str:
        """
        Return or create a global visitor_id for a (camera, track) pair.

        For entry cameras: IDs are created fresh on first entry.
        Cross-camera matching (entry1 ↔ entry2) is handled by merge_entry_events().
        """
        key = (camera_id, local_track_id)
        if key in self._local_to_visitor:
            return self._local_to_visitor[key]

        # Create new visitor_id
        vid = _make_visitor_id(self.store_id, f"{camera_id}_{local_track_id}_{timestamp:.1f}")
        self._local_to_visitor[key] = vid
        self._sessions[vid] = VisitorSession(vid, self.store_id)
        return vid

    def get_session(self, visitor_id: str) -> Optional[VisitorSession]:
        return self._sessions.get(visitor_id)

    def get_or_create_session(self, visitor_id: str) -> VisitorSession:
        if visitor_id not in self._sessions:
            self._sessions[visitor_id] = VisitorSession(visitor_id, self.store_id)
        return self._sessions[visitor_id]

    def next_seq(self, visitor_id: str) -> int:
        session = self.get_or_create_session(visitor_id)
        return session.next_seq()

    def record_entry(self, visitor_id: str, timestamp_epoch: float):
        session = self.get_or_create_session(visitor_id)
        session.is_inside = True
        session.entry_count += 1
        session.last_exit_ts = None

    def record_exit(self, visitor_id: str, timestamp_epoch: float):
        session = self.get_or_create_session(visitor_id)
        session.is_inside = False
        session.exit_count += 1
        session.last_exit_ts = timestamp_epoch

    def detect_reentry(self, visitor_id: str, timestamp_epoch: float) -> bool:
        """
        Returns True if this event is a re-entry (visitor previously exited).
        Updates session state accordingly.
        """
        session = self.get_or_create_session(visitor_id)
        if session.last_exit_ts is None:
            return False
        elapsed = timestamp_epoch - session.last_exit_ts
        if 0 < elapsed <= self.reentry_window_sec:
            session.reentry_count += 1
            session.is_inside = True
            return True
        return False

    def remap_visitor(self, old_visitor_id: str, new_visitor_id: str, camera_id: str, local_track_id: int):
        """
        After entry-camera dedup merge: remap local track to the canonical visitor_id.
        """
        key = (camera_id, local_track_id)
        self._local_to_visitor[key] = new_visitor_id
        if old_visitor_id != new_visitor_id:
            # Transfer session if needed
            if old_visitor_id in self._sessions and new_visitor_id not in self._sessions:
                self._sessions[new_visitor_id] = self._sessions.pop(old_visitor_id)
            elif old_visitor_id in self._sessions:
                # Merge seq counter (take max)
                old_seq = self._sessions[old_visitor_id].session_seq
                self._sessions[new_visitor_id].session_seq = max(
                    self._sessions[new_visitor_id].session_seq, old_seq
                )
                del self._sessions[old_visitor_id]


# ─── Entry-camera deduplication ────────────────────────────────────────────────

def merge_entry_events(
    events: List[Dict[str, Any]],
    primary_camera_id: str = "CAM_ENTRY_01",
    secondary_camera_id: str = "CAM_ENTRY_02",
    dedup_window_seconds: float = 4.0,
    identity_manager: Optional[VisitorIdentityManager] = None,
) -> List[Dict[str, Any]]:
    """
    Deduplicate ENTRY/EXIT events from two entry cameras.

    Rules:
    1. Find pairs: (primary ENTRY/EXIT) within dedup_window_seconds of (secondary ENTRY/EXIT)
       with same event_type.
    2. Keep the primary camera event.
    3. Boost confidence if secondary confirms.
    4. Drop the secondary duplicate.
    5. If only secondary saw it: keep it with lower confidence.

    Args:
        events: Sorted list of event dicts (by timestamp).
        primary_camera_id: e.g. "CAM_ENTRY_01"
        secondary_camera_id: e.g. "CAM_ENTRY_02"
        dedup_window_seconds: Merge window.
        identity_manager: If provided, remap secondary visitor_ids to primary.

    Returns:
        Deduplicated event list.
    """
    entry_exit_types = {"ENTRY", "EXIT", "REENTRY"}

    # Separate relevant and irrelevant events
    primary_events = []
    secondary_events = []
    other_events = []

    for ev in events:
        et = ev.get("event_type", "")
        cam = ev.get("camera_id", "")
        if et in entry_exit_types and cam == primary_camera_id:
            primary_events.append(ev)
        elif et in entry_exit_types and cam == secondary_camera_id:
            secondary_events.append(ev)
        else:
            other_events.append(ev)

    # For each secondary event, find matching primary event
    merged = list(primary_events)
    unmatched_secondary = []

    for sec_ev in secondary_events:
        sec_ts = _parse_ts(sec_ev["timestamp"])
        sec_type = sec_ev["event_type"]
        matched = False

        for pri_ev in primary_events:
            pri_ts = _parse_ts(pri_ev["timestamp"])
            pri_type = pri_ev["event_type"]

            if pri_type != sec_type:
                continue
            if abs(pri_ts - sec_ts) <= dedup_window_seconds:
                # Confirmed by secondary → boost primary confidence
                new_conf = min(1.0, pri_ev["confidence"] + 0.08)
                pri_ev["confidence"] = round(new_conf, 4)
                pri_ev.setdefault("metadata", {})
                pri_ev["metadata"]["secondary_confirmed"] = True

                # Remap secondary visitor_id → primary visitor_id
                if identity_manager and sec_ev.get("visitor_id") != pri_ev.get("visitor_id"):
                    identity_manager.remap_visitor(
                        sec_ev["visitor_id"],
                        pri_ev["visitor_id"],
                        secondary_camera_id,
                        -1,  # We don't have local_track_id here
                    )

                matched = True
                break

        if not matched:
            # Secondary saw it alone → keep with reduced confidence
            sec_ev["confidence"] = round(max(0.3, sec_ev.get("confidence", 0.5) - 0.1), 4)
            sec_ev.setdefault("metadata", {})
            sec_ev["metadata"]["single_camera_only"] = True
            unmatched_secondary.append(sec_ev)

    result = merged + unmatched_secondary + other_events

    # Re-sort by timestamp
    result.sort(key=lambda e: e.get("timestamp", ""))
    return result


def _parse_ts(ts_str: str) -> float:
    """Parse ISO-8601 UTC timestamp to epoch seconds."""
    try:
        if ts_str.endswith("Z"):
            ts_str = ts_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts_str)
        return dt.timestamp()
    except Exception:
        return 0.0
