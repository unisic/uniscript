"""Main uniscript window."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path

from rich.segment import Segment
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.markup import escape
from textual.strip import Strip
from textual.theme import Theme
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    ProgressBar,
    RichLog,
    SelectionList,
    Static,
)
from textual.widgets.option_list import OptionDoesNotExist
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

# The value of a list entry that is a category header rather than a task.
_HEADER_PREFIX = "\x00header:"

# One palette, so a colour always means the same thing: green is done, amber is
# "changes system behaviour", red is "can break the system".
DARK_THEME = Theme(
    name="uniscript-dark",
    primary="#5a9fd4",
    secondary="#5fb3a1",
    accent="#7aa2f7",
    warning="#d9a441",
    error="#d96666",
    success="#7fb069",
    foreground="#c9ced8",
    background="#15181e",
    surface="#1c2028",
    panel="#242935",
    dark=True,
)

LIGHT_THEME = Theme(
    name="uniscript-light",
    primary="#2f6ea5",
    secondary="#2f7d6c",
    accent="#3b5fa8",
    warning="#9a6a12",
    error="#a83232",
    success="#3f7a2e",
    foreground="#1f2328",
    background="#fbfbfa",
    surface="#f2f2f0",
    panel="#e6e6e3",
    dark=False,
)


class TaskList(SelectionList[str]):
    """A selection list where category headers are rows without a checkbox.

    SelectionList draws a toggle button in front of every entry, including the
    disabled ones, so an unmodified header would read as an unticked task. Here
    the button is replaced by blank space of the same width, which keeps the
    titles of the tasks aligned.
    """

    def render_line(self, y: int) -> Strip:
        _, scroll_y = self.scroll_offset
        try:
            option = self.get_option_at_index(scroll_y + y)
        except OptionDoesNotExist:
            return super().render_line(y)
        if not str(option.value).startswith(_HEADER_PREFIX):
            return super().render_line(y)
        line = OptionList.render_line(self, y)
        segments = list(line)
        style = segments[0].style if segments else self.rich_style
        return Strip([Segment(" " * self._get_left_gutter_width(), style=style), *segments])


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
        Binding("slash", "search", "Search"),
        Binding("r", "start", "Run"),
        Binding("e", "preset_recommended", "Essentials"),
        Binding("g", "preset_gaming", "Gaming"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
        # The rest stay off the footer, which only has room for a handful.
        Binding("a", "select_category", "Select the whole group", show=False),
        Binding("n", "clear_selection", "Deselect everything", show=False),
        Binding("d", "toggle_dry_run", "Dry run", show=False),
        Binding("s", "show_system", "System", show=False),
        Binding("t", "switch_palette", "Light or dark palette", show=False),
        Binding("l", "toggle_console", "Log panel", show=False),
        Binding("c", "clear_log", "Clear log", show=False),
        Binding("escape", "abort", "Abort", show=False),
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
        self._query = ""
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
        with Vertical(id="workspace"):
            with Horizontal(id="filterbar"):
                yield Input(placeholder="press / to filter the tasks", id="search")
                yield Label("", id="match-count")
            yield TaskList(id="tasks")
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
        self.register_theme(DARK_THEME)
        self.register_theme(LIGHT_THEME)
        self.theme = "uniscript-dark"
        tasks = self.query_one("#tasks", TaskList)
        tasks.border_title = "Tasks"
        # The keys that are not in the footer but are needed to pick anything.
        tasks.border_subtitle = "space toggles, a takes the whole group"
        self.query_one("#detail", VerticalScroll).border_title = "Description"
        self.query_one("#log", RichLog).border_title = "Log"
        self.query_one("#progress", ProgressBar).display = False
        # The log only earns its screen space once something is actually running.
        self.query_one("#console", Vertical).display = False
        self.query_one("#tasks", TaskList).focus()
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

    def _matches(self, task: Task) -> bool:
        if not self._query:
            return True
        haystack = " ".join(
            (task.title, task.summary, task.id, task.category.label, " ".join(task.tags))
        ).lower()
        return all(word in haystack for word in self._query.lower().split())

    def _visible_tasks(self) -> list[Task]:
        """Tasks passing the filter, in catalogue order so categories stay grouped."""
        return [task for task in self.tasks if self._matches(task)]

    def _category_of_highlight(self) -> Category | None:
        widget = self.query_one("#tasks", TaskList)
        index = widget.highlighted
        if index is None:
            return None
        try:
            value = widget.get_option_at_index(index).value
        except Exception:
            return None
        if isinstance(value, str) and value.startswith(_HEADER_PREFIX):
            return Category.__members__.get(value.removeprefix(_HEADER_PREFIX))
        task = self._task_by_id(value) if isinstance(value, str) else None
        return task.category if task else None

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
        widget = self.query_one("#tasks", TaskList)
        visible = self._visible_tasks()
        previous = widget.highlighted
        rows: list[Task | None] = []
        entries: list[Selection[str]] = []
        current: Category | None = None
        for task in visible:
            if task.category is not current:
                if current is not None:
                    # A blank line between the groups, so a long list still reads
                    # as sections rather than as one wall of titles.
                    entries.append(
                        Selection("", f"{_HEADER_PREFIX}gap:{task.category.name}", False, disabled=True)
                    )
                    rows.append(None)
                current = task.category
                entries.append(
                    Selection(
                        f"[b $text-accent]{escape(current.label.upper())}[/]",
                        f"{_HEADER_PREFIX}{current.name}",
                        False,
                        disabled=True,
                    )
                )
                rows.append(None)
            entries.append(Selection(self._task_prompt(task), task.id, task.id in self.selected))
            rows.append(task)

        self._syncing = True
        try:
            widget.clear_options()
            widget.add_options(entries)
        finally:
            self._syncing = False

        self._refresh_match_count(len(visible))
        selectable = [index for index, task in enumerate(rows) if task is not None]
        if not selectable:
            self._show_detail(None)
            return
        target = min(previous or selectable[0], len(rows) - 1)
        if rows[target] is None:
            # Landed on a header, take the nearest real task below it.
            target = next((index for index in selectable if index >= target), selectable[0])
        widget.highlighted = target
        self._show_detail(rows[target])

    def _refresh_match_count(self, matching: int) -> None:
        # The log panel is hidden until something runs, so the count of what is
        # selected has to live here, where it is always visible.
        parts = [f"[$text-accent]{len(self.selected)}[/] selected"]
        if self._query:
            parts.append(f"[$text-muted]{matching} of {len(self.tasks)} match[/]")
            parts.append("[$text-muted]escape clears[/]")
        else:
            parts.append(f"[$text-muted]{len(self.tasks)} tasks[/]")
            done = sum(1 for value in self._applied.values() if value)
            if done:
                parts.append(f"[$text-muted]{done} already done[/]")
        self.query_one("#match-count", Label).update("   ".join(parts))

    def _show_detail(self, task: Task | None) -> None:
        target = self.query_one("#detail-content", Static)
        if task is None:
            if self._query:
                target.update(
                    f"[$text-muted]Nothing matches [/][b]{escape(self._query)}[/]"
                    "[$text-muted]. Press escape to clear the filter.[/]"
                )
            else:
                target.update("[$text-muted]No tasks are available for this system.[/]")
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
        self._refresh_match_count(len(self._visible_tasks()))

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

    @on(SelectionList.SelectedChanged, "#tasks")
    def _selection_changed(self, event: SelectionList.SelectedChanged[str]) -> None:
        if self._syncing:
            return
        visible = {task.id for task in self._visible_tasks()}
        chosen = {
            value for value in event.selection_list.selected if not value.startswith(_HEADER_PREFIX)
        }
        self.selected -= visible - chosen
        self.selected |= chosen
        self._refresh_status()

    @on(SelectionList.SelectionHighlighted, "#tasks")
    def _selection_highlighted(self, event: SelectionList.SelectionHighlighted[str]) -> None:
        task = self._task_by_id(event.selection.value)
        if task is not None:
            self._show_detail(task)

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self._query = event.value.strip()
        self._refresh_task_list()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.query_one("#tasks", TaskList).focus()

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
        category = self._category_of_highlight()
        if category is None:
            return
        ids = set(self.selected)
        ids |= {
            task.id
            for task in self._visible_tasks()
            if task.category is category and not self._applied.get(task.id)
        }
        self._apply_preset(ids, f"whole group: {category.label}")

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

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

    def action_switch_palette(self) -> None:
        # Textual cannot ask the terminal for its background colour, so the
        # light palette needs a key of its own.
        self.theme = LIGHT_THEME.name if self.theme == DARK_THEME.name else DARK_THEME.name

    def action_show_system(self) -> None:
        self.push_screen(SystemScreen(self.system))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_abort(self) -> None:
        # Aborting a running job wins over clearing the filter: that is the
        # emergency use of this key.
        if self._busy:
            for worker in self.workers:
                if worker.group == "run":
                    worker.cancel()
            self._log("warn", "aborting the current work")
            return
        search = self.query_one("#search", Input)
        if search.has_focus or self._query:
            search.value = ""
            self.query_one("#tasks", TaskList).focus()

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
        # From here on there is output worth watching, so the log earns its place.
        self.query_one("#console", Vertical).display = True
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
        self.query_one("#tasks", TaskList).disabled = not enabled
        self.query_one("#search", Input).disabled = not enabled


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
