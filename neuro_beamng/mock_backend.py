"""A fake BeamNG for testing the Neuro side without the game running."""

from __future__ import annotations

import logging
import math
import random
import time

from .backend import GameBackend, NearbyVehicle, Telemetry, relative_direction

log = logging.getLogger(__name__)


class MockBackend(GameBackend):
    def __init__(self, crash_chance: float = 0.01) -> None:
        self.level = "mock_city"
        self.vehicle_model = "etk800"
        self.crash_chance = crash_chance
        self.t = Telemetry(gear="P", engine_on=True, gearbox_mode="realistic")
        self._gear: str | None = None
        self._controls = dict(throttle=0.0, brake=0.0, steering=0.0, parkingbrake=0.0)
        self._yaw = 0.0
        self._last = time.monotonic()
        self._traffic: list[tuple[str, tuple[float, float, float]]] = []
        self._events: list[dict] = []
        self._pilot: dict = {"mode": "off"}

    async def connect(self) -> None:
        log.info("Mock game ready")

    async def close(self) -> None:
        pass

    async def poll(self) -> Telemetry:
        now = time.monotonic()
        dt, self._last = now - self._last, now
        t = self.t

        piloting = self._pilot.get("mode") not in (None, "off")
        if piloting:
            moving_gear = t.engine_on is not False and t.gear not in ("P", "N")
            target = self._pilot.get("speed", 30) if moving_gear else 0.0
            t.speed_kmh += max(-30 * dt, min(15 * dt, target - t.speed_kmh))
            t.pilot = {"mode": self._pilot["mode"], "speed": self._pilot.get("speed", 30), "status": "cruising",
                       "turn": self._pilot.get("turn"), "warn": None if moving_gear else "you are in park",
                       "passing": self._pilot.get("pass"), "route": self._pilot.get("route"),
                       "junction": {"dist": 50.0, "branches": 3, "name": "j1"}}
        else:
            t.pilot = None
        if piloting:
            pass
        elif self.ai_mode not in ("disabled", "stopping"):
            target = self.ai_speed_kmh or 60.0
            t.speed_kmh += max(-30 * dt, min(15 * dt, target - t.speed_kmh))
            self._yaw += random.uniform(-0.2, 0.2) * dt
        else:
            c = self._controls
            in_gear = t.engine_on is not False and t.gear not in ("P", "N")
            accel = 20 * c["throttle"] * in_gear - 40 * c["brake"] - 30 * c["parkingbrake"] - 2
            t.speed_kmh = max(0.0, t.speed_kmh + accel * dt)
            self._yaw += c["steering"] * dt * min(1.0, t.speed_kmh / 20)

        if t.speed_kmh > 20 and not t.tipped_over and random.random() < self.crash_chance:
            log.info("[mock] CRASH")
            t.damage += random.uniform(500, 5000)
            t.broken_parts += random.randint(1, 4)
            if "Radiator leak!" not in t.problems:
                t.problems = [*t.problems, "Radiator leak!"]
                self._events.append({"text": "Radiator leak!", "category": "vehicle.engine.radiatorLeak.true",
                                     "source": "car"})
            t.speed_kmh = random.uniform(0, 10)
            if random.random() < 0.3:
                t.up = (0.0, 0.0, -1.0)

        t.dir = (math.sin(self._yaw), math.cos(self._yaw), 0.0)
        step = t.speed_kmh / 3.6 * dt
        t.pos = (t.pos[0] + t.dir[0] * step, t.pos[1] + t.dir[1] * step, t.pos[2])
        if self.ai_mode not in ("disabled", "stopping"):
            t.gear = "D"
        elif self._gear:
            t.gear = {"park": "P", "reverse": "R", "neutral": "N", "drive": "D", "up": "D", "down": "D"}.get(self._gear, self._gear)
        t.rpm = 800 + t.speed_kmh * 30
        return Telemetry(**{**t.__dict__, "extra": {}})

    async def set_paused(self, paused: bool) -> None:
        log.info("[mock] paused -> %s", paused)
        self.t.paused = paused

    async def pilot(self, **command) -> None:
        log.info("[mock] pilot %s", command)
        if command.get("mode"):
            self._pilot = {"mode": command["mode"], "speed": self._pilot.get("speed", 30)}
        if command.get("speed") is not None:
            self._pilot["speed"] = command["speed"]
        if command.get("route"):
            self._pilot["route"] = {"name": command["route"]["name"], "remaining": 1200.0}
        if command.get("pass"):
            self._pilot["pass"] = None if command["pass"] == "none" else command["pass"]
        if "turn" in command:
            self._pilot["turn"] = None if command["turn"] == "straight" else command["turn"]

    def take_events(self) -> list[dict]:
        events, self._events = self._events, []
        return events

    async def nearby_vehicles(self, radius_m: float = 300.0) -> list[NearbyVehicle]:
        out = []
        for vid, pos in self._traffic:
            d = math.dist(self.t.pos, pos)
            if d <= radius_m:
                out.append(NearbyVehicle(vid, "covet", d, relative_direction(self.t, pos)))
        return sorted(out, key=lambda n: n.distance_m)

    async def road_ahead(self, max_dist: float = 160.0) -> dict | None:
        # A gentle fake curve that bends a little left of the current heading.
        pts, (x, y), yaw = [], self.t.pos[:2], self._yaw
        for i in range(1, 9):
            yaw -= 0.08
            x, y = x + math.sin(yaw) * 20, y + math.cos(yaw) * 20
            pts.append([x, y, 1 if i == 5 else 0])
        return {"dist": 1.0, "halfWidth": 4.0, "points": pts}

    async def set_ai_mode(self, mode: str, target_vid: str | None = None) -> None:
        log.info("[mock] AI mode -> %s (target %s)", mode, target_vid)
        self.ai_mode = mode

    async def set_ai_speed(self, kmh: float, exact: bool) -> None:
        log.info("[mock] AI speed -> %s km/h (%s)", kmh, "exact" if exact else "limit")
        self.ai_speed_kmh = kmh

    async def set_ai_style(self, aggression: float | None, in_lane: bool | None) -> None:
        log.info("[mock] AI style aggression=%s in_lane=%s", aggression, in_lane)

    async def control(self, throttle=0.0, brake=0.0, steering=0.0, parkingbrake=0.0) -> None:
        self._controls = dict(throttle=throttle, brake=brake, steering=steering, parkingbrake=parkingbrake)
        log.info("[mock] control %s", self._controls)

    async def shift(self, target: str) -> None:
        log.info("[mock] shift -> %s", target)
        self._gear = target

    async def set_engine(self, on: bool) -> None:
        log.info("[mock] engine %s", "start" if on else "stop")
        self.t.engine_on = on

    async def destinations(self) -> dict | None:
        return {"level": self.level, "waypoint": None, "places": [
            {"name": "Downtown", "kind": "spawnPoint", "pos": [400.0, 300.0, 0.0]},
            {"name": "Harbour", "kind": "spawnPoint", "pos": [-900.0, 100.0, 0.0]},
            {"name": "Highway", "kind": "driftSpot", "pos": [1500.0, -200.0, 0.0]},
            {"name": "Apex Gas Station", "kind": "gasStation", "pos": [250.0, 50.0, 0.0]},
            {"name": "Apex Charging Station", "kind": "gasStation", "pos": [-300.0, 600.0, 0.0]},
            {"name": "Mountain Race", "kind": "mission:aiRace", "pos": [2000.0, 900.0, 0.0]},
            {"name": "Fuel Delivery", "kind": "mission:delivery", "pos": [-500.0, -700.0, 0.0]},
        ]}

    async def ground_scan(self, max_range: float = 50.0, bearings: int = 16, step: float = 2.0) -> dict | None:
        # Flat all round, except: a jump ahead-left, water to the right, a building behind, a hill to the left.
        n = int(max_range / step)
        scans = [{"h": [0.0] * n, "objs": []} for _ in range(bearings)]
        per = bearings // 8
        scans[7 * per]["h"] = [0.0] * 9 + [0.5, 1.2, 2.0, 0.0] + [0.0] * (n - 13)
        scans[2 * per] = {"h": [0.0, -0.2, -0.4, -0.5, -0.6], "objs": [[5, "WaterBlock:"]]}
        scans[4 * per] = {"h": [0.0] * 4, "stop": "wall", "at": 9.0, "tall": 6.0, "objs": [[5, "TSStatic:building_shed"]]}
        scans[6 * per]["h"] = [round(0.4 * (i + 1), 1) for i in range(n)]
        return {"step": step, "bearings": bearings, "scans": scans}

    async def lidar(self, max_range: float = 150.0, beams: int = 36, height: float = 0.8) -> list | None:
        # A wall ahead-right, a ramp ahead-left that faces the car, and a parked car behind. The car's
        # closing speed follows our own speed, so it only triggers a warning when reversing towards it.
        reversing_speed = self.t.speed_kmh / 3.6 if self.t.gear == "R" else 0.0
        return [
            [40.0, 30.0, -0.7, -0.7, 0.0, 1.0, "TSStatic", "s_gm_wall_straight_32m", 0.64, 0.77, 0.0],
            [350.0, 45.0, 0.1, -0.6, 0.75, 0.5, "TSStatic", "s_gm_ramp_8x4x3", -0.17, 0.98, 0.0],
            [180.0, 25.0, 0.0, 1.0, 0.0, 0.0, "BeamNGVehicle", "Gavril D-Series", 0.0, -1.0, reversing_speed],
        ]

    async def vehicle_controls(self) -> list[dict]:
        return [
            {"name": "tailgate", "title": "Toggle Tailgate", "axis": False},
            {"name": "hydraulics_jump_full", "title": "Lowrider: Hop All", "axis": False},
            {"name": "PTOAxis", "title": "Set Hydraulic PTO Control Output", "axis": True},
        ]

    async def use_control(self, name: str, phase: str, value: float | None = None) -> None:
        log.info("[mock] control %s %s %s", name, phase, "" if value is None else value)

    async def horn(self, on: bool) -> None:
        log.info("[mock] horn %s", "HONK" if on else "off")

    async def set_lights(self, headlights=None, left_signal=None, right_signal=None, hazard_signal=None) -> None:
        log.info("[mock] lights head=%s L=%s R=%s hazard=%s", headlights, left_signal, right_signal, hazard_signal)

    async def recover(self) -> None:
        log.info("[mock] recover")
        self.t.up = (0.0, 0.0, 1.0)
        self.t.speed_kmh = 0.0
        self.ai_mode = "disabled"
        self.generation += 1

    async def car_configs(self, model: str) -> list[dict]:
        return [{"key": "stock", "name": "Base (A)", "type": "Factory", "power": 200},
                {"key": "police", "name": "Police Package (A)", "type": "Police", "power": 223,
                 "desc": "Pushbar and heavy duty suspension"}]

    async def change_vehicle(self, model: str, config: str | None = None) -> None:
        log.info("[mock] change vehicle -> %s (%s)", model, config or "default")
        self.vehicle_model, self.vehicle_config = model, config
        self.t = Telemetry(pos=self.t.pos, gear="P")
        self.ai_mode = "disabled"
        self.generation += 1

    async def spawn_traffic(self, amount: int, police_ratio: float) -> None:
        log.info("[mock] spawn %d traffic cars (%.0f%% police)", amount, police_ratio * 100)
        for i in range(amount):
            p = self.t.pos
            self._traffic.append((f"traffic_{len(self._traffic)}", (p[0] + random.uniform(-200, 200), p[1] + random.uniform(-200, 200), p[2])))

    async def set_time_of_day(self, hour: float) -> None:
        log.info("[mock] time of day -> %02.0f:00", hour)

    async def set_gravity(self, gravity: float) -> None:
        log.info("[mock] gravity -> %s", gravity)
