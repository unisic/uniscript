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
    return parser


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
    for task in tasks:
        if task.category is not current:
            current = task.category
            print()
            print(f"== {current.label}")
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

    from .tui.app import run_app

    run_app(
        system=system,
        probe=probe,
        privileges=PrivilegeManager(system),
        backup_root=data_dir() / "backups",
        dry_run=args.dry_run,
    )
    return 0
