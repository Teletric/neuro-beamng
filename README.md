# neuro-beamng

[Neuro API](https://github.com/VedalAI/neuro-sdk) integration that lets Neuro-sama drive in BeamNG.drive (work in progress)

Neuro makes the driver's decisions (start the engine, pick a gear, follow the road, overtake, where to
go, how hard to drive) and a control layer in the game steers and brakes along her chosen line every
frame (like a real self-driving car). No actual game modification is needed as the bridge uses BeamNG.drive's built-in MCP server, added in version 0.40. 

* [How it works](docs/HowItWorks.md) - the driving layers, what she sees, and when she's asked
* [Actions](docs/Actions.md) - everything she can do, and the streamer switches
* [Testing](docs/Testing.md) - testing in the game without Neuro

## Requirements
* **BeamNG.drive 0.40 or newer** (tested on 0.40.3)
  * The **MCP server** turned on: **Options → Enable MCP server**, next to "Competitive scenarios". It's saved, so you only do this once
* [Python](https://www.python.org/downloads/) **3.10+**
  * `winget install Python.Python.3.12` (or install it manually)
* [Optional] A second Python environment, `.venv-florence` (Python 3.8 with PyTorch), and the
  [Florence-2-base](https://huggingface.co/microsoft/Florence-2-base) model in `models\Florence-2-base`, for the camera view

## Getting Started
### Clone Repository
```bat
git clone https://github.com/Teletric/neuro-beamng
cd neuro-beamng
```

## Setup
```bat
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Testing/Running
You'll need to use a neuro-api-compatible engine ([Gary](https://github.com/Govorunb/gary)/[Jippity](https://github.com/EnterpriseScratchDev/neuro-api-jippity)/[Tony](https://github.com/Pasu4/neuro-api-tony)/[Randy](https://github.com/VedalAI/neuro-sdk/tree/main/Randy)/etc) in order to drive the integration.
### [Testing](docs/Testing.md) - testing guide

### Launching the game
1. Start BeamNG.drive, load a level in **Freeroam** and spawn a car.
2. Start your Neuro API engine, then run the bridge:
```bat
.venv\Scripts\python -m neuro_beamng
```

The bridge waits until it can reach the game and a car exists, so the start order doesn't matter. It
always controls **the car you're currently in**, so switching cars in-game works too.

### Options
To view all options, run `.venv\Scripts\python -m neuro_beamng --help`. Useful ones:
* `--neuro-url ws://host:port` - where the Neuro API engine is (default `ws://localhost:8000`)
* `--gearbox realistic|arcade|keep` - gearbox mode for her car (default `realistic`, so shifting is up to her)
* `--time-scale 0.5` - slow motion while the bridge runs, to offset her reaction time
* `--allow-autopilot` / `--allow-chaos` - also offer BeamNG's AI autopilot / chaos actions like `set_gravity`
* `--disable change_car,spawn_traffic` - switch off admin actions (or use the switches page at `http://localhost:8002`)
* `--idle-force 0`, `--status-interval 0` - never force her when parked / no silent status updates
* `-v` - verbose logging

### Vision (optional)
Off by default. I figured it would be nice for her to be able to see her environment and react according to visual cues (like a real self-driving car), but it hasn't been successful.

If you want to try it anyway, start the vision server, then run the bridge with `--vision-interval 3`:
```bat
tools\start_vision.bat
```
`tools\start_vision.bat --cpu` keeps it off the GPU.

## Drive safe, Neuro!
