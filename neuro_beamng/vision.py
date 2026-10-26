"""Turns what's on screen into a line of text for Neuro, using a small local vision model.

The Neuro API only carries text, so the bridge looks at the game itself and passes on a
description. Any OpenAI-compatible server with image input works; tools\\start_vision.bat
runs Florence-2-base (tools\\florence_server.py) on port 11435.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request

from .backend import GameBackend

log = logging.getLogger(__name__)

# Florence-2 (tools/florence_server.py) ignores the prompt and runs its own caption + detection
# tasks; the plain request is for generic chat-style vision servers if one is used instead.
PROMPT = "Describe this scene in one sentence."


class Vision:
    def __init__(self, game: GameBackend, url: str, interval: float = 3.0, scale: float = 0.4) -> None:
        self.game = game
        self.url = url.rstrip("/") + "/chat/completions"
        self.interval = interval
        self.scale = scale
        self.description: str | None = None
        self.updated = 0.0

    def summary(self) -> str | None:
        """Dashboard line, or None if there's no fresh description."""
        if not self.description:
            return None
        age = time.monotonic() - self.updated
        if age > self.interval * 4:
            return None
        return (f"Camera view ({age:.0f} s ago; the car in the middle of the shot is yours, and positions are "
                f"on screen, not around the car): {self.description}")

    def _ask(self, jpeg_b64: str) -> str:
        body = {
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{jpeg_b64}"}},
                {"type": "text", "text": PROMPT},
            ]}],
            "max_tokens": 60,
            "temperature": 0.3,
            "repeat_penalty": 1.3,
            "stop": ["\n"],
        }
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())["choices"][0]["message"]["content"].strip()

    async def run(self) -> None:
        warned = False
        while True:
            started = time.monotonic()
            wait = self.interval
            try:
                frame = await self.game.capture_frame(self.scale)
                if frame:
                    self.description = await asyncio.to_thread(self._ask, frame)
                    self.updated = time.monotonic()
                    log.debug("vision (%.1fs): %s", self.updated - started, self.description)
                    warned = False
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if not warned:
                    log.warning("Vision server not reachable at %s (%s); run tools\\start_vision.bat. Retrying quietly.",
                                self.url, e)
                    warned = True
                wait = 15.0  # don't keep taking screenshots for a server that isn't there
            except Exception:
                log.exception("Vision update failed")
            await asyncio.sleep(max(0.5, wait - (time.monotonic() - started)))
