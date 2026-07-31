"""Main uniscript window."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from pathlib import Path

from rich.segment import Segment
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.strip import Strip
from textual.theme import Theme
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    OptionList,
    ProgressBar,
    RichLog,
    SelectionList,
    Static,
)
from textual.widgets.option_list import Option, OptionDoesNotExist
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

# List entries that are not tasks all start with NUL, which no task id does:
# group headers in search results, subcategory directories and the ".." row.
_HEADER_PREFIX = "\x00header:"
_GAP_PREFIX = "\x00gap:"
_DIR_PREFIX = "\x00dir:"
_UP_VALUE = "\x00up"

# The dark palette is Tokyo Night (folke/tokyonight.nvim, night variant):
# a near-black background, blue for the cursor and a ticked box, cyan for
# structure (brand, group labels, directory chevrons). Amber, red and green
# carry the task states: "changes behaviour", "can break things", "done".
DARK_THEME = Theme(
    name="uniscript-dark",
    primary="#7aa2f7",
    secondary="#bb9af7",
    accent="#7dcfff",
    warning="#e0af68",
    error="#f7768e",
    success="#9ece6a",
    foreground="#c0caf5",
    background="#16161e",
    surface="#1a1b26",
    panel="#24283b",
    dark=True,
    variables={
        "toggle-on": "#7aa2f7",
        "btn-bg": "#2f334d",
        "btn-hover": "#3b4261",
    },
)

# The same roles on the Tokyo Night day palette.
LIGHT_THEME = Theme(
    name="uniscript-light",
    primary="#2e7de9",
    secondary="#9854f1",
    accent="#007197",
    warning="#8c6c3e",
    error="#f52a65",
    success="#587539",
    foreground="#343b58",
    background="#e1e2e7",
    surface="#e9e9ed",
    panel="#d3d7e4",
    dark=False,
    variables={
        "toggle-on": "#2e7de9",
        "btn-bg": "#c8cede",
        "btn-hover": "#b9c1d6",
    },
)

# Sidebar captions; at most 12 cells, so a selected-count badge still fits
# next to the longest of them.
_SIDEBAR_LABELS = {
    Category.SYSTEM: "System",
    Category.REPOS: "Repositories",
    Category.DRIVERS: "Drivers",
    Category.MULTIMEDIA: "Multimedia",
    Category.PACKAGING: "Flatpak/Snap",
    Category.GAMING: "Gaming",
    Category.TWEAKS: "Tweaks",
    Category.SHELL: "Shell",
    Category.APPS: "Applications",
    Category.MAINTENANCE: "Maintenance",
}


def _parse_header(value: str) -> tuple[Category, str | None] | None:
    """The (category, subcategory) a search result header stands for."""
    name, _, subcategory = value.removeprefix(_HEADER_PREFIX).partition(":")
    category = Category.__members__.get(name)
    if category is None:
        return None
    return category, subcategory or None


class TaskList(SelectionList[str]):
    """A selection list where headers, directories and ".." have no checkbox.

    SelectionList draws a toggle button in front of every entry, so an
    unmodified header or directory row would read as an unticked task. Here
    the button is replaced by blank space of the same width, which keeps the
    titles of the tasks aligned. Directory rows and the ".." row stay enabled,
    so the cursor reaches them, but selecting one navigates instead of
    toggling a checkbox.
    """

    class HeaderClicked(Message):
        """A group header row was clicked with the mouse."""

        def __init__(self, category: Category, subcategory: str | None) -> None:
            super().__init__()
            self.category = category
            self.subcategory = subcategory

    class DirEntered(Message):
        """A subcategory directory row was activated."""

        def __init__(self, subcategory: str) -> None:
            super().__init__()
            self.subcategory = subcategory

    class WentUp(Message):
        """The ".." row was activated."""

    def _highlighted_value(self) -> str | None:
        if self.highlighted is None:
            return None
        try:
            return str(self.get_option_at_index(self.highlighted).value)
        except OptionDoesNotExist:
            return None

    def render_line(self, y: int) -> Strip:
        _, scroll_y = self.scroll_offset
        try:
            option = self.get_option_at_index(scroll_y + y)
        except OptionDoesNotExist:
            return super().render_line(y)
        if not str(option.value).startswith("\x00"):
            return super().render_line(y)
        line = OptionList.render_line(self, y)
        segments = list(line)
        style = segments[0].style if segments else self.rich_style
        return Strip([Segment(" " * self._get_left_gutter_width(), style=style), *segments])

    def action_select(self) -> None:
        # Space or enter on a directory descends into it and on ".." goes
        # back up; only real tasks fall through to the checkbox toggle.
        value = self._highlighted_value()
        if value == _UP_VALUE:
            self.post_message(self.WentUp())
            return
        if value is not None and value.startswith(_DIR_PREFIX):
            self.post_message(self.DirEntered(value.removeprefix(_DIR_PREFIX)))
            return
        super().action_select()

    def _on_click(self, event: events.Click) -> None:
        # OptionList drops clicks on disabled rows, but a click on a group
        # header should act on the whole group, like the a key does. The message
        # pump calls OptionList._on_click on its own for every click (private
        # handlers run per class), so this must never call super() itself.
        index = event.style.meta.get("option")
        if index is None:
            return
        try:
            value = str(self.get_option_at_index(index).value)
        except OptionDoesNotExist:
            return
        if not value.startswith(_HEADER_PREFIX):
            return
        parsed = _parse_header(value)
        if parsed is not None:
            event.stop()
            self.post_message(self.HeaderClicked(*parsed))


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
        Binding("ctrl+f", "search", "Search", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
        # Run, dry run and the presets are buttons on the action bar, and the
        # rest stays off the footer, which only has room for a handful.
        Binding("r", "start", "Run", show=False),
        Binding("e", "preset_recommended", "Essentials", show=False),
        Binding("g", "preset_gaming", "Gaming", show=False),
        Binding("a", "select_category", "Select the whole group", show=False),
        Binding("n", "clear_selection", "Deselect everything", show=False),
        Binding("d", "toggle_dry_run", "Dry run", show=False),
        Binding("s", "show_system", "System", show=False),
        Binding("t", "switch_palette", "Light or dark palette", show=False),
        Binding("shift+down", "scroll_detail(1)", "Scroll the description", show=False),
        Binding("shift+up", "scroll_detail(-1)", "Scroll the description", show=False),
        Binding("left", "nav_left", "Back", show=False),
        Binding("right", "nav_right", "Enter", show=False),
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
        self._active_category: Category | None = self.categories[0] if self.categories else None
        self._subcategory: str | None = None
        self._syncing = False
        self._busy = False
        # Before compose: the stylesheet uses variables these themes define.
        self.register_theme(DARK_THEME)
        self.register_theme(LIGHT_THEME)
        self.theme = "uniscript-dark"

    def _subtitle(self) -> str:
        manager = self.system.package_manager.name if self.system.package_manager else "none"
        return f"{self.system.pretty_name}  |  {manager}"

    def _sidebar_prompt(self, category: Category) -> str:
        label = _SIDEBAR_LABELS.get(category, category.label)
        chosen = sum(
            1 for task in self.tasks if task.category is category and task.id in self.selected
        )
        if chosen:
            return f"{label:<12} [$text-accent]{chosen:>2}[/]"
        return label

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Label("[$text-accent]▌[/]uniscript", id="brand")
            yield Input(placeholder="search the tasks  ( / )", id="search")
            yield Label("", id="match-count")
            yield Label(self._subtitle(), id="sysinfo")
        with Horizontal(id="workspace"):
            yield OptionList(
                *(
                    Option(self._sidebar_prompt(category), id=category.name)
                    for category in self.categories
                ),
                id="sidebar",
            )
            yield TaskList(id="tasks")
            with VerticalScroll(id="detail"):
                yield Static("", id="detail-content")
        with Horizontal(id="actionbar"):
            yield Button("Run selected (r)", id="act-run", compact=True)
            yield Button("Dry run: on (d)", id="act-dry", compact=True)
            yield Button("Essentials (e)", id="act-ess", compact=True)
            yield Button("Gaming (g)", id="act-gaming", compact=True)
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
        sidebar = self.query_one("#sidebar", OptionList)
        if self.categories:
            sidebar.highlighted = 0
        tasks = self.query_one("#tasks", TaskList)
        # The keys that are not in the footer but are needed to pick anything.
        tasks.border_subtitle = "space toggles, right enters, left backs out"
        detail = self.query_one("#detail", VerticalScroll)
        detail.border_title = "Description"
        detail.border_subtitle = (
            "[$text-warning]●[/] care  [$text-error]▲[/] risk  [$text-success]✓[/] done"
        )
        # A click on the description, the log or a button must not steal the
        # keyboard from the list: arrows always drive the focused list, left
        # and right move between the panes, the wheel scrolls what it hovers
        # over and shift+arrows scroll the description.
        detail.can_focus = False
        log = self.query_one("#log", RichLog)
        log.border_title = "Log"
        # The panel appears on its own with the first run, so the way to get
        # rid of it has to be written on the panel itself.
        log.border_subtitle = "l hides this panel, c clears it"
        log.can_focus = False
        for button in self.query("#actionbar Button"):
            button.can_focus = False
        self.query_one("#progress", ProgressBar).display = False
        # The log only earns its screen space once something is actually running.
        self.query_one("#console", Vertical).display = False
        self.query_one("#tasks", TaskList).focus()
        self._refresh_task_list()
        self._refresh_status()
        if os.environ.get("UNISCRIPT_DEBUG_FOCUS"):
            # Live view of who owns the keyboard, in the corner the brand
            # occupies; for chasing focus reports from a terminal.
            self.set_interval(0.5, self._debug_focus)
        self._log("info", f"uniscript: detected {self.system.pretty_name}")
        if self.privileges.backend == "none":
            self._log(
                "warn",
                "No sudo, no doas and not root. System tasks will not work.",
            )
        self._detect_applied()

    def _debug_focus(self) -> None:
        focused = self.screen.focused
        name = f"{type(focused).__name__}#{focused.id}" if focused is not None else "None"
        self.query_one("#brand", Label).update(
            f"focus={name} app={'on' if self.app_focus else 'OFF'}"
        )

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
        """The tasks shown as rows right now: search results, or one level."""
        if self._query:
            return [task for task in self.tasks if self._matches(task)]
        if self._active_category is None:
            return []
        in_category = [task for task in self.tasks if task.category is self._active_category]
        if self._subcategory is not None:
            return [task for task in in_category if task.subcategory == self._subcategory]
        return [task for task in in_category if task.subcategory is None]

    def _group_of_highlight(self) -> tuple[Category, str | None] | None:
        """The (category, subcategory) group the cursor is in."""
        value = self.query_one("#tasks", TaskList)._highlighted_value()
        if value is None or value == _UP_VALUE:
            return None
        if value.startswith(_DIR_PREFIX):
            if self._active_category is None:
                return None
            return self._active_category, value.removeprefix(_DIR_PREFIX)
        task = self._task_by_id(value)
        if task is None:
            return None
        return task.category, task.subcategory

    def _task_prompt(self, task: Task) -> str:
        # One-character flags keep the titles in a straight column; the legend
        # sits under the description panel and in the help.
        applied = self._applied.get(task.id)
        if applied:
            marker = "[$text-success]✓[/]"
        elif task.risk is Risk.HIGH:
            marker = "[$text-error]▲[/]"
        elif task.risk is Risk.MEDIUM:
            marker = "[$text-warning]●[/]"
        else:
            marker = " "
        title = escape(task.title)
        if applied:
            title = f"[$text-muted]{title}[/]"
        return f"{marker} {title}"

    def _search_entries(self) -> list[Selection[str]]:
        """Search results as one flat list with a header per group."""
        visible = self._visible_tasks()
        counts: dict[tuple[Category, str | None], int] = {}
        for task in visible:
            key = (task.category, task.subcategory)
            counts[key] = counts.get(key, 0) + 1
        entries: list[Selection[str]] = []
        current: tuple[Category, str | None] | None = None
        for task in visible:
            key = (task.category, task.subcategory)
            if key != current:
                value = f"{_HEADER_PREFIX}{task.category.name}:{task.subcategory or ''}"
                if current is not None:
                    # A blank line between the groups, so a long list still reads
                    # as sections rather than as one wall of titles.
                    entries.append(Selection("", f"{_GAP_PREFIX}{value}", False, disabled=True))
                current = key
                label = task.category.label.upper()
                if task.subcategory:
                    label += f" › {task.subcategory.upper()}"
                entries.append(
                    Selection(
                        f"[b $text-accent]{escape(label)}[/]  [$text-muted]{counts[key]}[/]",
                        value,
                        False,
                        disabled=True,
                    )
                )
            entries.append(Selection(self._task_prompt(task), task.id, task.id in self.selected))
        return entries

    def _dir_prompt(self, subcategory: str, tasks: list[Task]) -> str:
        chosen = sum(1 for task in tasks if task.id in self.selected)
        prompt = f"[$text-accent]▸[/] [b]{escape(subcategory)}[/]  [$text-muted]{len(tasks)}[/]"
        if chosen:
            prompt += f"  [$text-accent]{chosen} ✓[/]"
        return prompt

    def _level_entries(self) -> list[Selection[str]]:
        """One directory level of the active category, linutil style."""
        if self._active_category is None:
            return []
        in_category = [task for task in self.tasks if task.category is self._active_category]
        # Ordered as in the catalogue.
        subcategories: list[str] = []
        for task in in_category:
            if task.subcategory and task.subcategory not in subcategories:
                subcategories.append(task.subcategory)
        if self._subcategory is not None and self._subcategory not in subcategories:
            self._subcategory = None
        entries: list[Selection[str]] = []
        if self._subcategory is None:
            for subcategory in subcategories:
                inside = [task for task in in_category if task.subcategory == subcategory]
                entries.append(
                    Selection(
                        self._dir_prompt(subcategory, inside),
                        f"{_DIR_PREFIX}{subcategory}",
                        False,
                    )
                )
            for task in in_category:
                if task.subcategory is None:
                    entries.append(
                        Selection(self._task_prompt(task), task.id, task.id in self.selected)
                    )
        else:
            entries.append(Selection("[$text-muted]‹ ..[/]", _UP_VALUE, False))
            for task in in_category:
                if task.subcategory == self._subcategory:
                    entries.append(
                        Selection(self._task_prompt(task), task.id, task.id in self.selected)
                    )
        return entries

    def _refresh_task_list(self) -> None:
        widget = self.query_one("#tasks", TaskList)
        previous = widget.highlighted
        entries = self._search_entries() if self._query else self._level_entries()

        self._syncing = True
        try:
            widget.clear_options()
            widget.add_options(entries)
        finally:
            self._syncing = False

        self._refresh_breadcrumb()
        self._refresh_match_count(len(self._visible_tasks()))
        reachable = [index for index, entry in enumerate(entries) if not entry.disabled]
        if not reachable:
            self._show_detail(None)
            return
        target = min(previous if previous is not None else reachable[0], len(entries) - 1)
        if entries[target].disabled:
            # Landed on a header, take the nearest reachable row below it.
            target = next((index for index in reachable if index >= target), reachable[0])
        widget.highlighted = target
        self._show_detail_for_value(str(entries[target].value))

    def _refresh_breadcrumb(self) -> None:
        widget = self.query_one("#tasks", TaskList)
        if self._query:
            widget.border_title = "Search results"
        elif self._active_category is None:
            widget.border_title = "Tasks"
        elif self._subcategory is not None:
            widget.border_title = f"{self._active_category.label} › {self._subcategory}"
        else:
            widget.border_title = self._active_category.label

    def _refresh_match_count(self, matching: int) -> None:
        # The selected count lives on the run button, so this stays about the view.
        if self._query:
            text = (
                f"[$text-accent]{matching}[/] of {len(self.tasks)} match"
                "   [$text-muted]escape clears[/]"
            )
        else:
            done = sum(1 for value in self._applied.values() if value)
            text = f"[$text-muted]{len(self.tasks)} tasks, {done} already done[/]"
        self.query_one("#match-count", Label).update(text)

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
        # The same glyphs and colours the list uses, so the flag on a row and
        # the badge in its description read as one thing.
        if task.risk is Risk.HIGH:
            badges = ["[b $text-error]▲ risky[/]"]
        elif task.risk is Risk.MEDIUM:
            badges = ["[$text-warning]● needs care[/]"]
        else:
            badges = ["[$text-muted]safe[/]"]
        if task.reboot:
            badges.append("[$text-warning]needs a reboot[/]")
        if applied is True:
            badges.append("[$text-success]✓ already applied[/]")
        if task.default:
            badges.append("[$text-muted]essentials[/]")
        if "gaming" in task.tags:
            badges.append("[$text-muted]gaming[/]")
        lines.append("[$text-muted]  ·  [/]".join(badges))
        lines.append("")
        lines.append(escape(task.summary))

        if task.warning:
            lines.append("")
            lines.append("[b $text-warning]▲ Warning[/]")
            lines.append(escape(task.warning))

        if task.details:
            lines.append("")
            for detail in task.details:
                lines.append(f"  [$text-accent]•[/] {escape(detail)}")

        commands = task.preview(self.system)
        if commands:
            lines.append("")
            lines.append("[b]What will be done[/]")
            # Not every preview line is a shell command (some are notes or
            # file writes), so the marker is a neutral arrow rather than $.
            for command in commands:
                lines.append(f"  [$text-muted]›[/] {escape(command)}")

        target.update("\n".join(lines))

    def _show_dir_detail(self, subcategory: str) -> None:
        target = self.query_one("#detail-content", Static)
        tasks = [
            task
            for task in self.tasks
            if task.category is self._active_category and task.subcategory == subcategory
        ]
        lines = [f"[b]{escape(subcategory)}[/]", ""]
        lines.append(
            f"[$text-muted]{len(tasks)} applications. Press right or enter to open "
            "the group, a selects all of it.[/]"
        )
        lines.append("")
        for task in tasks:
            marker = "[$text-success]✓[/]" if self._applied.get(task.id) else " "
            title = escape(task.title)
            if task.id in self.selected:
                title = f"[b]{title}[/]"
            lines.append(f"  {marker} {title}[$text-muted]: {escape(task.summary)}[/]")
        target.update("\n".join(lines))

    def _show_detail_for_value(self, value: str) -> None:
        if value == _UP_VALUE and self._active_category is not None:
            self.query_one("#detail-content", Static).update(
                f"[$text-muted]Back to {escape(self._active_category.label)}.[/]"
            )
        elif value.startswith(_DIR_PREFIX):
            self._show_dir_detail(value.removeprefix(_DIR_PREFIX))
        else:
            self._show_detail(self._task_by_id(value))

    # --- directory navigation -------------------------------------------------

    def _enter_dir(self, subcategory: str) -> None:
        self._subcategory = subcategory
        self._refresh_task_list()
        widget = self.query_one("#tasks", TaskList)
        # Past the ".." row, straight onto the first application.
        if widget.option_count > 1:
            widget.highlighted = 1

    def _go_up(self) -> None:
        left = self._subcategory
        self._subcategory = None
        self._refresh_task_list()
        if left is None:
            return
        widget = self.query_one("#tasks", TaskList)
        for index in range(widget.option_count):
            if str(widget.get_option_at_index(index).value) == f"{_DIR_PREFIX}{left}":
                widget.highlighted = index
                break

    def _refresh_status(self) -> None:
        mode = self.query_one("#status-mode", Label)
        if self.dry_run:
            mode.update("[$text-warning]dry run[/]")
        else:
            mode.update("[$text-success]live mode[/]")
        pending = sum(1 for task in self.tasks if task.id in self.selected)
        self.query_one("#status-count", Label).update(f"selected: {pending}")
        dry = self.query_one("#act-dry", Button)
        dry.label = f"Dry run: {'on' if self.dry_run else 'OFF'} (d)"
        dry.set_class(not self.dry_run, "-live")
        run = self.query_one("#act-run", Button)
        run.label = f"▶ Run {pending} (r)"
        # Nothing selected means nothing to run; a lit green button would lie.
        run.disabled = pending == 0 or self._busy
        sidebar = self.query_one("#sidebar", OptionList)
        for category in self.categories:
            sidebar.replace_option_prompt(category.name, self._sidebar_prompt(category))
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
            value for value in event.selection_list.selected if not value.startswith("\x00")
        }
        self.selected -= visible - chosen
        self.selected |= chosen
        self._refresh_status()

    @on(SelectionList.SelectionHighlighted, "#tasks")
    def _selection_highlighted(self, event: SelectionList.SelectionHighlighted[str]) -> None:
        self._show_detail_for_value(str(event.selection.value))

    @on(OptionList.OptionHighlighted, "#sidebar")
    def _category_changed(self, event: OptionList.OptionHighlighted) -> None:
        category = Category.__members__.get(event.option.id or "")
        if category is None or category is self._active_category:
            return
        self._active_category = category
        self._subcategory = None
        if self._query:
            # Picking a category leaves the search; clearing the input refreshes.
            self.query_one("#search", Input).value = ""
            return
        self._refresh_task_list()

    @on(OptionList.OptionSelected, "#sidebar")
    def _category_selected(self, event: OptionList.OptionSelected) -> None:
        # Enter on the category list hands the keyboard to the tasks,
        # the way l and Right do in linutil.
        self.query_one("#tasks", TaskList).focus()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        self._query = event.value.strip()
        self._refresh_task_list()

    @on(Input.Submitted, "#search")
    def _search_submitted(self) -> None:
        self.query_one("#tasks", TaskList).focus()

    @on(TaskList.HeaderClicked)
    def _header_clicked(self, event: TaskList.HeaderClicked) -> None:
        if not self._busy:
            self._toggle_group(event.category, event.subcategory)

    @on(TaskList.DirEntered)
    def _dir_entered(self, event: TaskList.DirEntered) -> None:
        if not self._busy:
            self._enter_dir(event.subcategory)

    @on(TaskList.WentUp)
    def _went_up(self, event: TaskList.WentUp) -> None:
        if not self._busy:
            self._go_up()

    @on(Button.Pressed, "#act-run")
    def _button_run(self) -> None:
        self.action_start()

    @on(Button.Pressed, "#act-dry")
    def _button_dry(self) -> None:
        self.action_toggle_dry_run()

    @on(Button.Pressed, "#act-ess")
    def _button_essentials(self) -> None:
        self.action_preset_recommended()

    @on(Button.Pressed, "#act-gaming")
    def _button_gaming(self) -> None:
        self.action_preset_gaming()

    def on_key(self, event: events.Key) -> None:
        # A focus-out from the terminal makes Textual drop the focus, and if
        # the matching focus-in never arrives the keyboard stays dead. Any key
        # that reaches the app with nothing focused puts the list back in
        # charge and then runs normally, so one lost handshake costs nothing.
        if self.screen.focused is None and not self._busy:
            self.query_one("#tasks", TaskList).focus()
            key = event.key
            event.stop()
            # Without this the app's own binding for the key would fire here
            # AND from the simulation below, so a toggle would toggle twice.
            event.prevent_default()
            self.call_later(self.simulate_key, key)
            return
        # Arrows in the filter field jump to the list, so typing a filter and
        # moving through the matches is one motion without enter in between.
        search = self.query_one("#search", Input)
        if search.has_focus and event.key in ("down", "up"):
            event.stop()
            self.query_one("#tasks", TaskList).focus()

    def on_click(self, event: events.Click) -> None:
        # The system summary in the corner opens the same details as the s key.
        widget = event.widget
        if widget is not None and widget.id == "sysinfo":
            self.action_show_system()

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
        self.notify(
            f"Essentials: {len(ids)} tasks selected. Press r or click Run selected.",
            timeout=4,
        )

    def action_preset_gaming(self) -> None:
        ids = {
            task.id
            for task in self.tasks
            if (task.default or "gaming" in task.tags) and not self._applied.get(task.id)
        }
        self._apply_preset(ids, "gaming set")
        self.notify(
            f"Gaming set: {len(ids)} tasks selected. Press r or click Run selected.",
            timeout=4,
        )

    def _toggle_group(self, category: Category, subcategory: str | None) -> None:
        """Select the whole group, or clear it when it is already selected."""
        label = f"{category.label} › {subcategory}" if subcategory else category.label
        ids = {
            task.id
            for task in self.tasks
            if task.category is category
            and task.subcategory == subcategory
            and self._matches(task)
            and not self._applied.get(task.id)
        }
        if not ids:
            return
        if ids <= self.selected:
            self._apply_preset(self.selected - ids, f"group cleared: {label}")
        else:
            self._apply_preset(self.selected | ids, f"whole group: {label}")

    def action_select_category(self) -> None:
        group = self._group_of_highlight()
        if group is not None:
            self._toggle_group(*group)

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_nav_left(self) -> None:
        # The way back: out of a subcategory first, then onto the sidebar.
        if self._busy:
            return
        tasks = self.query_one("#tasks", TaskList)
        if not tasks.has_focus:
            return
        if self._subcategory is not None and not self._query:
            self._go_up()
        else:
            self.query_one("#sidebar", OptionList).focus()

    def action_nav_right(self) -> None:
        # The way in: from the sidebar to the list, from a directory row into it.
        if self._busy:
            return
        sidebar = self.query_one("#sidebar", OptionList)
        if sidebar.has_focus:
            self.query_one("#tasks", TaskList).focus()
            return
        tasks = self.query_one("#tasks", TaskList)
        if not tasks.has_focus:
            return
        value = tasks._highlighted_value()
        if value is not None and value.startswith(_DIR_PREFIX):
            self._enter_dir(value.removeprefix(_DIR_PREFIX))

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

    def action_scroll_detail(self, direction: int) -> None:
        detail = self.query_one("#detail", VerticalScroll)
        if direction > 0:
            detail.scroll_down()
        else:
            detail.scroll_up()

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
        self.query_one("#sidebar", OptionList).disabled = not enabled
        for button in self.query("#actionbar Button"):
            button.disabled = not enabled


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
