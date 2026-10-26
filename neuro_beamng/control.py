"""Streamer-side switches: turn "admin" actions (switching cars, spawning traffic, ...) on and off live.

Disabling an action unregisters it from Neuro (actions/unregister), so she can't see or use it;
enabling registers it again. Choices are saved to a JSON file so they survive restarts. A small HTTP
server (default http://localhost:8002) serves a switches page and its API at /api/actions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .actions import Driver
from .neuro_api import NeuroClient

log = logging.getLogger(__name__)

# Actions the streamer can switch off, with labels for the control panel.
TOGGLEABLE = {
    "change_car": "Switch cars",
    "list_car_configs": "Look up car configs",
    "spawn_traffic": "Spawn traffic",
    "set_time_of_day": "Change time of day",
    "set_gravity": "Change gravity (--allow-chaos)",
    "pause_game": "Pause / unpause the game",
    "recover_car": "Recover / repair / teleport car",
}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Neuro BeamNG Switches</title>
<style>
:root { --bg:#f6f6f8; --panel:#fff; --text:#1d1d22; --muted:#6b6b76; --border:#e2e2e8; }
@media (prefers-color-scheme: dark) { :root { --bg:#141418; --panel:#1c1c22; --text:#ececf1; --muted:#a2a2ad; --border:#2c2c35; } }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui,"Segoe UI",sans-serif; }
main { max-width:420px; margin:32px auto; padding:0 16px; }
h1 { font-size:18px; } p { color:var(--muted); font-size:14px; }
.box { background:var(--panel); border:1px solid var(--border); border-radius:12px; padding:8px 16px; }
label { display:flex; gap:10px; align-items:center; padding:10px 0; border-bottom:1px solid var(--border); cursor:pointer; }
label:last-child { border-bottom:none; }
</style></head><body><main>
<h1>Neuro's game controls</h1>
<p>Unticked actions are removed from Neuro's action list right away. Saved across restarts.</p>
<div class="box" id="list">Loading...</div>
</main><script>
async function load(list) {
  if (!list) list = (await (await fetch("/api/actions")).json()).actions;
  const box = document.getElementById("list"); box.innerHTML = "";
  for (const a of list) {
    const l = document.createElement("label"), cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = a.enabled;
    cb.onchange = async () => { cb.disabled = true;
      const r = await fetch("/api/actions", {method: "POST", headers: {"Content-Type": "application/json"},
                            body: JSON.stringify({name: a.name, enabled: cb.checked})});
      load((await r.json()).actions); };
    l.append(cb, a.label); box.appendChild(l);
  }
}
load(); setInterval(load, 10000);
</script></body></html>"""


class ActionSwitches:
    def __init__(self, driver: Driver, neuro: NeuroClient, settings_path: Path) -> None:
        self.driver = driver
        self.neuro = neuro
        self.path = settings_path
        self.loop: asyncio.AbstractEventLoop | None = None
        saved = self._load().get("disabled_actions", [])
        self.driver.disabled.update(n for n in saved if n in TOGGLEABLE)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        data = self._load()
        data["disabled_actions"] = sorted(self.driver.disabled)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def listing(self) -> list[dict]:
        available = {a.name for a in self.driver.actions}
        return [{"name": name, "label": label, "enabled": name not in self.driver.disabled}
                for name, label in TOGGLEABLE.items() if name in available]

    async def set_enabled(self, name: str, enabled: bool) -> None:
        if name not in TOGGLEABLE:
            raise KeyError(name)
        if enabled:
            self.driver.disabled.discard(name)
            action = next((a for a in self.driver.actions if a.name == name), None)
            if action and self.driver.available(name):  # stays hidden while the game is paused
                await self.neuro.register([action])
        else:
            self.driver.disabled.add(name)
            await self.neuro.unregister([name])
        self._save()
        log.info("Action %s %s", name, "enabled" if enabled else "disabled")

    # --- HTTP API -------------------------------------------------------------

    def serve(self, port: int) -> None:
        self.loop = asyncio.get_running_loop()
        switches = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:
                pass

            def _reply(self, code: int, obj) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")  # so a page on another port can use the API too
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:
                self._reply(204, {})

            def do_GET(self) -> None:
                if self.path.rstrip("/") == "/api/actions":
                    self._reply(200, {"actions": switches.listing()})
                elif self.path.split("?")[0] in ("/", "/index.html"):
                    body = PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._reply(404, {"error": "not found"})

            def do_POST(self) -> None:
                if self.path.rstrip("/") != "/api/actions":
                    self._reply(404, {"error": "not found"})
                    return
                try:
                    req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    fut = asyncio.run_coroutine_threadsafe(
                        switches.set_enabled(str(req["name"]), bool(req["enabled"])), switches.loop)
                    fut.result(timeout=5)
                    self._reply(200, {"actions": switches.listing()})
                except KeyError:
                    self._reply(400, {"error": "unknown action"})
                except Exception as e:
                    self._reply(500, {"error": str(e)})

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("Action switches: http://localhost:%d", port)
