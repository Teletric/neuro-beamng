"""The actions Neuro can take, and their handlers.

Neuro thinks in seconds, not milliseconds, so she makes the driving decisions
(start the engine, shift, follow the road, overtake, where to go, how hard to
drive) and the pilot in bridge.lua carries them out frame by frame, steering
and braking along her chosen line. BeamNG's own AI autopilot is not used unless
the bridge is started with --allow-autopilot.
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import math
from typing import Any, Awaitable, Callable

from .backend import AI_MODE_LABELS, GameBackend, Telemetry, describe_road, relative_direction
from .neuro_api import Action, InvalidAction
from .ground import describe_ground, open_ground
from .lidar import classify, describe_lidar, nearest_blocking, obstacle_name, solid_note
from .vehicle_lua import GEAR_TARGETS
from .vision import Vision

log = logging.getLogger(__name__)

# Friendly name -> (BeamNG model id, short description)
CARS = {
    "etk_800": ("etk800", "ETK 800, a fast German-style wagon/sedan"),
    "d_series_pickup": ("pickup", "Gavril D-Series pickup truck"),
    "covet": ("covet", "Ibishu Covet, a small cheap hatchback"),
    "pessima": ("pessima", "Ibishu Pessima, a boring family sedan"),
    "sunburst": ("sunburst", "Hirochi Sunburst, a sporty compact"),
    "sbr4": ("sbr", "Hirochi SBR4, a sports car"),
    "bolide_supercar": ("bolide", "Civetta Bolide, a 90s supercar"),
    "scintilla_hypercar": ("scintilla", "Civetta Scintilla, a hypercar"),
    "barstow_muscle": ("barstow", "Gavril Barstow, a classic muscle car"),
    "bastion": ("bastion", "Bruckell Bastion, a modern muscle sedan"),
    "grand_marshal": ("fullsize", "Gavril Grand Marshal, a big old police-style sedan"),
    "h_series_van": ("van", "Gavril H-Series van"),
    "t_series_semi": ("us_semi", "Gavril T-Series semi truck"),
    "city_bus": ("citybus", "Wentward DT40L city bus"),
    "piccolina_tiny": ("autobello", "Autobello Piccolina, a tiny old Italian car"),
    "pigeon_three_wheeler": ("pigeon", "Ibishu Pigeon, a wobbly three-wheeler"),
}

# Maneuver -> (control inputs, default duration). Steering: negative = left.
MANEUVERS: dict[str, tuple[dict[str, float], float]] = {
    "full_throttle": (dict(throttle=1.0), 3.0),
    "brake_hard": (dict(brake=1.0), 3.0),
    "turn_left": (dict(throttle=0.4, steering=-0.7), 2.0),
    "turn_right": (dict(throttle=0.4, steering=0.7), 2.0),
    "drift_left": (dict(throttle=1.0, steering=-1.0), 3.0),
    "drift_right": (dict(throttle=1.0, steering=1.0), 3.0),
    "handbrake_turn_left": (dict(throttle=0.3, steering=-1.0, parkingbrake=1.0), 1.5),
    "handbrake_turn_right": (dict(throttle=0.3, steering=1.0, parkingbrake=1.0), 1.5),
    "burnout": (dict(throttle=1.0, brake=1.0), 4.0),
    "donuts": (dict(throttle=1.0, steering=-1.0), 6.0),
}

GO_TO_TARGETS = ["nearest_ramp", "nearest_car", "nearest_police", "suspect", "nearest_road", "direction"]
FLEE_FROM = ["nearest_police", "nearest_car", "suspect"]
STOPPED_KMH = 2.0
DEADMAN_SECONDS = 20.0  # held controls are released if she gives no new driving input for this long
STUCK_SECONDS = 3.0  # throttle held this long without moving -> STUCK line on the dashboard
GROUND_RANGE, GROUND_BEARINGS = 50.0, 16  # height scan: ~20 ms in-game, so keep it modest
MAX_VEHICLE_CONTROLS = 40  # keep the action description a sensible size
# The only actions that make sense while the game is paused; the rest are unregistered until it's unpaused.
PAUSED_ACTIONS = {"pause_game", "list_car_configs", "list_destinations"}
# driving_style: what each choice means to the pilot (see bridge.lua's STYLE)
STYLE_CHOICES = {
    "cornering": {"gentle": {"corner": 0.7}, "normal": {"corner": 1.0}, "fast": {"corner": 1.3},
                  "on_the_limit": {"corner": 1.6}},
    "braking": {"gentle": {"brake": 0.6}, "normal": {"brake": 1.0}, "hard": {"brake": 1.5}, "late": {"brake": 1.9}},
    "acceleration": {"gentle": {"throttleCap": 0.4, "throttleGain": 0.06},
                     "normal": {"throttleCap": 1.0, "throttleGain": 0.12},
                     "full": {"throttleCap": 1.0, "throttleGain": 0.3}},
    "gap": {"roomy": {"gap": 11}, "normal": {"gap": 7}, "tight": {"gap": 3.5}},
    "lane": {"own_lane": {"lane": "own_lane"}, "middle": {"lane": "middle"},
             "other_lane": {"lane": "other_lane"}, "shoulder": {"lane": "shoulder"},
             "racing_line": {"lane": "racing_line"}},
    "obstacles": {"stop": {"swerve": False, "ram": False}, "swerve": {"swerve": True, "ram": False},
                  "ram": {"swerve": False, "ram": True}},
}
STYLE_PRESETS = {
    "cautious": {"cornering": "gentle", "braking": "gentle", "acceleration": "gentle", "gap": "roomy",
                 "lane": "own_lane", "obstacles": "stop"},
    "normal": {"cornering": "normal", "braking": "normal", "acceleration": "normal", "gap": "normal",
               "lane": "own_lane", "obstacles": "stop"},
    "sporty": {"cornering": "fast", "braking": "hard", "acceleration": "full", "gap": "normal",
               "lane": "own_lane", "obstacles": "swerve"},
    "reckless": {"cornering": "on_the_limit", "braking": "late", "acceleration": "full", "gap": "tight",
                 "lane": "racing_line", "obstacles": "swerve"},
}
# drive_to options that aren't a named place on the map
DESTINATION_SPECIALS = ["nearest gas station", "nearest charging station", "map waypoint", "mission"]

DRIVE_STYLES = {
    "cruise": ("traffic", True, "cruising along the roads, following traffic rules"),
    "explore": ("random", False, "exploring random roads"),
    "tour": ("span", False, "touring the whole map"),
}

GRAVITY = {"earth": -9.81, "moon": -1.62, "mars": -3.71, "jupiter": -24.79, "floaty": -0.5}

HONKS = {"short": [0.3], "long": [2.0], "angry": [0.15, 0.15, 0.15, 0.15, 0.6], "shave_and_a_haircut": [0.2, 0.1, 0.1, 0.2, 0.2]}


def _enum(params: dict, key: str, choices, default: Any = ..., label: str | None = None) -> Any:
    value = params.get(key, default)
    if value is ...:
        raise InvalidAction(f"Missing '{key}'. Pick one of: {', '.join(choices)}.")
    if value is not default and value not in choices:
        raise InvalidAction(f"'{value}' is not a valid {label or key}. Pick one of: {', '.join(choices)}.")
    return value


def _in_gear(current: str, target: str, has_d: bool = True) -> bool:
    """Is the car already in what shift_gear was asked for? (On a car without a D, "drive" gives manual
    mode, so M1, M2... count as drive there.)"""
    current = (current or "").upper()
    if target == "drive":
        return current in ("D", "S") or (current.startswith("M") and not has_d)
    if target in ("reverse", "neutral", "park"):
        return current == target[0].upper()
    if target.isdigit():
        return current in (target, "M" + target)
    return False  # up / down always shift


def _power_state(t: Telemetry) -> str:
    if t.electric:
        return "electric, switched " + ("OFF" if t.engine_on is False else "on")
    return "engine " + ("OFF" if t.engine_on is False else "running")


def _switched_off(t: Telemetry) -> str:
    return "the car is switched OFF" if t.electric else "the engine is OFF"


def _label_places(places: list[dict]) -> dict[str, dict]:
    """drive_to labels for a map's places: drift spots say so, and repeated names say which area they're in."""
    areas = [p for p in places if p["kind"] == "spawnPoint"]
    labels: dict[str, dict] = {}
    named = []
    for p in places:
        if "Rules" in p["name"]:  # the drag strip's rules board sits next to the strip itself
            continue
        named.append((f"{p['name']} drift spot" if p["kind"] == "driftSpot" else p["name"], p))
    counts: dict[str, int] = {}
    for label, _ in named:
        counts[label] = counts.get(label, 0) + 1
    for label, p in named:
        if counts[label] > 1 and areas:
            near = min((a for a in areas if a is not p), key=lambda a: math.dist(a["pos"][:2], p["pos"][:2]),
                       default=None)
            if near and near["name"] != p["name"]:
                label = f"{label} (near {near['name']})"
        base, n = label, 2
        while label in labels:
            label, n = f"{base} {n}", n + 1
        labels[label] = p
    return labels


def _closest_name(wanted: str, items: list[dict]) -> dict | None:
    """Exact, then substring, then fuzzy match on the items' names."""
    w = wanted.lower()
    for item in items:
        if item["name"].lower() == w:
            return item
    for item in items:
        if w in item["name"].lower():
            return item
    names = [item["name"].lower() for item in items]
    match = difflib.get_close_matches(w, names, n=1, cutoff=0.5)
    return items[names.index(match[0])] if match else None


def _number(params: dict, key: str, lo: float, hi: float, default: Any = ...) -> Any:
    value = params.get(key, default)
    if value is ...:
        raise InvalidAction(f"Missing '{key}' (a number from {lo} to {hi}).")
    if value is default:
        return value
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise InvalidAction(f"'{key}' must be a number from {lo} to {hi}.") from None
    if not lo <= value <= hi:
        raise InvalidAction(f"'{key}' must be between {lo} and {hi}, got {value:g}.")
    return value


def _bool(params: dict, key: str, default: Any = None) -> Any:
    value = params.get(key, default)
    if value is default or isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise InvalidAction(f"'{key}' must be true or false.")


class Driver:
    """Owns the game backend and implements every action."""

    def __init__(self, game: GameBackend, allow_chaos: bool = False, allow_autopilot: bool = False) -> None:
        self.game = game
        self.allow_chaos = allow_chaos
        self.allow_autopilot = allow_autopilot
        self._maneuver: asyncio.Task | None = None
        self._input_gen = 0  # bumped by every new driving input, so a superseded one doesn't release the pedals
        self.held_since = 0.0
        self.held_inputs: dict[str, float] = {}  # inputs of the latest drive_manually
        self._pushing_since: float | None = None  # throttle held but car not moving
        self.notices: list[str] = []  # things to tell her (e.g. the dead man's switch), drained by the director
        self.last_input = "none yet"
        self.vision: Vision | None = None  # set by __main__ when a vision server is configured
        self.lidar_range, self.lidar_beams = 150.0, 36  # set from __main__ flags; range 0 = off
        self.pilot_on = False  # the control layer is carrying out follow_road / go_to
        self.last_pilot_cmd: dict | None = None  # last order given to the control layer, resumed after back_up
        self.pilot_target = ""  # what go_to is heading for, for the dashboard
        self.vehicle_controls: list[dict] = []  # the current car's own controls, see refresh_vehicle_controls
        self.disabled: set[str] = set()  # actions the streamer switched off (control.py)
        self.paused = False  # game paused: only PAUSED_ACTIONS are registered
        self._handlers: dict[str, Callable[[dict], Awaitable[str]]] = {"use_vehicle_control": self.use_vehicle_control,
                                                                     "drive_to": self.drive_to}
        self.places: dict[str, dict] = {}  # drive_to label -> {name, kind, pos}, for the current map
        self.missions: list[dict] = []
        self.style_choice: dict[str, str] = dict(STYLE_PRESETS["normal"])  # driving_style, sent with every order
        self.last_crash: tuple[float, str] | None = None  # (when, who hit whom) - on the dashboard for a minute
        # Model id -> display name. Replaced with the game's full catalog by load_car_catalog().
        self.car_catalog: dict[str, str] = {model: desc for model, desc in CARS.values()}
        self._configs: dict[str, list[dict]] = {}  # model -> configs, fetched on demand
        self.actions = self._build_actions()

    async def load_car_catalog(self) -> None:
        """Offer every drivable model the game has, instead of the built-in shortlist."""
        try:
            models = await self.game.car_models()
        except Exception:
            log.warning("Couldn't read the game's car list; using the built-in one", exc_info=True)
            return
        if not models:
            return
        self.car_catalog = {m["key"]: f"{m['name']} ({m['type'].lower()})" for m in models}
        self.actions = self._build_actions()  # change_car and list_car_configs embed the catalog

    def _pilot_lines(self, t: Telemetry, ahead: tuple[float, str] | None = None) -> str | None:
        p = t.pilot
        if not p or p.get("mode") in (None, "off"):
            if self.pilot_on and p is None:
                self.pilot_on = False  # it switched itself off (arrived, car changed, ...)
            return None
        status = p.get("status") or ""
        if p.get("fleeing"):
            gap = p.get("fleeDist")
            route = "junctions are picked to get away" if p.get("mode") == "road" else "going off-road, straight away"
            line = (f"- Driving: FLEEING from {self.pilot_target}" + (f", {gap:.0f} m from you" if gap else "")
                    + f", at up to {p.get('speed', 0):.0f} km/h ({status}); {route}")
        elif p.get("mode") == "road" and p.get("route"):
            r = p["route"]
            rem = r.get("remaining")
            togo = "" if rem is None else f", {rem / 1000:.1f} km to go" if rem >= 1000 else f", {rem:.0f} m to go"
            line = (f"- Driving: on your route to {r.get('name')}{togo}, at up to {p.get('speed', 0):.0f} km/h "
                    f"({status}); every junction is taken for you")
            if p.get("passing"):
                line += "; OVERTAKING, back into your lane once you're past"
        elif p.get("mode") == "road":
            line = f"- Driving: following the road at up to {p.get('speed', 0):.0f} km/h ({status})"
            if p.get("passing"):
                where = {"left": "in the other lane", "right": "on the shoulder"}.get(p["passing"], "on a clear line")
                line += f"; OVERTAKING {where}, back into your lane once you're past"
            j = p.get("junction")
            if j and j.get("branches", 1) > 1:
                way = p.get("turn") or "straight"
                line += f"; next junction in {j['dist']:.0f} m, you'll go {way} (turn_at_next_junction to change)"
        else:
            dist = p.get("targetDist")
            where = f", {dist:.0f} m away" if dist is not None else ""
            line = f"- Driving: heading for {self.pilot_target}{where} at up to {p.get('speed', 0):.0f} km/h ({status})"
        if label := self._style_label():
            line += f"; your style: {label}"
        if p.get("warn"):
            line += f"\n- YOU CAN'T MOVE: {p['warn']} - fix that first{_gears_hint(t)}"
        elif p.get("blocked") is not None:
            what = obstacle_name(p.get("obstacleWhat")) if p.get("obstacleWhat") else (
                f"a {ahead[1]}" if ahead else "something")
            line += (f"\n- STOPPED FOR: {what}, {p['blocked']:.0f} m in front of your bumper."
                     + solid_note(what.removeprefix("a ")) + " Your call: go around it (overtake), push_through "
                     "it, back_up, or wait")
        elif p.get("stuck"):
            line += ("\n- STUCK: you're giving it throttle but not moving - something's in the way. Reverse out "
                     "with drive_manually after shift_gear reverse, or recover_car")
        return line

    def _stuck_line(self, t: Telemetry, blocked: tuple[float, str] | None = None) -> str | None:
        """Throttle held for a while but the car isn't moving: say so, and why if we can tell."""
        pushing = self.input_active and self.held_inputs.get("throttle", 0) >= 0.2 and t.speed_kmh < 1
        now = asyncio.get_running_loop().time()
        if not pushing:
            self._pushing_since = None
            return None
        self._pushing_since = self._pushing_since or now
        if now - self._pushing_since < STUCK_SECONDS:
            return None
        if t.engine_on is False:
            why = _switched_off(t) + " - start it (engine start)"
        elif t.gear in ("N", "P"):
            why = f"you're in {'neutral' if t.gear == 'N' else 'park'} - pick a gear{_gears_hint(t)}"
        elif self.held_inputs.get("parkingbrake") or self.held_inputs.get("brake", 0) >= 0.5:
            why = "you're also holding the brake/handbrake - let go of it"
        elif blocked and t.gear != "R":
            why = (f"you're up against a {blocked[1]} {blocked[0]:.0f} m in front of you.{solid_note(blocked[1])} "
                   "shift_gear reverse and back up a few metres, then turn")
        else:
            why = ("something is blocking the car (check the lidar) - reverse out (shift_gear reverse, then "
                   "throttle) or use recover_car")
        return f"- STUCK: you've held the throttle for {now - self._pushing_since:.0f} s and the car isn't moving: {why}"

    def _police_line(self, t: Telemetry) -> str | None:
        p = t.police
        if not p:
            return None
        cops = p.get("police") or 0
        if p.get("role") == "police":
            s = p.get("suspect")
            if not s:
                return "- You're driving a POLICE car. No suspect assigned right now."
            name = self.car_catalog.get(s["name"], s["name"]).split(" (")[0]
            dist = math.dist(t.pos, s["pos"])
            chase = "you're in pursuit" if s.get("chasing") else "not yet in pursuit"
            return f"- You're POLICE. Suspect: {name}, {dist:.0f} m {relative_direction(t, tuple(s['pos']))}, {chase}"
        mode = p.get("mode") or 0
        near = p.get("nearest")
        nearest = (f"; nearest police car {near['dist']:.0f} m {relative_direction(t, tuple(near['pos']))}"
                   if near else "")
        if mode < 0:
            return "- BUSTED: the police have stopped you"
        if mode > 0:
            return f"- WANTED: police pursuit level {mode} of 3, {cops} police cars around{nearest} (flee to escape)"
        return f"- Police: not after you ({cops} police cars around{nearest})" if cops else None

    async def _car_label(self) -> str:
        g = self.game
        name = self.car_catalog.get(g.vehicle_model, g.vehicle_model)
        if g.vehicle_config:
            for c in await self.configs_for(g.vehicle_model):
                if c["key"] == g.vehicle_config:
                    return f"{name} - {c['name']}"
        return name

    def _change_car_action(self) -> Action:
        listing = "; ".join(f"{k} = {name}" for k, name in self.car_catalog.items())
        return Action(
            "change_car",
            f"Swap your car for a different one. Models: {listing}. Every model has configurations (police, "
            "drift, race, off-road, ...): call list_car_configs to see them, then pass config. Without config "
            "you get the model's default.",
            {"type": "object", "properties": {
                "car": {"type": "string", "enum": list(self.car_catalog)},
                "config": {"type": "string"},
            }, "required": ["car"]},
        )

    async def configs_for(self, model: str, fresh: bool = False) -> list[dict]:
        """A model's configs. Cached for the dashboard; fresh=True re-asks the game (e.g. a config saved since)."""
        if fresh or model not in self._configs:
            try:
                self._configs[model] = await self.game.car_configs(model)
            except Exception:
                log.debug("car_configs failed for %s", model, exc_info=True)
                return []
        return self._configs[model]

    async def handle(self, name: str, params: dict[str, Any]) -> str:
        if name in self.disabled:  # unregistered already; this only catches a stale request
            raise InvalidAction(f"'{name}' is turned off by the streamer right now.")
        if self.paused and name not in PAUSED_ACTIONS:  # answer to a force sent before the pause
            return "The game is paused, so nothing can move - that didn't happen. Wait until it's unpaused."
        return await self._handlers[name](params)

    @property
    def enabled_actions(self) -> list[Action]:
        return [a for a in self.actions if self.available(a.name)]

    def available(self, name: str) -> bool:
        """Registered with Neuro right now: not switched off, and usable in the current pause state."""
        return name not in self.disabled and (not self.paused or name in PAUSED_ACTIONS)

    def _add(self, actions: list[Action], action: Action, handler: Callable[[dict], Awaitable[str]]) -> None:
        actions.append(action)
        self._handlers[action.name] = handler

    def _build_actions(self) -> list[Action]:
        a: list[Action] = []
        if self.allow_autopilot:
            self._add(a, Action(
                "drive",
                "Turn on the autopilot and let the car drive itself. 'cruise' follows the roads and traffic rules, "
                "'explore' takes random roads, 'tour' tries to cover the whole map. Optionally set a speed limit.",
                {"type": "object", "properties": {
                    "style": {"type": "string", "enum": list(DRIVE_STYLES)},
                    "max_speed_kmh": {"type": "integer", "minimum": 10, "maximum": 300},
                }, "required": ["style"]},
            ), self.drive)
            self._add(a, Action(
                "pursue_nearest_car",
                "Make the autopilot chase the nearest other car, or run away from it.",
                {"type": "object", "properties": {"behavior": {"type": "string", "enum": ["chase", "flee"]}},
                 "required": ["behavior"]},
            ), self.pursue)
            self._add(a, Action(
                "set_speed",
                "Change how fast the autopilot drives. 'limit' means drive up to this speed, 'exact' means hold this speed no matter what.",
                {"type": "object", "properties": {
                    "speed_kmh": {"type": "integer", "minimum": 5, "maximum": 300},
                    "mode": {"type": "string", "enum": ["limit", "exact"]},
                }, "required": ["speed_kmh"]},
            ), self.set_speed)
            self._add(a, Action(
                "set_driving_style",
                "Change the autopilot's personality. aggression 0 is a nervous student driver, 100 is a street racer. "
                "stay_in_lane false lets it cut corners and use the whole road.",
                {"type": "object", "properties": {
                    "aggression": {"type": "integer", "minimum": 0, "maximum": 100},
                    "stay_in_lane": {"type": "boolean"},
                }},
            ), self.set_style)
        self._add(a, Action(
            "follow_road",
            "Drive along the road at a steady speed. Your hands and feet do the fine work for you: you stay in "
            "lane, slow down for bends, and brake for obstacles in your path. You keep deciding the speed and "
            "which way to go at junctions. The car still needs the engine running and a forward gear.",
            {"type": "object", "properties": {
                "speed_kmh": {"type": "integer", "minimum": 5, "maximum": 250},
                "next_turn": {"type": "string", "enum": ["left", "right", "straight"]},
            }, "required": ["speed_kmh"]},
        ), self.follow_road)
        self._add(a, Action(
            "turn_at_next_junction",
            "Choose which way to go at the next junction while following the road.",
            {"type": "object", "properties": {"direction": {"type": "string", "enum": ["left", "right", "straight"]}},
             "required": ["direction"]},
        ), self.turn_at_next_junction)
        self._add(a, Action(
            "set_target_speed",
            "Change the speed you're driving at while following the road or heading somewhere.",
            {"type": "object", "properties": {"speed_kmh": {"type": "integer", "minimum": 0, "maximum": 250}},
             "required": ["speed_kmh"]},
        ), self.set_target_speed)
        self._add(a, Action(
            "driving_style",
            "How you drive whenever you're following the road or heading somewhere: cornering (how fast you "
            "take bends), braking (how hard - 'late' brakes as late and hard as possible), acceleration, gap "
            "(how far back you stop behind things), lane (own_lane - your side of the road for this map's "
            "traffic - the middle of the road, other_lane - the wrong side, into oncoming traffic - the "
            "shoulder, or the "
            "racing_line that cuts corners) and obstacles (stop behind stopped cars and things in the road, "
            "swerve around them without stopping - you always swerve while fleeing - or ram: never brake for "
            "anything). A preset sets them all; anything else you give overrides it. "
            "It stays until you change it.",
            {"type": "object", "properties": {
                "preset": {"type": "string", "enum": list(STYLE_PRESETS)},
                **{k: {"type": "string", "enum": list(v)} for k, v in STYLE_CHOICES.items()},
            }},
        ), self.driving_style)
        self._add(a, Action(
            "go_to",
            "Drive to a target, on or off the road, straight across open ground if needed: the nearest ramp "
            "(to jump it), the nearest car, your police suspect, back to the nearest road, or a direction "
            "(angle_deg relative to your nose, negative = left, plus distance_m). on_arrival 'stop' pulls up "
            "there; 'drive_through' keeps going (jumps, ramming). You brake for obstacles on the way, except "
            "the target itself.",
            {"type": "object", "properties": {
                "target": {"type": "string", "enum": GO_TO_TARGETS},
                "speed_kmh": {"type": "integer", "minimum": 5, "maximum": 250},
                "on_arrival": {"type": "string", "enum": ["stop", "drive_through"]},
                "angle_deg": {"type": "integer", "minimum": -180, "maximum": 180},
                "distance_m": {"type": "integer", "minimum": 5, "maximum": 1000},
            }, "required": ["target"]},
        ), self.go_to)
        self._add(a, Action(
            "back_up",
            "Reverse out of a tight spot (e.g. nose against a wall or trees): you shift into reverse, back up "
            "distance_m metres, shift back to drive, and carry on with what you were doing. steering while "
            "reversing: negative swings the nose left, positive swings it right.",
            {"type": "object", "properties": {
                "distance_m": {"type": "integer", "minimum": 2, "maximum": 30},
                "steering": {"type": "integer", "minimum": -100, "maximum": 100},
            }},
        ), self.back_up)
        self._add(a, Action(
            "push_through",
            "Drive into whatever you've stopped for, on purpose: shove a car out of the way, smash through a "
            "fence or a barrier, plough through a bush, ram a police car. You stop braking for that one thing "
            "until you're past it (trees, walls and rocks won't budge, though).",
            None,
        ), self.push_through)
        self._add(a, Action(
            "overtake",
            "Drive around whatever is in your lane - a stopped or slow car, a traffic jam, a crash - and keep "
            "following the road. side 'auto' (the default) checks every line across the road - both lanes, the "
            "middle, both shoulders - and takes the clear one closest to where you are; 'other_lane' forces "
            "the oncoming lane (watch for oncoming traffic: you brake for it, but they may not), 'shoulder' the "
            "shoulder just off the road edge on your side. You move back into your lane by yourself once you're "
            "past, and back up a little first if you're too close to swing out. (To drive somewhere else on the "
            "road for a while - the other lane, the middle, the shoulder - set driving_style lane instead.)",
            {"type": "object", "properties": {
                "side": {"type": "string", "enum": ["auto", "other_lane", "shoulder"]},
                "speed_kmh": {"type": "integer", "minimum": 5, "maximum": 250},
            }},
        ), self.overtake)
        self._add(a, Action(
            "flee",
            "Get away from someone (e.g. run from the police). route 'roads' follows the roads and takes "
            "whichever way leads away from them at every junction; 'off_road' cuts straight across country, "
            "directly away from them (you still brake for walls and trees). Your speed, your call.",
            {"type": "object", "properties": {
                "from": {"type": "string", "enum": FLEE_FROM},
                "route": {"type": "string", "enum": ["roads", "off_road"]},
                "speed_kmh": {"type": "integer", "minimum": 10, "maximum": 250},
            }, "required": ["from"]},
        ), self.flee)
        self._add(a, Action(
            "drive_manually",
            "Your main driving control: set the pedals and steering wheel. They STAY where you set them until your "
            "next drive_manually (like a real driver's feet and hands), and you'll be checked in with every few "
            "seconds while driving. Only give release_after_seconds for a quick tap that lets go by itself; "
            "normally leave it out. throttle and "
            "brake are 0-100 (%), steering is -100 (full left) to 100 (full right); set throttle 0 to lift off. "
            "To follow the road, steer toward where it goes: a road 20 degrees left needs roughly steering -25; "
            "turn harder at low speed and gentler at high speed, and straighten up (steering 0) after a bend. "
            "The car only moves if the engine is running and you're in a driving gear (reverse goes backwards).",
            {"type": "object", "properties": {
                "throttle": {"type": "integer", "minimum": 0, "maximum": 100},
                "brake": {"type": "integer", "minimum": 0, "maximum": 100},
                "steering": {"type": "integer", "minimum": -100, "maximum": 100},
                "handbrake": {"type": "boolean"},
                "release_after_seconds": {"type": "number", "minimum": 0.5, "maximum": 5},
            }},
        ), self.drive_manually)
        self._add(a, Action(
            "shift_gear",
            "Change gear. Your dashboard lists the gears THIS car has. park/reverse/neutral/drive work on any car "
            "('drive' picks D, or the nearest forward mode if the car has no D); up/down shift one gear; a number "
            "selects that gear (manual mode on automatics). In a manual gear, shift up as the RPM gets high and "
            "down as you slow down.",
            {"type": "object", "properties": {"gear": {"type": "string", "enum": GEAR_TARGETS}}, "required": ["gear"]},
        ), self.shift_gear)
        self._add(a, Action(
            "engine",
            "Turn the ignition on and start the engine, or switch it off. On an electric car this switches "
            "it on or off. When starting you can also put it in gear straight after (then_shift), the way a "
            "driver turns the key and selects drive in one go.",
            {"type": "object", "properties": {
                "state": {"type": "string", "enum": ["start", "stop"]},
                "then_shift": {"type": "string", "enum": ["drive", "reverse", "neutral", "park"]},
            }, "required": ["state"]},
        ), self.engine)
        self._add(a, Action(
            "stop_car",
            "Brake firmly to a stop, then pull the handbrake." + (" Also turns off the autopilot." if self.allow_autopilot else ""),
            None,
        ), self.stop)
        self._add(a, Action(
            "do_maneuver",
            "Do a preset stunt for a few seconds (drifts, donuts, burnout, handbrake turns, ...).",
            {"type": "object", "properties": {
                "maneuver": {"type": "string", "enum": list(MANEUVERS)},
                "seconds": {"type": "number", "minimum": 0.5, "maximum": 8},
            }, "required": ["maneuver"]},
        ), self.maneuver)
        self._add(a, Action(
            "honk",
            "Honk the horn.",
            {"type": "object", "properties": {"pattern": {"type": "string", "enum": list(HONKS)}}},
        ), self.honk)
        self._add(a, Action(
            "set_lights",
            "Control the car's lights and indicators.",
            {"type": "object", "properties": {
                "headlights": {"type": "string", "enum": ["off", "on", "high_beams"]},
                "indicator": {"type": "string", "enum": ["none", "left", "right", "hazards"]},
            }},
        ), self.set_lights)
        self._add(a, Action(
            "recover_car",
            "Teleport the car back onto its wheels on the nearest road, repaired. Use this when flipped, stuck, or wrecked.",
            None,
        ), self.recover)
        self._add(a, self._change_car_action(), self.change_car)
        self._add(a, Action(
            "list_car_configs",
            "List the configurations of a car model (police, drift, race, off-road, limo, ...), so you can "
            "pick one with change_car.",
            {"type": "object", "properties": {"car": {"type": "string", "enum": list(self.car_catalog)}},
             "required": ["car"]},
        ), self.list_car_configs)
        self._add(a, Action(
            "list_destinations",
            "List the places on this map you can drive_to (named areas, gas and charging stations, drift spots, "
            "missions), nearest first, with how far away they are.",
            None,
        ), self.list_destinations)
        self._add(a, Action(
            "check_surroundings",
            "Look at your dashboard and around you: speed, gear, damage, where the road goes, and nearby cars.",
            None,
        ), self.check)
        self._add(a, Action(
            "spawn_traffic",
            "Fill the roads with AI traffic cars.",
            {"type": "object", "properties": {
                "amount": {"type": "integer", "minimum": 1, "maximum": 20},
            }, "required": ["amount"]},
        ), self.spawn_traffic)
        self._add(a, Action(
            "pause_game",
            "Pause or unpause the whole game. While paused nothing moves, including you.",
            {"type": "object", "properties": {"paused": {"type": "boolean"}}, "required": ["paused"]},
        ), self.pause_game)
        self._add(a, Action(
            "set_time_of_day",
            "Change the time of day in the game world (0-23).",
            {"type": "object", "properties": {"hour": {"type": "integer", "minimum": 0, "maximum": 23}}, "required": ["hour"]},
        ), self.set_time)
        if self.allow_chaos:
            self._add(a, Action(
                "set_gravity",
                "Change the world's gravity. Chaos guaranteed.",
                {"type": "object", "properties": {"preset": {"type": "string", "enum": list(GRAVITY)}}, "required": ["preset"]},
            ), self.set_gravity)
        return a

    async def refresh_vehicle_controls(self) -> Action | None:
        """Re-read the current car's own controls and rebuild use_vehicle_control for them.

        Returns the action to (re-)register, or None if this car has no special controls.
        """
        try:
            self.vehicle_controls = (await self.game.vehicle_controls())[:MAX_VEHICLE_CONTROLS]
        except Exception:
            log.warning("Couldn't read the car's controls", exc_info=True)
            self.vehicle_controls = []
        self.actions = [a for a in self.actions if a.name != "use_vehicle_control"]
        if not self.vehicle_controls:
            return None
        listing = "; ".join(
            f"{c['name']} = {c['title']}" + (" (lever: value -1 to 1)" if c.get("axis") else "")
            for c in self.vehicle_controls)
        action = Action(
            "use_vehicle_control",
            f"Use one of this {self.game.vehicle_model}'s own controls: {listing}. Buttons are pressed once "
            "(hold_seconds keeps them held); levers are set to value and stay there unless you give hold_seconds.",
            {"type": "object", "properties": {
                "control": {"type": "string", "enum": [c["name"] for c in self.vehicle_controls]},
                "value": {"type": "number", "minimum": -1, "maximum": 1},
                "hold_seconds": {"type": "number", "minimum": 0, "maximum": 10},
            }, "required": ["control"]},
        )
        self.actions.append(action)
        log.info("Car controls: %s", ", ".join(c["name"] for c in self.vehicle_controls))
        return action

    # --- status -------------------------------------------------------------

    @property
    def input_active(self) -> bool:
        return self._maneuver is not None and not self._maneuver.done()

    async def status_markdown(self, t: Telemetry | None = None, include_nearby: bool = True) -> str:
        g = self.game
        t = t or await g.poll()
        gearbox = f" ({t.gearbox_mode} gearbox)" if t.gearbox_mode else ""
        lines = [
            *(["- GAME PAUSED: nothing moves until it's unpaused"
               + (" (pause_game to unpause)" if "pause_game" not in self.disabled else " (the streamer controls this)")]
              if t.paused else []),
            f"- Car: {await self._car_label()} on map {g.level}"
            + (" (traffic here drives on the LEFT)" if t.drive_left else ""),
            f"- Speed: {t.speed_kmh:.0f} km/h" + (" BACKWARDS" if t.backwards else "") + f", car facing {t.heading}",
            f"- Gear: {t.gear}{gearbox}, {t.rpm:.0f} RPM, " + _power_state(t)
            + (" - REVERSE: throttle drives you backwards" if t.gear == "R" else "")
            + (f"; this car's gears: {t.gears}" if t.gears else ""),
            f"- Damage: {_damage_word(t.damage)}" + (f", {t.broken_parts} damaged parts" if t.broken_parts else ""),
        ]
        if self.allow_autopilot:
            mode = AI_MODE_LABELS.get(g.ai_mode, g.ai_mode)
            if g.ai_mode != "disabled" and g.ai_speed_kmh:
                mode += f" (speed {g.ai_speed_kmh:.0f} km/h)"
            lines.append(f"- Autopilot: {mode}")
        if self.input_active:
            lines.append(f"- You are currently holding: {self.last_input}")
        else:
            lines.append(f"- Your last input (now released): {self.last_input}")
        hits = None
        if self.lidar_range > 0:
            try:
                hits = await g.lidar(self.lidar_range, self.lidar_beams)
            except Exception:
                log.debug("lidar failed", exc_info=True)
        blocked = nearest_blocking(hits)  # (distance, what) if something is right in front of her nose
        # The pilot stops up to 8 m short of an obstacle, so look further out to say what it is.
        if pilot := self._pilot_lines(t, nearest_blocking(hits, within=10)):
            lines.append(pilot)
        elif stuck := self._stuck_line(t, blocked):
            lines.append(stuck)
        if self.last_crash and (ago := asyncio.get_running_loop().time() - self.last_crash[0]) < 60:
            lines.append(f"- Last crash ({ago:.0f} s ago): {self.last_crash[1]}")
        if t.problems:
            lines.append("- Car problems: " + "; ".join(t.problems) + " (recover_car repairs the car)")
        if police := self._police_line(t):
            lines.append(police)
        if t.upside_down:
            lines.append("- The car is UPSIDE DOWN")
        elif t.tipped_over:
            lines.append("- The car is on its side")
        if include_nearby:
            try:
                nearby = await g.nearby_vehicles()
            except Exception:
                log.debug("nearby_vehicles failed", exc_info=True)
                nearby = []
            if nearby:
                desc = ", ".join(f"{n.model} {n.distance_m:.0f} m {n.direction}" for n in nearby[:4])
                lines.append(f"- Nearby cars ({len(nearby)}): {desc}")
            else:
                lines.append("- No other cars nearby")
        try:
            road = describe_road(t, await g.road_ahead())
            if blocked:  # don't let the road hint steer her into the wall she's touching
                road = road.replace("\n- Steering to follow it:",
                                    f"\n- BLOCKED: a {blocked[1]} is {blocked[0]:.0f} m in front of you."
                                    + solid_note(blocked[1]) + " Back up (reverse) or overtake to get around "
                                    "it. Steering once clear:")
            lines.append(road)
        except Exception:
            log.debug("road_ahead failed", exc_info=True)
        ground, scan = "", None
        if self.lidar_range > 0:
            try:
                scan = await g.ground_scan(GROUND_RANGE, GROUND_BEARINGS, 2.0)
                ground = describe_ground(scan, GROUND_RANGE)
            except Exception:
                log.debug("ground scan failed", exc_info=True)
        if ground:
            lines.append(ground)
            if open_dirs := open_ground(scan):
                wanted = ((t.police or {}).get("mode") or 0) > 0
                lines.append("- Open ground you could drive straight across (30 m+, gentle): " + ", ".join(open_dirs)
                             + (" - flee with route off_road cuts straight across it" if wanted else ""))
        if hits is not None and (seen := describe_lidar(hits, t.speed_kmh, self.lidar_range, bool(ground))):
            lines.append(seen)
        if self.vehicle_controls:
            titles = [c["title"] for c in self.vehicle_controls]
            more = f" (+{len(titles) - 10} more)" if len(titles) > 10 else ""
            lines.append(f"- This car's special controls (use_vehicle_control): {', '.join(titles[:10])}{more}")
        if self.vision and (seen := self.vision.summary()):
            lines.append(f"- {seen}")
        return "\n".join(lines)

    # --- handlers -----------------------------------------------------------

    async def drive(self, p: dict) -> str:
        style = _enum(p, "style", DRIVE_STYLES)
        speed = _number(p, "max_speed_kmh", 10, 300, None)
        mode, in_lane, label = DRIVE_STYLES[style]
        self._cancel_maneuver()
        await self.game.control()  # release any manual inputs
        await self.game.set_ai_mode(mode)
        await self.game.set_ai_style(None, in_lane)
        if speed is not None:
            await self.game.set_ai_speed(speed, exact=False)
        return f"Autopilot on: {label}" + (f", up to {speed:.0f} km/h." if speed else ".")

    async def pursue(self, p: dict) -> str:
        behavior = _enum(p, "behavior", ["chase", "flee"])
        nearby = await self.game.nearby_vehicles(radius_m=2000)
        if not nearby:
            return "There are no other cars around. Try spawn_traffic first."
        target = nearby[0]
        self._cancel_maneuver()
        await self.game.control()
        await self.game.set_ai_mode(behavior, target_vid=target.vid)
        verb = "Chasing" if behavior == "chase" else "Fleeing from"
        return f"{verb} a {target.model} {target.distance_m:.0f} m {target.direction}."

    async def set_speed(self, p: dict) -> str:
        speed = _number(p, "speed_kmh", 5, 300)
        exact = _enum(p, "mode", ["limit", "exact"], "limit") == "exact"
        await self.game.set_ai_speed(speed, exact)
        note = "" if self.game.ai_mode != "disabled" else " The autopilot is off though - use drive to start it."
        return f"Speed {'locked to' if exact else 'limited to'} {speed:.0f} km/h.{note}"

    async def set_style(self, p: dict) -> str:
        aggression = _number(p, "aggression", 0, 100, None)
        in_lane = _bool(p, "stay_in_lane")
        if aggression is None and in_lane is None:
            raise InvalidAction("Give at least one of 'aggression' or 'stay_in_lane'.")
        # BeamNG's AI misbehaves below ~0.1 aggression.
        await self.game.set_ai_style(None if aggression is None else 0.1 + aggression / 100 * 0.9, in_lane)
        parts = []
        if aggression is not None:
            parts.append(f"aggression {aggression:.0f}%")
        if in_lane is not None:
            parts.append("staying in lane" if in_lane else "using the whole road")
        return "Driving style: " + ", ".join(parts) + "."

    async def stop(self, p: dict) -> str:
        self._input_gen += 1
        self._cancel_maneuver()
        await self._pilot_off()
        await self.game.set_ai_mode("disabled")
        self._maneuver = asyncio.create_task(self._brake_to_stop())
        return ("Autopilot off, braking" if self.allow_autopilot else "Braking") + " to a stop, then the handbrake goes on."

    async def _brake_to_stop(self, timeout: float = 15.0) -> None:
        # Holding the brake at a standstill makes BeamNG's arcade gearbox shift into
        # reverse, so swap to the handbrake once stopped.
        await self.game.control(brake=1.0)
        await self._wait_until_stopped(timeout)
        await self.game.control(parkingbrake=1.0)

    async def _wait_until_stopped(self, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            if (await self.game.poll()).speed_kmh < STOPPED_KMH:
                return True
            await asyncio.sleep(0.2)
        return False

    async def _fresh_poll(self) -> Telemetry:
        """A reading taken after the car has answered: each poll asks the car, and its answer arrives by the next."""
        await self.game.poll()
        await asyncio.sleep(0.35)
        return await self.game.poll()

    async def _wait_for_gear(self, gear: str, timeout: float) -> Telemetry:
        """Poll until the car reports `gear` (the gearbox can take a moment), returning the last reading."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while True:
            await asyncio.sleep(0.3)
            t = await self.game.poll()
            if t.gear == gear or loop.time() >= end:
                return t

    async def maneuver(self, p: dict) -> str:
        name = _enum(p, "maneuver", MANEUVERS)
        inputs, default_secs = MANEUVERS[name]
        secs = _number(p, "seconds", 0.5, 8, default_secs)
        self._input_gen += 1
        self._cancel_maneuver()
        await self._pilot_off()
        self._maneuver = asyncio.create_task(self._run_maneuver(inputs, secs, self.game.ai_mode))
        self.last_input = f"{name.replace('_', ' ')} for {secs:g} s"
        return f"Doing {name.replace('_', ' ')} for {secs:g} seconds." + await self._gear_warning(inputs)

    # --- driver-level orders (carried out by the bridge's control layer) ------------------------

    async def follow_road(self, p: dict) -> str:
        speed = _number(p, "speed_kmh", 5, 250)
        turn = _enum(p, "next_turn", ["left", "right", "straight"], None)
        self._take_over_from_manual()
        await self._start_pilot(mode="road", speed=speed, turn=turn)
        self.pilot_on, self.pilot_target = True, "the road"
        reply = f"Following the road at up to {speed:.0f} km/h"
        reply += f", going {turn} at the next junction." if turn else "."
        return reply + await self._car_state_hint()

    async def turn_at_next_junction(self, p: dict) -> str:
        direction = _enum(p, "direction", ["left", "right", "straight"])
        await self.game.pilot(turn=direction)
        note = "" if self.pilot_on else " (Use follow_road to start driving along the road.)"
        return f"At the next junction you'll go {direction}.{note}"

    async def set_target_speed(self, p: dict) -> str:
        speed = _number(p, "speed_kmh", 0, 250)
        await self.game.pilot(speed=speed)
        note = "" if self.pilot_on else " (Takes effect when you follow_road or go_to something.)"
        return f"Target speed {speed:.0f} km/h.{note}"

    async def go_to(self, p: dict) -> str:
        target = _enum(p, "target", GO_TO_TARGETS)
        speed = _number(p, "speed_kmh", 5, 250, None)
        arrive = _enum(p, "on_arrival", ["stop", "drive_through"], None)
        t = await self.game.poll()
        if target == "nearest_road":
            return await self.follow_road({"speed_kmh": speed or 40})

        cmd: dict[str, Any] = {"mode": "target"}
        if target == "nearest_ramp":
            ramp = await self._nearest_ramp(t)
            if not ramp:
                return "Your lidar doesn't see a ramp you can jump right now. Drive around to find one."
            cmd["pos"], desc = ramp
            arrive, speed = arrive or "drive_through", speed or 60
        elif target in ("nearest_car", "suspect", "nearest_police"):
            if target == "nearest_police":
                cop = (t.police or {}).get("nearest")
                if not cop:
                    return "There are no police cars around (they come with traffic: spawn_traffic)."
                cmd["vid"], desc = cop["vid"], "the nearest police car"
            elif target == "suspect":
                s = (t.police or {}).get("suspect")
                if not s:
                    return "You don't have a suspect right now (that needs a police car and a pursuit)."
                cmd["vid"], desc = s["vid"], f"your suspect ({self.car_catalog.get(s['name'], s['name']).split(' (')[0]})"
            else:
                cars = await self.game.nearby_vehicles(1000)
                if not cars:
                    return "There are no other cars around."
                cmd["vid"], desc = cars[0].vid, f"the {self.car_catalog.get(cars[0].model, cars[0].model).split(' (')[0]}"
            arrive, speed = arrive or "stop", speed or 50
        else:  # direction
            angle = _number(p, "angle_deg", -180, 180)
            dist = _number(p, "distance_m", 5, 1000, 100)
            heading = math.atan2(t.dir[0], t.dir[1]) + math.radians(angle)
            cmd["pos"] = [t.pos[0] + math.sin(heading) * dist, t.pos[1] + math.cos(heading) * dist, t.pos[2]]
            desc = f"a spot {dist:.0f} m away, {abs(angle):.0f}° {'left' if angle < 0 else 'right'}"
            arrive, speed = arrive or "stop", speed or 40
        cmd.update(speed=speed, arrive="through" if arrive == "drive_through" else "stop")
        self._take_over_from_manual()
        await self._start_pilot(**cmd)
        self.pilot_on, self.pilot_target = True, desc
        what = "and drive straight through it" if arrive == "drive_through" else "and stop there"
        return f"Heading for {desc} at up to {speed:.0f} km/h {what}." + await self._car_state_hint()

    async def push_through(self, p: dict) -> str:
        t = await self.game.poll()
        pilot = t.pilot or {}
        if not self.pilot_on or pilot.get("obstacle") is None:
            return ("That didn't do anything: you haven't stopped for anything right now. (To hit something on "
                    "purpose, go_to it with on_arrival drive_through.)")
        what = obstacle_name(pilot.get("obstacleWhat"))
        await self.game.pilot(push_through=True)
        return f"Going straight into {what}." + solid_note(what.removeprefix("a "))

    async def overtake(self, p: dict) -> str:
        # The pilot's own words for the two fixed choices are 'left' (the other lane) and 'right' (the
        # shoulder), from driving on the right; it mirrors them on maps that drive on the left.
        raw = p.get("side", "auto")
        if raw not in ("left", "right"):  # (the older names are still accepted)
            raw = _enum(p, "side", ["auto", "other_lane", "shoulder"], "auto")
        side = {"other_lane": "left", "shoulder": "right"}.get(raw, raw)
        speed = _number(p, "speed_kmh", 5, 250, None)
        t = await self.game.poll()
        gap = (t.pilot or {}).get("obstacle")
        if gap is None and self.lidar_range > 0:
            touching = nearest_blocking(await self.game.lidar(self.lidar_range, self.lidar_beams))
            gap = touching[0] if touching else None
        room = gap is None or gap >= 5.0 or t.speed_kmh >= STOPPED_KMH  # a car needs ~5 m to swing out
        last = self.last_pilot_cmd or {}
        if self.pilot_on and last.get("mode") == "road":
            cmd = {**last, "pass": side}  # keep her speed, junction choice and any flight
        else:
            cmd = {"mode": "road", "speed": (last.get("speed") if self.pilot_on else None) or 30, "pass": side}
            self.pilot_on, self.pilot_target = True, "the road"
        if speed:
            cmd["speed"] = speed
        where = {"auto": "onto whichever line across the road is clear", "left": "into the other lane - watch "
                 "for oncoming cars", "right": "onto the shoulder"}[side]
        if side != "auto" and t.drive_left:
            where += " (this map drives on the left, so that's to your " + ("right)" if side == "left" else "left)")
        if not room:  # too close to swing out: reverse a little first, like a driver would, then go
            back = round(6 - gap) + 1
            self._take_over_from_manual()
            await self._pilot_off()
            self.last_pilot_cmd = cmd
            self._maneuver = asyncio.create_task(self._back_up(back, 0.0, cmd))
            self.last_input = f"backing up {back} m to make room to overtake"
            return (f"It's only {gap:.0f} m in front of you, too close to swing out, so you back up {back} m first, "
                    f"then pull out {where} at up to {cmd['speed']:.0f} km/h.")
        self._take_over_from_manual()
        await self._start_pilot(**cmd)
        return (f"Pulling out {where} at up to {cmd['speed']:.0f} km/h; you'll move back into your lane once "
                "you're past." + await self._car_state_hint())

    async def refresh_destinations(self) -> Action | None:
        """Re-read this map's places and rebuild drive_to for them. None if the map has none."""
        try:
            data = await self.game.destinations()
        except Exception:
            log.warning("Couldn't read the map's destinations", exc_info=True)
            data = None
        self.actions = [a for a in self.actions if a.name != "drive_to"]
        found = (data or {}).get("places") or []
        self.missions = [p for p in found if p["kind"].startswith("mission")]
        self.places = _label_places([p for p in found if not p["kind"].startswith("mission")])
        if not found:
            return None
        options = DESTINATION_SPECIALS[:3] + (["mission"] if self.missions else []) + list(self.places)
        action = Action(
            "drive_to",
            "Drive to a place on this map. The route over the roads is planned for you and marked on screen "
            "with GPS arrows; you take every junction on it by yourself and stop when you get there. "
            "'map waypoint' is wherever the streamer marked on the big map. 'mission' needs mission_name "
            "(list_destinations shows them, with distances). follow_road or turn_at_next_junction leaves the route.",
            {"type": "object", "properties": {
                "destination": {"type": "string", "enum": options},
                "mission_name": {"type": "string"},
                "speed_kmh": {"type": "integer", "minimum": 10, "maximum": 250},
            }, "required": ["destination"]},
        )
        self.actions.append(action)
        log.info("Destinations on %s: %d places, %d missions", (data or {}).get("level"), len(self.places),
                 len(self.missions))
        return action

    async def list_destinations(self, p: dict) -> str:
        data = await self.game.destinations() or {}
        if not self.places and not self.missions:
            return "This map has no marked places to drive to."
        t = await self.game.poll()

        def dist(place: dict) -> float:
            return math.dist(t.pos[:2], place["pos"][:2])

        def where(place: dict) -> str:
            return f"{dist(place) / 1000:.1f} km {relative_direction(t, tuple(place['pos']))}"

        places = sorted(self.places.items(), key=lambda kv: dist(kv[1]))
        lines = ["Places: " + "; ".join(f"{label} ({where(pl)})" for label, pl in places)]
        if self.missions:
            near = sorted(self.missions, key=dist)[:25]
            lines.append("Missions (drive_to destination 'mission' with mission_name): " + "; ".join(
                f"{m['name']} [{m['kind'].split(':', 1)[1]}] ({where(m)})" for m in near)
                + (f"; and {len(self.missions) - len(near)} more further away" if len(self.missions) > len(near) else ""))
        lines.append("The streamer has set a waypoint on the map ('map waypoint')." if data.get("waypoint")
                     else "There's no waypoint on the map right now.")
        return "\n".join(lines)

    async def drive_to(self, p: dict) -> str:
        options = DESTINATION_SPECIALS + list(self.places)
        dest = _enum(p, "destination", options, label="destination")
        speed = _number(p, "speed_kmh", 10, 250, None)
        t = await self.game.poll()
        waypoint = False
        if dest in ("nearest gas station", "nearest charging station"):
            charging = dest == "nearest charging station"
            stations = [(label, pl) for label, pl in self.places.items()
                        if pl["kind"] == "gasStation" and ("charging" in label.lower()) == charging]
            if not stations:
                return f"There's no {dest.replace('nearest ', '')} on this map."
            label, place = min(stations, key=lambda s: math.dist(t.pos[:2], s[1]["pos"][:2]))
        elif dest == "map waypoint":
            data = await self.game.destinations() or {}
            if not data.get("waypoint"):
                return "There's no waypoint on the map right now (the streamer can set one on the big map)."
            label, place, waypoint = "the map waypoint", {"pos": data["waypoint"]}, True
        elif dest == "mission":
            wanted = str(p.get("mission_name") or "").strip()
            if not wanted:
                raise InvalidAction("Say which mission with mission_name (list_destinations shows them).")
            place = _closest_name(wanted, self.missions)
            if not place:
                near = sorted(self.missions, key=lambda m: math.dist(t.pos[:2], m["pos"][:2]))[:8]
                raise InvalidAction(f"There's no mission called '{wanted}'. Nearby ones: "
                                    + ", ".join(m["name"] for m in near) + ".")
            label = f"the {place['name']} mission"
        else:
            label, place = dest, self.places[dest]
        last = self.last_pilot_cmd or {}
        speed = speed or (last.get("speed") if self.pilot_on else None) or 50
        self._take_over_from_manual()
        await self._start_pilot(mode="road", speed=speed,
                                route={"pos": list(place["pos"]), "name": label, "waypoint": waypoint})
        self.pilot_on, self.pilot_target = True, label
        crow = math.dist(t.pos[:2], place["pos"][:2]) / 1000
        return (f"Route planned to {label} ({crow:.1f} km away in a straight line), at up to {speed:.0f} km/h. "
                "It's marked on the road with GPS arrows; you'll take every junction on it and stop when you "
                "arrive." + await self._car_state_hint())

    def _style(self) -> dict:
        out: dict = {}
        for key, choice in self.style_choice.items():
            out.update(STYLE_CHOICES[key][choice])
        return out

    def _style_label(self) -> str:
        """The style in words, leaving out what's normal ('' if it's all normal)."""
        normal = STYLE_PRESETS["normal"]
        return ", ".join(f"{k} {v.replace('_', ' ')}" for k, v in self.style_choice.items() if v != normal[k])

    async def driving_style(self, p: dict) -> str:
        preset = _enum(p, "preset", STYLE_PRESETS, None)
        if preset is None and not any(k in p for k in STYLE_CHOICES):
            raise InvalidAction("Give a preset or at least one of: " + ", ".join(STYLE_CHOICES) + ".")
        if p.get("lane") == "keep_right":  # older name
            p = {**p, "lane": "own_lane"}
        choice = dict(STYLE_PRESETS[preset]) if preset else dict(self.style_choice)
        for key, options in STYLE_CHOICES.items():
            if key in p:
                choice[key] = _enum(p, key, options)
        self.style_choice = choice
        await self.game.pilot(style=self._style())
        if self.last_pilot_cmd:
            self.last_pilot_cmd["style"] = self._style()
        label = self._style_label() or "normal all round"
        return f"Driving style: {label}." + ("" if self.pilot_on else " (It applies once you follow_road, drive_to or go_to.)")

    async def describe_impact(self, speed_kmh: float) -> str:
        """Who hit whom, or what she hit, for the crash message (and the dashboard for a minute after)."""
        try:
            data = await self.game.last_impact() or {}
        except Exception:
            log.debug("last_impact failed", exc_info=True)
            data = {}
        parts = []
        for v in sorted(data.get("vehicles") or [], key=lambda v: -(v["weInto"] + v["theyInto"]))[:2]:
            name = self.car_catalog.get(v["name"], v["name"]).split(" (")[0].split(",")[0]
            who = f"the {name}" + (" police car" if v.get("police") else "")
            mine, theirs = v["weInto"] * 3.6, v["theyInto"] * 3.6
            if theirs > 7 and theirs > mine * 1.5:
                you = "you were stopped" if v["ourSpeed"] < 1 else f"you were doing {v['ourSpeed'] * 3.6:.0f}"
                parts.append(f"{who[0].upper() + who[1:]} crashed into YOU at {theirs:.0f} km/h ({you})")
            elif mine > 7 and mine > theirs * 1.5:
                it = "it was standing still" if v["theirSpeed"] < 1 else f"it was doing {v['theirSpeed'] * 3.6:.0f}"
                parts.append(f"You crashed into {who} at {mine:.0f} km/h ({it})")
            else:
                parts.append(f"You and {who} crashed into each other (you at {v['ourSpeed'] * 3.6:.0f}, it at "
                             f"{v['theirSpeed'] * 3.6:.0f} km/h)")
        fixed = data.get("fixed")
        if not parts and fixed:
            kind = classify(min(float(fixed.get("normalZ") or 0.0), 0.8), fixed.get("cls") or "", fixed.get("shape") or "")
            thing = kind if kind and not kind.startswith(("slope", "steep")) else "the ground" if kind else "something"
            parts.append(f"You hit {'a ' + thing if thing not in ('the ground', 'something') else thing} "
                         f"at about {speed_kmh:.0f} km/h")
        text = "; ".join(parts) if parts else f"You hit something at about {speed_kmh:.0f} km/h"
        self.last_crash = (asyncio.get_running_loop().time(), text)
        return text

    async def _start_pilot(self, **cmd) -> None:
        cmd.setdefault("style", self._style())
        """Hand a driver-level order to the control layer, remembering it so back_up can resume it."""
        self.last_pilot_cmd = cmd
        await self.game.pilot(**cmd)

    async def back_up(self, p: dict) -> str:
        dist = _number(p, "distance_m", 2, 30, 6)
        steer = _number(p, "steering", -100, 100, 0) / 100
        resume = self.last_pilot_cmd if self.pilot_on else None
        self._take_over_from_manual()
        await self._pilot_off()
        self._maneuver = asyncio.create_task(self._back_up(dist, steer, resume))
        self.last_input = f"backing up {dist:.0f} m"
        then = "then carry on as before" if resume else "then stop in drive"
        return f"Backing up {dist:.0f} m ({then})."

    async def _back_up(self, dist: float, steer: float, resume: dict | None) -> None:
        g = self.game
        try:
            await g.shift("reverse")
            t = await self._wait_for_gear("R", 2.0)
            if t.gear != "R":
                self.notices.append(f"Backing up didn't work: the car wouldn't go into reverse (it's in {t.gear}). "
                                    "Try shift_gear reverse and drive_manually, or recover_car.")
                return
            start, throttle = t.pos, 0.35
            await g.control(throttle=throttle, steering=steer)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 4 + dist  # give up if something behind stops us too
            moved, best, progress_at = 0.0, 0.0, loop.time()
            while loop.time() < deadline:
                await asyncio.sleep(0.2)
                t = await g.poll()
                moved = math.dist(start[:2], t.pos[:2])
                if moved >= dist:
                    break
                if moved > best + 0.2:
                    best, progress_at = moved, loop.time()
                elif loop.time() - progress_at > 1.2 and throttle < 0.9:  # wedged: give it more
                    throttle, progress_at = min(0.9, throttle + 0.25), loop.time()
                    await g.control(throttle=throttle, steering=steer)
            await g.control(brake=1.0)
            await self._wait_until_stopped(3)
            await g.control()
            await g.shift("drive")
            await asyncio.sleep(0.8)
            if moved < 0.5:  # resuming would only push into the same obstacle again
                why = (_switched_off(t) if t.engine_on is False
                       else "it's hung up on something, or the wheels can't get grip")
                self.notices.append(f"Backing up didn't work: the car didn't move, even with more throttle ({why}). "
                                    "You're back in drive. recover_car will get you unstuck.")
                return
            if resume:
                await g.pilot(**resume)
                self.pilot_on = True
            short = f" (not the full {dist:.0f} m - something stopped you)" if moved < dist * 0.6 else ""
            self.notices.append(f"You backed up {moved:.0f} m{short} and you're back in drive"
                                + (", carrying on." if resume else "."))
        except asyncio.CancelledError:
            await g.control()
            raise

    async def flee(self, p: dict) -> str:
        source = _enum(p, "from", FLEE_FROM)
        route = _enum(p, "route", ["roads", "off_road"], "roads")
        speed = _number(p, "speed_kmh", 10, 250, 90)
        t = await self.game.poll()
        police = t.police or {}
        if source == "nearest_police":
            cop = police.get("nearest")
            if not cop:
                return "There are no police cars around to run from."
            vid, desc = cop["vid"], "the police"
        elif source == "suspect":
            s = police.get("suspect")
            if not s:
                return "You don't have a suspect right now."
            vid, desc = s["vid"], "your suspect"
        else:
            cars = await self.game.nearby_vehicles(1000)
            if not cars:
                return "There are no other cars around."
            vid, desc = cars[0].vid, f"the {self.car_catalog.get(cars[0].model, cars[0].model).split(' (')[0]}"
        self._take_over_from_manual()
        if route == "off_road":
            await self._start_pilot(mode="target", speed=speed, flee_vid=vid, arrive="through")
            how = "cutting straight across country, directly away from them"
        else:
            await self._start_pilot(mode="road", speed=speed, flee_vid=vid)
            how = "turning away from them at every junction"
        self.pilot_on, self.pilot_target = True, desc
        return f"Fleeing from {desc} at up to {speed:.0f} km/h, {how}." + await self._car_state_hint()

    async def _nearest_ramp(self, t: Telemetry) -> tuple[list[float], str] | None:
        """World position of the nearest ramp the lidar sees, preferring ones that face the car."""
        hits = await self.game.lidar(self.lidar_range or 150, max(self.lidar_beams, 72)) or []
        best = None
        for hit in hits:
            angle, dist, nx, ny, nz, _h, cls, shape, dx, dy = hit[:10]
            if classify(nz, cls, shape) != "ramp":
                continue
            facing = -(nx * dx + ny * dy) / (math.hypot(nx, ny) or 1.0)
            score = dist - (100 if facing > 0.5 else 0)  # a ramp facing us beats a closer side-on one
            if best is None or score < best[0]:
                pos = [t.pos[0] + dx * dist, t.pos[1] + dy * dist, t.pos[2]]
                name = shape.replace("s_gm_", "").replace("_", " ") or "ramp"
                best = (score, pos, f"the {name} {dist:.0f} m away")
        return (best[1], best[2]) if best else None

    def _take_over_from_manual(self) -> None:
        """A driver-level order replaces whatever manual input she was holding."""
        self._input_gen += 1
        self._cancel_maneuver()

    async def _pilot_off(self) -> None:
        if self.pilot_on:
            self.pilot_on = False
            try:
                await self.game.pilot(mode="off")
            except Exception:
                log.debug("pilot off failed", exc_info=True)

    async def drive_manually(self, p: dict) -> str:
        if "release_after_seconds" in p:
            p = {**p, "seconds": p["release_after_seconds"]}  # older name still accepted
        secs = _number(p, "seconds", 0.5, 5, None)  # None = hold until the next input
        inputs = dict(
            throttle=_number(p, "throttle", 0, 100, 0) / 100,
            brake=_number(p, "brake", 0, 100, 0) / 100,
            steering=_number(p, "steering", -100, 100, 0) / 100,
            parkingbrake=1.0 if _bool(p, "handbrake", False) else 0.0,
        )
        self._input_gen += 1
        self._cancel_maneuver()
        self._maneuver = asyncio.create_task(self._run_maneuver(inputs, secs, self.game.ai_mode))
        parts = [f"{k} {v * 100:.0f}%" for k, v in inputs.items() if v and k != "parkingbrake"]
        if inputs["parkingbrake"]:
            parts.append("handbrake on")
        held = ", ".join(parts) or "nothing"
        self.held_inputs = inputs
        await self._pilot_off()  # taking the wheel herself
        if secs is None:
            self.last_input = f"{held} (held until you change it)"
            return f"Now holding {held} until your next input." + await self._gear_warning(inputs)
        self.last_input = f"{held} for {secs:g} s"
        return f"Holding {held} for {secs:g} seconds, then letting go." + await self._gear_warning(inputs)

    async def _gear_warning(self, inputs: dict[str, float]) -> str:
        if not inputs.get("throttle"):
            return ""
        if inputs.get("brake", 0) >= 0.1:
            return (" Careful: you're pressing throttle AND brake at the same time, so they fight each other - "
                    "use one or the other.")
        t = await self.game.poll()
        if t.engine_on is False:
            return " The engine is off though - start it first."
        if self.game.ai_mode == "disabled" and t.gear in ("P", "N"):
            return (f" You're in {'park' if t.gear == 'P' else 'neutral'} though, so the car won't move - "
                    f"shift_gear first{_gears_hint(t)}.")
        if t.gear == "R":
            return f" You're in REVERSE, so this drives you backwards. Pick a forward gear to go forwards{_gears_hint(t)}."
        return ""

    async def shift_gear(self, p: dict) -> str:
        gear = _enum(p, "gear", GEAR_TARGETS, label="gear")
        t = await self._fresh_poll()  # (right after a reset the last reading can be out of date)
        before = t.gear
        if _in_gear(before, gear, has_d="D (" in (t.gears or "")):  # don't wait for a change that won't come
            return f"You're already in {before}."
        await self.game.shift(gear)
        note = " (The autopilot is on and may shift again by itself.)" if self.game.ai_mode != "disabled" else ""
        # Report the gear actually engaged: some gearboxes take a couple of seconds, and some cars lack
        # positions (no D on a rock crawler, so "drive" gives manual mode).
        now = before
        for _ in range(12):
            await asyncio.sleep(0.25)
            now = (await self.game.poll()).gear
            if now != before:
                break
        if now == before and gear not in ("up", "down"):
            return f"Shifting to {gear} - still in {now} for now (this gearbox can be slow to change).{note}"
        extra = ""
        if gear == "drive" and now.startswith("M"):
            extra = " This car has no D, so you're in manual mode: shift up yourself as you speed up."
        return f"Now in {now}.{extra}{note}"

    async def use_vehicle_control(self, p: dict) -> str:
        controls = {c["name"]: c for c in self.vehicle_controls}
        name = _enum(p, "control", controls, label="control on this car")
        control = controls[name]
        hold = _number(p, "hold_seconds", 0, 10, None)
        if control.get("axis"):
            value = _number(p, "value", -1, 1, 1.0)
            await self.game.use_control(name, "change", value)
            if hold:
                asyncio.create_task(self._release_later(name, "change", hold, 0.0))
            return f"{control['title']}: set to {value:g}" + (f" for {hold:g} s." if hold else ".")
        await self.game.use_control(name, "down")
        # Release after the hold (or a short tap) so momentary controls don't stick.
        asyncio.create_task(self._release_later(name, "up", hold or 0.2))
        return f"{control['title']}." + (f" Held for {hold:g} s." if hold else "")

    async def _release_later(self, name: str, phase: str, delay: float, value: float | None = None) -> None:
        await asyncio.sleep(delay)
        try:
            await self.game.use_control(name, phase, value)
        except Exception:
            log.debug("Releasing %s failed", name, exc_info=True)

    async def engine(self, p: dict) -> str:
        on = _enum(p, "state", ["start", "stop"]) == "start"
        then = _enum(p, "then_shift", ["drive", "reverse", "neutral", "park"], None)
        if on and then:  # key on and into gear in one go
            started = await self._start(p)
            if started.startswith(("You cranked", "You tried")):
                return started  # it didn't start: no point shifting
            return started.split(" You're in ")[0] + " " + await self.shift_gear({"gear": then})
        if not on:
            self._cancel_maneuver()
            await self.game.set_ai_mode("disabled")
            await self.game.set_engine(False)
            return "Engine off."
        return await self._start(p)

    async def _start(self, p: dict) -> str:
        t = await self._fresh_poll()  # right after a reset the last reading can still say "running"
        if t.engine_on:
            return "It's already switched on." if t.electric else "The engine is already running."
        await self.game.set_engine(True)
        loop = asyncio.get_running_loop()
        end = loop.time() + 3.0  # an engine takes a moment to catch, and the reading lags a poll behind
        while True:
            await asyncio.sleep(0.4)
            t = await self.game.poll()
            if t.engine_on or loop.time() > end:
                break
        if not t.engine_on:
            if t.electric:
                return "You tried to switch it on, but it didn't come on. Check the battery and damage, or try again."
            return "You cranked it but the engine didn't start. Check fuel and damage, or try again."
        done = "Switched on (it's electric)." if t.electric else "Engine started."
        return done + (f" You're in {t.gear} - pick a gear to move{_gears_hint(t)}." if t.gear in ("P", "N") else "")

    async def _run_maneuver(self, inputs: dict[str, float], secs: float | None, resume_mode: str) -> None:
        """Apply inputs for secs, or hold them until the next input (secs=None, with a dead man's switch)."""
        g = self.game
        gen = self._input_gen
        self.held_since = asyncio.get_running_loop().time()
        # chase/flee need a target we no longer have, so only resume plain driving modes.
        resume = resume_mode in ("traffic", "random", "span")
        braking = inputs.get("brake", 0) > 0 and not inputs.get("throttle")
        superseded = False
        try:
            await g.set_ai_mode("disabled")
            await g.control(**inputs)
            limit = DEADMAN_SECONDS if secs is None else secs
            loop = asyncio.get_running_loop()
            start = loop.time()
            if braking and await self._wait_until_stopped(limit):
                await g.control(parkingbrake=1.0)  # don't let the brake shift into reverse
                if secs is None:  # stopped early: keep holding until her next input or the dead man's switch
                    await asyncio.sleep(max(0.0, limit - (loop.time() - start)))
            else:
                await asyncio.sleep(limit)
            if secs is None:
                self.notices.append(f"You gave no driving input for {DEADMAN_SECONDS:.0f} s, so you let go of "
                                    "the controls and the car is coasting.")
        except asyncio.CancelledError:
            superseded = self._input_gen != gen  # a newer input took over; leave the pedals to it
            raise
        finally:
            if not superseded:
                await g.control(parkingbrake=1.0 if braking and not resume else 0.0)
                self.last_input = self.last_input.split(" (held")[0] + " (now released)"
        if resume:
            await g.set_ai_mode(resume_mode)

    def _cancel_maneuver(self) -> None:
        if self._maneuver and not self._maneuver.done():
            self._maneuver.cancel()

    @property
    def holding(self) -> bool:
        """Controls are being held with no end time (the driving loop should keep checking in)."""
        return self.input_active and "(held" in self.last_input

    def take_notices(self) -> list[str]:
        notices, self.notices = self.notices, []
        return notices

    async def honk(self, p: dict) -> str:
        pattern = _enum(p, "pattern", HONKS, "short")

        async def go() -> None:
            for dur in HONKS[pattern]:
                await self.game.horn(True)
                await asyncio.sleep(dur)
                await self.game.horn(False)
                await asyncio.sleep(0.12)

        asyncio.create_task(go())
        return "HONK!"

    async def set_lights(self, p: dict) -> str:
        head = _enum(p, "headlights", ["off", "on", "high_beams"], None)
        ind = _enum(p, "indicator", ["none", "left", "right", "hazards"], None)
        if head is None and ind is None:
            raise InvalidAction("Give 'headlights' and/or 'indicator'.")
        kwargs: dict[str, Any] = {}
        if head is not None:
            kwargs["headlights"] = {"off": 0, "on": 1, "high_beams": 2}[head]
        if ind is not None:
            kwargs.update(left_signal=ind == "left", right_signal=ind == "right", hazard_signal=ind == "hazards")
        await self.game.set_lights(**kwargs)
        return "Lights updated."

    async def recover(self, p: dict) -> str:
        self._cancel_maneuver()
        await self.game.recover()
        await asyncio.sleep(1.0)  # the car is re-initialised; telemetry needs a moment
        reply = "Car recovered and repaired on the nearest road." + (" The autopilot is off." if self.allow_autopilot else "")
        return reply + await self._car_state_hint()

    async def _car_state_hint(self) -> str:
        """What she has to do before the car will move again (a reset car often comes back off and in neutral)."""
        try:
            t = await self.game.poll()
        except Exception:
            return ""
        todo = []
        if t.engine_on is False:
            todo.append(_switched_off(t) + " (engine start)")
        if t.gear in ("N", "P"):
            todo.append(f"you're in {'neutral' if t.gear == 'N' else 'park'} (pick a gear{_gears_hint(t)})")
        return (" Before you can drive: " + " and ".join(todo) + ".") if todo else ""

    async def list_car_configs(self, p: dict) -> str:
        model = _enum(p, "car", self.car_catalog, label="car")
        configs = await self.configs_for(model, fresh=True)
        if not configs:
            return f"No configurations found for {self.car_catalog[model]}; change_car without config gives the default."
        lines = []
        for c in configs:
            extra = ", ".join(x for x in (c.get("type"), f"{c['power']:.0f} hp" if c.get("power") else None) if x)
            desc = f" - {c['desc'][:80]}" if c.get("desc") else ""
            lines.append(f"{c['key']}: {c['name']}" + (f" ({extra})" if extra else "") + desc)
        return f"Configurations for {self.car_catalog[model]} (use the key as config):\n" + "\n".join(lines)

    async def change_car(self, p: dict) -> str:
        model = _enum(p, "car", self.car_catalog, label="car")
        wanted = p.get("config")
        config = None
        if wanted not in (None, "", "default"):
            configs = await self.configs_for(model)
            config = _match_config(str(wanted), configs)
            if config is None:  # maybe it was saved after we cached the list
                configs = await self.configs_for(model, fresh=True)
                config = _match_config(str(wanted), configs)
            if config is None:
                options = ", ".join(f"{c['key']} ({c['name']})" for c in configs) or "none"
                raise InvalidAction(f"{self.car_catalog[model]} has no configuration '{wanted}'. Options: {options}.")
        self._cancel_maneuver()
        await self.game.change_vehicle(model, config["key"] if config else None)
        what = f"{self.car_catalog[model]}, {config['name']}" if config else self.car_catalog[model]
        await asyncio.sleep(2.0)  # let the new car spawn before reading its state
        return (f"You're now driving the {what}." + (" The autopilot is off." if self.allow_autopilot else "")
                + await self._car_state_hint())

    async def check(self, p: dict) -> str:
        return await self.status_markdown()

    async def spawn_traffic(self, p: dict) -> str:
        amount = int(_number(p, "amount", 1, 20))
        await self.game.spawn_traffic(amount, 0.0)
        return f"Spawning up to {amount} traffic cars."

    async def pause_game(self, p: dict) -> str:
        paused = _bool(p, "paused")
        if paused is None:
            raise InvalidAction("Say paused: true or false.")
        if (await self.game.poll()).paused == paused:
            return f"The game is already {'paused' if paused else 'running'}."
        await self.game.set_paused(paused)
        return "Game paused - everything is frozen." if paused else "Game unpaused - back to it."

    async def set_time(self, p: dict) -> str:
        hour = _number(p, "hour", 0, 23)
        await self.game.set_time_of_day(hour)
        return f"It's now {hour:02.0f}:00."

    async def set_gravity(self, p: dict) -> str:
        preset = _enum(p, "preset", GRAVITY)
        await self.game.set_gravity(GRAVITY[preset])
        return f"Gravity set to {preset} ({-GRAVITY[preset]:g} m/s²)."


def _gears_hint(t: Telemetry) -> str:
    return f" (this car's gears: {t.gears})" if t.gears else ""


def _match_config(wanted: str, configs: list[dict]) -> dict | None:
    """Find a config by key or (loosely) by name, e.g. "police interceptor" -> interceptor."""
    w = wanted.strip().lower()
    for field in ("key", "name"):
        for c in configs:
            if c[field].lower() == w:
                return c
    words = w.replace("_", " ").split()
    hits = [c for c in configs if all(x in f"{c['key']} {c['name']}".lower() for x in words)]
    return min(hits, key=lambda c: len(c["name"])) if hits else None


def _damage_word(damage: float) -> str:
    if damage < 50:
        return "none"
    if damage < 1000:
        return "scratched"
    if damage < 5000:
        return "dented"
    if damage < 20000:
        return "badly damaged"
    return "totaled"
