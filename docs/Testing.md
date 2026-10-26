# Testing

## Neuro API engine
You'll need to use a neuro-api-compatible engine in place of Neuro to drive the integration:
* [Tony](https://github.com/Pasu4/neuro-api-tony) - a GUI where you pick the actions and fill in their parameters yourself. Best for trying one action at a time
* [Randy](https://github.com/VedalAI/neuro-sdk/tree/main/Randy) - picks actions at random
* [Gary](https://github.com/Govorunb/gary) / [Jippity](https://github.com/EnterpriseScratchDev/neuro-api-jippity) - an LLM picks the actions, closer to how Neuro plays

The bridge connects to `ws://localhost:8000` by default. If your engine listens somewhere else, pass `--neuro-url ws://host:port`
(or set `NEURO_SDK_WS_URL`).

## Testing in the game
1. Start BeamNG.drive with the MCP server turned on, load a level in **Freeroam** and spawn a car.
2. Start your engine, then run the bridge with verbose logging:
```bat
.venv\Scripts\python -m neuro_beamng -v
```
3. Every action, its result and every context message shows in the bridge's log and in the engine.

Set the scene before testing a driving feature, so the test shows what you meant to test:
* **Driving and cornering** - a quiet, bendy road. `engine` with `then_shift: drive`, then `follow_road` at a speed.
* **Overtaking** - a long straight with a car parked or driving slowly in her lane, and no junction or bend
  just ahead. `follow_road`, then `overtake` (or wait for the on-the-fly prompt as she slows for it).
* **Routes** - `list_destinations`, then `drive_to` one of them. The route shows on screen with GPS arrows.
* **Driving styles** - `driving_style` with a preset, then the same road again to compare.
* **Police chases** - spawn traffic (`spawn_traffic`) and get the police after her; she's asked to flee, keep
  driving or give up when the pursuit starts.
* **Pausing** - pause the game: every action except `pause_game`, `list_car_configs` and
  `list_destinations` is unregistered until it's unpaused.
* **Resets** - press R in the game: she's told the car was reset and what she needs to do before driving.
* **Streamer switches** - open `http://localhost:8002` and untick an action: it disappears from the engine's
  action list straight away.

If a test is interrupted, send `stop_car` so the control layer doesn't keep driving.

## Mock mode (developers only)
`--mock` swaps the game for a crude fake car. It only checks that the actions and messages reach the
engine and that bad input is rejected. Nothing actually drives, so it is no substitute for the game:
```bat
.venv\Scripts\python -m neuro_beamng --mock -v
```

## Vision (optional)
Start the vision server, then run the bridge with the camera view on:
```bat
tools\start_vision.bat
.venv\Scripts\python -m neuro_beamng -v --vision-interval 3
```
The camera description shows on her dashboard (`check_surroundings`). If the server isn't running, the
bridge logs one warning and retries quietly.
