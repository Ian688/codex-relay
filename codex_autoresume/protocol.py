"""Minimal, dependency-free Codex app-server JSON-RPC client.

App-server speaks newline-delimited JSON over stdio; it is not the MCP
Content-Length wire framing. Only an explicitly launched local Codex binary
is contacted. Stdout must be reserved for protocol messages.
"""
from __future__ import annotations
import asyncio
import contextlib
import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

class ProtocolError(RuntimeError):
    pass

class AppServer:
    def __init__(self, binary: str = "codex"):
        self.binary = binary
        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self.notifications: asyncio.Queue[dict] = asyncio.Queue(maxsize=1024)
        self._seq = 0
        self._write_lock = asyncio.Lock()

    async def start(self):
        if self.proc:
            return self
        self.proc = await asyncio.create_subprocess_exec(
            self.binary, "app-server", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
            # Persisted thread history is returned as one JSON line.
            limit=64 * 1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr = asyncio.create_task(self._read_stderr())
        try:
            await self.request("initialize", {"clientInfo": {
                "name": "codex-autoresume", "title": "Codex AutoResume",
                "version": "0.1.0"}, "capabilities": {"experimentalApi": False}})
            await self.notify("initialized", {})
        except Exception:
            await self.close()
            raise
        return self

    async def _read_stdout(self):
        assert self.proc and self.proc.stdout
        try:
            while line := await self.proc.stdout.readline():
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    log.warning("Unexpected stdout from codex app-server (not JSON)")
                    continue
                if "id" in obj and ("result" in obj or "error" in obj):
                    fut = self._pending.pop(obj["id"], None)
                    if fut and not fut.done():
                        if "error" in obj:
                            fut.set_exception(ProtocolError(str(obj["error"])))
                        else:
                            fut.set_result(obj.get("result") or {})
                elif "method" in obj:
                    # Server-initiated requests need a host/UI. Never silently grant
                    # permissions or answer approvals. Fail closed where possible.
                    if "id" in obj:
                        await self._send({"id": obj["id"], "error": {
                            "code": -32601, "message": "Unattended AutoResume denies server requests"}})
                    elif obj["method"] in {"turn/completed", "turn/started", "error", "account/rateLimits/updated"}:
                        try:
                            self.notifications.put_nowait(obj)
                        except asyncio.QueueFull:
                            # Never lose a completion in favor of optional telemetry.
                            await self.notifications.put(obj)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ProtocolError("codex app-server disconnected"))
            self._pending.clear()

    async def _read_stderr(self):
        assert self.proc and self.proc.stderr
        while line := await self.proc.stderr.readline():
            log.debug("codex app-server: %s", line.decode(errors="replace").rstrip()[:400])

    async def _send(self, msg: dict[str, Any]):
        if not self.proc or not self.proc.stdin or self.proc.returncode is not None:
            raise ProtocolError("codex app-server not running")
        async with self._write_lock:
            self.proc.stdin.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
            await self.proc.stdin.drain()

    async def notify(self, method: str, params: dict | None = None):
        await self._send({"method": method, "params": params or {}})

    async def request(self, method: str, params: dict | None = None, timeout: int = 45):
        self._seq += 1
        ident = self._seq
        fut = asyncio.get_running_loop().create_future()
        self._pending[ident] = fut
        try:
            await self._send({"id": ident, "method": method, "params": params or {}})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(ident, None)

    async def limits(self):
        return await self.request("account/rateLimits/read")

    async def account(self):
        return await self.request("account/read", {"refreshToken": False})

    async def list_threads(self, pages: int = 2, limit: int = 50):
        found = []
        cursor = None
        seen = set()
        for _ in range(pages):
            params = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            response = await self.request("thread/list", params)
            found.extend(response.get("data", []))
            cursor = response.get("nextCursor")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        return found

    async def read_thread(self, thread_id: str):
        response = await self.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        return response["thread"]

    async def resume_thread(self, thread_id: str):
        return await self.request("thread/resume", {"threadId": thread_id})

    async def start_turn(self, thread_id: str, message: str):
        # Explicitly disallow interactive escalation. Inherits persisted sandbox
        # settings; we never set danger-full-access or bypass approvals.
        return await self.request("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": message, "text_elements": []}],
            "approvalPolicy": "never"})

    async def close(self):
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                self.proc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.proc.wait(), timeout=2)
        for task in (self._reader, self._stderr):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.proc = None
