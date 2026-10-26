"""Watches the game and tells Neuro about things worth reacting to."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .actions import Driver
from .lidar import obstacle_name
from .backend import Telemetry
from .neuro_api import NeuroClient

log = logging.getLogger(__name__)

MESSAGE_REPEAT_SECONDS = 10.0  # don't repeat the same game message more often than this
QUIET_MESSAGE_CATEGORIES = ("vehicle.shiftermode", "vehicle.ignition")

PILOT_ACTIONS = ["turn_at_next_junction", "set_target_speed", "driving_style", "follow_road", "drive_to", "go_to",
                 "flee", "overtake", "push_through",
                 "back_up",
                 "stop_car",
                 "shift_gear",
                 "engine", "drive_manually", "do_maneuver", "use_vehicle_control", "honk", "set_lights", "recover_car"]

DRIVING_ACTIONS = ["drive_manually", "follow_road", "drive_to", "go_to", "flee", "overtake", "back_up", "shift_gear", "engine", "stop_car", "do_maneuver",
                   "use_vehicle_control", "honk", "set_lights", "recover_car"]


@dataclass
class DirectorConfig:
    poll_interval: float = 0.5
    status_interval: float = 30.0  # silent status update; 0 disables
    idle_force_after: float = 30.0  # force Neuro to act when parked this long; 0 disables
    drive_loop_min_kmh: float = 3.0  # moving faster than this with no input held -> ask for the next input
    drive_force_gap: float = 1.0  # seconds between driving-loop forces
    hold_checkin: float = 3.0  # while she holds controls, ask her to adjust this often
    pilot_checkin: float = 20.0  # while the control layer drives, check in this often
    junction_ask_m: float = 70.0  # ask which way to go when a junction is this close
    pilot_problem_gap: float = 10.0  # don't repeat "you can't drive" prompts more often than this
    crash_speed_drop_kmh: float = 25.0  # per poll interval
    crash_damage_jump: float = 400.0
    stuck_after: float = 15.0


class Director:
    def __init__(self, neuro: NeuroClient, driver: Driver, cfg: DirectorConfig | None = None) -> None:
        self.neuro = neuro
        self.driver = driver
        self.game = driver.game
        self.cfg = cfg or DirectorConfig()
        self._prev: Telemetry | None = None
        self._generation = self.game.generation
        self._last_status = time.monotonic()
        self._last_crash = 0.0
        self._flipped_since: float | None = None
        self._flip_reported = False
        self._slow_since: float | None = None
        self._stuck_reported = False
        self._idle_since: float | None = None
        self._last_idle_force = 0.0
        self._top_speed = 0.0
        self._last_drive_force = 0.0
        self._destinations_level: str | None = None  # map the drive_to action was built for
        self._controls_generation: int | None = None  # car the use_vehicle_control action was built for
        self._recent_messages: dict[str, float] = {}  # text -> when it was last passed on
        self._known_problems: set[str] = set()
        self._problems_generation: int | None = None  # car the known problems belong to
        self._car_changed_at = 0.0  # when the car was last spawned, swapped or recovered
        self._asked_junction = None  # junction she was last asked about
        self._asked_obstacle: tuple[object, float] | None = None  # (car id or place, when) - asked to overtake
        self._paused_since = 0.0
        self._wanted = 0  # police pursuit level she was last asked about
        self._last_pilot_problem = 0.0

    async def announce(self) -> None:
        """Sent every time Neuro (re)connects."""
        status = await self.driver.status_markdown()
        await self.neuro.context(
            "You are driving a car in BeamNG.drive, a soft-body physics driving game where crashes are "
            "realistic and spectacular. "
            + ("You can give orders to the car's autopilot or take the wheel yourself."
               if self.driver.allow_autopilot else
               "You are the driver: start the engine, pick a gear, then steer and work the pedals yourself. "
               "While the car is moving you'll keep being asked for your next input.")
            + " Other cars crumple and get shoved around, but trees, walls, rocks and buildings are solid "
              "scenery: they never break or move, so drive around them."
            + f"\n{status}",
            silent=False,
        )

    async def run(self) -> None:
        while True:
            try:
                t = await self.game.poll()
            except Exception as e:
                log.warning("Telemetry poll failed: %s", e)
                await asyncio.sleep(2)
                continue
            try:
                await self._sync_vehicle_controls()
                await self._sync_destinations()
                await self._check(t)
            except Exception:
                log.exception("Director check failed")
            self._prev = t
            await asyncio.sleep(self.cfg.poll_interval)

    async def _sync_destinations(self) -> None:
        """Each map has its own places, so rebuild drive_to whenever the map changes."""
        if self._destinations_level == self.game.level:
            return
        self._destinations_level = self.game.level
        action = await self.driver.refresh_destinations()
        if action:
            if self.driver.available(action.name):  # while paused it's registered on unpause instead
                await self.neuro.register([action])
        else:
            await self.neuro.unregister(["drive_to"])

    async def _sync_vehicle_controls(self) -> None:
        """Each car has its own controls, so rebuild use_vehicle_control whenever the car changes."""
        if self._controls_generation == self.game.generation:
            return
        self._controls_generation = self.game.generation
        action = await self.driver.refresh_vehicle_controls()
        if action:
            if self.driver.available(action.name):  # while paused it's registered on unpause instead
                await self.neuro.register([action])
        else:
            await self.neuro.unregister(["use_vehicle_control"])

    async def _relay_game_messages(self, t: Telemetry, now: float) -> None:
        """Pass on what the game shows on screen, and newly appearing faults with the car."""
        for msg in self.game.take_events():
            if msg.get("category") == "reset":  # reset by someone else (her own recover_car is filtered out)
                if now - self._car_changed_at < 3.0:
                    continue
                self._car_changed_at = self._last_crash = now  # the sudden stop isn't a crash
                moved = msg.get("moved")
                where = f", {moved:.0f} m from where it was" if moved and moved > 15 else ""
                await self.neuro.context("Your car was just reset: repaired and set back on its wheels" + where
                                         + "." + await self.driver._car_state_hint(), silent=False)
                continue
            text = str(msg.get("text", "")).strip()
            if not text or now - self._recent_messages.get(text, -1e9) < MESSAGE_REPEAT_SECONDS:
                continue
            self._recent_messages[text] = now
            category = str(msg.get("category", ""))
            # Background: messages caused by the bridge itself (gearbox mode, ignition), and the burst of
            # "Drivemode: ..." style announcements a car makes right after it is spawned or recovered.
            quiet = (any(category.startswith(c) for c in QUIET_MESSAGE_CATEGORIES)
                     or now - self._car_changed_at < 3.0)
            await self.neuro.context(f"Game message: {text}", silent=quiet)

        problems = set(t.problems)
        if self.game.generation != self._problems_generation:
            self._problems_generation = self.game.generation
            self._known_problems = problems  # new car or repaired: don't announce what it starts with
            return
        # Compare kinds of problem ("body damage"), not details: more dented corners isn't news.
        kind = lambda p: p.split(" (")[0]  # noqa: E731
        known_kinds = {kind(p) for p in self._known_problems}
        new = [p for p in sorted(problems) if kind(p) not in known_kinds
               and now - self._recent_messages.get(p, -1e9) >= MESSAGE_REPEAT_SECONDS]
        self._known_problems = problems
        if new:
            await self.neuro.context("New problem with your car: " + "; ".join(new), silent=False)

    def _obstacle_ahead(self, t: Telemetry, p: dict, now: float) -> str | None:
        """The pilot has just started slowing for something in her lane: ask while she's still rolling, so
        she can overtake on the fly instead of only once she's stopped behind it."""
        d = p.get("obstacle")
        status = p.get("status") or ""
        if d is None or p.get("passing") or p.get("fleeing") or not status.startswith(("obstacle", "following")):
            return None
        if t.speed_kmh < 8 or d > max(25.0, t.speed_kmh / 3.6 * 4 + 7):  # about 4 s before it'd have to stop
            return None
        key = p.get("obstacleVid") or p.get("obstacleWhat")  # a car by its id, a thing by what it is
        if self._asked_obstacle and self._asked_obstacle[0] == key and now - self._asked_obstacle[1] < 20:
            return None
        self._asked_obstacle = (key, now)
        what = obstacle_name(p.get("obstacleWhat"))
        speed = p.get("obstacleSpeed")
        moving = f", doing {speed:.0f} km/h" if speed and speed > 3 else (", stopped" if p.get("obstacleVid") else "")
        return (f"{what[0].upper() + what[1:]} ahead in your way, {d:.0f} m{moving} - you're slowing for it. Overtake "
                "it now (overtake), stay behind it (set_target_speed with your speed), or push_through?")

    async def _pilot_decisions(self, t: Telemetry, now: float) -> None:
        """Ask her only when a driver would decide something: a junction coming up, a problem, or a check-in."""
        p, cfg = t.pilot or {}, self.cfg
        query, priority = None, "low"
        j = p.get("junction")
        if (j and j.get("branches", 1) > 1 and j.get("dist", 1e9) < cfg.junction_ask_m and not p.get("turn")
                and not p.get("fleeing") and j.get("name") != self._asked_junction):
            self._asked_junction = j.get("name")
            query, priority = (f"Junction coming up in {j['dist']:.0f} m. Go left, right or straight? "
                               "(turn_at_next_junction; straight is the default)"), "medium"
        elif (ahead := self._obstacle_ahead(t, p, now)):
            query, priority = ahead, "medium"
        elif ((p.get("warn") or p.get("stuck") or p.get("blocked") is not None)
              and now - self._last_pilot_problem > cfg.pilot_problem_gap):
            self._last_pilot_problem = now
            if p.get("warn"):
                query = f"You can't drive right now: {p['warn']}. Sort it out."
            elif p.get("blocked") is not None:
                what = obstacle_name(p.get("obstacleWhat"))
                query = (f"You've stopped for {what}, {p['blocked']:.0f} m in front of your bumper. Your call: go "
                         "around it (overtake), push_through it, back_up, or wait?")
            else:
                query = "You're stuck: throttle but no movement. Sort it out."
            priority = "medium"
        elif now - self._last_drive_force > cfg.pilot_checkin:
            query = (f"You're driving at {t.speed_kmh:.0f} km/h ({p.get('status') or 'driving'}). Anything to "
                     "change - speed, direction, a target, a stunt? Or carry on (set_target_speed with the same speed).")
        if query is None:
            return
        state = await self.driver.status_markdown(t)
        names = {a.name for a in self.driver.enabled_actions}
        if await self.neuro.force(query, [n for n in PILOT_ACTIONS if n in names], state=state,
                                  priority=priority, ephemeral=True):
            self._last_drive_force = now
            self._last_status = now

    async def _police_decision(self, t: Telemetry, now: float) -> None:
        """A police pursuit starting is a driver's decision: run (by road or across country), or give up."""
        police = t.police or {}
        mode = police.get("mode") or 0
        if mode <= 0 or police.get("role") == "police":
            self._wanted = min(mode, 0)
            return
        if self._wanted > 0 or self.neuro.force_pending:
            return  # already asked about this pursuit (or another prompt is open; try again next poll)
        names = {a.name for a in self.driver.enabled_actions}
        options = [n for n in ("flee", "drive_to", "go_to", "follow_road", "driving_style", "stop_car") if n in names]
        near = police.get("nearest")
        where = f" (nearest police car {near['dist']:.0f} m away)" if near else ""
        if await self.neuro.force(
            f"The police are chasing you{where}! Run - flee by road, or off_road straight across open ground - "
            "keep driving, or give up (stop_car)?",
            options, state=await self.driver.status_markdown(t), priority="high", ephemeral=True,
        ):
            self._wanted = mode
            self._last_drive_force = now

    async def _set_paused(self, paused: bool) -> None:
        """Paused: unregister everything that needs the game running, so she can't try to drive a frozen car.
        Unpaused: register it all again."""
        names = [a.name for a in self.driver.enabled_actions]
        self.driver.paused = paused
        if paused:
            await self.neuro.unregister([n for n in names if not self.driver.available(n)])
            note = ("The game is now PAUSED: everything is frozen and your driving actions are gone until it's "
                    "unpaused." + (" You can unpause it with pause_game." if self.driver.available("pause_game")
                                   else " Just wait."))
        else:
            await self.neuro.register([a for a in self.driver.enabled_actions if a.name not in names])
            note = "The game is unpaused: things are moving again and your driving actions are back."
        await self.neuro.context(note, silent=False)

    async def _check(self, t: Telemetry) -> None:
        now = time.monotonic()
        prev = self._prev
        if self.game.generation != self._generation:
            self._generation = self.game.generation
            self._car_changed_at = now
            prev = None  # car was reset/replaced; don't compare against the old one
        cfg = self.cfg

        if bool(t.paused) != self.driver.paused:
            await self._set_paused(bool(t.paused))
            self._paused_since = now
        if t.paused:
            # Nothing can move, so she gets nothing about the car: no game messages (dropped, not saved up
            # for later), no status, no driving prompts. If she's allowed to unpause, nudge her now and then.
            self.game.take_events()
            can_unpause = "pause_game" not in self.driver.disabled
            if (can_unpause and cfg.idle_force_after and now - self._paused_since > cfg.idle_force_after
                    and now - self._last_idle_force > cfg.idle_force_after and not self.neuro.force_pending):
                if await self.neuro.force("The game is paused. Unpause it when you're ready (pause_game), or wait.",
                                          ["pause_game"], ephemeral=True):
                    self._last_idle_force = now
            return

        await self._relay_game_messages(t, now)
        await self._police_decision(t, now)

        # Crash: sudden deceleration or a big jump in damage.
        if prev is not None and now - self._last_crash > 5:
            drop = prev.speed_kmh - t.speed_kmh
            dmg = t.damage - prev.damage
            if drop > cfg.crash_speed_drop_kmh or dmg > cfg.crash_damage_jump:
                self._last_crash = now
                severity = "Huge crash" if drop > 60 or dmg > 5000 else "Crash"
                what = await self.driver.describe_impact(prev.speed_kmh)
                await self.neuro.context(
                    f"{severity}! {what}."
                    + (f" The car now has {t.broken_parts} damaged parts." if t.broken_parts else ""),
                    silent=False,
                )

        # Flipped over for more than 2 seconds.
        if t.tipped_over:
            self._flipped_since = self._flipped_since or now
            if not self._flip_reported and now - self._flipped_since > 2:
                self._flip_reported = True
                where = "upside down" if t.upside_down else "on its side"
                await self.neuro.context(f"Your car is {where}! Use recover_car to get back on the road.", silent=False)
        else:
            self._flipped_since, self._flip_reported = None, False

        # Autopilot on but not moving.
        driving = self.game.ai_mode not in ("disabled", "stopping")
        if driving and t.speed_kmh < 2:
            self._slow_since = self._slow_since or now
            if not self._stuck_reported and now - self._slow_since > cfg.stuck_after:
                self._stuck_reported = True
                await self.neuro.context(
                    "The autopilot seems stuck - the car hasn't moved in a while. Maybe recover_car or try a maneuver.",
                    silent=False,
                )
        else:
            self._slow_since, self._stuck_reported = None, False

        # New top speed (silent - just flavor for her memory).
        if t.speed_kmh > self._top_speed + 20 and t.speed_kmh > 100:
            self._top_speed = t.speed_kmh
            await self.neuro.context(f"New top speed: {t.speed_kmh:.0f} km/h.", silent=True)

        # Manual driving loop: the car is rolling and nothing is held, so she needs to decide again.
        for notice in self.driver.take_notices():
            await self.neuro.context(notice, silent=False)

        # Driver-level control: the control layer drives, so she's only asked when a decision is due.
        piloting = bool(t.pilot and t.pilot.get("mode") not in (None, "off"))
        if piloting and not self.neuro.force_pending:
            await self._pilot_decisions(t, now)
        driving = driving or piloting

        # Manual driving loop. Coasting: nothing held but the car is rolling. Holding: her controls stay set
        # until she changes them, so check in every few seconds to let her adjust.
        coasting = not driving and not self.driver.input_active and t.speed_kmh > cfg.drive_loop_min_kmh
        holding = (not driving and self.driver.holding
                   and now - max(self.driver.held_since, self._last_drive_force) >= cfg.hold_checkin)
        if (coasting or holding) and now - self._last_drive_force > cfg.drive_force_gap and not self.neuro.force_pending:
            state = await self.driver.status_markdown(t)
            situation = (f"Speed {t.speed_kmh:.0f} km/h. You're holding: {self.driver.last_input}."
                         if holding else
                         f"Speed {t.speed_kmh:.0f} km/h, your hands and feet are OFF the controls, the car is coasting.")
            if await self.neuro.force(
                f"{situation} Look at where the road goes and the suggested speed, then set your controls: steer "
                "toward the road, brake if you're too fast, throttle if you're too slow, or keep them as they are "
                "by sending the same input again.",
                [n for n in DRIVING_ACTIONS if n in {a.name for a in self.driver.enabled_actions}],
                state=state,
                priority="medium",
                ephemeral=True,
            ):
                self._last_drive_force = now
                self._last_status = now  # the force carried the status already

        if cfg.status_interval and now - self._last_status > cfg.status_interval:
            self._last_status = now
            await self.neuro.context("Status update:\n" + await self.driver.status_markdown(t), silent=True)

        # Parked with nothing happening: make her choose something.
        if not driving and not self.driver.input_active and t.speed_kmh < 1:
            self._idle_since = self._idle_since or now
            if (
                cfg.idle_force_after
                and now - self._idle_since > cfg.idle_force_after
                and now - self._last_idle_force > cfg.idle_force_after
            ):
                state = await self.driver.status_markdown(t)
                if await self.neuro.force(
                    "Your car is just sitting there. Decide what to do next!",
                    [a.name for a in self.driver.enabled_actions if a.name != "check_surroundings"],
                    state=state,
                    ephemeral=True,
                ):
                    self._last_idle_force = now
        else:
            self._idle_since = None
