"""Modal screens: plan, prompts, summary, help."""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ..core.system import System
from ..core.tasks import InputPrompt, Risk, Task


class PlanScreen(ModalScreen[bool]):
    """Preview of every command before anything runs."""

    BINDINGS = [
        Binding("escape", "dismiss(False)", "Cancel"),
        Binding("enter", "confirm", "Run", priority=True),
    ]

    def __init__(self, tasks: list[Task], system: System, dry_run: bool) -> None:
        super().__init__()
        self.tasks = tasks
        self.system = system
        self.dry_run = dry_run

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-dialog"):
            mode = (
                "DRY RUN, nothing will be changed"
                if self.dry_run
                else "LIVE, the system will be changed"
            )
            yield Label(f"Plan: {len(self.tasks)} tasks", id="plan-title")
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


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,enter,q,question_mark", "dismiss(None)", "Close")]

    HELP = """[b]Navigation[/]
  [$accent]arrows[/]          move through the list
  [$accent]space[/]           select or deselect a task
  [$accent]/[/]               filter the list, escape clears it
  [$accent]tab[/]             next panel
  [$accent]s[/]               details of the detected system
  [$accent]t[/]               switch between the dark and the light palette

[b]Selection[/]
  [$accent]e[/]               essentials set
  [$accent]g[/]               gaming set
  [$accent]a[/]               select the whole group the cursor is in
  [$accent]n[/]               deselect everything

[b]Running[/]
  [$accent]r[/]               show the plan and run
  [$accent]d[/]               toggle dry run
  [$accent]l[/]               show or hide the log
  [$accent]c[/]               clear the log
  [$accent]q[/]               quit

[b]Task markers[/]
  [$success]done[/]            detected as already applied
  [$warning]care[/]            changes system behaviour, read the description
  [$error]risk[/]            can break the system or weaken its security

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
