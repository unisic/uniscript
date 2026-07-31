"""Modal screens: plan, prompts, summary, help."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from ..core.backup import RestoreSession
from ..core.system import System
from ..core.tasks import InputPrompt, Risk, Task


class PlanScreen(ModalScreen[bool]):
    """Preview of every command before anything runs."""

    BINDINGS = [
        Binding("escape", "dismiss(False)", "Cancel"),
        Binding("enter", "confirm", "Run", priority=True),
    ]

    def __init__(
        self,
        tasks: list[Task],
        system: System,
        dry_run: bool,
        removals: list[Task] | None = None,
    ) -> None:
        super().__init__()
        self.tasks = tasks
        self.system = system
        self.dry_run = dry_run
        self.removals = removals or []

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-dialog"):
            mode = (
                "DRY RUN, nothing will be changed"
                if self.dry_run
                else "LIVE, the system will be changed"
            )
            yield Label(f"Plan: {len(self.tasks) + len(self.removals)} tasks", id="plan-title")
            yield Label(mode, id="plan-mode", classes="dry" if self.dry_run else "live")
            with VerticalScroll(id="plan-body"):
                yield Static(self._plan_text(), id="plan-content")
            with Horizontal(id="plan-buttons"):
                yield Button("Run", variant="success", id="plan-run")
                yield Button("Cancel", variant="default", id="plan-cancel")

    def _plan_text(self) -> str:
        lines: list[str] = []
        warnings = [task for task in self.tasks if task.warning]
        if warnings:
            lines.append("[b $warning]Warnings[/]")
            for task in warnings:
                lines.append(f"  [$warning]{task.title}[/]")
                lines.append(f"    {task.warning}")
            lines.append("")

        risky = [task for task in self.tasks if task.risk is Risk.HIGH]
        if risky:
            lines.append("[b $error]Tasks marked as risky[/]")
            for task in risky:
                lines.append(f"  [$error]{task.title}[/]")
            lines.append("")

        for index, task in enumerate(self.tasks, start=1):
            lines.append(f"[b]{index}. {task.title}[/]  [dim]({task.risk.label})[/]")
            for command in task.preview(self.system):
                lines.append(f"    [$text-muted]{escape(command)}[/]")
            lines.append("")

        if self.removals:
            lines.append("[b $error]Uninstall[/]")
            for index, task in enumerate(self.removals, start=len(self.tasks) + 1):
                lines.append(f"[b]{index}. Uninstall {task.title}[/]")
                for command in task.undo_preview(self.system):
                    lines.append(f"    [$text-muted]{escape(command)}[/]")
                lines.append("")

        if any(task.reboot for task in self.tasks):
            lines.append("[$warning]Some tasks require a reboot.[/]")
        return "\n".join(lines)

    @on(Button.Pressed, "#plan-run")
    def _run(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#plan-cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


class PromptScreen(ModalScreen[str | None]):
    """Asks for the single value a task needs."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, task: Task, prompt: InputPrompt) -> None:
        super().__init__()
        # The name "task" is taken by MessagePump.task.
        self.for_task = task
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-dialog"):
            yield Label(self.for_task.title, id="prompt-title")
            yield Label(self.prompt.label, id="prompt-label")
            yield Input(
                value=self.prompt.default,
                placeholder=self.prompt.placeholder,
                id="prompt-input",
            )
            yield Label("", id="prompt-error")
            with Horizontal(id="prompt-buttons"):
                yield Button("Confirm", variant="primary", id="prompt-ok")
                yield Button("Skip task", id="prompt-skip")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    @on(Input.Submitted, "#prompt-input")
    @on(Button.Pressed, "#prompt-ok")
    def _accept(self) -> None:
        value = self.query_one("#prompt-input", Input).value
        error = self.prompt.validate(value)
        if error:
            self.query_one("#prompt-error", Label).update(f"[$error]{error}[/]")
            return
        self.dismiss(value.strip())

    @on(Button.Pressed, "#prompt-skip")
    def _skip(self) -> None:
        self.dismiss(None)


class SummaryScreen(ModalScreen[None]):
    """The outcome: what passed, what failed, what needs a reboot."""

    BINDINGS = [Binding("escape,enter,space", "dismiss(None)", "Close")]

    def __init__(
        self,
        done: list[str],
        failed: list[tuple[str, str]],
        skipped: list[str],
        notes: list[str],
        reboot: bool,
        backups: str,
        dry_run: bool,
    ) -> None:
        super().__init__()
        self.done = done
        self.failed = failed
        self.skipped = skipped
        self.notes = notes
        self.reboot = reboot
        self.backups = backups
        self.dry_run = dry_run

    def compose(self) -> ComposeResult:
        with Vertical(id="summary-dialog"):
            yield Label("Summary", id="summary-title")
            with VerticalScroll(id="summary-body"):
                yield Static(self._text(), id="summary-content")
            yield Button("Close", variant="primary", id="summary-close")

    def _text(self) -> str:
        lines: list[str] = []
        if self.dry_run:
            lines.append("[$warning]Dry run: nothing was written.[/]")
            lines.append("")
        if self.failed:
            lines.append(f"[b $error]Failed ({len(self.failed)})[/]")
            for title, error in self.failed:
                lines.append(f"  [$error]{title}[/]")
                lines.append(f"    [dim]{escape(error)}[/]")
            lines.append("")
        if self.done:
            lines.append(f"[b $success]Applied ({len(self.done)})[/]")
            for title in self.done:
                lines.append(f"  {title}")
            lines.append("")
        if self.skipped:
            lines.append(f"[b]Skipped ({len(self.skipped)})[/]")
            for title in self.skipped:
                lines.append(f"  [dim]{title}[/]")
            lines.append("")
        if self.notes:
            lines.append("[b]Still to do[/]")
            for note in self.notes:
                lines.append(f"  [$accent]-[/] {note}")
            lines.append("")
        lines.append(f"[dim]Backups: {self.backups}[/]")
        if self.reboot:
            lines.append("")
            lines.append("[b $warning]A reboot is required.[/]")
        return "\n".join(lines)

    @on(Button.Pressed, "#summary-close")
    def _close(self) -> None:
        self.dismiss(None)


class SystemScreen(ModalScreen[None]):
    """The full list of detected facts about the machine."""

    BINDINGS = [Binding("escape,enter,q", "dismiss(None)", "Close")]

    def __init__(self, system: System) -> None:
        super().__init__()
        self.system = system

    def compose(self) -> ComposeResult:
        with Vertical(id="system-dialog"):
            yield Label("Detected system", id="system-title")
            with VerticalScroll(id="system-body"):
                yield Static(self._text(), id="system-content")
            yield Button("Close", variant="primary", id="system-close")

    def _text(self) -> str:
        lines = []
        for label, value in self.system.summary_rows():
            lines.append(f"[$text-muted]{label:<20}[/] {escape(value)}")
        lines.append("")
        lines.append("[b]Detected tools[/]")
        tools = ", ".join(sorted(self.system.tools)) or "none"
        lines.append(f"[dim]{tools}[/]")
        if self.system.gpus:
            lines.append("")
            lines.append("[b]Graphics cards[/]")
            for gpu in self.system.gpus:
                extra = ""
                if gpu.vendor == "nvidia":
                    extra = (
                        "  open kernel module: yes"
                        if gpu.open_kernel_module_capable
                        else "  open kernel module: no (chip older than Turing)"
                    )
                lines.append(
                    f"  [{gpu.pci_slot}] {escape(gpu.name)}  "
                    f"[dim]{gpu.vendor_id}:{gpu.device_id}, "
                    f"driver: {gpu.driver or 'none'}[/]{extra}"
                )
        return "\n".join(lines)

    @on(Button.Pressed, "#system-close")
    def _close(self) -> None:
        self.dismiss(None)


class RestoreScreen(ModalScreen[RestoreSession | None]):
    """Pick a backup session and revert the file changes it recorded."""

    BINDINGS = [Binding("escape,q", "dismiss(None)", "Close")]

    def __init__(self, sessions: list[RestoreSession]) -> None:
        super().__init__()
        self.sessions = sessions

    def compose(self) -> ComposeResult:
        with Vertical(id="restore-dialog"):
            yield Label("Restore a previous state", id="restore-title")
            yield Static(
                "Every run backs up the configuration files it changes. Pick a "
                "session to put those files back the way they were. Installed "
                "packages are not touched; uninstall applications from the list "
                "with the u key.",
                id="restore-intro",
            )
            yield OptionList(
                *(
                    Option(
                        f"{session.label}  [$text-muted]{len(session.files)} files[/]",
                        id=session.name,
                    )
                    for session in self.sessions
                ),
                id="restore-sessions",
            )
            yield Static("", id="restore-files")
            with Horizontal(id="restore-buttons"):
                yield Button("Restore this session", variant="primary", id="restore-go")
                yield Button("Cancel", id="restore-cancel")

    def on_mount(self) -> None:
        self.query_one("#restore-sessions", OptionList).highlighted = 0

    def _highlighted(self) -> RestoreSession | None:
        index = self.query_one("#restore-sessions", OptionList).highlighted
        if index is None:
            return None
        return self.sessions[index]

    @on(OptionList.OptionHighlighted, "#restore-sessions")
    def _session_highlighted(self) -> None:
        session = self._highlighted()
        if session is None:
            return
        shown = session.files[:12]
        lines = [f"  [$text-muted]{escape(path)}[/]" for path in shown]
        if len(session.files) > len(shown):
            lines.append(f"  [$text-muted]... and {len(session.files) - len(shown)} more[/]")
        if not lines:
            lines = ["  [$text-muted]The file list could not be read.[/]"]
        self.query_one("#restore-files", Static).update("\n".join(lines))

    @on(OptionList.OptionSelected, "#restore-sessions")
    @on(Button.Pressed, "#restore-go")
    def _go(self) -> None:
        self.dismiss(self._highlighted())

    @on(Button.Pressed, "#restore-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class WelcomeScreen(ModalScreen[None]):
    """Three steps for the first start; shown once, ? brings the full help."""

    BINDINGS = [Binding("escape,enter,q", "dismiss(None)", "Close")]

    TEXT = """[b]Welcome to uniscript[/]

It sets a fresh Linux up: repositories, drivers, codecs,
applications, gaming, tweaks. Nothing runs on its own.

  [b $accent]1.[/] Move with the arrows, tick tasks with [b]space[/].
  [b $accent]2.[/] Press [b]r[/] to see the exact commands.
  [b $accent]3.[/] Confirm, and only then anything runs.

Not sure yet? Press [b]d[/] first: dry run shows everything
and changes nothing. [b]e[/] ticks a sensible starter set.
Every file change is backed up; [b]b[/] restores, [b]u[/]
uninstalls an application, [b]?[/] shows all the keys."""

    def compose(self) -> ComposeResult:
        with Vertical(id="welcome-dialog"):
            yield Static(self.TEXT, id="welcome-content")
            yield Button("Got it", variant="primary", id="welcome-close")

    @on(Button.Pressed, "#welcome-close")
    def _close(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,enter,q,question_mark", "dismiss(None)", "Close")]

    HELP = """[b]First steps[/]
  Tick tasks with space, press r, read the plan, confirm. Nothing
  runs before the plan is confirmed. Dry run (d) prints every
  command and changes nothing, so it is the safe way to explore.

[b]Navigation[/]
  [$accent]up, down[/]        move through the list
  [$accent]right, enter[/]    open the group under the cursor; from the
                  category list, jump to the tasks
  [$accent]left[/]            back out of a group, then to the category list
  [$accent]space[/]           select or deselect a task
  [$accent]/[/]               search everywhere, escape clears it
  [$accent]f[/]               switch an application between Flatpak and the
                  system package (where both exist)
  [$accent]shift+arrows[/]    scroll the description (the wheel works too)
  [$accent]s[/]               details of the detected system
  [$accent]t[/]               switch between the dark and the light palette

[b]Selection[/]
  [$accent]e[/]               essentials set
  [$accent]g[/]               gaming set
  [$accent]a[/]               select or clear the whole group the cursor is in
  [$accent]n[/]               deselect everything

[b]Undoing things[/]
  [$accent]u[/]               mark an installed application to be uninstalled;
                  u again unmarks it, r runs it like any other task
  [$accent]b[/]               restore configuration files from a backup session,
                  the way back to the previous state

[b]Mouse[/]
  A click ticks a task, a click on a group row opens it, a click on a
  search result header ticks the whole group, and the buttons at the
  bottom mirror the r, d, e and g keys. The wheel scrolls the panel
  under the pointer. Inside tmux the mouse needs "set -g mouse on".

[b]Running[/]
  [$accent]r[/]               show the plan and run
  [$accent]d[/]               toggle dry run
  [$accent]l[/]               show or hide the log
  [$accent]c[/]               clear the log
  [$accent]q[/]               quit

[b]Task markers[/]
  [$success]✓[/]               detected as already applied
  [$warning]●[/]               changes system behaviour, read the description
  [$error]▲[/]               can break the system or weaken its security

[b]Safety[/]
  Every configuration file change is copied to a backup first, under
  ~/.local/share/uniscript/backups. The same directory gets a restore.sh
  script that reverts every change made in that session.

  Dry run shows the full list of commands and the file diffs without
  running anything."""

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Label("Help", id="help-title")
            with VerticalScroll(id="help-body"):
                yield Static(self.HELP, id="help-content")
            yield Button("Close", variant="primary", id="help-close")

    @on(Button.Pressed, "#help-close")
    def _close(self) -> None:
        self.dismiss(None)
