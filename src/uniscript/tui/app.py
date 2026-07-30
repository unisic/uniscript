"""Main uniscript window."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.markup import escape
from textual.widgets import (
    Button,
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    ProgressBar,
    RichLog,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from ..catalog import build_tasks, categories_of
from ..core.backup import BackupStore
from ..core.context import ExecContext
from ..core.privileges import PrivilegeManager
from ..core.probe import Probe
from ..core.runner import CommandFailed
from ..core.system import System
from ..core.tasks import Category, Risk, Task
from .screens import HelpScreen, PlanScreen, PromptScreen, SummaryScreen, SystemScreen

LOG_MAX_LINES = 2000

_LOG_STYLES = {
    "cmd": "bold cyan",
    "dry": "bold magenta",
    "warn": "yellow",
    "error": "bold red",
    "skip": "dim",
    "diff": "blue",
    "note": "green",
    "task": "bold",
    "ok": "bold green",
    "info": "",
}


class UniscriptApp(App[None]):
    """Pick and run configuration tasks."""

    CSS_PATH = "uniscript.tcss"
    TITLE = "uniscript"

    BINDINGS = [
        Binding("r", "start", "Run"),
        Binding("e", "preset_recommended", "Essentials"),
        Binding("g", "preset_gaming", "Gaming"),
        Binding("a", "select_category", "Whole category"),
        Binding("n", "clear_selection", "Deselect"),
        Binding("d", "toggle_dry_run", "Dry run"),
        Binding("s", "show_system", "System"),
        Binding("c", "clear_log", "Clear log", show=False),
        Binding("l", "toggle_console", "Log panel", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("escape", "abort", "Abort", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        system: System,
        probe: Probe,
        privileges: PrivilegeManager,
        backups: BackupStore,
        dry_run: bool = False,
    ) -> None:
        super().__init__()
        self.system = system
        self.probe = probe
        self.privileges = privileges
        self.backups = backups
        self.dry_run = dry_run
        self.tasks: list[Task] = build_tasks(system)
        self.categories: list[Category] = categories_of(self.tasks)
        self.selected: set[str] = set()
        self._applied: dict[str, bool | None] = {}
        self._current_category: Category | None = self.categories[0] if self.categories else None
        self._syncing = False
        self._busy = False
        self.sub_title = self._subtitle()

    def _subtitle(self) -> str:
        manager = self.system.package_manager.name if self.system.package_manager else "none"
        return f"{self.system.pretty_name}  |  {manager}  |  {len(self.tasks)} tasks"

    def format_title(self, title: str, sub_title: str) -> Content:
        """The default header joins the two with an em dash, the rest of the app uses a bar."""
        if not sub_title:
            return Content(title)
        return Content.assemble(
            Content(title),
            ("  |  ", "dim"),
            Content(sub_title).stylize("dim"),
        )

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static(self._facts_text(), id="facts")
                yield ListView(
                    *[
                        ListItem(Label(category.label), id=f"cat-{category.name}")
                        for category in self.categories
                    ],
                    id="categories",
                )
                with Vertical(id="actions"):
                    yield Button("Essentials", compact=True, id="btn-recommended")
                    yield Button("Gaming setup", compact=True, id="btn-gaming")
                    yield Button("Deselect all", compact=True, id="btn-clear")
                    yield Button("Run selected", compact=True, id="btn-run")
            with Vertical(id="workspace"):
                yield SelectionList[str](id="tasks")
                with VerticalScroll(id="detail"):
                    yield Static("", id="detail-content")
        with Vertical(id="console"):
            with Horizontal(id="statusbar"):
                yield Label("", id="status-mode")
                yield Label("", id="status-count")
                yield ProgressBar(total=100, show_eta=False, id="progress")
            yield RichLog(
                max_lines=LOG_MAX_LINES,
                wrap=True,
                markup=False,
                highlight=False,
                id="log",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#tasks", SelectionList).border_title = "Tasks"
        self.query_one("#detail", VerticalScroll).border_title = "Description"
        self.query_one("#log", RichLog).border_title = "Log"
        self.query_one("#categories", ListView).border_title = "Categories"
        self.query_one("#progress", ProgressBar).display = False
        self._refresh_task_list()
        self._refresh_status()
        self._log("info", f"uniscript: detected {self.system.pretty_name}")
        if self.privileges.backend == "none":
            self._log(
                "warn",
                "No sudo, no doas and not root. System tasks will not work.",
            )
        self._detect_applied()

    # --- detected task state --------------------------------------------------

    @work(thread=False, group="detect")
    async def _detect_applied(self) -> None:
        self._log("info", "checking what is already done")
        applied = await asyncio.to_thread(self._compute_applied)
        self._applied = applied
        done = sum(1 for value in applied.values() if value)
        self._log("info", f"already done: {done} of {len(self.tasks)}")
        self._refresh_task_list()
        self._refresh_status()

    def _compute_applied(self) -> dict[str, bool | None]:
        return {task.id: task.is_applied(self.probe, self.system) for task in self.tasks}

    # --- view -----------------------------------------------------------------

    def _facts_text(self) -> str:
        gpus = ", ".join(sorted(self.system.gpu_vendors)) or "no data"
        manager = self.system.package_manager.name if self.system.package_manager else "none"
        rows = [
            ("System", self.system.pretty_name),
            ("Packages", manager),
            ("Kernel", self.system.kernel),
            ("Graphics", gpus),
            ("Memory", f"{self.system.mem_total_gib:.1f} GiB"),
        ]
        return "\n".join(f"[$text-muted]{label:<8}[/] {escape(value)}" for label, value in rows)

    def _tasks_in_category(self) -> list[Task]:
        if self._current_category is None:
            return list(self.tasks)
        return [task for task in self.tasks if task.category is self._current_category]

    def _task_prompt(self, task: Task) -> str:
        applied = self._applied.get(task.id)
        if applied:
            marker = "[$text-success]done[/]"
        elif task.risk is Risk.HIGH:
            marker = "[$text-error]risk[/]"
        elif task.risk is Risk.MEDIUM:
            marker = "[$text-warning]care[/]"
        else:
            marker = "    "
        title = escape(task.title)
        if applied:
            title = f"[$text-muted]{title}[/]"
        return f"{marker}  {title}"

    def _refresh_task_list(self) -> None:
        widget = self.query_one("#tasks", SelectionList)
        visible = self._tasks_in_category()
        highlighted = widget.highlighted
        self._syncing = True
        try:
            widget.clear_options()
            widget.add_options(
                [
                    Selection(self._task_prompt(task), task.id, task.id in self.selected)
                    for task in visible
                ]
            )
        finally:
            self._syncing = False
        if visible:
            index = min(highlighted or 0, len(visible) - 1)
            widget.highlighted = index
            self._show_detail(visible[index])
        else:
            self._show_detail(None)

    def _show_detail(self, task: Task | None) -> None:
        target = self.query_one("#detail-content", Static)
        if task is None:
            target.update("[$text-muted]No tasks in this category.[/]")
            return

        lines = [f"[b]{escape(task.title)}[/]", ""]
        applied = self._applied.get(task.id)
        badges = [f"[$text-muted]risk:[/] {task.risk.label}"]
        if applied is True:
            badges.append("[$text-success]already applied[/]")
        elif applied is False:
            badges.append("[$text-muted]not applied[/]")
        if task.reboot:
            badges.append("[$text-warning]needs a reboot[/]")
        if task.default:
            badges.append("[$text-muted]in the essentials set[/]")
        if "gaming" in task.tags:
            badges.append("[$text-muted]in the gaming set[/]")
        lines.append("   ".join(badges))
        lines.append("")
        lines.append(escape(task.summary))

        if task.warning:
            lines.append("")
            lines.append(f"[$text-warning]Warning:[/] {escape(task.warning)}")

        if task.details:
            lines.append("")
            for detail in task.details:
                lines.append(f"  [$text-accent]-[/] {escape(detail)}")

        commands = task.preview(self.system)
        if commands:
            lines.append("")
            lines.append("[b]What will be done[/]")
            for command in commands:
                lines.append(f"  [$text-muted]{escape(command)}[/]")

        target.update("\n".join(lines))

    def _refresh_status(self) -> None:
        mode = self.query_one("#status-mode", Label)
        if self.dry_run:
            mode.update("[$text-warning]dry run[/]")
        else:
            mode.update("[$text-success]live mode[/]")
        pending = sum(1 for task in self.tasks if task.id in self.selected)
        self.query_one("#status-count", Label).update(f"selected: {pending}")

    def _log(self, level: str, message: str) -> None:
        try:
            widget = self.query_one("#log", RichLog)
        except Exception:
            return
        if level == "out":
            widget.write(Text(f"  {message}", style="dim"))
        else:
            widget.write(Text(message, style=_LOG_STYLES.get(level, "")))

    # --- events ---------------------------------------------------------------

    @on(ListView.Highlighted, "#categories")
    def _category_changed(self, event: ListView.Highlighted) -> None:
        if event.item is None or event.item.id is None:
            return
        name = event.item.id.removeprefix("cat-")
        self._current_category = Category[name]
        self._refresh_task_list()

    @on(SelectionList.SelectedChanged, "#tasks")
    def _selection_changed(self, event: SelectionList.SelectedChanged[str]) -> None:
        if self._syncing:
            return
        visible = {task.id for task in self._tasks_in_category()}
        chosen = set(event.selection_list.selected)
        self.selected -= visible - chosen
        self.selected |= chosen
        self._refresh_status()

    @on(SelectionList.SelectionHighlighted, "#tasks")
    def _selection_highlighted(self, event: SelectionList.SelectionHighlighted[str]) -> None:
        task = self._task_by_id(event.selection.value)
        if task is not None:
            self._show_detail(task)

    @on(Button.Pressed, "#btn-recommended")
    def _button_recommended(self) -> None:
        self.action_preset_recommended()

    @on(Button.Pressed, "#btn-gaming")
    def _button_gaming(self) -> None:
        self.action_preset_gaming()

    @on(Button.Pressed, "#btn-clear")
    def _button_clear(self) -> None:
        self.action_clear_selection()

    @on(Button.Pressed, "#btn-run")
    def _button_run(self) -> None:
        self.action_start()

    def _task_by_id(self, task_id: str) -> Task | None:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    # --- actions --------------------------------------------------------------

    def _apply_preset(self, ids: set[str], description: str) -> None:
        self.selected = ids
        self._refresh_task_list()
        self._refresh_status()
        self._log("info", f"{description}: {len(ids)} tasks selected")

    def action_preset_recommended(self) -> None:
        ids = {task.id for task in self.tasks if task.default and not self._applied.get(task.id)}
        self._apply_preset(ids, "essentials")

    def action_preset_gaming(self) -> None:
        ids = {
            task.id
            for task in self.tasks
            if (task.default or "gaming" in task.tags) and not self._applied.get(task.id)
        }
        self._apply_preset(ids, "gaming set")

    def action_select_category(self) -> None:
        ids = set(self.selected)
        ids |= {task.id for task in self._tasks_in_category() if not self._applied.get(task.id)}
        self._apply_preset(ids, "whole category")

    def action_clear_selection(self) -> None:
        self._apply_preset(set(), "selection cleared")

    def action_toggle_dry_run(self) -> None:
        if self._busy:
            self.notify("The mode cannot change while tasks are running.", severity="warning")
            return
        self.dry_run = not self.dry_run
        self._refresh_status()
        self._log("info", "dry run enabled" if self.dry_run else "live mode enabled")

    def action_toggle_console(self) -> None:
        console = self.query_one("#console", Vertical)
        console.display = not console.display

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_show_system(self) -> None:
        self.push_screen(SystemScreen(self.system))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_abort(self) -> None:
        if not self._busy:
            return
        for worker in self.workers:
            if worker.group == "run":
                worker.cancel()
        self._log("warn", "aborting the current work")

    def action_quit(self) -> None:
        if self._busy:
            self.notify("Tasks are running. Press Escape to abort, then q.", severity="warning")
            return
        self.exit()

    def action_start(self) -> None:
        if self._busy:
            self.notify("Tasks are already running.", severity="warning")
            return
        if not self.selected:
            self.notify("No task is selected.", severity="warning")
            return
        self._execute()

    # --- execution ------------------------------------------------------------

    async def _ask_inputs(self, tasks: list[Task]) -> tuple[list[Task], dict[str, str], list[str]]:
        planned: list[Task] = []
        inputs: dict[str, str] = {}
        skipped: list[str] = []
        for task in tasks:
            if task.prompt is None:
                planned.append(task)
                continue
            value = await self.push_screen_wait(PromptScreen(task, task.prompt))
            if value is None:
                skipped.append(task.title)
                continue
            inputs[task.id] = value
            planned.append(task)
        return planned, inputs, skipped

    def _prime_privileges(self) -> bool:
        argv = self.privileges.interactive_prime_command()
        try:
            with self.suspend():
                print()
                print("uniscript needs administrator privileges.")
                print("Enter the password once. The interface returns right after.")
                print()
                code = subprocess.call(argv)
        except Exception as exc:
            self._log("error", f"could not ask for the password: {exc}")
            return False
        return code == 0

    async def _interactive(self, argv: list[str], reason: str) -> int:
        printable = " ".join(shlex.quote(part) for part in argv)
        try:
            with self.suspend():
                print()
                print(reason)
                print(f"$ {printable}")
                print()
                code = subprocess.call(argv)
                input("\nPress Enter to return to uniscript. ")
        except Exception as exc:
            self._log("error", f"could not hand over the terminal: {exc}")
            return -1
        return code

    @work(group="run", exclusive=False)
    async def _execute(self) -> None:
        ordered = [task for task in self.tasks if task.id in self.selected]
        planned, inputs, skipped = await self._ask_inputs(ordered)
        if not planned:
            self._log("warn", "nothing left to do")
            return

        confirmed = await self.push_screen_wait(PlanScreen(planned, self.system, self.dry_run))
        if not confirmed:
            self._log("info", "cancelled")
            return

        needs_root = any(task.requires_root() for task in planned)
        if needs_root and not self.dry_run and self.privileges.backend in {"sudo", "doas"}:
            if not await self.privileges.is_primed():
                if not self._prime_privileges():
                    self._log("error", "no privileges, aborting")
                    return
            self.privileges.start_keepalive()

        ctx = ExecContext(
            system=self.system,
            probe=self.probe,
            privileges=self.privileges,
            backups=self.backups,
            dry_run=self.dry_run,
            sink=self._log,
            inputs=inputs,
            interactive=self._interactive,
        )

        done: list[str] = []
        done_ids: set[str] = set()
        failed: list[tuple[str, str]] = []
        progress = self.query_one("#progress", ProgressBar)
        self._busy = True
        self._set_controls_enabled(False)
        progress.display = True
        progress.update(total=len(planned), progress=0)

        try:
            for index, task in enumerate(planned, start=1):
                self._log("task", f"[{index}/{len(planned)}] {task.title}")
                try:
                    await task.run(ctx)
                except asyncio.CancelledError:
                    self._log("warn", f"aborted: {task.title}")
                    skipped.extend(item.title for item in planned[index - 1 :])
                    raise
                except CommandFailed as exc:
                    failed.append((task.title, str(exc)))
                    self._log("error", f"failed: {task.title}")
                    for line in exc.tail[-10:]:
                        self._log("error", f"  {line}")
                except Exception as exc:  # one task must not take the app down
                    failed.append((task.title, f"{type(exc).__name__}: {exc}"))
                    self._log("error", f"failed: {task.title}: {exc}")
                else:
                    done.append(task.title)
                    done_ids.add(task.id)
                    self._log("ok", f"done: {task.title}")
                finally:
                    progress.advance(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.privileges.stop_keepalive()
            self._busy = False
            self._set_controls_enabled(True)
            progress.display = False

        if not self.dry_run:
            self.probe.invalidate()
            self._applied = await asyncio.to_thread(self._compute_applied)
        self.selected -= done_ids
        self._refresh_task_list()
        self._refresh_status()

        await self.push_screen_wait(
            SummaryScreen(
                done=done,
                failed=failed,
                skipped=skipped,
                notes=list(ctx.notes),
                reboot=ctx.reboot_required,
                backups=self.backups.summary(),
                dry_run=self.dry_run,
            )
        )

    def _set_controls_enabled(self, enabled: bool) -> None:
        for button_id in ("#btn-recommended", "#btn-gaming", "#btn-clear", "#btn-run"):
            self.query_one(button_id, Button).disabled = not enabled
        self.query_one("#tasks", SelectionList).disabled = not enabled


def run_app(
    system: System,
    probe: Probe,
    privileges: PrivilegeManager,
    backup_root: Path,
    dry_run: bool,
) -> None:
    app = UniscriptApp(
        system=system,
        probe=probe,
        privileges=privileges,
        backups=BackupStore(backup_root),
        dry_run=dry_run,
    )
    app.run()
