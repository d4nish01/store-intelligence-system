"""
pipeline/geometry.py

Responsibilities:
- Bounding box centroid calculation.
- Point-in-polygon test (ray casting).
- Line crossing detection (segment intersection).
- Inbound/outbound direction inference.
- Zone membership lookup.
"""

import numpy as np
from typing import List, Tuple, Optional, Dict, Any


Point = Tuple[float, float]
Polygon = List[Point]          # list of (x, y) vertices
LinePoints = List[Point]       # exactly 2 points defining a line segment


# ─── Centroid ──────────────────────────────────────────────────────────────────

def bbox_centroid(bbox: List[float]) -> Point:
    """
    Return (cx, cy) for bbox [x1, y1, x2, y2].
    Uses bottom-center for better floor-plane accuracy.
    """
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2.0
    cy = y2  # bottom-center better represents standing position on floor
    return (cx, cy)


def bbox_center(bbox: List[float]) -> Point:
    """Return geometric center (cx, cy)."""
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


# ─── Point-in-polygon (ray casting) ────────────────────────────────────────────

def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """
    Ray casting algorithm.
    Returns True if point is inside the polygon.
    polygon: list of (x, y) vertices (closed or open; we close it automatically).
    """
    if len(polygon) < 3:
        return False
    x, y = point
    n = len(polygon)
    inside = False
    px, py = polygon[-1]
    for i in range(n):
        cx_v, cy_v = polygon[i]
        if ((cy_v > y) != (py > y)) and (
            x < (px - cx_v) * (y - cy_v) / (py - cy_v + 1e-10) + cx_v
        ):
            inside = not inside
        px, py = cx_v, cy_v
    return inside


# ─── Zone membership ───────────────────────────────────────────────────────────

def get_zone_for_point(
    point: Point,
    zones: List[Dict[str, Any]],
    camera_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Return the first zone whose polygon contains `point`, or None.

    zones: list of zone dicts from layout JSON, each with:
      {
        "zone_id": str,
        "zone_type": str,     # "product", "billing", "entry", "restricted"
        "sku_zone": str|None,
        "polygon": [[x,y], ...]
        "camera_ids": [str, ...]  # optional: which cameras see this zone
      }

    If camera_id is provided, only check zones covered by that camera (if camera_ids is set).
    """
    for zone in zones:
        # Optional: filter by camera coverage
        cam_ids = zone.get("camera_ids") or zone.get("camera_coverage", [])
        if camera_id and cam_ids and camera_id not in cam_ids:
            continue

        polygon = zone.get("polygon", [])
        if not polygon:
            continue

        # Normalize polygon to list of tuples
        poly_tuples = [(float(p[0]), float(p[1])) for p in polygon]
        if point_in_polygon(point, poly_tuples):
            return zone

    return None


def is_billing_zone(zone: Dict[str, Any]) -> bool:
    """Returns True if zone is a billing/queue zone."""
    if zone is None:
        return False
    zone_type = zone.get("zone_type", "").lower()
    zone_id = zone.get("zone_id", "").upper()
    return zone_type in ("billing", "checkout", "queue") or "BILLING" in zone_id or "CHECKOUT" in zone_id


# ─── Line crossing ─────────────────────────────────────────────────────────────

def _segments_intersect(
    p1: Point, p2: Point,
    p3: Point, p4: Point,
) -> bool:
    """
    Returns True if segment p1-p2 intersects segment p3-p4.
    Uses cross-product sign change method.
    """
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)

    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True

    # Collinear edge cases
    def on_segment(p, q, r):
        return (min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and
                min(p[1], r[1]) <= q[1] <= max(p[1], r[1]))

    if d1 == 0 and on_segment(p3, p1, p4):
        return True
    if d2 == 0 and on_segment(p3, p2, p4):
        return True
    if d3 == 0 and on_segment(p1, p3, p2):
        return True
    if d4 == 0 and on_segment(p1, p4, p2):
        return True

    return False


def line_crossed(
    prev_point: Point,
    curr_point: Point,
    line_points: LinePoints,
) -> bool:
    """
    Returns True if the trajectory from prev_point to curr_point crosses
    the line defined by line_points (exactly 2 endpoints).
    """
    if len(line_points) < 2:
        return False
    lp1 = (float(line_points[0][0]), float(line_points[0][1]))
    lp2 = (float(line_points[1][0]), float(line_points[1][1]))
    return _segments_intersect(prev_point, curr_point, lp1, lp2)


# ─── Direction inference ────────────────────────────────────────────────────────

def infer_direction(
    prev_point: Point,
    curr_point: Point,
    inbound_direction: str,
) -> str:
    """
    Determine if movement from prev_point to curr_point is "inbound" or "outbound".

    inbound_direction from layout config: "top_to_bottom", "bottom_to_top",
    "left_to_right", "right_to_left".

    Returns: "inbound" or "outbound"
    """
    dx = curr_point[0] - prev_point[0]
    dy = curr_point[1] - prev_point[1]

    direction_map = {
        "top_to_bottom": dy > 0,
        "bottom_to_top": dy < 0,
        "left_to_right": dx > 0,
        "right_to_left": dx < 0,
    }

    inbound_condition = direction_map.get(inbound_direction)
    if inbound_condition is None:
        # Try vector-based fallback: parse "top_to_bottom" style
        if "top" in inbound_direction and "bottom" in inbound_direction:
            inbound_condition = dy > 0
        elif "bottom" in inbound_direction and "top" in inbound_direction:
            inbound_condition = dy < 0
        elif "left" in inbound_direction and "right" in inbound_direction:
            inbound_condition = dx > 0
        else:
            inbound_condition = dy > 0  # default guess

    return "inbound" if inbound_condition else "outbound"


# ─── Entry line helpers ────────────────────────────────────────────────────────

def check_entry_crossing(
    prev_point: Optional[Point],
    curr_point: Point,
    entry_lines: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Check if trajectory from prev_point → curr_point crosses any entry line.

    entry_lines: list of dicts from layout JSON:
    {
      "line_id": "ENTRY_LINE_01",
      "points": [[x1,y1],[x2,y2]],
      "inbound_direction": "top_to_bottom"
    }

    Returns:
      None if no crossing.
      {"line_id": str, "direction": "inbound"|"outbound", "entry_line": dict}
    """
    if prev_point is None:
        return None

    for entry_line in entry_lines:
        pts = entry_line.get("points", [])
        if len(pts) < 2:
            continue
        if line_crossed(prev_point, curr_point, pts):
            inbound_dir = entry_line.get("inbound_direction", "top_to_bottom")
            direction = infer_direction(prev_point, curr_point, inbound_dir)
            return {
                "line_id": entry_line.get("line_id", "ENTRY_LINE"),
                "direction": direction,
                "entry_line": entry_line,
            }
    return None


def euclidean_distance(p1: Point, p2: Point) -> float:
    """Euclidean distance between two points."""
    return float(np.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2))
