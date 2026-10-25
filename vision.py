"""
vision.py — Unified Computer Vision Pipeline for Plant Measurement

Extracts:
1. Reference rod detection -> pixels-per-cm scale calibration
2. Plant segmentation -> height (cm), stem width (cm), true leaf count
3. Visual debug overlay rendering
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Union
import cv2
import numpy as np


@dataclass
class Rectangle:
    x: int
    y: int
    w: int
    h: int

    @property
    def area(self) -> int:
        return self.w * self.h


@dataclass
class MeasurementResult:
    height_cm: float
    stem_width_cm: Optional[float]
    leaf_count: int
    pixels_per_cm: float
    calibrated: bool
    annotated_image: np.ndarray
    rod_rect: Optional[Rectangle] = None
    plant_rect: Optional[Rectangle] = None


def detect_reference_rod(
    image: np.ndarray,
    rod_length_cm: float = 12.0,
    rod_hue: int = 0,
    rod_hue_tolerance: int = 15,
) -> Tuple[Optional[Rectangle], float, bool]:
    """
    Detects the vertical calibration rod (typically red/orange) and
    calculates the pixels_per_cm conversion factor.
    Returns: (rod_rect, pixels_per_cm, is_calibrated)
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    lower_hue = (rod_hue - rod_hue_tolerance) % 180
    upper_hue = (rod_hue + rod_hue_tolerance) % 180

    if lower_hue <= upper_hue:
        mask = cv2.inRange(
            hsv,
            np.array([lower_hue, 70, 40], dtype=np.uint8),
            np.array([upper_hue, 255, 255], dtype=np.uint8),
        )
    else:
        m1 = cv2.inRange(
            hsv,
            np.array([0, 70, 40], dtype=np.uint8),
            np.array([upper_hue, 255, 255], dtype=np.uint8),
        )
        m2 = cv2.inRange(
            hsv,
            np.array([lower_hue, 70, 40], dtype=np.uint8),
            np.array([179, 255, 255], dtype=np.uint8),
        )
        mask = cv2.bitwise_or(m1, m2)

    # Vertical opening to emphasize vertical rods and remove isolated noise
    h, w = image.shape[:2]
    k_h = max(15, h // 80)
    kernel_vert = cv2.getStructuringElement(cv2.MORPH_RECT, (3, k_h))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel_vert)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best_rect: Optional[Rectangle] = None
    best_aspect = 0.0

    min_rod_h = h // 15
    for c in contours:
        rx, ry, rw, rh = cv2.boundingRect(c)
        if rh < min_rod_h:
            continue
        aspect = rh / max(1, rw)
        # Vertical rod aspect ratio > 2.0
        if aspect > 2.0 and aspect > best_aspect:
            best_aspect = aspect
            best_rect = Rectangle(rx, ry, rw, rh)

    if best_rect and best_rect.h > 0:
        px_per_cm = best_rect.h / float(rod_length_cm)
        return best_rect, px_per_cm, True

    # Fallback calibration (typical 1080p-4k webcam distance ~ 35-70 px/cm)
    default_px_per_cm = 50.0
    return None, default_px_per_cm, False


def detect_plant(
    image: np.ndarray,
    pixels_per_cm: float,
    rod_rect: Optional[Rectangle] = None,
) -> Tuple[Rectangle, float, Optional[float], int, np.ndarray]:
    """
    Segments green plant foliage and measures height, stem width, and leaves.
    Returns: (plant_rect, height_cm, stem_width_cm, leaf_count, plant_mask)
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    # Plant foliage green range
    plant_mask = cv2.inRange(
        hsv,
        np.array([25, 30, 25], dtype=np.uint8),
        np.array([90, 255, 255], dtype=np.uint8),
    )

    # Exclude reference rod region if detected
    if rod_rect:
        pad = 10
        x0 = max(0, rod_rect.x - pad)
        y0 = max(0, rod_rect.y - pad)
        x1 = min(image.shape[1], rod_rect.x + rod_rect.w + pad)
        y1 = min(image.shape[0], rod_rect.y + rod_rect.h + pad)
        plant_mask[y0:y1, x0:x1] = 0

    # Morphological noise filtering
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    plant_mask = cv2.morphologyEx(plant_mask, cv2.MORPH_OPEN, kernel)
    plant_mask = cv2.morphologyEx(plant_mask, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(plant_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return Rectangle(0, 0, 0, 0), 0.0, None, 0, plant_mask

    # Pick the main plant contour (largest area)
    main_contour = max(contours, key=cv2.contourArea)
    px, py, pw, ph = cv2.boundingRect(main_contour)
    plant_rect = Rectangle(px, py, pw, ph)

    # Height in cm
    height_cm = round(ph / pixels_per_cm, 2)

    # Stem width: inspect contour width in lower 15% of the plant bounding box
    stem_y_start = py + int(ph * 0.85)
    stem_y_end = py + ph
    stem_region = plant_mask[stem_y_start:stem_y_end, px : px + pw]
    stem_widths = []
    for row in stem_region:
        white_pixels = np.count_nonzero(row)
        if white_pixels > 0:
            stem_widths.append(white_pixels)

    stem_width_cm = None
    if stem_widths:
        median_stem_px = float(np.median(stem_widths))
        stem_width_cm = round(median_stem_px / pixels_per_cm, 3)

    # Leaf count estimation: convexity defects & contour peaks
    leaf_count = 2  # base cotyledon / true leaves
    hull = cv2.convexHull(main_contour, returnPoints=False)
    if hull is not None and len(hull) > 3 and len(main_contour) > 3:
        try:
            defects = cv2.convexityDefects(main_contour, hull)
            if defects is not None:
                # Count significant deep defects representing leaf separations
                significant_defects = 0
                for i in range(defects.shape[0]):
                    s, e, f, d = defects[i, 0]
                    depth_px = d / 256.0
                    if depth_px > (ph * 0.05):  # Defect depth > 5% of height
                        significant_defects += 1
                leaf_count = max(2, min(14, significant_defects + 1))
        except Exception:
            pass

    return plant_rect, height_cm, stem_width_cm, leaf_count, plant_mask


def measure_image(
    image_input: Union[str, Path, np.ndarray, bytes],
    rod_length_cm: float = 12.0,
) -> MeasurementResult:
    """
    Main vision entry point:
    Accepts image path, bytes, or numpy ndarray.
    Measures rod + plant and returns annotated image.
    """
    if isinstance(image_input, (str, Path)):
        img = cv2.imread(str(image_input))
        if img is None:
            raise FileNotFoundError(f"Could not load image from: {image_input}")
    elif isinstance(image_input, bytes):
        arr = np.frombuffer(image_input, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not decode image from provided bytes.")
    elif isinstance(image_input, np.ndarray):
        img = image_input.copy()
    else:
        raise TypeError(f"Unsupported image input type: {type(image_input)}")

    # 1. Detect calibration rod
    rod_rect, px_per_cm, calibrated = detect_reference_rod(img, rod_length_cm=rod_length_cm)

    # 2. Detect plant
    plant_rect, height_cm, stem_width_cm, leaf_count, _ = detect_plant(img, px_per_cm, rod_rect)

    # 3. Create rich annotated visualization
    annotated = img.copy()

    # Draw reference rod annotation (Orange / Yellow)
    if rod_rect:
        cv2.rectangle(
            annotated,
            (rod_rect.x, rod_rect.y),
            (rod_rect.x + rod_rect.w, rod_rect.y + rod_rect.h),
            (0, 165, 255),
            2,
        )
        cv2.putText(
            annotated,
            f"Rod: {rod_length_cm:.1f}cm ({rod_rect.h}px)",
            (rod_rect.x, max(25, rod_rect.y - 10)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 165, 255),
            2,
        )

    # Draw plant bounding box (Emerald Green)
    if plant_rect.w > 0 and plant_rect.h > 0:
        cv2.rectangle(
            annotated,
            (plant_rect.x, plant_rect.y),
            (plant_rect.x + plant_rect.w, plant_rect.y + plant_rect.h),
            (46, 204, 113),
            2,
        )

        # Height span vertical line
        x_center = plant_rect.x + plant_rect.w // 2
        cv2.line(
            annotated,
            (x_center, plant_rect.y),
            (x_center, plant_rect.y + plant_rect.h),
            (0, 255, 255),
            2,
        )

        # Overlay measurement HUD card
        hud_text = f"Height: {height_cm:.2f} cm | Stem: {stem_width_cm or 0:.2f} cm | Leaves: {leaf_count}"
        cv2.putText(
            annotated,
            hud_text,
            (plant_rect.x, max(30, plant_rect.y - 12)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (46, 204, 113),
            2,
        )

    # Scale status
    status_text = f"Scale: {px_per_cm:.2f} px/cm ({'CALIBRATED' if calibrated else 'FALLBACK ESTIMATE'})"
    cv2.putText(
        annotated,
        status_text,
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 0) if calibrated else (0, 140, 255),
        2,
    )

    return MeasurementResult(
        height_cm=height_cm,
        stem_width_cm=stem_width_cm,
        leaf_count=leaf_count,
        pixels_per_cm=px_per_cm,
        calibrated=calibrated,
        annotated_image=annotated,
        rod_rect=rod_rect,
        plant_rect=plant_rect,
    )
