"""Task model: steps, risk, state detection."""

from __future__ import annotations

import shlex
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from .context import ExecContext
from .probe import Probe
from .runner import CommandFailed
from .system import System


class Risk(Enum):
    SAFE = ("safe", "risk-safe")
    MEDIUM = ("needs care", "risk-medium")
    HIGH = ("risky", "risk-high")

    def __init__(self, label: str, css_class: str) -> None:
        self.label = label
        self.css_class = css_class


class Category(Enum):
    SYSTEM = ("System basics", 10)
    REPOS = ("Repositories", 20)
    DRIVERS = ("Drivers and graphics", 30)
    MULTIMEDIA = ("Multimedia and codecs", 40)
    PACKAGING = ("Flatpak and Snap", 50)
    GAMING = ("Gaming", 60)
    TWEAKS = ("Tweaks", 70)
    SHELL = ("Shell and prompt", 75)
    APPS = ("Applications", 80)
    MAINTENANCE = ("Maintenance", 90)

    def __init__(self, label: str, order: int) -> None:
        self.label = label
        self.order = order


class Step(ABC):
    """A single operation inside a task."""

    @abstractmethod
    def preview(self, system: System) -> list[str]:
        """Plan lines shown to the user before anything runs."""

    @abstractmethod
    async def run(self, ctx: ExecContext) -> None:
        """Execute the step."""

    def requires_root(self) -> bool:
        """Whether the step needs administrator privileges."""
        return True


@dataclass
class Run(Step):
    argv: list[str]
    root: bool = True
    allow_fail: bool = False
    timeout: float = 1800.0
    label: str | None = None

    def requires_root(self) -> bool:
        return self.root

    def preview(self, system: System) -> list[str]:
        prefix = "sudo " if self.root and not system.is_root else ""
        return [prefix + " ".join(shlex.quote(part) for part in self.argv)]

    async def run(self, ctx: ExecContext) -> None:
        await ctx.run(self.argv, root=self.root, allow_fail=self.allow_fail, timeout=self.timeout)


@dataclass
class Shell(Step):
    """A command that needs a shell (pipe, redirection)."""

    script: str
    root: bool = True
    allow_fail: bool = False
    timeout: float = 1800.0

    def requires_root(self) -> bool:
        return self.root

    def preview(self, system: System) -> list[str]:
        prefix = "sudo " if self.root and not system.is_root else ""
        return [f"{prefix}bash -c {shlex.quote(self.script)}"]

    async def run(self, ctx: ExecContext) -> None:
        await ctx.run(
            ["bash", "-c", self.script],
            root=self.root,
            allow_fail=self.allow_fail,
            timeout=self.timeout,
        )


@dataclass
class Install(Step):
    packages: list[str]
    optional: bool = False
    extra_args: list[str] = field(default_factory=list)

    def preview(self, system: System) -> list[str]:
        pm = system.package_manager
        if pm is None:
            return ["no package manager"]
        argv = pm.install_cmd(self.packages)
        argv = [argv[0], *self.extra_args, *argv[1:]] if self.extra_args else argv
        prefix = "" if system.is_root else "sudo "
        return [prefix + " ".join(argv)]

    async def run(self, ctx: ExecContext) -> None:
        pm = ctx.system.package_manager
        if pm is None:
            ctx.log("no package manager, skipping the install", "warn")
            return
        missing = [p for p in self.packages if p not in ctx.probe.installed_packages]
        if not missing:
            ctx.log(f"already installed: {' '.join(self.packages)}", "skip")
            return
        argv = pm.install_cmd(missing)
        if self.extra_args:
            argv = [argv[0], *self.extra_args, *argv[1:]]
        try:
            await ctx.run(argv, root=True, timeout=self.timeout_for(missing))
        except CommandFailed:
            if not self.optional:
                raise
            ctx.log("batch install failed, retrying one package at a time", "warn")
            for package in missing:
                single = pm.install_cmd([package])
                if self.extra_args:
                    single = [single[0], *self.extra_args, *single[1:]]
                await ctx.run(single, root=True, allow_fail=True, timeout=900.0)

    @staticmethod
    def timeout_for(packages: list[str]) -> float:
        return min(3600.0, 600.0 + 60.0 * len(packages))


@dataclass
class Remove(Step):
    packages: list[str]
    allow_fail: bool = True

    def preview(self, system: System) -> list[str]:
        pm = system.package_manager
        if pm is None:
            return ["no package manager"]
        prefix = "" if system.is_root else "sudo "
        return [prefix + " ".join(pm.remove_cmd(self.packages))]

    async def run(self, ctx: ExecContext) -> None:
        pm = ctx.system.package_manager
        if pm is None:
            return
        present = [p for p in self.packages if p in ctx.probe.installed_packages]
        if not present:
            ctx.log("nothing to remove", "skip")
            return
        await ctx.run(pm.remove_cmd(present), root=True, allow_fail=self.allow_fail)


@dataclass
class WriteFile(Step):
    path: str
    builder: Callable[[ExecContext], Awaitable[str]]
    description: str
    mode: str = "0644"
    root: bool = True

    def requires_root(self) -> bool:
        return self.root

    def preview(self, system: System) -> list[str]:
        return [f"write {self.path} ({self.description})"]

    async def run(self, ctx: ExecContext) -> None:
        content = await self.builder(ctx)
        path = (
            self.path.replace("~", str(ctx.system.home), 1)
            if self.path.startswith("~")
            else self.path
        )
        await ctx.write_file(path, content, root=self.root, mode=self.mode)


@dataclass
class Unit(Step):
    """Enable or disable a systemd unit."""

    unit: str
    action: str = "enable"
    now: bool = True

    def preview(self, system: System) -> list[str]:
        prefix = "" if system.is_root else "sudo "
        now = " --now" if self.now else ""
        return [f"{prefix}systemctl {self.action}{now} {self.unit}"]

    async def run(self, ctx: ExecContext) -> None:
        if not ctx.system.has_systemd:
            ctx.log("no systemd, skipping", "skip")
            return
        argv = ["systemctl", self.action]
        if self.now:
            argv.append("--now")
        argv.append(self.unit)
        await ctx.run(argv, root=True, allow_fail=True)


@dataclass
class Interactive(Step):
    """A step that needs a real terminal, for example a password prompt."""

    argv: list[str]
    reason: str
    root: bool = True

    def requires_root(self) -> bool:
        return False  # the command asks for the password on the terminal itself

    def preview(self, system: System) -> list[str]:
        prefix = "sudo " if self.root and not system.is_root else ""
        return [
            prefix
            + " ".join(shlex.quote(part) for part in self.argv)
            + "   (the interface is suspended, the prompt appears in the terminal)"
        ]

    async def run(self, ctx: ExecContext) -> None:
        argv = [*ctx.privileges.prefix(), *self.argv] if self.root else list(self.argv)
        # The command prompts for input itself, so sudo must not run in -n mode.
        argv = [part for part in argv if part != "-n"]
        await ctx.run_interactive(argv, self.reason)


@dataclass
class Note(Step):
    text: str

    def requires_root(self) -> bool:
        return False

    def preview(self, system: System) -> list[str]:
        return [f"note: {self.text}"]

    async def run(self, ctx: ExecContext) -> None:
        ctx.note(self.text)


@dataclass
class Custom(Step):
    """A step with custom logic, for when state has to be read before deciding."""

    description: str
    action: Callable[[ExecContext], Awaitable[None]]
    root: bool = True

    def requires_root(self) -> bool:
        return self.root

    def preview(self, system: System) -> list[str]:
        return [self.description]

    async def run(self, ctx: ExecContext) -> None:
        await self.action(ctx)


DetectFn = Callable[[Probe, System], bool]
AvailableFn = Callable[[System], bool]


@dataclass(frozen=True)
class InputPrompt:
    """A task that needs a value from the user before it can run."""

    label: str
    placeholder: str = ""
    default: str = ""
    validator: Callable[[str], str | None] | None = None

    def validate(self, value: str) -> str | None:
        value = value.strip()
        if not value:
            return "The value cannot be empty."
        return self.validator(value) if self.validator else None


@dataclass
class Task:
    id: str
    title: str
    summary: str
    category: Category
    steps: list[Step]
    risk: Risk = Risk.SAFE
    details: list[str] = field(default_factory=list)
    tags: frozenset[str] = frozenset()
    default: bool = False
    reboot: bool = False
    detect: DetectFn | None = None
    available: AvailableFn | None = None
    warning: str | None = None
    prompt: InputPrompt | None = None

    def is_available(self, system: System) -> bool:
        return self.available(system) if self.available else True

    def is_applied(self, probe: Probe, system: System) -> bool | None:
        """True when the task is already applied, None when it cannot be determined."""
        if self.detect is None:
            return None
        try:
            return self.detect(probe, system)
        except Exception:  # a probe must never take the interface down
            return None

    def requires_root(self) -> bool:
        return any(step.requires_root() for step in self.steps)

    def preview(self, system: System) -> list[str]:
        lines: list[str] = []
        for step in self.steps:
            lines.extend(step.preview(system))
        return lines

    async def run(self, ctx: ExecContext) -> None:
        ctx.current_task_id = self.id
        for step in self.steps:
            await step.run(ctx)
        if self.reboot:
            ctx.require_reboot()
