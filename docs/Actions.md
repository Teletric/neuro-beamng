# Actions

| Action | What it does |
|---|---|
| `follow_road`, `turn_at_next_junction`, `set_target_speed` | Follow the road at a speed, choosing turns at junctions |
| `drive_to`, `list_destinations` | Drive a planned route to a place on the current map |
| `go_to` | Drive to a ramp, car, police car, suspect, road or direction |
| `flee` | Get away from the police, a car or her suspect, by road or off-road |
| `overtake` | Drive around whatever is in her lane |
| `driving_style` | Cornering, braking, acceleration, gap, lane and obstacle handling, or a preset |
| `push_through` | Stop braking for what she's stopped for and drive into it |
| `back_up` | Reverse a few metres, optionally steering, then carry on |
| `drive_manually` | Holds throttle, brake, steering and handbrake until her next input |
| `shift_gear` | Park, reverse, neutral, drive, up, down, or gear 1–8. Works on automatic, manual, sequential, DCT and electric gearboxes |
| `engine` | Starts or stops the engine (switches an electric car on or off), optionally shifting straight after (`then_shift`) |
| `stop_car` | Brakes to a stop, then pulls the handbrake |
| `use_vehicle_control` | The current car's own controls, re-read whenever the car changes: doors, hood and tailgate, truck lift axles, retarder, hydraulic PTO, lowrider hops and so on. Only registered when the car has any |
| `do_maneuver` | A preset stunt: full throttle, brake hard, turns, drifts, handbrake turns, burnout or donuts |
| `honk` | Short, long, angry or shave-and-a-haircut |
| `set_lights` | Headlights (off, on, high beams) and indicators (left, right, hazards) |
| `recover_car` | Puts the car back on its wheels on the nearest road, repaired |
| `change_car` | Swaps to any drivable model the game has (including mods), optionally with a `config` such as `police`. Config names match loosely |
| `list_car_configs` | Lists a model's configurations with type, power and description |
| `check_surroundings` | Reports the full dashboard |
| `spawn_traffic` | Spawns AI traffic |
| `pause_game` | Pauses or unpauses the game |
| `set_time_of_day` | Sets the hour |
| `drive`, `pursue_nearest_car`, `set_speed`, `set_driving_style` | BeamNG's AI autopilot. Only registered with `--allow-autopilot` |
| `set_gravity` | Moon, Mars, Jupiter and more. Only registered with `--allow-chaos` |

Neuro's input is validated before anything runs, and bad input returns `success: false` with a reason
she can act on. When something fails in the game instead, the result is `success: true` with an
explanation. This follows the spec's advice and stops a failing force from retrying in a loop.

## Streamer switches

The "admin" actions can be switched off: switching cars, looking up configs, spawning traffic, time of
day, gravity, pausing, and recover/teleport. A switched-off action is unregistered, so Neuro can't see
or use it.
* Flip them live on the switches page at `http://localhost:8002` (`--control-port`, 0 = off). The same
  server has a small API at `/api/actions`.
* Or set them at start with `--disable change_car,spawn_traffic`.
* Choices are saved to `bridge_settings.json`, so they survive restarts.
