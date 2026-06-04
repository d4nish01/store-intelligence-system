"""
tools/calibrate_zones.py

Purpose:
    Help create a layout JSON file from a store layout image or video frame.
    The output JSON defines zone polygons, entry lines, and camera coverage
    used by the detection pipeline.

Usage:
    # From image
    python tools/calibrate_zones.py \\
        --image data/raw/store_001/layout.png \\
        --store-id STORE_001 \\
        --out config/layouts/STORE_001.layout.json

    # From video (uses first frame)
    python tools/calibrate_zones.py \\
        --video data/raw/store_001/zone.mp4 \\
        --store-id STORE_001 \\
        --out config/layouts/STORE_001.layout.json

    # Generate template only (no image)
    python tools/calibrate_zones.py \\
        --store-id STORE_001 \\
        --out config/layouts/STORE_001.layout.json \\
        --template-only

Modes:
    1. Interactive GUI (requires OpenCV window support):
       - Click to add polygon points.
       - Press ENTER to finish a polygon and name it.
       - Press 'l' to add an entry line (2 points).
       - Press 's' to save.
       - Press 'q' to quit without saving.

    2. Template mode (--template-only or no display):
       - Generates an editable JSON template with placeholder coordinates.
       - Prints clear instructions for manual editing.
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ─── Template layout builder ───────────────────────────────────────────────────

def build_template_layout(
    store_id: str,
    width: int = 1280,
    height: int = 720,
) -> Dict[str, Any]:
    """
    Generate a placeholder layout JSON template.
    Coordinates are expressed as [x, y] pixel positions in the camera frame.

    Edit this file with actual coordinates from your layout image.
    Use an image editor or paint tool to find pixel coordinates.
    """
    w, h = width, height
    return {
        "store_id": store_id,
        "open_time": "09:00",
        "close_time": "21:00",
        "coordinate_space": {
            "width": w,
            "height": h,
            "unit": "pixels",
            "note": "Coordinates are in camera/image pixel space. Edit to match your actual layout."
        },

        "zones": [
            {
                "zone_id": "ENTRY_AREA",
                "zone_type": "entry",
                "sku_zone": None,
                "camera_ids": ["CAM_ENTRY_01", "CAM_ENTRY_02"],
                "polygon": [
                    [0, 0], [w, 0], [w, int(h * 0.15)], [0, int(h * 0.15)]
                ],
                "note": "Entry/exit area near the store entrance. Adjust polygon to match actual location."
            },
            {
                "zone_id": "ZONE_A",
                "zone_type": "product",
                "sku_zone": "CATEGORY_A",
                "camera_ids": ["CAM_ZONE_01"],
                "polygon": [
                    [int(w * 0.05), int(h * 0.2)],
                    [int(w * 0.45), int(h * 0.2)],
                    [int(w * 0.45), int(h * 0.55)],
                    [int(w * 0.05), int(h * 0.55)],
                ],
                "note": "Product zone A. Change zone_id, sku_zone, and polygon to match your store layout."
            },
            {
                "zone_id": "ZONE_B",
                "zone_type": "product",
                "sku_zone": "CATEGORY_B",
                "camera_ids": ["CAM_ZONE_01"],
                "polygon": [
                    [int(w * 0.55), int(h * 0.2)],
                    [int(w * 0.95), int(h * 0.2)],
                    [int(w * 0.95), int(h * 0.55)],
                    [int(w * 0.55), int(h * 0.55)],
                ],
                "note": "Product zone B."
            },
            {
                "zone_id": "BILLING",
                "zone_type": "billing",
                "sku_zone": None,
                "camera_ids": ["CAM_BILLING_01", "CAM_ZONE_01"],
                "polygon": [
                    [int(w * 0.05), int(h * 0.65)],
                    [int(w * 0.55), int(h * 0.65)],
                    [int(w * 0.55), int(h * 0.95)],
                    [int(w * 0.05), int(h * 0.95)],
                ],
                "note": "Billing/checkout counter zone. IMPORTANT: zone_type must be 'billing'."
            },
        ],

        "entry_lines": [
            {
                "line_id": "ENTRY_LINE_01",
                "camera_ids": ["CAM_ENTRY_01", "CAM_ENTRY_02"],
                "points": [
                    [0, int(h * 0.1)],
                    [w, int(h * 0.1)],
                ],
                "inbound_direction": "top_to_bottom",
                "note": (
                    "A horizontal line across the entry. "
                    "inbound_direction: 'top_to_bottom' means moving down = entering store. "
                    "Options: top_to_bottom, bottom_to_top, left_to_right, right_to_left."
                )
            }
        ],

        "camera_coverage": {
            "CAM_ENTRY_01": {
                "role": "entry_exit_primary",
                "filename": "entry 1.mp4",
                "covered_zones": ["ENTRY_AREA"],
            },
            "CAM_ENTRY_02": {
                "role": "entry_exit_secondary",
                "filename": "entry 2.mp4",
                "covered_zones": ["ENTRY_AREA"],
            },
            "CAM_ZONE_01": {
                "role": "main_floor_zone",
                "filename": "zone.mp4",
                "covered_zones": ["ZONE_A", "ZONE_B", "BILLING"],
            },
            "CAM_BILLING_01": {
                "role": "billing",
                "filename": "billing_area.mp4",
                "covered_zones": ["BILLING"],
            },
        },

        "_instructions": [
            "1. Open your layout image in an image viewer or paint app.",
            "2. Find pixel coordinates for each zone's corners (top-left = 0,0).",
            "3. Edit the 'polygon' arrays with the actual corner coordinates.",
            "4. Set correct 'zone_id', 'zone_type', and 'sku_zone' for each zone.",
            "5. Edit 'entry_lines' to match where your entrance line crosses.",
            "6. Set 'inbound_direction' based on which direction means entering the store.",
            "7. Update 'camera_ids' for each zone to list which cameras cover it.",
            "8. Remove this '_instructions' key when done.",
            f"9. Save as: config/layouts/{store_id}.layout.json",
        ],
    }


# ─── Image/frame extractor ─────────────────────────────────────────────────────

def load_image_or_frame(
    image_path: Optional[str] = None,
    video_path: Optional[str] = None,
) -> Tuple[Optional[Any], int, int]:
    """
    Load image from file or extract first frame from video.
    Returns (frame_or_none, width, height).
    """
    try:
        import cv2
    except ImportError:
        print("[calibrate] NOTE: OpenCV not installed. Falling back to template-only mode.")
        print("  Install with: pip install opencv-python")
        return None, 1280, 720

    frame = None
    if image_path:
        frame = cv2.imread(image_path)
        if frame is None:
            print(f"[calibrate] ERROR: Cannot read image: {image_path}")
            return None, 1280, 720

    elif video_path:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            print(f"[calibrate] ERROR: Cannot open video: {video_path}")
            return None, 1280, 720
        ret, frame = cap.read()
        cap.release()
        if not ret or frame is None:
            print(f"[calibrate] ERROR: Cannot read first frame from: {video_path}")
            return None, 1280, 720

    if frame is not None:
        h, w = frame.shape[:2]
        return frame, w, h

    return None, 1280, 720


# ─── Interactive GUI mode ──────────────────────────────────────────────────────

def run_interactive_calibration(frame, store_id: str, output_path: str):
    """
    OpenCV mouse-click GUI for polygon definition.
    - Left click: add point to current polygon.
    - ENTER: finish polygon, prompt for name.
    - 'l': start entry line (requires exactly 2 clicks).
    - 's': save current state to JSON.
    - 'r': reset current polygon.
    - 'q': quit.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("[calibrate] OpenCV required for interactive mode.")
        return

    h, w = frame.shape[:2]
    display = frame.copy()
    current_points: List[List[int]] = []
    zones: List[Dict] = []
    entry_lines: List[Dict] = []
    line_mode = False
    line_points: List[List[int]] = []
    zone_colors = [
        (0, 255, 0), (255, 128, 0), (0, 128, 255),
        (255, 0, 255), (0, 255, 255), (128, 0, 255),
    ]

    def redraw():
        nonlocal display
        display = frame.copy()

        # Draw saved zones
        for i, zone in enumerate(zones):
            pts = np.array(zone["polygon"], dtype=np.int32)
            color = zone_colors[i % len(zone_colors)]
            cv2.polylines(display, [pts], True, color, 2)
            cx = int(sum(p[0] for p in zone["polygon"]) / len(zone["polygon"]))
            cy = int(sum(p[1] for p in zone["polygon"]) / len(zone["polygon"]))
            cv2.putText(display, zone["zone_id"], (cx - 30, cy),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        # Draw saved entry lines
        for el in entry_lines:
            pts = el["points"]
            if len(pts) >= 2:
                cv2.line(display, tuple(pts[0]), tuple(pts[1]), (0, 0, 255), 2)
                cv2.putText(display, el["line_id"], tuple(pts[0]),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

        # Draw current polygon in progress
        if current_points:
            for p in current_points:
                cv2.circle(display, tuple(p), 5, (255, 255, 0), -1)
            if len(current_points) > 1:
                for i in range(len(current_points) - 1):
                    cv2.line(display, tuple(current_points[i]), tuple(current_points[i+1]),
                             (255, 255, 0), 1)

        # Draw current line in progress
        if line_mode and line_points:
            for p in line_points:
                cv2.circle(display, tuple(p), 5, (0, 0, 255), -1)

        # Status text
        mode_txt = "LINE MODE" if line_mode else "POLYGON MODE"
        cv2.putText(display, f"[{mode_txt}] ENTER=save zone | l=entry line | s=save | r=reset | q=quit",
                    (10, h - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1)
        cv2.imshow("Zone Calibration", display)

    def mouse_callback(event, x, y, flags, param):
        nonlocal line_points
        if event == cv2.EVENT_LBUTTONDOWN:
            if line_mode:
                line_points.append([x, y])
                if len(line_points) == 2:
                    pass  # Will finalize on keypress
            else:
                current_points.append([x, y])
            redraw()

    cv2.namedWindow("Zone Calibration", cv2.WINDOW_NORMAL)
    cv2.setMouseCallback("Zone Calibration", mouse_callback)
    redraw()

    print("\n[calibrate] Interactive mode. Controls:")
    print("  Left click  — Add point")
    print("  ENTER       — Finish zone polygon (prompts for name)")
    print("  l           — Toggle entry line mode (click 2 points, then press l again)")
    print("  r           — Reset current polygon")
    print("  s           — Save layout JSON")
    print("  q           — Quit\n")

    while True:
        key = cv2.waitKey(1) & 0xFF

        if key == ord('q'):
            print("[calibrate] Quit without saving final zones.")
            break

        elif key == 13:  # ENTER
            if not line_mode and len(current_points) >= 3:
                zone_id = input(f"\nEnter zone_id for this polygon (e.g. ZONE_A, BILLING): ").strip()
                zone_type = input("zone_type (product/billing/entry/restricted): ").strip() or "product"
                sku_zone = input("sku_zone (optional, press ENTER to skip): ").strip() or None
                zones.append({
                    "zone_id": zone_id,
                    "zone_type": zone_type,
                    "sku_zone": sku_zone,
                    "camera_ids": [],
                    "polygon": list(current_points),
                })
                current_points.clear()
                print(f"[calibrate] Zone '{zone_id}' saved ({len(zones)} total).")
                redraw()
            else:
                print("[calibrate] Need at least 3 points to save a polygon.")

        elif key == ord('l'):
            if not line_mode:
                line_mode = True
                line_points = []
                print("[calibrate] Entry line mode ON. Click 2 points, then press 'l' again.")
            else:
                if len(line_points) == 2:
                    line_id = input(f"\nEnter line_id (e.g. ENTRY_LINE_01): ").strip() or f"ENTRY_LINE_0{len(entry_lines)+1}"
                    direction = input("inbound_direction (top_to_bottom/bottom_to_top/left_to_right/right_to_left): ").strip() or "top_to_bottom"
                    entry_lines.append({
                        "line_id": line_id,
                        "camera_ids": ["CAM_ENTRY_01", "CAM_ENTRY_02"],
                        "points": list(line_points),
                        "inbound_direction": direction,
                    })
                    print(f"[calibrate] Entry line '{line_id}' saved.")
                    line_mode = False
                    line_points = []
                    redraw()
                else:
                    print(f"[calibrate] Need exactly 2 points for a line. Got {len(line_points)}.")
                    line_mode = False
                    line_points = []

        elif key == ord('r'):
            current_points.clear()
            line_points.clear()
            print("[calibrate] Reset current polygon/line.")
            redraw()

        elif key == ord('s'):
            layout = build_template_layout(store_id, w, h)
            layout["zones"] = zones or layout["zones"]
            layout["entry_lines"] = entry_lines or layout["entry_lines"]
            layout["coordinate_space"] = {"width": w, "height": h, "unit": "pixels"}
            _save_layout(layout, output_path)
            print(f"[calibrate] Layout saved to: {output_path}")

    cv2.destroyAllWindows()

    # Final save if zones defined
    if zones or entry_lines:
        layout = build_template_layout(store_id, w, h)
        if zones:
            layout["zones"] = zones
        if entry_lines:
            layout["entry_lines"] = entry_lines
        _save_layout(layout, output_path)
        print(f"[calibrate] Final layout saved: {output_path}")


def _save_layout(layout: Dict[str, Any], output_path: str):
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(layout, f, indent=2, ensure_ascii=False)


def _check_display_available() -> bool:
    """Check if OpenCV display is available (X server or GUI available)."""
    try:
        import cv2
        # Try to create a named window; will fail if no display
        cv2.namedWindow("_test", cv2.WINDOW_NORMAL)
        cv2.destroyWindow("_test")
        return True
    except Exception:
        return False


# ─── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Calibrate zone polygons and entry lines for a store layout."
    )
    parser.add_argument("--image", help="Path to layout image (.png/.jpg)")
    parser.add_argument("--video", help="Path to camera video (uses first frame)")
    parser.add_argument("--store-id", required=True, help="Store identifier, e.g. STORE_001")
    parser.add_argument("--out", required=True, help="Output layout JSON path")
    parser.add_argument("--template-only", action="store_true",
                        help="Generate template JSON without GUI (edit coordinates manually)")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"  Store Intelligence — Zone Calibration Tool")
    print(f"  Store: {args.store_id}")
    print(f"  Output: {args.out}")
    print(f"{'='*60}\n")

    # Load image/frame
    frame, w, h = load_image_or_frame(args.image, args.video)

    if args.template_only or frame is None:
        # Template-only mode
        print("[calibrate] Generating layout template...")
        layout = build_template_layout(args.store_id, w, h)
        _save_layout(layout, args.out)

        print(f"\n[calibrate] Template layout saved to: {args.out}")
        print("\n" + "─"*60)
        print("  NEXT STEPS:")
        print(f"  1. Open {args.out} in any text editor.")
        if args.image:
            print(f"  2. Open {args.image} in an image viewer.")
        print("  3. For each zone, update the 'polygon' array with actual")
        print("     pixel coordinates from your layout image.")
        print("     (x=0, y=0 is top-left corner)")
        print("  4. Set correct 'zone_id', 'zone_type', 'sku_zone' for each zone.")
        print("  5. Update 'entry_lines' with the exact entry threshold line.")
        print("  6. Remove the '_instructions' key when done.")
        print("─"*60 + "\n")
        return

    # Try interactive GUI
    display_ok = _check_display_available()
    if display_ok:
        print("[calibrate] Interactive GUI mode. See instructions in terminal.")
        run_interactive_calibration(frame, args.store_id, args.out)
    else:
        print("[calibrate] No display available. Saving annotated template image and JSON template.")

        # Save annotated frame as reference
        try:
            import cv2
            ref_img_path = args.out.replace(".json", "_reference.jpg")
            cv2.imwrite(ref_img_path, frame)
            print(f"[calibrate] Reference frame saved to: {ref_img_path}")
            print(f"            Use this image to identify pixel coordinates for zones.")
        except Exception as e:
            print(f"[calibrate] Could not save reference image: {e}")

        layout = build_template_layout(args.store_id, w, h)
        _save_layout(layout, args.out)
        print(f"[calibrate] Template layout saved to: {args.out}")
        print("\n  Edit the layout JSON with actual coordinates from the reference image.")
        print(f"  Image dimensions: {w} × {h} pixels\n")


if __name__ == "__main__":
    main()
