"""Privilege escalation.

Passwords never pass through the interface. Authorisation happens once, on the
real terminal, with the TUI suspended. After that every command goes through
`sudo -n`, and a background task refreshes the sudo timestamp.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil

from .system import System

KEEPALIVE_INTERVAL = 60.0


class PrivilegeError(RuntimeError):
    pass


class PrivilegeManager:
    def __init__(self, system: System) -> None:
        self.system = system
        self._keepalive: asyncio.Task[None] | None = None
        if system.is_root:
            self.backend = "root"
        elif shutil.which("sudo"):
            self.backend = "sudo"
        elif shutil.which("doas"):
            self.backend = "doas"
        else:
            self.backend = "none"

    @property
    def available(self) -> bool:
        return self.backend != "none"

    def prefix(self) -> list[str]:
        """argv prefix for a command that needs root."""
        if self.backend == "root":
            return []
        if self.backend == "sudo":
            return ["sudo", "-n"]
        if self.backend == "doas":
            return ["doas", "-n"]
        raise PrivilegeError(
            "no sudo, no doas and not running as root: system tasks cannot be executed"
        )

    def interactive_prime_command(self) -> list[str]:
        if self.backend == "sudo":
            return ["sudo", "-v"]
        if self.backend == "doas":
            return ["doas", "true"]
        return ["true"]

    async def is_primed(self) -> bool:
        if self.backend == "root":
            return True
        if self.backend == "none":
            return False
        argv = ["sudo", "-n", "true"] if self.backend == "sudo" else ["doas", "-n", "true"]
        return await _quiet_call(argv) == 0

    def start_keepalive(self) -> None:
        if self.backend != "sudo" or self._keepalive is not None:
            return
        self._keepalive = asyncio.create_task(self._keepalive_loop())

    async def stop_keepalive(self) -> None:
        task = self._keepalive
        self._keepalive = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _keepalive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                await _quiet_call(["sudo", "-n", "-v"])
        except asyncio.CancelledError:
            raise


async def _quiet_call(argv: list[str], timeout: float = 10.0) -> int:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return 127
    try:
        return await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124
