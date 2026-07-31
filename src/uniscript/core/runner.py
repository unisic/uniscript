"""Running commands with streamed output.

Resource assumptions:
  - process output is read in 8 KiB chunks and split into lines, a single line
    is truncated to MAX_LINE characters, so a process spewing megabytes without
    a newline cannot grow the memory footprint,
  - only the last TAIL_LINES lines are kept for the error report (deque with
    maxlen),
  - every command has a timeout, after which the whole process group is killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

MAX_LINE = 4000
TAIL_LINES = 200
READ_CHUNK = 8192
DEFAULT_TIMEOUT = 1800.0


class CommandFailed(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, tail: list[str]) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.tail = tail
        command = " ".join(argv)
        super().__init__(f"command exited with code {returncode}: {command}")


@dataclass
class CommandResult:
    returncode: int
    tail: list[str]


async def stream_command(
    argv: Sequence[str],
    *,
    on_line: Callable[[str], None],
    stdin_data: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> CommandResult:
    """Run a process, hand every line to on_line, return the exit code."""
    full_env = {**os.environ, "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"}
    if env:
        full_env.update(env)

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=full_env,
        # A new process group so a timeout can kill the whole tree, but NOT a
        # new session: setsid would drop the controlling terminal, and sudo's
        # default tty_tickets binds the primed timestamp to that terminal, so
        # every `sudo -n` in a new session dies with "a password is required".
        process_group=0,
    )

    tail: deque[str] = deque(maxlen=TAIL_LINES)

    async def pump() -> None:
        assert process.stdout is not None
        buffer = ""
        while True:
            chunk = await process.stdout.read(READ_CHUNK)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            # A bare carriage return is the usual progress bar, treat it as end of line.
            buffer = buffer.replace("\r\n", "\n").replace("\r", "\n")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line[:MAX_LINE]
                if line.strip():
                    tail.append(line)
                    on_line(line)
            if len(buffer) > MAX_LINE:
                line, buffer = buffer[:MAX_LINE], buffer[MAX_LINE:]
                tail.append(line)
                on_line(line)
        if buffer.strip():
            line = buffer[:MAX_LINE]
            tail.append(line)
            on_line(line)

    async def feed() -> None:
        if stdin_data is None or process.stdin is None:
            return
        try:
            process.stdin.write(stdin_data.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                process.stdin.close()

    try:
        await asyncio.wait_for(asyncio.gather(pump(), feed(), process.wait()), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        _terminate_group(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            _kill_group(process)
            await process.wait()
        raise
    finally:
        if process.returncode is None:
            _kill_group(process)

    return CommandResult(returncode=process.returncode or 0, tail=list(tail))


def _terminate_group(process: asyncio.subprocess.Process) -> None:
    _signal_group(process, signal.SIGTERM)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    _signal_group(process, signal.SIGKILL)


def _signal_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(ProcessLookupError, OSError):
            process.send_signal(sig)


def run_blocking(argv: Sequence[str], *, timeout: float = 120.0) -> int:
    """Run a command interactively on the real terminal.

    Only used with the interface suspended (App.suspend), when a program has to
    ask the user for a password or for input the TUI cannot supply.
    """
    import subprocess

    try:
        return subprocess.call(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127
