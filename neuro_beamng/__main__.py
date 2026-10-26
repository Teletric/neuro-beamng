"""Run with: python -m neuro_beamng --help"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from .actions import Driver
from .backend import GameBackend
from .director import Director, DirectorConfig
from .control import TOGGLEABLE, ActionSwitches
from .neuro_api import NeuroClient
from .vision import Vision

GAME_NAME = "BeamNG.drive"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="neuro_beamng", description="Let Neuro-sama play BeamNG.drive.")
    p.add_argument("--neuro-url", default=os.environ.get("NEURO_SDK_WS_URL", "ws://localhost:8000"),
                   help="Neuro API websocket (default: $NEURO_SDK_WS_URL or ws://localhost:8000)")
    p.add_argument("--mock", action="store_true", help="use a fake game instead of BeamNG (for testing)")
    p.add_argument("--mcp-url", default="http://127.0.0.1:29292/mcp", help="BeamNG.drive's MCP server URL")
    p.add_argument("--gearbox", choices=["realistic", "arcade", "keep"], default="realistic",
                   help="gearbox mode to force on Neuro's car (keep = leave the game setting alone)")

    d = p.add_argument_group("behaviour")
    d.add_argument("--allow-autopilot", action="store_true",
                   help="also give Neuro BeamNG's AI autopilot (drive, pursue, set_speed, set_driving_style)")
    d.add_argument("--time-scale", type=float, default=1.0,
                   help="game speed while the bridge runs, e.g. 0.5 for slow motion to offset Neuro's reaction time")
    d.add_argument("--allow-chaos", action="store_true", help="register chaotic actions like set_gravity")
    d.add_argument("--idle-force", type=float, default=30.0, help="seconds parked before forcing an action (0=off)")
    d.add_argument("--status-interval", type=float, default=30.0, help="seconds between silent status updates (0=off)")
    d.add_argument("--lidar-range", type=float, default=150.0, help="simulated lidar range in metres (0 = off)")
    d.add_argument("--lidar-beams", type=int, default=36, help="lidar beams around the car (36 = every 10 degrees)")
    d.add_argument("--vision-url", default="http://127.0.0.1:11435/v1",
                   help="OpenAI-compatible vision model server (tools\\start_vision.bat)")
    d.add_argument("--vision-interval", type=float, default=0.0,
                   help="seconds between camera descriptions, e.g. 3 (0=off); needs tools\\start_vision.bat running")
    d.add_argument("--disable", default="", metavar="ACTIONS",
                   help="comma-separated actions to switch off, e.g. change_car,spawn_traffic (saved)")
    d.add_argument("--control-port", type=int, default=8002,
                   help="port for the action on/off switches page (0 = off)")
    d.add_argument("--settings", default=str(Path(__file__).resolve().parent.parent / "bridge_settings.json"),
                   help="where switched-off actions are remembered")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def make_backend(args: argparse.Namespace) -> GameBackend:
    if args.mock:
        from .mock_backend import MockBackend

        return MockBackend()
    from .mcp_backend import McpBackend

    return McpBackend(args.mcp_url, gearbox=None if args.gearbox == "keep" else args.gearbox)


async def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.INFO)

    game = make_backend(args)
    await game.connect()
    if not args.allow_autopilot:
        await game.set_ai_mode("disabled")  # make sure BeamNG's AI isn't driving alongside her
    if args.time_scale != 1.0:
        await game.set_time_scale(args.time_scale)
    driver = Driver(game, allow_chaos=args.allow_chaos, allow_autopilot=args.allow_autopilot)
    await driver.load_car_catalog()
    driver.lidar_range, driver.lidar_beams = args.lidar_range, args.lidar_beams
    tasks = []
    if args.vision_interval > 0 and not args.mock:
        driver.vision = Vision(game, args.vision_url, args.vision_interval)
        tasks.append(driver.vision.run())
    director: Director | None = None

    neuro = NeuroClient(args.neuro_url, GAME_NAME, [], driver.handle,
                        on_connected=lambda: director.announce())
    switches = ActionSwitches(driver, neuro, Path(args.settings))
    for name in filter(None, (n.strip() for n in args.disable.split(","))):
        if name in TOGGLEABLE:
            await switches.set_enabled(name, False)  # not connected yet: just records and saves it
        else:
            logging.getLogger(__name__).warning("--disable: %s can't be switched off (options: %s)",
                                                name, ", ".join(TOGGLEABLE))
    neuro.set_actions(driver.enabled_actions)
    if args.control_port:
        switches.serve(args.control_port)
    director = Director(neuro, driver, DirectorConfig(idle_force_after=args.idle_force,
                                                      status_interval=args.status_interval))
    try:
        await asyncio.gather(neuro.run_forever(), director.run(), *tasks)
    finally:
        if args.time_scale != 1.0:
            try:
                await game.set_time_scale(1.0)
            except Exception:
                pass
        await game.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
