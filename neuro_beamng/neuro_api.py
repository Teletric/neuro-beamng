"""Async client for the Neuro SDK websocket API.

Spec: https://github.com/VedalAI/neuro-sdk/blob/main/API/SPECIFICATION.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

# Neuro discards results that arrive after ~20s. We wait this long for a handler
# before answering "still in progress" and letting it finish in the background.
RESULT_DEADLINE = 5.0
# Safety net in case a force never resolves (e.g. Neuro got disconnected).
FORCE_TIMEOUT = 90.0


@dataclass
class Action:
    name: str
    description: str
    schema: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.schema is not None:
            out["schema"] = self.schema
        return out


class InvalidAction(Exception):
    """Raise from an action handler to reply success=false.

    During a force, Neuro immediately retries, so only use this for bad input
    she can fix - not for in-game failures.
    """


ActionHandler = Callable[[str, dict[str, Any]], Awaitable[str | None]]


class NeuroClient:
    def __init__(
        self,
        url: str,
        game: str,
        actions: Iterable[Action],
        on_action: ActionHandler,
        on_connected: Callable[[], Awaitable[None]] | None = None,
        reconnect_delay: float = 3.0,
    ) -> None:
        self.url = url
        self.game = game
        self.on_action = on_action
        self.on_connected = on_connected
        self.reconnect_delay = reconnect_delay
        self.character = "Neuro-sama"
        self._actions = {a.name: a for a in actions}
        self._ws: ClientConnection | None = None
        self._force: set[str] | None = None
        self._force_started = 0.0
        self._force_timeout = FORCE_TIMEOUT
        self._tasks: set[asyncio.Task] = set()

    @property
    def connected(self) -> bool:
        return self._ws is not None

    @property
    def force_pending(self) -> bool:
        if self._force is not None and time.monotonic() - self._force_started > self._force_timeout:
            log.warning("Action force timed out without a matching action; clearing it")
            self._force = None
        return self._force is not None

    async def run_forever(self) -> None:
        while True:
            try:
                async with connect(self.url) as ws:
                    self._ws = ws
                    log.info("Connected to Neuro at %s", self.url)
                    await self._send("startup")
                    await self._send("actions/register", {"actions": [a.to_json() for a in self._actions.values()]})
                    if self.on_connected:
                        self._spawn(self.on_connected())
                    async for raw in ws:
                        await self._handle(raw)
            except (OSError, ConnectionClosed) as e:
                log.warning("Neuro connection lost (%s); retrying in %.0fs", e, self.reconnect_delay)
            finally:
                self._ws = None
                self._force = None
            await asyncio.sleep(self.reconnect_delay)

    # --- outgoing ---------------------------------------------------------

    async def context(self, message: str, silent: bool = True) -> None:
        log.info("context%s: %s", "" if silent else " (loud)", message)
        await self._send("context", {"message": message, "silent": silent})

    def set_actions(self, actions: Iterable[Action]) -> None:
        """Replace the action list sent on (re)connect. Use register/unregister once connected."""
        self._actions = {a.name: a for a in actions}

    async def register(self, actions: Iterable[Action]) -> None:
        actions = list(actions)
        self._actions.update({a.name: a for a in actions})
        await self._send("actions/register", {"actions": [a.to_json() for a in actions]})

    async def unregister(self, names: Iterable[str]) -> None:
        names = list(names)
        for n in names:
            self._actions.pop(n, None)
        await self._send("actions/unregister", {"action_names": names})

    async def force(
        self,
        query: str,
        action_names: Iterable[str],
        state: str | None = None,
        priority: str = "low",
        ephemeral: bool = False,
        timeout: float = FORCE_TIMEOUT,
    ) -> bool:
        """Send an action force. Returns False if one is already in progress.

        timeout: after this long without a matching action, stop waiting and allow a new force.
        """
        names = [n for n in action_names if n in self._actions]
        if not self.connected or self.force_pending or not names:
            return False
        self._force = set(names)
        self._force_started = time.monotonic()
        self._force_timeout = timeout
        data: dict[str, Any] = {
            "query": query,
            "action_names": names,
            "ephemeral_context": ephemeral,
            "priority": priority,
        }
        if state:
            data["state"] = state
        log.info("force (%s): %s", priority, query)
        if state:
            log.debug("force state:\n%s", state)
        await self._send("actions/force", data)
        return True

    async def _send(self, command: str, data: dict[str, Any] | None = None) -> None:
        ws = self._ws
        if ws is None:
            log.debug("Not connected; dropping %s", command)
            return
        msg: dict[str, Any] = {"command": command, "game": self.game}
        if data is not None:
            msg["data"] = data
        try:
            await ws.send(json.dumps(msg))
        except ConnectionClosed:
            log.debug("Connection closed while sending %s", command)

    # --- incoming ---------------------------------------------------------

    async def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
            command = msg["command"]
        except (ValueError, KeyError, TypeError):
            log.warning("Ignoring malformed message: %r", raw)
            return
        data = msg.get("data") or {}

        if command == "action":
            self._spawn(self._dispatch(data))
        elif command == "actions/reregister_all":
            await self._send("actions/register", {"actions": [a.to_json() for a in self._actions.values()]})
        elif command == "startup":
            session = data.get("session") or {}
            self.character = session.get("displayName") or self.character
            log.info("Server acknowledged startup as %s", self.character)
        elif command == "speech_finished":
            log.debug("speech_finished: %s", data)
        else:
            log.debug("Ignoring unknown command %s", command)

    async def _dispatch(self, data: dict[str, Any]) -> None:
        action_id = str(data.get("id", ""))
        name = data.get("name", "")
        log.info("Neuro action: %s %s", name, data.get("data") or "")

        success, message = await self._run_action(name, data.get("data"))
        await self._send("action/result", {"id": action_id, "success": success, "message": message or ""})
        log.info("result %s: %s", "ok" if success else "FAIL", message)

        if success and self._force is not None and name in self._force:
            self._force = None

    async def _run_action(self, name: str, raw_params: Any) -> tuple[bool, str | None]:
        # An action unregistered while a force was waiting (e.g. the game got paused) still goes to the
        # handler, so it can answer with success and end that force instead of making Neuro retry it.
        if name not in self._actions and not (self._force and name in self._force):
            return False, f"Unknown action '{name}'."
        try:
            params = json.loads(raw_params) if raw_params else {}
        except (TypeError, ValueError):
            return False, "Action data was not valid JSON."
        if not isinstance(params, dict):
            return False, "Action data must be a JSON object."

        task = asyncio.ensure_future(self.on_action(name, params))
        task.add_done_callback(_log_late_failure)
        try:
            return True, await asyncio.wait_for(asyncio.shield(task), RESULT_DEADLINE)
        except InvalidAction as e:
            return False, str(e)
        except asyncio.TimeoutError:
            return True, "Working on it - this takes a moment."
        except Exception as e:  # game-side failure: don't trigger a force retry loop
            return True, f"That didn't work in-game: {e}"

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_late_failure)


def _log_late_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None and not isinstance(exc, InvalidAction):
        log.error("Background task failed", exc_info=exc)
