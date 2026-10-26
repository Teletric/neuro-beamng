"""Turns the ground scan (bridge.lua B.groundScan) into a description of the terrain's shape.

The scan is a rough elevation map: along each bearing around the car, the height of the ground every
few metres (relative to the ground under the car), ending early at a wall or a big drop. From the
shape alone this finds what a driver cares about - how far she can drive each way, slopes and their
angle, banks and walls with their height, drop-offs with their depth, ramp-shaped jumps - and water,
which is the one thing the shape can't show.
"""

from __future__ import annotations

import math
from typing import Any

from .lidar import NAMED, SECTORS

STEEP = 0.8  # rise per metre beyond which it's a bank or wall, not a slope (~39 degrees)
WATER_WORDS = ("river", "water", "ocean", "lake")


def _what(name: str | None) -> str | None:
    """'Class:shape' from the scan -> a plain word, or None for ordinary ground."""
    if not name:
        return None
    text = name.lower()
    if any(w in text for w in WATER_WORDS):
        return "water"
    if text.startswith("forest"):
        return "bush" if any(w in text for w in ("shrub", "bush", "grass", "fern")) else "trees"
    for words, label in NAMED:
        if any(w in text.split(":", 1)[-1] for w in words):
            return label
    return None


def _deg(grade: float) -> float:
    return math.degrees(math.atan(abs(grade)))


def analyse(scan: dict[str, Any], step: float) -> tuple[str, float | None]:
    """One bearing -> (description, distance to the first hazard or None)."""
    heights = scan.get("h") or []
    heights = list(heights.values()) if isinstance(heights, dict) else list(heights)
    objs = scan.get("objs") or []
    objs = list(objs.values()) if isinstance(objs, dict) else objs
    what_at = {int(i): _what(n) for i, n in objs if _what(n)}

    hazard, hazard_at, end = None, None, len(heights)
    i = 0
    while i < len(heights):
        prev = heights[i - 1] if i else 0.0
        grade = (heights[i] - prev) / step
        d = (i + 1) * step
        if what_at.get(i + 1) == "water":
            hazard, hazard_at, end = "water", d, i
            break
        if grade > STEEP or grade < -STEEP:
            j = i  # follow the bank/drop while it stays steep
            while j + 1 < len(heights) and abs(heights[j + 1] - heights[j]) / step > STEEP \
                    and (heights[j + 1] > heights[j]) == (grade > 0):
                j += 1
            size = abs(heights[j] - prev)
            if grade < 0:
                hazard = f"a drop-off {size:.0f} m deep" if size >= 1.5 else f"a {size:.1f} m drop"
            else:
                hazard = f"{_label(what_at.get(i + 1), 'a steep bank')} {size:.1f} m high"
            # Ramp-shaped: a climb of 1 m or more just before the drop is a jump.
            if grade < 0 and i >= 2:
                k = i
                while k > 0 and 0.1 < (heights[k - 1] - (heights[k - 2] if k >= 2 else 0.0)) / step <= STEEP:
                    k -= 1
                climb = heights[i - 1] - (heights[k - 1] if k else 0.0)
                if climb >= 1.0:
                    hazard = f"a jump (rises {climb:.1f} m, then drops {size:.1f} m)"
                    d, i = (k + 1) * step, k
            hazard_at, end = d - step / 2, i
            break
        i += 1
    else:
        if scan.get("stop") == "wall":
            tall = scan.get("tall")
            label = _label(what_at.get(len(heights) + 1), "a wall or very steep bank")
            hazard = f"{label} {tall:.0f} m high" if tall else label
            hazard_at = float(scan.get("at") or (len(heights) + 1) * step)
        elif scan.get("stop") == "drop":
            hazard, hazard_at = "a sheer drop (over 12 m)", float(scan.get("at") or (len(heights) + 1) * step)

    shape = _shape(heights[:end], step)
    if shape is None:
        return (f"{hazard} at {hazard_at:.0f} m" if hazard else "no ground found"), hazard_at
    stretch = f"{shape} for {end * step:.0f} m"
    return (f"{stretch}, then {hazard} at {hazard_at:.0f} m" if hazard else stretch), hazard_at


def _label(what: str | None, default: str) -> str:
    return {"trees": "a tree", "water": "water", None: default}.get(what, f"a {what}")


def _shape(heights: list[float], step: float) -> str | None:
    if not heights:
        return None
    rise, dist = heights[-1], len(heights) * step
    grades = [(h - (heights[i - 1] if i else 0.0)) / step for i, h in enumerate(heights)]
    overall, steepest = _deg(rise / dist), max(_deg(g) for g in grades)
    if overall < 3 and steepest < 8:
        text = "flat"
    elif overall < 3:
        text = "level but uneven"
    else:
        text = f"{'uphill' if rise > 0 else 'downhill'} {overall:.0f}° ({rise:+.{0 if abs(rise) >= 1 else 1}f} m)"
    if steepest >= overall + 10 and text != "level but uneven":
        text += f", bumpy (up to {steepest:.0f}°)"
    return text


def open_ground(data: dict[str, Any] | None, clear_m: float = 30.0, max_slope: float = 15.0) -> list[str]:
    """Sectors where the ground stays open and gentle for at least clear_m: somewhere to cut across."""
    if not data or not data.get("scans"):
        return []
    scans = data["scans"]
    scans = list(scans.values()) if isinstance(scans, dict) else scans
    step, n = float(data.get("step", 2)), len(scans)
    if n % 8:
        return []
    out = []
    for k, name in enumerate(SECTORS):
        scan = scans[k * (n // 8)]
        _, hazard_at = analyse(scan, step)
        heights = scan.get("h") or []
        heights = list(heights.values()) if isinstance(heights, dict) else list(heights)
        wanted = int(clear_m / step)
        if (hazard_at is not None and hazard_at < clear_m) or len(heights) < wanted:
            continue
        stretch = heights[:wanted]
        grades = [(h - (stretch[i - 1] if i else 0.0)) / step for i, h in enumerate(stretch)]
        if _deg(stretch[-1] / clear_m) <= max_slope and max(_deg(g) for g in grades) <= 25:
            out.append(name)
    return out


def describe_ground(data: dict[str, Any] | None, range_m: float) -> str:
    """Eight compass sectors around the car, each from its centre bearing; hazards just either side
    of the centre (the neighbouring bearings) are reported if they're closer."""
    if not data or not data.get("scans"):
        return ""
    scans = data["scans"]
    scans = list(scans.values()) if isinstance(scans, dict) else scans
    step, n = float(data.get("step", 2)), len(scans)
    if n % 8:
        return ""
    per = n // 8
    parts = []
    for k, name in enumerate(SECTORS):
        centre = k * per
        text, at = analyse(scans[centre], step)
        for j in (centre - 1, centre + 1) if per > 1 else ():
            side_text, side_at = analyse(scans[j % n], step)
            if side_at is not None and (at is None or side_at < at - 4) and side_at < 15:
                text += f" (just to the {'left' if j < centre else 'right'}: {side_text.split(', then ')[-1]})"
                break
        parts.append(f"{name}: {text}")
    return f"- Ground shape (height scan, {range_m:.0f} m): " + "; ".join(parts)
