"""Turns raw lidar hits (bridge.lua B.lidar) into a few lines Neuro can drive by.

Each hit: [angle_deg (clockwise from the nose), distance, nx, ny, nz, height above car, class, shape,
dir_x, dir_y, closing m/s]. For vehicle hits, class is "BeamNGVehicle" and shape is the model's name.
The surface normal separates what she can drive up (ramps, slopes) from what stops her (walls).
"""

from __future__ import annotations

import math

SECTORS = ["ahead", "ahead-right", "right", "behind-right", "behind", "behind-left", "left", "ahead-left"]

RAMP_WORDS = ("ramp", "jump", "kicker", "flip", "quarter", "halfpipe", "launch")
NAMED = [  # mesh/class keyword -> what to call it
    (("shrub", "bush", "grass", "fern"), "bush"),  # before tree: "tree_douglasfir_bush" is a bush
    (("tree", "forest", "palm", "pine", "oak", "fir", "aspen", "birch"), "tree"),
    (("rock", "boulder", "cliff"), "rock"),
    (("barrier", "guardrail", "jersey", "railing", "fence", "bollard"), "barrier"),
    (("building", "house", "shed", "garage", "warehouse", "tower", "hangar"), "building"),
    (("pole", "post", "lamp", "light", "sign"), "pole"),
    (("pipe",), "pipe"),
    (("hill", "pyramid", "mound", "dome"), "hill"),
    (("bridge",), "bridge"),
    (("container", "crate", "box", "barrel"), "container"),
    (("wall", "block", "cube", "rectangle", "concrete"), "wall"),
]
GROUND_CLASSES = ("terrain", "meshroad", "groundplane")  # the ground itself (class names, lowercased)
WARN_SECONDS = 4.0  # time-to-impact below which she gets a collision warning


DRIVABLE_NZ = 0.6  # a slope whose surface normal points at least this far up (~53 degrees or gentler)


def drivable_slope(kind: str | None, nz: float) -> bool:
    """A level beam hits any incline in front of the car; a gentle enough one is just road, not an obstacle."""
    return kind in ("slope", "ramp-like slope", "hill") and nz >= DRIVABLE_NZ


def solid_note(what: str) -> str:
    """Everything the lidar sees except cars and bushes is static scenery in BeamNG: it never breaks or moves."""
    if what.startswith("car") or what.startswith("something"):
        return ""
    if "bush" in what:
        return " It's a bush: you can push through the leaves, just not its stems."
    return f" The {what} is solid scenery: it won't break or move, however hard you push."


def obstacle_name(what: str | None) -> str:
    """The pilot's note on what stopped it ('middle: car Gavril Bluebuck', 'left corner: Forest:shrub_b')
    in words: 'a car (Gavril Bluebuck)', 'a bush'."""
    if not what:
        return "something"
    thing = what.split(": ", 1)[-1]
    if thing.startswith("car "):
        return f"a car ({thing[4:]})"
    cls, _, shape = thing.partition(":")
    kind = classify(0.0, cls, shape)
    return f"a {kind}" if kind and not kind.startswith(("steep", "slope")) else "something solid"


def classify(nz: float, cls: str, shape: str) -> str | None:
    """None = flat ground, not worth reporting."""
    if cls == "BeamNGVehicle":
        return "vehicle"
    text = (shape or cls).lower()  # the object's own name says more than its class ("Forest" holds rocks too)
    if any(w in text for w in RAMP_WORDS):
        # Only the sloped surface is a ramp; its vertical sides and back are just obstacles.
        return "ramp" if nz > 0.3 else "ramp side (not jumpable)"
    if nz > 0.85:
        return None
    if any(g in cls.lower() for g in GROUND_CLASSES):  # before NAMED: "TerrainBlock" isn't a wall
        return "slope" if nz > 0.3 else "steep terrain"
    for words, name in NAMED:
        if any(w in text for w in words):
            return name if nz <= 0.3 or name == "hill" else f"sloped {name}"
    if nz > 0.3:
        return "slope" if "terrain" in text else "ramp-like slope"
    return "steep terrain" if "terrain" in text else "wall"


def _side(rel: float) -> str:
    if abs(rel) < 8:
        return "dead ahead"
    if abs(rel) > 150:
        return "behind you"
    return f"{abs(rel):.0f}° {'left' if rel < 0 else 'right'}"


def _dedupe(items: list[tuple], gap: float) -> list[tuple]:
    """Neighbouring beams often hit the same object: keep the nearest hit per name within `gap` m."""
    kept: list[tuple] = []
    for item in sorted(items):
        if not any(item[1] == k[1] and abs(item[0] - k[0]) < gap for k in kept):
            kept.append(item)
    return kept


def nearest_blocking(hits: list | None, within: float = 3.0, cone: float = 45.0) -> tuple[float, str] | None:
    """The closest obstacle (not a ramp) right in front of the car's nose, as (distance, what)."""
    best = None
    for hit in hits or []:
        angle, dist, _nx, _ny, nz, _h, cls, shape = hit[:8]
        kind = classify(nz, cls, shape)
        rel = (angle + 180) % 360 - 180
        if (kind and kind not in ("ramp", "bush") and not drivable_slope(kind, nz) and abs(rel) <= cone and dist <= within
                and (best is None or dist < best[0])):
            best = (dist, f"car ({shape})" if kind == "vehicle" else kind)
    return best


def describe_lidar(hits: list | None, speed_kmh: float, max_range: float, ground_scanned: bool = False) -> str:
    """ground_scanned: the ground's shape is described separately, so leave terrain out of the sectors."""
    if hits is None:
        return ""
    by_sector: dict[str, tuple[float, str]] = {}
    ramps: list[tuple[float, str, str, str]] = []  # (distance, name, side, facing)
    cars: list[tuple[float, str, str, float]] = []  # (distance, name, side, closing m/s)
    warnings: list[tuple[float, str]] = []  # (seconds to impact, what)
    speed = speed_kmh / 3.6
    for hit in hits:
        angle, dist, nx, ny, nz, _height, cls, shape, dx, dy = hit[:10]
        closing = hit[10] if len(hit) > 10 else 0.0
        kind = classify(nz, cls, shape)
        if kind is None:
            continue
        rel = (angle + 180) % 360 - 180  # -180..180, negative = left
        sector = SECTORS[round(angle / 45) % 8]
        label = f"car ({shape})" if kind == "vehicle" else kind
        if ground_scanned and kind in ("slope", "steep terrain"):
            pass
        elif sector not in by_sector or dist < by_sector[sector][0]:
            by_sector[sector] = (dist, label)

        if kind == "vehicle":
            cars.append((dist, shape, _side(rel), closing))
            # A real threat: coming at her (faster still if from the side, like a T-bone) and not already
            # touching - a car tangled up with hers after a crash just jiggles, it isn't about to hit.
            threat = closing > (2.5 if abs(rel) <= 60 or abs(rel) >= 120 else 4.0) and dist > 1.5
            if threat and dist / closing < WARN_SECONDS:
                warnings.append((dist / closing, f"{shape} {_side(rel)}, {dist:.0f} m away"))
        elif kind == "ramp":
            # A ramp you can drive up has its surface tilted back toward you.
            horiz = math.hypot(nx, ny) or 1.0
            facing = -(nx * dx + ny * dy) / horiz
            how = "facing you - drive straight at it to jump" if facing > 0.5 else (
                "side-on" if facing > -0.3 else "facing away (you'd hit the back)")
            name = shape.replace("s_gm_", "").replace("_", " ") if shape else "ramp"
            ramps.append((dist, name, _side(rel), how))
        elif abs(rel) <= 15 and speed > 2 and dist / speed < WARN_SECONDS and not drivable_slope(kind, nz):
            warnings.append((dist / speed, f"{kind} {dist:.0f} m ahead"))

    parts = [f"{s}: {by_sector[s][1]} {by_sector[s][0]:.0f} m" if s in by_sector else f"{s}: clear"
             for s in SECTORS]
    lines = [f"Lidar{' objects' if ground_scanned else ''} ({max_range:.0f} m): " + "; ".join(parts)]
    if cars:
        def motion(closing: float) -> str:
            kmh = closing * 3.6
            return f"closing at {kmh:.0f} km/h" if kmh > 3 else f"pulling away at {-kmh:.0f} km/h" if kmh < -3 else "keeping pace"
        lines.append("Cars the lidar sees: " + "; ".join(
            f"{n} {d:.0f} m, {s}, {motion(c)}" for d, n, s, c in _dedupe(cars, 6)[:4]))
    if ramps:
        lines.append("Ramps the lidar sees: " + "; ".join(
            f"{n} {d:.0f} m, {s}, {h}" for d, n, s, h in _dedupe(ramps, 10)[:3]))
    if warnings:
        secs, what = min(warnings)
        lines.append(f"COLLISION WARNING: {what}, impact in about {secs:.1f} s")
    return "\n".join("- " + line for line in lines)
