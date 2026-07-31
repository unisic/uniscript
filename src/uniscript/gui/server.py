"""The local HTTP server behind the browser GUI.

Serves a single page application from app.html and a small JSON API over the
same core the TUI uses. Bound to 127.0.0.1 only, and every request has to
carry the token generated at start, so neither another local user nor a web
page the browser happens to have open can reach the API.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shlex
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from ..catalog import build_tasks, categories_of, quick_setup_ids
from ..catalog.common import SourceInstall
from ..core.backup import BackupStore
from ..core.context import ExecContext
from ..core.privileges import PrivilegeManager
from ..core.probe import Probe
from ..core.runner import CommandFailed
from ..core.system import System, detect_system
from ..core.tasks import Task

LOG_MAX_LINES = 5000

COPR_API = "https://copr.fedorainfracloud.org/api_3"


def _json_get(url: str) -> dict:
    """One bounded request to a public package API, errors become JSON."""
    try:
        # The COPR search endpoint routinely needs more than ten seconds.
        with urllib.request.urlopen(url, timeout=25) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"error": f"the service did not answer: {exc}"}


def _copr_get(path: str, params: dict[str, str]) -> dict:
    return _json_get(f"{COPR_API}/{path}?{urlencode(params)}")


def _copr_search(query: str) -> dict:
    if not query:
        return {"items": []}
    data = _copr_get("project/search", {"query": query})
    if "error" in data:
        return data
    return {
        "items": [
            {
                "full_name": item.get("full_name", ""),
                "description": (item.get("description") or "").strip()[:120],
            }
            for item in data.get("items", [])[:15]
        ]
    }


def _copr_packages(owner: str, project: str) -> dict:
    if not owner or not project:
        return {"items": []}
    data = _copr_get("package/list/", {"ownername": owner, "projectname": project})
    if "error" in data:
        return data
    return {"items": [item.get("name", "") for item in data.get("items", [])[:100]]}


def _aur_search(query: str) -> dict:
    if not query:
        return {"items": []}
    data = _json_get(f"https://aur.archlinux.org/rpc/v5/search/{quote(query)}")
    if "error" in data:
        return data
    results = sorted(
        data.get("results", []), key=lambda item: item.get("Popularity") or 0, reverse=True
    )
    return {
        "items": [
            {
                "name": item.get("Name", ""),
                "description": (item.get("Description") or "").strip()[:120],
            }
            for item in results[:20]
        ]
    }


class LogRing:
    """A bounded log the browser polls by absolute index."""

    def __init__(self) -> None:
        self._lines: deque[dict[str, str]] = deque(maxlen=LOG_MAX_LINES)
        self._dropped = 0
        self._lock = threading.Lock()

    def append(self, level: str, text: str) -> None:
        with self._lock:
            if len(self._lines) == self._lines.maxlen:
                self._dropped += 1
            self._lines.append({"level": level, "text": text})

    def since(self, index: int) -> tuple[list[dict[str, str]], int]:
        with self._lock:
            start = max(index - self._dropped, 0)
            lines = list(self._lines)[start:]
            return lines, self._dropped + len(self._lines)


class RunManager:
    """One run at a time, in its own thread with its own asyncio loop."""

    def __init__(self, server: GuiServer) -> None:
        self.server = server
        self.log = LogRing()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self.status: dict = {"phase": "idle"}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, tasks: list[Task], inputs: dict[str, str], dry_run: bool) -> str | None:
        """Begin a run; returns an error message instead when it cannot."""
        with self._lock:
            if self.running:
                return "A run is already in progress."
            needs_root = any(task.requires_root() for task in tasks)
            privileges = self.server.privileges
            if needs_root and not dry_run and privileges.backend == "none":
                return "No sudo, no doas and not root: system tasks cannot run."
            if (
                needs_root
                and not dry_run
                and privileges.backend in {"sudo", "doas"}
                and not sys.stdin.isatty()
                and not asyncio.run(privileges.is_primed())
            ):
                return (
                    "Administrator privileges are not primed and there is no terminal "
                    "to ask on. Start uniscript-gui from a terminal, or run sudo -v first."
                )
            self.status = {
                "phase": "running",
                "dry_run": dry_run,
                "index": 0,
                "total": len(tasks),
                "current": "",
                "done": [],
                "failed": [],
                "skipped": [],
                "notes": [],
                "reboot": False,
                "backups": "",
            }
            self._thread = threading.Thread(
                target=self._worker, args=(tasks, inputs, dry_run), daemon=True
            )
            self._thread.start()
            return None

    def abort(self) -> None:
        loop, task = self._loop, self._task
        if self.running and loop is not None and task is not None:
            self.log.append("warn", "aborting the current work")
            loop.call_soon_threadsafe(task.cancel)

    def _worker(self, tasks: list[Task], inputs: dict[str, str], dry_run: bool) -> None:
        privileges = self.server.privileges
        needs_root = any(task.requires_root() for task in tasks)
        keepalive = False
        try:
            if needs_root and not dry_run and privileges.backend in {"sudo", "doas"}:
                if not asyncio.run(privileges.is_primed()):
                    self.status["phase"] = "priming"
                    self.log.append("warn", "Enter the administrator password in the terminal.")
                    print("\nuniscript needs administrator privileges.", flush=True)
                    if subprocess.call(privileges.interactive_prime_command()) != 0:
                        self.log.append("error", "no privileges, aborting")
                        return
                keepalive = True
            self.status["phase"] = "running"
            asyncio.run(self._run(tasks, inputs, dry_run, keepalive))
        finally:
            self._loop = None
            self._task = None
            if not dry_run:
                self.server.probe.invalidate()
            self.server.refresh_applied()
            self.status["phase"] = "done"

    async def _run(
        self, tasks: list[Task], inputs: dict[str, str], dry_run: bool, keepalive: bool
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        if keepalive:
            # create_task needs the running loop, so this cannot happen in
            # _worker before asyncio.run; that is exactly the crash it caused.
            self.server.privileges.start_keepalive()

        async def interactive(argv: list[str], reason: str) -> int:
            # The step needs a real terminal; the one the server was started
            # from is still attached, so the prompt lands there.
            if not sys.stdin.isatty():
                self.log.append("error", "this step needs a terminal and there is none")
                return -1
            self.log.append("warn", f"{reason}: continue in the terminal")
            print(f"\n{reason}")
            print(f"$ {' '.join(shlex.quote(part) for part in argv)}\n", flush=True)
            return await asyncio.to_thread(subprocess.call, argv)

        ctx = ExecContext(
            system=self.server.system,
            probe=self.server.probe,
            privileges=self.server.privileges,
            backups=self.server.backups,
            dry_run=dry_run,
            sink=self.log.append,
            inputs=inputs,
            interactive=interactive,
        )
        status = self.status
        try:
            for index, task in enumerate(tasks, start=1):
                status["index"] = index
                status["current"] = task.title
                self.log.append("task", f"[{index}/{len(tasks)}] {task.title}")
                try:
                    await task.run(ctx)
                except asyncio.CancelledError:
                    self.log.append("warn", f"aborted: {task.title}")
                    status["skipped"] = [item.title for item in tasks[index - 1 :]]
                    break
                except CommandFailed as exc:
                    status["failed"].append({"title": task.title, "error": str(exc)})
                    self.log.append("error", f"failed: {task.title}")
                    for line in exc.tail[-10:]:
                        self.log.append("error", f"  {line}")
                except Exception as exc:  # one task must not take the run down
                    status["failed"].append(
                        {"title": task.title, "error": f"{type(exc).__name__}: {exc}"}
                    )
                    self.log.append("error", f"failed: {task.title}: {exc}")
                else:
                    status["done"].append(task.title)
                    self.log.append("ok", f"done: {task.title}")
        finally:
            status["notes"] = list(ctx.notes)
            status["reboot"] = ctx.reboot_required
            status["backups"] = self.server.backups.summary()
            await self.server.privileges.stop_keepalive()


class GuiServer:
    def __init__(self, system: System, dry_run: bool, backup_root: Path) -> None:
        self.system = system
        self.probe = Probe(system)
        self.privileges = PrivilegeManager(system)
        self.backups = BackupStore(backup_root)
        self.dry_run = dry_run
        self.tasks: list[Task] = build_tasks(system)
        self.applied: dict[str, bool | None] = {}
        self.applied_ready = False
        self.token = secrets.token_urlsafe(24)
        self.run_manager = RunManager(self)
        # The first probe reads the whole package list, which takes seconds;
        # the page shows up immediately and polls until this lands.
        threading.Thread(target=self.refresh_applied, daemon=True).start()

    def refresh_applied(self) -> None:
        self.applied = {
            task.id: task.is_applied(self.probe, self.system) for task in self.tasks
        }
        self.applied_ready = True

    def task_by_id(self, task_id: str) -> Task | None:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def state(self) -> dict:
        manager = self.system.package_manager.name if self.system.package_manager else "none"
        return {
            "system": {
                "pretty_name": self.system.pretty_name,
                "package_manager": manager,
                "rows": [[label, value] for label, value in self.system.summary_rows()],
                "privileges": self.privileges.backend,
            },
            "dry_run": self.dry_run,
            "detecting": not self.applied_ready,
            "quick": quick_setup_ids(self.tasks),
            "categories": [
                {"name": category.name, "label": category.label}
                for category in categories_of(self.tasks)
            ],
            "tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "summary": task.summary,
                    "category": task.category.name,
                    "subcategory": task.subcategory,
                    "risk": task.risk.name,
                    "risk_label": task.risk.label,
                    "applied": self.applied.get(task.id),
                    "default": task.default,
                    "gaming": "gaming" in task.tags,
                    "reboot": task.reboot,
                    "warning": task.warning,
                    "details": task.details,
                    "dual": any(isinstance(step, SourceInstall) for step in task.steps),
                    "preview": task.preview(self.system),
                    "prompt": (
                        {
                            "label": task.prompt.label,
                            "placeholder": task.prompt.placeholder,
                            "default": task.prompt.default,
                        }
                        if task.prompt
                        else None
                    ),
                }
                for task in self.tasks
            ],
        }

    def start_run(self, payload: dict) -> str | None:
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return "No task is selected."
        dry_run = bool(payload.get("dry_run"))
        raw_inputs = payload.get("inputs") or {}
        sources = payload.get("sources") or {}
        chosen: list[Task] = []
        inputs: dict[str, str] = {}
        for task in self.tasks:  # catalogue order, like the TUI
            if task.id not in ids:
                continue
            if task.prompt is not None:
                value = str(raw_inputs.get(task.id, "")).strip()
                error = task.prompt.validate(value)
                if error:
                    return f"{task.title}: {error}"
                inputs[task.id] = value
            if sources.get(task.id) == "native" and any(
                isinstance(step, SourceInstall) for step in task.steps
            ):
                inputs[task.id] = "native"
            chosen.append(task)
        if not chosen:
            return "None of the requested tasks exist."
        return self.run_manager.start(chosen, inputs, dry_run)


class _Handler(BaseHTTPRequestHandler):
    server_version = "uniscript-gui"
    gui: GuiServer  # set on the server class by serve()

    def log_message(self, format: str, *args) -> None:
        pass  # request logging would drown the terminal the sudo prompt uses

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        token = (query.get("token") or [""])[0]
        return secrets.compare_digest(token, self.gui.token)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            self._send(403, b"forbidden", "text/plain")
            return
        if url.path == "/":
            page = (Path(__file__).parent / "app.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self._send_json(self.gui.state())
        elif url.path == "/api/log":
            since = int((query.get("since") or ["0"])[0])
            lines, next_index = self.gui.run_manager.log.since(since)
            self._send_json(
                {"lines": lines, "next": next_index, "status": self.gui.run_manager.status}
            )
        elif url.path == "/api/copr/search":
            self._send_json(_copr_search((query.get("q") or [""])[0].strip()))
        elif url.path == "/api/aur/search":
            self._send_json(_aur_search((query.get("q") or [""])[0].strip()))
        elif url.path == "/api/copr/packages":
            self._send_json(
                _copr_packages(
                    (query.get("owner") or [""])[0].strip(),
                    (query.get("project") or [""])[0].strip(),
                )
            )
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        url = urlsplit(self.path)
        if not self._authorized(parse_qs(url.query)):
            self._send(403, b"forbidden", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send_json({"error": "bad json"}, 400)
            return
        if url.path == "/api/run":
            error = self.gui.start_run(payload)
            self._send_json({"error": error} if error else {"ok": True})
        elif url.path == "/api/abort":
            self.gui.run_manager.abort()
            self._send_json({"ok": True})
        else:
            self._send(404, b"not found", "text/plain")


def serve(
    port: int,
    open_browser: bool,
    dry_run: bool,
    backup_root: Path,
    system: System | None = None,
) -> int:
    gui = GuiServer(system or detect_system(), dry_run, backup_root)
    handler = type("BoundHandler", (_Handler,), {"gui": gui})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?token={gui.token}"
    print(f"uniscript GUI: {url}", flush=True)
    print("The link only works on this machine. Ctrl+C stops the server.", flush=True)
    if gui.privileges.backend in {"sudo", "doas"}:
        print("Keep this terminal open: the administrator password is asked here.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
    return 0
