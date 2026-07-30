"""Execution context: access to the system, privileges, backups and the log."""

from __future__ import annotations

import difflib
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .backup import BackupStore
from .privileges import PrivilegeManager
from .probe import Probe
from .runner import DEFAULT_TIMEOUT, CommandFailed, stream_command
from .system import System

LogSink = Callable[[str, str], None]

DIFF_CONTEXT_LINES = 2
MAX_DIFF_LINES = 60


@dataclass
class ExecContext:
    system: System
    probe: Probe
    privileges: PrivilegeManager
    backups: BackupStore
    dry_run: bool
    sink: LogSink
    reboot_required: bool = False
    notes: list[str] = field(default_factory=list)
    inputs: dict[str, str] = field(default_factory=dict)
    current_task_id: str = ""
    interactive: Callable[[list[str], str], Awaitable[int]] | None = None

    def log(self, message: str, level: str = "info") -> None:
        self.sink(level, message)

    def input_value(self) -> str:
        """The value the user supplied for the current task."""
        return self.inputs.get(self.current_task_id, "")

    def note(self, message: str) -> None:
        """A note shown in the summary once the work is done."""
        if message not in self.notes:
            self.notes.append(message)
        self.log(message, "note")

    def require_reboot(self) -> None:
        self.reboot_required = True

    async def run(
        self,
        argv: list[str],
        *,
        root: bool = False,
        allow_fail: bool = False,
        quiet: bool = False,
        stdin_data: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        cwd: str | None = None,
    ) -> int:
        full = [*self.privileges.prefix(), *argv] if root else list(argv)
        printable = " ".join(shlex.quote(part) for part in full)

        if self.dry_run:
            self.log(f"$ {printable}", "dry")
            return 0

        self.log(f"$ {printable}", "cmd")
        try:
            result = await stream_command(
                full,
                on_line=(lambda line: None) if quiet else (lambda line: self.log(line, "out")),
                stdin_data=stdin_data,
                timeout=timeout,
                cwd=cwd,
            )
        except FileNotFoundError:
            if allow_fail:
                self.log(f"skipped, command not found: {full[0]}", "warn")
                return 127
            raise CommandFailed(full, 127, [f"command not found: {full[0]}"]) from None

        if result.returncode != 0:
            if allow_fail:
                self.log(f"exit code {result.returncode}, continuing (failure allowed)", "warn")
                return result.returncode
            raise CommandFailed(full, result.returncode, result.tail)
        return 0

    async def run_interactive(self, argv: list[str], reason: str) -> int:
        """Run a command on the real terminal with the interface suspended.

        Only used where a program has to ask a question the TUI cannot handle
        (the MOK password, makepkg prompts).
        """
        printable = " ".join(shlex.quote(part) for part in argv)
        if self.dry_run:
            self.log(f"$ {printable}  (interactive, on the terminal)", "dry")
            return 0
        if self.interactive is None:
            self.log("Run this manually in a terminal:", "warn")
            self.log(f"  {printable}", "warn")
            return -1
        self.log(f"{reason}: handing control over to the terminal", "info")
        code = await self.interactive(argv, reason)
        self.log(f"interactive command exited with code {code}", "info")
        return code

    async def capture(self, argv: list[str], *, root: bool = False, timeout: float = 60.0) -> str:
        """Run a command and return its output. The output is not logged."""
        full = [*self.privileges.prefix(), *argv] if root else list(argv)
        lines: list[str] = []
        try:
            result = await stream_command(full, on_line=lines.append, timeout=timeout)
        except (OSError, CommandFailed):
            return ""
        if result.returncode != 0:
            return ""
        return "\n".join(lines)

    async def read_file(self, path: str | Path) -> str | None:
        """Read a file, through sudo if needed. None when the file is missing."""
        target = Path(path)
        try:
            return target.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None
        except PermissionError:
            pass
        except OSError:
            return None
        if not target.exists():
            return None
        content = await self.capture(["cat", str(target)], root=True)
        return content

    async def write_file(
        self,
        path: str | Path,
        content: str,
        *,
        root: bool = True,
        mode: str = "0644",
    ) -> bool:
        """Write a file, with a backup and a diff preview.

        Returns True when the file was changed (or would be changed in dry-run
        mode).
        """
        target = Path(path)
        current = await self.read_file(target)
        if current == content:
            self.log(f"{target} already has the target content", "skip")
            return False

        self._log_diff(target, current, content)

        if not self.dry_run:
            self.backups.record(
                target,
                current.encode("utf-8") if current is not None else None,
                mode=mode,
                root=root,
            )
            if current is not None:
                self.log(f"backup: {self.backups.root}", "info")

        await self.run(["mkdir", "-p", str(target.parent)], root=root, quiet=True)
        await self.run(["tee", str(target)], root=root, stdin_data=content, quiet=True)
        await self.run(["chmod", mode, str(target)], root=root, quiet=True)
        return True

    def _log_diff(self, target: Path, current: str | None, new: str) -> None:
        if current is None:
            self.log(f"new file {target}:", "diff")
            for line in new.splitlines()[:MAX_DIFF_LINES]:
                self.log(f"+ {line}", "diff")
            return
        diff = list(
            difflib.unified_diff(
                current.splitlines(),
                new.splitlines(),
                fromfile=f"{target} (before)",
                tofile=f"{target} (after)",
                n=DIFF_CONTEXT_LINES,
                lineterm="",
            )
        )
        self.log(f"change in {target}:", "diff")
        for line in diff[:MAX_DIFF_LINES]:
            self.log(line, "diff")
        if len(diff) > MAX_DIFF_LINES:
            self.log(f"... ({len(diff) - MAX_DIFF_LINES} more lines)", "diff")
