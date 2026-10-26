"""Game backend interface shared by the real BeamNG backend and the mock."""

from __future__ import annotations

import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]

# Autopilot modes we expose, mapped to BeamNG AI modes.
AI_MODE_LABELS = {
    "disabled": "off",
    "traffic": "cruising with traffic",
    "random": "exploring random roads",
    "span": "touring the whole map",
    "chase": "chasing another car",
    "flee": "fleeing from another car",
    "stopping": "stopping",
}


@dataclass
class NearbyVehicle:
    vid: str
    model: str
    distance_m: float
    direction: str  # relative to our car, e.g. "ahead-left"


@dataclass
class Telemetry:
    speed_kmh: float = 0.0
    gear: str = "?"
    rpm: float = 0.0
    damage: float = 0.0
    broken_parts: int = 0
    fuel: float | None = None
    engine_on: bool | None = None
    electric: bool = False  # electric motors only: "on/off" rather than an engine that cranks
    drive_left: bool = False  # this map's traffic drives on the left (its road rules say so)
    backwards: bool = False  # rolling backwards relative to the car's nose
    problems: list = field(default_factory=list)  # active damage/fault descriptions from the car
    police: dict | None = None  # {role, mode (-1 busted, 0 none, 1-3 pursuit level), police, suspect?}
    pilot: dict | None = None  # control-layer status: {mode, speed, status, warn, turn, stuck, event, junction?...}
    gears: str | None = None  # this car's gear positions, e.g. "P, R, N, M1-M3 (manual mode)"
    paused: bool = False  # the whole game is paused
    gearbox_mode: str | None = None
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    dir: tuple[float, float, float] = (0.0, 1.0, 0.0)
    up: tuple[float, float, float] = (0.0, 0.0, 1.0)
    extra: dict = field(default_factory=dict)

    @property
    def tipped_over(self) -> bool:
        return self.up[2] < 0.3

    @property
    def upside_down(self) -> bool:
        return self.up[2] < -0.3

    @property
    def heading(self) -> str:
        angle = math.degrees(math.atan2(self.dir[0], self.dir[1])) % 360
        return COMPASS[round(angle / 45) % 8]


def relative_direction(me: Telemetry, other_pos: tuple[float, float, float]) -> str:
    dx, dy = other_pos[0] - me.pos[0], other_pos[1] - me.pos[1]
    fx, fy = me.dir[0], me.dir[1]
    # Angle of the other car relative to our forward vector, positive = to the right.
    angle = math.degrees(math.atan2(dx * fy - dy * fx, dx * fx + dy * fy))
    if abs(angle) < 30:
        return "ahead"
    if abs(angle) > 150:
        return "behind"
    side = "right" if angle > 0 else "left"
    if abs(angle) < 70:
        return f"ahead-{side}"
    if abs(angle) > 110:
        return f"behind-{side}"
    return f"to the {side}"


def _bearing(me: Telemetry, x: float, y: float) -> float:
    """Degrees from the car's heading to a point; negative = left."""
    dx, dy = x - me.pos[0], y - me.pos[1]
    fx, fy = me.dir[0], me.dir[1]
    return math.degrees(math.atan2(dx * fy - dy * fx, dx * fx + dy * fy))


def _turn_word(b: float) -> str:
    side = "left" if b < 0 else "right"
    a = abs(b)
    if a < 8:
        return "straight ahead"
    if a < 25:
        return f"slightly {side} ({a:.0f}°)"
    if a < 60:
        return f"{side} ({a:.0f}°)"
    if a < 120:
        return f"sharp {side} ({a:.0f}°)"
    return f"behind you ({a:.0f}° {side})"


def describe_road(me: Telemetry, road: dict | None) -> str:
    """Plain-English road-ahead summary from a backend's road_ahead() data."""
    if not road:
        return "Road ahead: unknown"
    if road.get("noRoads"):
        return "Road ahead: there are no roads on this map near you"
    dist, half = float(road.get("dist") or 0), float(road.get("halfWidth") or 4)
    pts = road.get("points") or []
    off_road = dist > half + 2
    lines = []
    aim: float | None = None  # bearing to steer toward
    if off_road:
        if pts:
            aim = _bearing(me, pts[0][0], pts[0][1])
            turn = "turn around toward it" if abs(aim) > 120 else "head toward it"
            lines.append(f"You are OFF the road; the nearest road is {dist - half:.0f} m away, {_turn_word(aim)} - {turn}")
        else:
            lines.append(f"You are OFF the road; the nearest road is {dist - half:.0f} m away")
    else:
        lines.append(f"You are on the road ({dist:.1f} m from its center line, road half-width {half:.1f} m)")

    marks, out, junction = [15, 40, 80, 150], [], None
    sharpest = 0.0  # biggest bend within ~100 m
    travelled, last = 0.0, (me.pos[0], me.pos[1])
    for x, y, is_junction in pts:
        travelled += math.dist(last, (x, y))
        last = (x, y)
        if is_junction and junction is None and travelled > 5:
            junction = travelled
        while marks and travelled >= marks[0]:
            b = _bearing(me, x, y)
            if marks[0] <= 80:
                sharpest = max(sharpest, abs(b))
            if aim is None:
                aim = b  # on the road: aim at the point ~15 m ahead
            out.append(f"{marks.pop(0)} m: {_turn_word(b)}")
    if out:
        lines.append("Where the road goes (direction relative to your car's nose): " + "; ".join(out))
    if aim is not None:
        # Sat-nav style hint: angle -> steering. Steer harder at low speed, gentler when fast.
        gain = 1.5 if me.speed_kmh < 40 else 1.0
        steer = max(-100, min(100, round(aim * gain / 5) * 5))
        side = "straight" if steer == 0 else ("left" if steer < 0 else "right")
        lines.append(f"Steering to follow it: about {steer} ({side})")
    if marks and pts:
        lines.append(f"The road ends about {travelled:.0f} m ahead")
    if pts:
        limit = 100 if sharpest < 8 else 70 if sharpest < 25 else 45 if sharpest < 60 else 25 if sharpest < 120 else 15
        if marks and travelled < 60:
            limit = min(limit, 30)
        lines.append(f"Suggested max speed for what's ahead: about {limit} km/h")
    if junction is not None:
        lines.append(f"Next junction in about {junction:.0f} m")
    return "\n".join("- " + line for line in lines)


AUTO_POSITIONS = {"P": "P (park)", "R": "R (reverse)", "N": "N (neutral)", "D": "D (drive)", "S": "S (sport)",
                  "2": "2 (low)", "1": "1 (lowest)"}


def describe_gears(gearbox_type: str | None, auto_modes: str | None, max_gear) -> str | None:
    """Readable list of the gear positions this car actually has."""
    if not gearbox_type:
        return None
    top = int(max_gear) if isinstance(max_gear, (int, float)) and max_gear >= 1 else None
    if gearbox_type in ("manualGearbox", "sequentialGearbox"):
        kind = "manual" if gearbox_type == "manualGearbox" else "sequential"
        return f"R, N, 1-{top} ({kind}: shift up as you speed up)" if top else f"R, N, numbered gears ({kind})"
    modes = auto_modes or ("PRND" if gearbox_type in ("cvtGearbox", "electricMotor") else "PRNDS21M")
    out = [AUTO_POSITIONS[m] for m in modes if m in AUTO_POSITIONS]
    if "M" in modes:
        out.append(f"M1-M{top} (manual mode: shift up yourself)" if top else "M (manual mode)")
    return ", ".join(out)


WHEEL_POSITIONS = {"F": "front", "R": "rear", "M": "middle", "L": "left"}


def _words(name: str) -> str:
    """camelCase / snake_case -> words."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ").lower()


def pretty_problems(raw: list[str]) -> list[str]:
    """Readable problem list. Translated game text passes through; raw "group: name" entries
    (no translation in the game) become words, and per-corner body damage is merged into one line."""
    out, body = [], []
    for p in raw:
        group, sep, name = p.partition(": ")
        if not sep or " " in group or " " in name:
            out.append(p)  # already readable
        elif group == "body" and re.fullmatch(r"[FRM][LR]", name):
            body.append(f"{WHEEL_POSITIONS[name[0]]}-{'left' if name[1] == 'L' else 'right'}")
        else:
            out.append(f"{_words(group)}: {_words(name)}")
    if body:
        out.insert(0, "body damage (" + ", ".join(body) + ")")
    return out


class GameBackend(ABC):
    """Everything the Neuro integration needs from the game. All methods are async."""

    level: str = "unknown"
    vehicle_model: str = "unknown"
    vehicle_config: str | None = None  # config key of the current car, if known
    ai_mode: str = "disabled"
    ai_speed_kmh: float | None = None
    # Bumped whenever the car is reset or replaced, so speed/damage jumps aren't read as crashes.
    generation: int = 0

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def poll(self) -> Telemetry: ...

    @abstractmethod
    async def set_ai_mode(self, mode: str, target_vid: str | None = None) -> None: ...

    @abstractmethod
    async def set_ai_speed(self, kmh: float, exact: bool) -> None: ...

    @abstractmethod
    async def set_ai_style(self, aggression: float | None, in_lane: bool | None) -> None: ...

    @abstractmethod
    async def control(
        self,
        throttle: float = 0.0,
        brake: float = 0.0,
        steering: float = 0.0,
        parkingbrake: float = 0.0,
    ) -> None: ...

    @abstractmethod
    async def shift(self, target: str) -> None:
        """target is one of vehicle_lua.GEAR_TARGETS."""

    @abstractmethod
    async def set_engine(self, on: bool) -> None: ...

    @abstractmethod
    async def horn(self, on: bool) -> None: ...

    @abstractmethod
    async def set_lights(
        self,
        headlights: int | None = None,
        left_signal: bool | None = None,
        right_signal: bool | None = None,
        hazard_signal: bool | None = None,
    ) -> None: ...

    @abstractmethod
    async def recover(self) -> None: ...

    @abstractmethod
    async def change_vehicle(self, model: str, config: str | None = None) -> None:
        """Replace the car. config is a key from car_configs(model); None = the model's default."""

    async def car_models(self) -> list[dict]:
        """Drivable models: [{key, name, type}]. Empty = use the built-in list."""
        return []

    async def car_configs(self, model: str) -> list[dict]:
        """A model's configurations: [{key, name, type?, power?, desc?}]."""
        return []

    @abstractmethod
    async def nearby_vehicles(self, radius_m: float = 300.0) -> list[NearbyVehicle]: ...

    async def road_ahead(self, max_dist: float = 160.0) -> dict | None:
        """Points along the road ahead (see bridge.lua roadAhead). None if unsupported."""
        return None

    async def set_time_scale(self, scale: float) -> None:
        """1 = normal speed, <1 = slow motion."""

    async def set_paused(self, paused: bool) -> None:
        raise NotImplementedError("this backend can't pause the game")

    async def pilot(self, **command) -> None:
        """Command the control layer: mode ('road'|'target'|'off'), speed (km/h), turn, pos, vid, arrive."""
        raise NotImplementedError("this backend has no pilot")

    def take_events(self) -> list[dict]:
        """On-screen game messages seen since the last call: [{text, category, source}]."""
        return []

    async def lidar(self, max_range: float = 150.0, beams: int = 36, height: float = 0.8) -> list | None:
        """Level fan of rays around the car (see bridge.lua B.lidar). None if unsupported."""
        return None

    async def last_impact(self) -> dict | None:
        """What she hit or what hit her just now: {vehicles: [{name, police, ourSpeed, theirSpeed, weInto,
        theyInto}], fixed: {cls, shape, normalZ, speed}} (speeds in m/s). None if unsupported."""
        return None

    async def destinations(self) -> dict | None:
        """Places to drive to on this map: {level, places: [{name, kind, pos}], waypoint: pos or None}."""
        return None

    async def ground_scan(self, max_range: float = 50.0, bearings: int = 16, step: float = 2.0) -> dict | None:
        """Rough elevation map around the car (see bridge.lua B.groundScan). None if unsupported."""
        return None

    async def vehicle_controls(self) -> list[dict]:
        """The current car's own controls: [{name, title, desc?, axis}]. Empty if unsupported."""
        return []

    async def use_control(self, name: str, phase: str, value: float | None = None) -> None:
        """Run a vehicle control. phase: 'down', 'up', or 'change' (axis, with value)."""
        raise NotImplementedError("vehicle controls aren't supported by this backend")

    async def capture_frame(self, scale: float = 0.4) -> str | None:
        """Base64 JPEG of the current game view, or None if unsupported."""
        return None

    @abstractmethod
    async def spawn_traffic(self, amount: int, police_ratio: float) -> None: ...

    @abstractmethod
    async def set_time_of_day(self, hour: float) -> None: ...

    @abstractmethod
    async def set_gravity(self, gravity: float) -> None: ...
