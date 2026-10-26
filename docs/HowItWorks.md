# How it works

The bridge talks to Neuro over the SDK websocket and to the game through BeamNG.drive's **built-in MCP
server**, added in 0.40. It doesn't need a game mod.

```
Neuro  ⇄  websocket (Neuro API)  ⇄  neuro_beamng  ⇄  HTTP (127.0.0.1:29292/mcp)  ⇄  BeamNG.drive
```

On connect, the bridge installs a small Lua helper (`neuro_beamng/bridge.lua`) through the MCP
`run_lua` tool and calls into it. The helper reinstalls itself automatically if the game reloads
its Lua.

## Neuro is the driver

She turns the ignition on and off, picks gears, and decides where to go, how fast and how. BeamNG's own
AI autopilot is switched off at startup and isn't offered to her unless you pass `--allow-autopilot`.

No AI can steer well when each decision takes seconds, so the bridge is layered like a self-driving
car: Neuro makes the driver's decisions, and a control layer in the bridge's game-side Lua carries
them out every frame. The full list of her actions is in [Actions](Actions.md).

### Her decisions

* `follow_road` at a speed, with `turn_at_next_junction` left, right or straight.
* `drive_to` a place on the map: the game's own points of interest (named areas, gas and charging
  stations, drift spots, missions by name), the nearest gas or charging station, or the waypoint set
  on the big map. The game's route planner picks the roads, the route shows on screen with GPS arrows,
  the pilot takes every junction on it and re-plans if knocked off, and she stops on arrival.
  `list_destinations` lists the places with their distances. The list is rebuilt for each map.
* `go_to` the nearest ramp, the nearest car, the nearest police car, her police suspect, the nearest
  road, or a direction, across open ground if needed. `on_arrival: drive_through` rams a car or jumps a
  ramp.
* `flee` from the police, a car or her suspect: by road (taking the way that leads away at every
  junction) or off-road (straight across country, away from them). She always swerves around cars in
  the way while fleeing.
* `overtake` whatever is in her lane: a stopped or slow car, a jam, a crash. With `side: auto` (the
  default) the pilot compares lines across the road (her lane, the middle, the other lane, both
  shoulders) as car-wide corridors ahead and takes the clear one closest to where she is.
  `other_lane` or `shoulder` force one. It leaves room for both cars' widths plus about a metre,
  backs up a little first if it's too close to swing out, and moves back into her lane once past.
* `driving_style`: how hard she corners (gentle to on the limit), brakes (gentle to late) and
  accelerates; how far back she stops behind things; where on the road she drives (her own lane, the
  middle, the other lane, the shoulder, or the racing line that cuts corners); and whether she stops
  for obstacles, swerves around them, or rams them. Presets: cautious, normal, sporty, reckless. It
  applies to every bend and stop until she changes it.
* `push_through` whatever she's stopped for (shove a car, smash a fence, plough through a bush, ram a
  police car): the pilot stops braking for that one thing until she's past it. Trees, walls and rocks
  don't move.
* `back_up` out of a tight spot (into reverse, back a few metres, optionally steering, then back to
  what she was doing).
* `set_target_speed` and `stop_car`.

Lanes follow each map's road rules: on maps that drive on the left (Automation Test Track among the
stock maps) her own lane, the other lane and the shoulder are mirrored, and her dashboard says so.

### The control layer

* **Steering**: pure pursuit along the road path, resampled every 2 m so it aims at exactly the
  look-ahead distance. When overtaking, the path starts beside the car in the passing line, so it
  swings out at once.
* **Speed**: the path ahead is resampled every 3 m, each change of direction gives a corner radius,
  and the car brakes early enough to reach every corner at a speed it can hold. It follows slower cars
  at their speed and stops behind stopped ones.
* **Obstacles**: checked from the front of the car's bounding box, along the middle of the car and
  along both front corners following the planned path, sized from the car's real width. Plants are
  judged by their trunk or core, not their leaves, so it pushes through the foliage of a bush but not
  into a tree.
* **Learning the car**: from how much the car actually turns, it works out its real steering lock and,
  when the front slides wide (understeer), eases off and lowers its cornering speed for that car.

The control layer never starts the engine. Apart from `back_up` (and `overtake` backing up when it's
too close), it doesn't shift gears either. If the car can't drive, it tells her why.

### When she's asked

With the control layer driving, she's only asked when a driver would decide something:
* a junction coming up (about 70 m ahead);
* something in her lane she's starting to slow for, while she's still rolling, so she can overtake on
  the fly (about 4 s before she'd have to stop);
* having stopped for something ("You've stopped for a car (Gavril Bluebuck), 4 m in front of your
  bumper. Your call: …"). Stopping is reported as her choice to make, not as "can't drive";
* a problem (no gear, engine off, stuck);
* a police pursuit starting: flee by road or across country, keep driving, or give up;
* otherwise a check-in every 20 s.

### Driving by hand

* **`drive_manually`** sets throttle, brake (0–100 %), steering (−100 to 100) and handbrake. They stay
  held until her next input, like a real driver's hands and feet (`release_after_seconds` 0.5–5 makes
  it a quick tap instead). While she holds them she's checked in every 3 s; if the car rolls with
  nothing held she's asked straight away; and if she gives no driving input for 20 s, she lets go.
* **`do_maneuver`** runs a preset stunt for 0.5–8 s.

Either one takes over from the control layer.

The bridge puts her car in the **realistic gearbox** mode (`--gearbox realistic`, the default), so
shifting is up to her. If her reaction time is too slow for the game, `--time-scale 0.5` runs the
game in slow motion while the bridge is connected.

## What she sees

Every prompt carries a dashboard:
* speed, gear (and the gears this car has), RPM, whether the engine is running, damage and car
  problems, nearby cars, and her last input;
* what the control layer is doing: route and distance left, overtaking, her driving style, and what
  she has stopped for;
* **where the road goes**: the bridge follows the game's road network about 150 m ahead and reports
  bends as angles relative to the car's nose ("40 m: left (26°)"), junctions, dead ends, whether she's
  off the road, and a suggested speed for the bends ahead;
* **simulated lidar**: a level fan of 36 rays (every 10°, `--lidar-beams`) out to 150 m (`--lidar-range`,
  0 = off), cast from the car's body against the world's geometry. It reports the nearest thing in each
  of 8 directions, and the hit object's name says what it is (car, ramp, wall, tree, bush, rock,
  barrier…). Cars are listed with whether they're closing in or pulling away, and **ramps** with
  whether they face her, so she can line up a jump. A **COLLISION WARNING** appears when something
  solid ahead, or a car coming at her, is under 4 s from impact;
* **ground shape**: a height scan builds a rough elevation map around the car. In 16 directions,
  vertical rays measure the ground every 2 m out to 50 m. From that it reads, without relying on object
  names, how far she can drive each way, slopes and their angle, banks and walls with their height,
  drop-offs with their depth, ramp-shaped jumps (a climb ending in a drop) and water, and lists the
  open, gentle ground she could cut across;
* the **last crash** for a minute afterwards;
* **police status**: wanted level 1–3 and how many police cars are around, or "BUSTED". In a police car,
  her suspect's model, distance and direction. This needs the game's traffic system running
  (e.g. `spawn_traffic`);
* **camera view** (experimental, off by default; `--vision-interval 3` turns it on): the bridge grabs a small
  screenshot and **Florence-2-base** turns it into a caption plus the objects it detects, with where
  they are on screen. Her own car is filtered out, and the screenshot is deleted straight away. See
  [Vision](../README.md#vision-optional) for setup. If the server isn't running, the bridge just
  retries quietly.

## Context messages

The **director** polls the car twice a second and tells her what happens:
* **Spoken** (`silent: false`):
  * crashes, detected from sudden deceleration or a jump in damage, saying who hit whom (from the
    game's contact list, with speeds and who was moving into whom, e.g. "The Bruckell Bastion police
    car crashed into YOU at 43 km/h (you were stopped)") or what she hit (a tree, a wall…);
  * the car flipping over;
  * the car being reset by anyone but her (the streamer pressing R, a reload), with how far it moved
    and what she has to do before driving;
  * new car problems from the car's damage tracker (radiator leak, overheating, deflated tires…);
  * on-screen game messages ("Engine stalled", "Low fuel", police notices…).
* **Silent**: a status update every 30 s (skipped while she's being asked anyway, since every prompt
  carries one), new top-speed records, and game messages caused by the bridge itself (gearbox mode,
  ignition).
* **Forced choices**: the prompts above, and a nudge if the car sits parked for 30 s.
* **While the game is paused** she gets one "paused" notice and then nothing about the car. Every
  action except `pause_game`, `list_car_configs` and `list_destinations` is unregistered until the game
  is unpaused, so she can't try to drive a frozen car. If she's allowed to unpause, she's nudged every
  30 s.

## Code layout

| File | Purpose |
|---|---|
| `neuro_beamng/__main__.py` | Entry point (`python -m neuro_beamng`) |
| `neuro_beamng/neuro_api.py` | Generic Neuro API client: reconnects, registers actions, sends one force at a time, and sends results within the deadline |
| `neuro_beamng/actions.py` | Action schemas, input validation, handlers and the dashboard |
| `neuro_beamng/director.py` | Event detection, context messages and when she's asked |
| `neuro_beamng/control.py` | Streamer switches and their page |
| `neuro_beamng/backend.py` | Game interface and the telemetry types |
| `neuro_beamng/mcp_backend.py` + `bridge.lua` + `vehicle_lua.py` | Default backend, using BeamNG.drive's MCP server; `bridge.lua` is the control layer |
| `neuro_beamng/lidar.py` | Simulated lidar → text |
| `neuro_beamng/ground.py` | Ground height scan → terrain shape |
| `neuro_beamng/vision.py` + `tools/florence_server.py` + `tools/start_vision.bat` | Optional camera vision. Runs on the streamer's PC; Neuro still receives only text |
| `neuro_beamng/mock_backend.py` | Fake game for testing (`--mock`) |

To add an action, write a handler on `Driver` and register it in `Driver._build_actions`. If it needs
something new from the game, add a method to `GameBackend` and implement it in the backends.
