"""BeamNG backend that uses the game's built-in MCP server (BeamNG.drive 0.40+).

Enable it in-game: Options -> "Enable MCP server". It listens on
http://127.0.0.1:29292/mcp and speaks JSON-RPC over plain HTTP POST. We install a
small Lua helper (bridge.lua) with the run_lua tool and call into it.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .backend import GameBackend, NearbyVehicle, Telemetry, describe_gears, pretty_problems, relative_direction
from . import vehicle_lua

log = logging.getLogger(__name__)

BRIDGE_LUA = (Path(__file__).parent / "bridge.lua").read_text(encoding="utf-8")


class GameError(RuntimeError):
    pass


def _lstr(s: str) -> str:
    """Lua long-string literal."""
    assert "]==]" not in s
    return f"[==[{s}]==]"


def _as_list(value) -> list:
    """Lua's jsonEncode writes an empty table as {} and arrays as lists."""
    if not value:
        return []
    return list(value.values()) if isinstance(value, dict) else list(value)


def _lnum(x: float) -> str:
    return repr(float(x))


def _fmt_gear(gear: Any) -> str:
    if isinstance(gear, str):
        return gear
    if isinstance(gear, (int, float)):
        g = int(gear)
        return "R" if g < 0 else "N" if g == 0 else str(g)
    return "?"


class McpBackend(GameBackend):
    def __init__(self, url: str = "http://127.0.0.1:29292/mcp", gearbox: str | None = "realistic") -> None:
        self.url = url
        self.gearbox = gearbox  # forced on every new car; None leaves the player's setting alone
        self._ids = itertools.count(1)
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="beamng-mcp")
        self._vid: int | None = None
        self._events: list[dict] = []  # game messages collected by poll(), drained by take_events()
        self._gearbox_applied = 0.0
        self._last = Telemetry()

    # --- transport ----------------------------------------------------------

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
        # Generous: the game can stall for seconds while it recovers or spawns a car.
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read() or b"{}")

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(self._exec, self._post, payload)
        if "error" in resp:
            raise GameError(resp["error"].get("message", str(resp["error"])))
        return resp.get("result")

    async def _tool(self, name: str, args: dict[str, Any]) -> str:
        res = await self._rpc("tools/call", {"name": name, "arguments": args})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("isError"):
            raise GameError(text)
        return text

    async def _lua(self, code: str) -> str:
        """Run GE Lua; reinstalls the helper if the game's Lua VM was reloaded."""
        try:
            return await self._tool("run_lua", {"code": code})
        except GameError as e:
            if "neuroBridge" not in str(e):
                raise
            log.info("Lua helper missing (game Lua reloaded?); reinstalling")
            await self._install()
            return await self._tool("run_lua", {"code": code})

    async def _install(self) -> None:
        log.debug("Installing Lua helper: %s", await self._tool("run_lua", {"code": BRIDGE_LUA}))

    async def _vlua(self, code: str) -> None:
        await self._lua(f"return neuroBridge.vlua({_lstr(code)})")

    # --- connection ---------------------------------------------------------

    async def connect(self) -> None:
        warned = False
        while True:
            try:
                info = await self._rpc("initialize", {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "neuro-beamng", "version": "0.1"},
                })
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if not warned:
                    log.warning(
                        "Can't reach BeamNG's MCP server at %s (%s). Start the game and turn on "
                        "Options -> 'Enable MCP server'. Retrying...", self.url, e)
                    warned = True
                await asyncio.sleep(3)
        log.info("Connected to %s", info.get("serverInfo", {}).get("name", "BeamNG MCP"))
        await self._install()
        # Wait until we're actually in a level with a car.
        warned = False
        while True:
            try:
                await self.poll()
                break
            except GameError:
                pass
            if not warned:
                log.warning("No player vehicle yet - load a level and spawn a car. Waiting...")
                warned = True
            await asyncio.sleep(3)
        log.info("Driving %s on %s", self.vehicle_model, self.level)

    async def close(self) -> None:
        self._exec.shutdown(wait=False)

    # --- telemetry ----------------------------------------------------------

    async def poll(self) -> Telemetry:
        d = json.loads(await self._lua("return neuroBridge.poll()"))
        if "error" in d:
            raise GameError(d["error"])

        if d["vid"] != self._vid:
            if self._vid is not None:
                # Car was swapped (by us, the player, or the game).
                self.generation += 1
                self.ai_mode = "disabled"
            self._vid = d["vid"]
        self.vehicle_model = d.get("model") or self.vehicle_model
        self.vehicle_config = d.get("config") or self.vehicle_config
        self.level = d.get("level") or self.level

        self._events.extend(_as_list(d.get("events")))
        if (d.get("pilot") or {}).get("event"):  # one-off, so queue it with the game messages
            self._events.append({"text": d["pilot"]["event"], "category": "pilot", "source": "pilot"})
        del self._events[:-50]
        speed = d.get("speed")
        if speed is None:
            speed = d.get("wheelspeed") or 0.0
        self._last = Telemetry(
            speed_kmh=abs(float(speed)) * 3.6,
            gear=_fmt_gear(d.get("gear")),
            rpm=float(d.get("rpm") or 0.0),
            damage=float(d.get("damage") or 0.0),
            fuel=d.get("fuel"),
            engine_on=None if d.get("engineRunning") is None else bool(d["engineRunning"]),
            electric=bool(d.get("electric")),
            drive_left=bool(d.get("driveLeft")),
            gearbox_mode=d.get("gearboxMode"),
            backwards=(d.get("fwdSpeed") or 0.0) < -0.5,
            problems=pretty_problems(_as_list(d.get("problems"))),
            police=d.get("police") or None,
            pilot=d.get("pilot") or None,
            gears=describe_gears(d.get("gearboxType"), d.get("autoModes"), d.get("maxGear")),
            paused=bool(d.get("paused")),
            pos=tuple(d["pos"]),
            dir=tuple(d["dir"]),
            up=tuple(d["up"]),
        )
        await self._enforce_gearbox(self._last)
        return self._last

    async def _enforce_gearbox(self, t: Telemetry) -> None:
        # The AI flips to arcade while driving and restores afterwards, so only check while it's off.
        # Re-applying also covers fresh cars and the game's own settings overriding ours.
        if not self.gearbox or self.ai_mode != "disabled" or t.gearbox_mode in (None, self.gearbox):
            return
        now = asyncio.get_running_loop().time()
        if now - self._gearbox_applied > 2:
            self._gearbox_applied = now
            log.info("Setting gearbox to %s (was %s)", self.gearbox, t.gearbox_mode)
            await self._vlua(vehicle_lua.gearbox_mode(self.gearbox))

    async def nearby_vehicles(self, radius_m: float = 300.0) -> list[NearbyVehicle]:
        me = await self.poll()
        raw = json.loads(await self._lua(f"return neuroBridge.nearby({_lnum(radius_m)})"))
        if isinstance(raw, dict):  # empty Lua table may encode as {}
            raw = list(raw.values())
        out = []
        for v in raw:
            pos = tuple(v["pos"])
            dist = sum((a - b) ** 2 for a, b in zip(me.pos, pos)) ** 0.5
            out.append(NearbyVehicle(str(v["vid"]), v.get("model") or "car", dist, relative_direction(me, pos)))
        return sorted(out, key=lambda n: n.distance_m)

    async def road_ahead(self, max_dist: float = 160.0) -> dict | None:
        await self.poll()  # keep our pose fresh for describe_road
        return json.loads(await self._lua(f"return neuroBridge.roadAhead({_lnum(max_dist)})"))

    def take_events(self) -> list[dict]:
        events, self._events = self._events, []
        return events

    async def set_paused(self, paused: bool) -> None:
        await self._lua(f"return neuroBridge.setPause({'true' if paused else 'false'})")

    async def pilot(self, **command) -> None:
        await self._lua(f"return neuroBridge.pilotCommand({_lstr(json.dumps(command))})")
        if command.get("mode") in ("road", "target"):
            self.ai_mode = "disabled"

    async def lidar(self, max_range: float = 150.0, beams: int = 36, height: float = 0.8) -> list | None:
        raw = json.loads(await self._lua(f"return neuroBridge.lidar({_lnum(max_range)}, {int(beams)}, {_lnum(height)})"))
        return list(raw.values()) if isinstance(raw, dict) else raw

    async def last_impact(self) -> dict | None:
        raw = json.loads(await self._lua("return neuroBridge.lastImpact()"))
        if not isinstance(raw, dict):
            return None
        raw["vehicles"] = _as_list(raw.get("vehicles"))
        return raw

    async def destinations(self) -> dict | None:
        raw = json.loads(await self._lua("return neuroBridge.destinations()"))
        if not isinstance(raw, dict):
            return None
        raw["places"] = _as_list(raw.get("places"))
        for place in raw["places"]:
            place["pos"] = _as_list(place.get("pos"))
        raw["waypoint"] = _as_list(raw.get("waypoint")) or None
        return raw

    async def ground_scan(self, max_range: float = 50.0, bearings: int = 16, step: float = 2.0) -> dict | None:
        raw = json.loads(await self._lua(
            f"return neuroBridge.groundScan({_lnum(max_range)}, {int(bearings)}, {_lnum(step)})"))
        return None if not isinstance(raw, dict) or raw.get("error") else raw

    async def car_models(self) -> list[dict]:
        raw = json.loads(await self._lua("return neuroBridge.carModels()"))
        return list(raw.values()) if isinstance(raw, dict) else raw

    async def car_configs(self, model: str) -> list[dict]:
        raw = json.loads(await self._lua(f"return neuroBridge.carConfigs({_lstr(model)})"))
        return list(raw.values()) if isinstance(raw, dict) else raw

    async def vehicle_controls(self) -> list[dict]:
        raw = json.loads(await self._lua("return neuroBridge.vehicleControls()"))
        return list(raw.values()) if isinstance(raw, dict) else raw

    async def use_control(self, name: str, phase: str, value: float | None = None) -> None:
        await self._lua(f"return neuroBridge.useControl({_lstr(name)}, '{phase}', "
                        f"{'nil' if value is None else _lnum(value)})")

    async def capture_frame(self, scale: float = 0.4) -> str | None:
        await self._lua(f"return neuroBridge.captureStart({_lnum(scale)})")
        for _ in range(20):  # the game writes the file a few frames later
            await asyncio.sleep(0.15)
            data = await self._lua("return neuroBridge.captureFetch()")
            if data:
                return data
        return None

    async def set_time_scale(self, scale: float) -> None:
        await self._tool("set_simulation_speed", {"scale": scale})

    # --- driving ------------------------------------------------------------

    async def set_ai_mode(self, mode: str, target_vid: str | None = None) -> None:
        if target_vid is not None:
            await self._vlua(f"if ai then ai.setState({{mode = '{mode}', targetObjectID = {int(target_vid)}}}) end")
        else:
            await self._vlua(f"if ai then ai.setMode('{mode}') end")
        self.ai_mode = mode

    async def set_ai_speed(self, kmh: float, exact: bool) -> None:
        mode = "set" if exact else "limit"
        await self._vlua(f"if ai then ai.setSpeedMode('{mode}') ai.setSpeed({_lnum(kmh / 3.6)}) end")
        self.ai_speed_kmh = kmh

    async def set_ai_style(self, aggression: float | None, in_lane: bool | None) -> None:
        code = []
        if aggression is not None:
            code.append(f"ai.setAggression({_lnum(aggression)})")
        if in_lane is not None:
            code.append(f"ai.driveInLane('{'on' if in_lane else 'off'}')")
        if code:
            await self._vlua("if ai then " + " ".join(code) + " end")

    async def control(self, throttle=0.0, brake=0.0, steering=0.0, parkingbrake=0.0) -> None:
        events = dict(throttle=throttle, brake=brake, steering=steering, parkingbrake=parkingbrake)
        await self._vlua(" ".join(f"input.event('{k}', {_lnum(v)}, 2)" for k, v in events.items()))

    async def shift(self, target: str) -> None:
        await self._vlua(vehicle_lua.shift(target))

    async def set_engine(self, on: bool) -> None:
        if not on:
            await self._vlua(vehicle_lua.IGNITION_OFF)
            return
        await self._vlua(vehicle_lua.CRANK)
        # Crank until it runs (an electric car is on at once; an engine catches within a couple of
        # seconds), at most 2.5 s. Each poll asks the car, and its answer arrives by the next one.
        loop = asyncio.get_running_loop()
        end = loop.time() + 2.5
        await self.poll()
        while loop.time() < end:
            await asyncio.sleep(0.25)
            if (await self.poll()).engine_on:
                break
        await self._vlua(vehicle_lua.STOP_CRANKING)

    async def horn(self, on: bool) -> None:
        await self._vlua(f"electrics.horn({'true' if on else 'false'})")

    async def set_lights(self, headlights=None, left_signal=None, right_signal=None, hazard_signal=None) -> None:
        code = []
        if headlights is not None:
            code.append(f"electrics.setLightsState({int(headlights)})")
        if hazard_signal is not None:
            code.append(f"electrics.set_warn_signal({'true' if hazard_signal else 'false'})")
        if not hazard_signal:
            if left_signal is not None:
                code.append(f"electrics.set_left_signal({'true' if left_signal else 'false'})")
            if right_signal is not None:
                code.append(f"electrics.set_right_signal({'true' if right_signal else 'false'})")
        if code:
            await self._vlua(" ".join(code))

    async def recover(self) -> None:
        await self._lua("return neuroBridge.recover()")
        self.ai_mode = "disabled"
        self.generation += 1

    async def change_vehicle(self, model: str, config: str | None = None) -> None:
        cfg = _lstr(config) if config else "nil"
        await self._lua(f"return neuroBridge.changeVehicle({_lstr(model)}, {cfg})")
        self.vehicle_model, self.vehicle_config = model, config
        self.ai_mode = "disabled"
        self.generation += 1

    # --- world --------------------------------------------------------------

    async def spawn_traffic(self, amount: int, police_ratio: float = 0.0) -> None:
        await self._lua(f"return neuroBridge.spawnTraffic({int(amount)})")

    async def set_time_of_day(self, hour: float) -> None:
        # 0 = midnight, 0.5 = noon
        await self._lua(f"return neuroBridge.setTimeOfDay({_lnum(hour / 24)})")

    async def set_gravity(self, gravity: float) -> None:
        await self._lua(f"return neuroBridge.setGravity({_lnum(gravity)})")
