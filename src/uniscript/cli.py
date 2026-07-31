"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import __version__
from .catalog import build_tasks
from .core.backup import BackupStore
from .core.context import ExecContext
from .core.privileges import PrivilegeManager
from .core.probe import Probe
from .core.runner import CommandFailed
from .core.system import System, detect_system
from .core.tasks import Risk, Task


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "uniscript"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uniscript",
        description="Post-install Linux configuration.",
    )
    parser.add_argument("--version", action="version", version=f"uniscript {__version__}")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="change nothing, only show the commands and file diffs",
    )
    parser.add_argument(
        "--system",
        action="store_true",
        help="print the detected system information and exit",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the available tasks and exit",
    )
    parser.add_argument(
        "--plan",
        metavar="ID[,ID...]",
        help="print the commands of the selected tasks and exit",
    )
    parser.add_argument(
        "--run",
        metavar="ID[,ID...]",
        help="run the selected tasks without the interface",
    )
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="ID=VALUE",
        help="value for a task that asks a question (may be given several times)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt for --run",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="start the terminal interface without asking",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="start the browser GUI without asking",
    )
    parser.add_argument(
        "--input-probe",
        action="store_true",
        help="show the raw key and mouse events this terminal delivers, then exit",
    )
    return parser


def _input_probe() -> int:
    """Print every byte the terminal sends, with mouse reporting switched on.

    The interface cannot see events the terminal never delivers. This shows
    which side is broken: if moving and clicking the mouse prints nothing
    here, the terminal (or tmux, screen, an IDE panel) is not passing the
    mouse through, and no change in uniscript can fix that.
    """
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    if not os.isatty(fd):
        print("stdin is not a terminal", file=sys.stderr)
        return 1
    print("Move the mouse, click and scroll inside this window.")
    print("Mouse events look like \\x1b[<0;12;5M. Press q to finish.\n")
    # A terminal under test may repaint over the lines faster than anyone can
    # read them; the raw bytes in a file outlive the window.
    log_path = os.environ.get("UNISCRIPT_PROBE_LOG")
    log = open(log_path, "ab") if log_path else None
    old = termios.tcgetattr(fd)
    # Click, any-motion and SGR extended reporting, the same set Textual asks for.
    sys.stdout.write("\x1b[?1000h\x1b[?1003h\x1b[?1006h")
    sys.stdout.flush()
    mouse_events = 0
    keys = 0
    try:
        tty.setcbreak(fd)
        while True:
            ready, _, _ = select.select([fd], [], [], 30.0)
            if not ready:
                break
            data = os.read(fd, 1024)
            if not data:
                break
            if log is not None:
                log.write(data)
                log.flush()
            if b"q" in data and b"\x1b[<" not in data:
                break
            shown = repr(data)[2:-1]
            if data.startswith(b"\x1b[<"):
                mouse_events += data.count(b"\x1b[<")
                print(f"mouse: {shown}\r")
            else:
                keys += 1
                print(f"key:   {shown}\r")
    finally:
        if log is not None:
            log.close()
        sys.stdout.write("\x1b[?1003l\x1b[?1000l\x1b[?1006l")
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    print(f"\nmouse events: {mouse_events}, key presses: {keys}")
    if mouse_events == 0:
        print("No mouse events arrived. This terminal is not passing the mouse")
        print("through (tmux needs 'set -g mouse on'; IDE and web terminals")
        print("often do not forward the mouse at all). Try a plain terminal.")
    else:
        print("The mouse reaches the application, so uniscript will see it too.")
    return 0


def _ask_interface() -> str:
    """One question before anything starts: the terminal or the browser."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return "tui"
    print("How do you want to run uniscript?")
    print("  1) Terminal interface (TUI)")
    print("  2) Browser GUI")
    while True:
        try:
            answer = input("Choice [1]: ").strip().lower()
        except EOFError:
            print()
            return "tui"
        except KeyboardInterrupt:
            print()
            raise SystemExit(130) from None
        if answer in ("", "1", "t", "tui"):
            return "tui"
        if answer in ("2", "g", "gui", "w", "web"):
            return "gui"
        print("1 or 2, please.")


def _select(tasks: list[Task], spec: str) -> tuple[list[Task], list[str]]:
    wanted = [item.strip() for item in spec.split(",") if item.strip()]
    by_id = {task.id: task for task in tasks}
    chosen: list[Task] = []
    missing: list[str] = []
    for item in wanted:
        task = by_id.get(item)
        if task is None:
            missing.append(item)
        elif task not in chosen:
            chosen.append(task)
    order = {task.id: index for index, task in enumerate(tasks)}
    chosen.sort(key=lambda task: order[task.id])
    return chosen, missing


def _print_system(system: System, probe: Probe, tasks: list[Task]) -> None:
    for label, value in system.summary_rows():
        print(f"{label:<22} {value}")
    print(f"{'Tasks available':<22} {len(tasks)}")
    if system.gpus:
        print()
        print("Graphics cards:")
        for gpu in system.gpus:
            extra = ""
            if gpu.vendor == "nvidia":
                extra = (
                    ", open kernel module: yes"
                    if gpu.open_kernel_module_capable
                    else ", open kernel module: no"
                )
            print(
                f"  [{gpu.pci_slot}] {gpu.name} "
                f"({gpu.vendor_id}:{gpu.device_id}, driver: {gpu.driver or 'none'}{extra})"
            )
    print()
    print(f"Tools: {', '.join(sorted(system.tools)) or 'none'}")


def _print_list(system: System, probe: Probe, tasks: list[Task]) -> None:
    current = None
    current_sub: str | None = None
    for task in tasks:
        if task.category is not current:
            current = task.category
            current_sub = None
            print()
            print(f"== {current.label}")
        if task.subcategory != current_sub:
            current_sub = task.subcategory
            if current_sub is not None:
                print(f"   -- {current_sub}")
        applied = task.is_applied(probe, system)
        state = "done" if applied else "    "
        flags = []
        if task.default:
            flags.append("recommended")
        if "gaming" in task.tags:
            flags.append("gaming")
        if task.risk is not Risk.SAFE:
            flags.append(task.risk.label)
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {state}  {task.id:<28} {task.title}{suffix}")


def _print_plan(system: System, tasks: list[Task]) -> None:
    for task in tasks:
        print(f"# {task.title}")
        for command in task.preview(system):
            print(f"  {command}")
        print()


async def _run_headless(
    system: System,
    probe: Probe,
    privileges: PrivilegeManager,
    tasks: list[Task],
    inputs: dict[str, str],
    dry_run: bool,
) -> int:
    def sink(level: str, message: str) -> None:
        prefix = {"cmd": "$ ", "dry": "$ ", "out": "  ", "error": "! ", "warn": "* "}.get(level, "")
        text = message[2:] if level in {"cmd", "dry"} and message.startswith("$ ") else message
        print(f"{prefix}{text}", flush=True)

    async def interactive(argv: list[str], reason: str) -> int:
        print(f"\n{reason}")
        print(f"$ {' '.join(shlex.quote(part) for part in argv)}\n", flush=True)
        return await asyncio.to_thread(subprocess.call, argv)

    ctx = ExecContext(
        system=system,
        probe=probe,
        privileges=privileges,
        backups=BackupStore(data_dir() / "backups"),
        dry_run=dry_run,
        sink=sink,
        inputs=inputs,
        interactive=interactive,
    )

    needs_root = any(task.requires_root() for task in tasks)
    if (
        needs_root
        and not dry_run
        and privileges.backend in {"sudo", "doas"}
        and not await privileges.is_primed()
    ):
        if subprocess.call(privileges.interactive_prime_command()) != 0:
            print("No administrator privileges.", file=sys.stderr)
            return 1
        privileges.start_keepalive()

    failures = 0
    try:
        for index, task in enumerate(tasks, start=1):
            print(f"\n[{index}/{len(tasks)}] {task.title}", flush=True)
            try:
                await task.run(ctx)
            except CommandFailed as exc:
                failures += 1
                print(f"! error: {exc}", file=sys.stderr, flush=True)
            except Exception as exc:  # one task must not take the whole run down
                failures += 1
                print(f"! error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        await privileges.stop_keepalive()

    print()
    if ctx.notes:
        print("To do by hand:")
        for note in ctx.notes:
            print(f"  - {note}")
    print(f"Backups: {ctx.backups.summary()}")
    if ctx.reboot_required:
        print("A reboot is required.")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.input_probe:
        return _input_probe()

    system = detect_system()
    probe = Probe(system)
    tasks = build_tasks(system)

    if args.system:
        _print_system(system, probe, tasks)
        return 0

    if args.list:
        _print_list(system, probe, tasks)
        return 0

    if args.plan:
        chosen, missing = _select(tasks, args.plan)
        for item in missing:
            print(f"unknown task: {item}", file=sys.stderr)
        _print_plan(system, chosen)
        return 1 if missing else 0

    if args.run:
        chosen, missing = _select(tasks, args.run)
        for item in missing:
            print(f"unknown task: {item}", file=sys.stderr)
        if not chosen:
            return 1
        inputs: dict[str, str] = {}
        for pair in args.input:
            key, _, value = pair.partition("=")
            if not value:
                print(f"bad --input format: {pair}", file=sys.stderr)
                return 1
            inputs[key.strip()] = value.strip()
        missing_inputs = [
            task.id for task in chosen if task.prompt is not None and task.id not in inputs
        ]
        if missing_inputs:
            for task_id in missing_inputs:
                print(f"task {task_id} needs --input {task_id}=VALUE", file=sys.stderr)
            return 1
        if not args.dry_run and not args.yes:
            print("The following tasks will run:")
            for task in chosen:
                print(f"  - {task.title}")
            answer = input("Continue? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                return 1
        privileges = PrivilegeManager(system)
        return asyncio.run(_run_headless(system, probe, privileges, chosen, inputs, args.dry_run))

    interface = "gui" if args.gui else "tui" if args.tui else _ask_interface()
    if interface == "gui":
        from .gui.server import serve

        return serve(
            port=0,
            open_browser=True,
            dry_run=args.dry_run,
            backup_root=data_dir() / "backups",
            system=system,
        )

    from .tui.app import run_app

    run_app(
        system=system,
        probe=probe,
        privileges=PrivilegeManager(system),
        backup_root=data_dir() / "backups",
        dry_run=args.dry_run,
    )
    return 0
