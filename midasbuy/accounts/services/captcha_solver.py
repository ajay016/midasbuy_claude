"""
Free (self-hosted) Tencent TCaptcha slider solver.

The redeem endpoint returns FLEXIBLE_RISK_CONTROL:graphic with a slider URL
(card.harvestsharp.com/.../slider.html). The page renders it via
window.midas.newRiskControl(source) inside a #riskControlComponent iframe and
resolves with {rc_token, rc_uuid} once solved. This module does the actual
solving: screenshot the slider, find the gap offset, and drag the handle with a
human-like trajectory.

This is the "free" path (no paid API). A paid solver later is a drop-in swap of
solve_slider() with a service call. CV gap-detection is inherently tuning-heavy
and TCaptcha-version specific, so every attempt saves a debug PNG.

Dependencies: numpy (required), opencv-python (optional, improves detection).
"""
import logging
import os
import random
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

try:
    import cv2
except Exception:  # pragma: no cover
    cv2 = None


def _png_to_gray(png_bytes: bytes):
    """Decode PNG bytes to a grayscale numpy array."""
    if cv2 is not None:
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        return img
    # Pillow fallback
    from io import BytesIO
    from PIL import Image
    img = Image.open(BytesIO(png_bytes)).convert("L")
    return np.array(img)


def detect_gap_offset(png_bytes: bytes, save_debug_path: Optional[str] = None) -> Optional[int]:
    """
    Estimate the horizontal pixel offset of the puzzle gap from the left edge of
    the slider image. Uses vertical-edge energy: the gap/puzzle notch produces a
    strong vertical edge column. Returns x in image pixels, or None.

    This is a heuristic. Inspect the saved debug PNG and tune if needed.
    """
    if np is None:
        logger.error("[CAPTCHA] numpy not installed — cannot detect gap")
        return None
    try:
        gray = _png_to_gray(png_bytes)
        if gray is None:
            return None
        h, w = gray.shape[:2]

        if cv2 is not None:
            edges = cv2.Canny(gray, 100, 200)
        else:
            # crude gradient magnitude on the x axis
            gx = np.abs(np.diff(gray.astype(np.int16), axis=1))
            edges = (gx > 60).astype(np.uint8) * 255
            edges = np.pad(edges, ((0, 0), (0, 1)), mode="edge")

        # Sum edge energy per column; ignore the far-left band where the handle
        # sits, so we find the gap, not the puzzle piece's origin.
        col_energy = edges.sum(axis=0).astype(float)
        left_ignore = int(w * 0.12)
        col_energy[:left_ignore] = 0

        # Smooth a little so we pick a column, not noise.
        k = max(1, w // 80)
        kernel = np.ones(k) / k
        smoothed = np.convolve(col_energy, kernel, mode="same")
        gap_x = int(np.argmax(smoothed))

        if save_debug_path:
            try:
                _save_debug(png_bytes, gap_x, save_debug_path)
            except Exception:
                pass

        logger.info("[CAPTCHA] gap detected at x=%d (img %dx%d)", gap_x, w, h)
        return gap_x
    except Exception:
        logger.exception("[CAPTCHA] gap detection failed")
        return None


def _save_debug(png_bytes: bytes, gap_x: int, path: str) -> None:
    from io import BytesIO
    from PIL import Image, ImageDraw
    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    d = ImageDraw.Draw(img)
    d.line([(gap_x, 0), (gap_x, img.height)], fill=(255, 0, 0), width=2)
    img.save(path)
    logger.info("[CAPTCHA] debug image saved: %s", path)


def human_trajectory(distance: int, steps: int = 30) -> list:
    """
    Build a human-like drag trajectory: accelerate, overshoot slightly, settle
    back. TCaptcha analyses the motion curve, so a constant-velocity slide fails.
    Returns a list of cumulative x offsets.
    """
    if distance <= 0:
        return [0]
    track = []
    current = 0.0
    mid = distance * random.uniform(0.7, 0.85)
    v = 0.0
    overshoot = distance + random.randint(2, 6)
    while current < overshoot:
        a = random.uniform(2.0, 4.0) if current < mid else -random.uniform(2.0, 4.0)
        v += a
        if v < 0:
            v = random.uniform(0.4, 1.0)
        current += v
        track.append(round(current))
    # settle back to the target with small corrections
    while current > distance:
        current -= random.uniform(0.6, 1.4)
        track.append(round(current))
    track.append(distance)
    return track


def drag_slider(page, handle_x: float, handle_y: float, distance: int) -> None:
    """Drag the slider handle by `distance` px with a human-like trajectory."""
    page.mouse.move(handle_x, handle_y)
    page.wait_for_timeout(random.randint(120, 280))
    page.mouse.down()
    page.wait_for_timeout(random.randint(80, 160))

    track = human_trajectory(distance)
    prev = 0
    for x in track:
        dx = x - prev
        prev = x
        jitter_y = handle_y + random.uniform(-1.5, 1.5)
        page.mouse.move(handle_x + x, jitter_y)
        page.wait_for_timeout(random.randint(8, 22))
    page.wait_for_timeout(random.randint(120, 260))
    page.mouse.up()


def solve_slider_in_container(
    page,
    container_selector: str,
    session_dir: Optional[str] = None,
    scale_hint: float = 1.0,
    attempt: int = 1,
) -> bool:
    """
    Screenshot the slider container, detect the gap, and drag the handle.
    Fallback when the clean background image isn't available.
    """
    try:
        el = page.query_selector(container_selector)
        if el is None:
            logger.error("[CAPTCHA] slider container %s not found", container_selector)
            return False
        box = el.bounding_box()
        if not box:
            logger.error("[CAPTCHA] slider container has no bounding box")
            return False

        png = el.screenshot()
        debug_path = os.path.join(session_dir, f"captcha_slider_{attempt}.png") if session_dir else None
        gap_x = detect_gap_offset(png, save_debug_path=debug_path)
        if gap_x is None:
            return False

        # The handle starts near the left; the drag distance is gap_x minus the
        # handle's resting position, with a small calibration margin.
        handle_start = box["x"] + box["width"] * 0.06
        handle_y = box["y"] + box["height"] * 0.5
        distance = int(gap_x * scale_hint - box["width"] * 0.06)
        distance = max(8, distance)

        logger.info(
            "[CAPTCHA] dragging handle start=(%.0f,%.0f) distance=%d (box %.0fx%.0f)",
            handle_start, handle_y, distance, box["width"], box["height"],
        )
        drag_slider(page, handle_start, handle_y, distance)
        return True
    except Exception:
        logger.exception("[CAPTCHA] solve_slider_in_container failed")
        return False


def solve_from_clean_bg(
    page,
    container_selector: str,
    bg_png: bytes,
    session_dir: Optional[str] = None,
    scale: float = 0.5,
    x_offset: int = 0,
    attempt: int = 1,
) -> bool:
    """
    Detect the gap on the CLEAN TCaptcha background image (672px natural width)
    fetched from cap_union_new_getcapbysig?img_index=1, then drag the rendered
    handle by gap_x * scale + x_offset.

    `scale` maps natural image px -> displayed px (TCaptcha renders the bg
    scaled; ~0.5 for the 672-wide global template). Tune via MIDASBUY_CAPTCHA_SCALE
    using the saved captcha_bg_*.png (red line = detected gap).
    """
    try:
        el = page.query_selector(container_selector)
        if el is None:
            return False
        box = el.bounding_box()
        if not box:
            return False

        debug_path = os.path.join(session_dir, f"captcha_bg_{attempt}.png") if session_dir else None
        gap_x = detect_gap_offset(bg_png, save_debug_path=debug_path)
        if gap_x is None:
            return False

        handle_start = box["x"] + box["width"] * 0.06
        handle_y = box["y"] + box["height"] * 0.5
        distance = max(8, int(gap_x * scale + x_offset))
        logger.info(
            "[CAPTCHA] clean-bg gap_x=%d scale=%.3f x_offset=%d -> distance=%d",
            gap_x, scale, x_offset, distance,
        )
        drag_slider(page, handle_start, handle_y, distance)
        return True
    except Exception:
        logger.exception("[CAPTCHA] solve_from_clean_bg failed")
        return False
