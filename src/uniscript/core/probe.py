"""Cheap, cached probes of the system state.

Asking one question at a time (is this package installed, is this repository
enabled) would spawn one process per question. Instead we fetch whole sets once
and keep them in memory for the lifetime of the program. The size is bounded by
the number of packages on the system, on the order of a few thousand short
strings.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from functools import cached_property
from pathlib import Path

from .system import System

PROBE_TIMEOUT = 20.0


def _capture(argv: list[str], timeout: float = PROBE_TIMEOUT) -> str:
    if not shutil.which(argv[0]):
        return ""
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout


class Probe:
    """The system state needed to recognise tasks that are already applied."""

    def __init__(self, system: System) -> None:
        self.system = system

    def invalidate(self) -> None:
        """Drop the cache after running tasks so statuses are recomputed."""
        for name in list(self.__dict__):
            if name != "system":
                del self.__dict__[name]

    @cached_property
    def installed_packages(self) -> frozenset[str]:
        family = self.system.family
        if family in ("rhel", "suse"):
            out = _capture(["rpm", "-qa", "--queryformat", "%{NAME}\\n"], 40.0)
        elif family == "debian":
            out = _capture(["dpkg-query", "-W", "-f", "${Package} ${db:Status-Status}\n"], 40.0)
            return frozenset(
                line.split()[0] for line in out.splitlines() if line.endswith(" installed")
            )
        elif family == "arch":
            out = _capture(["pacman", "-Qq"], 40.0)
        else:
            return frozenset()
        return frozenset(line.strip() for line in out.splitlines() if line.strip())

    def has_package(self, *names: str) -> bool:
        return all(name in self.installed_packages for name in names)

    def has_any_package(self, *names: str) -> bool:
        return any(name in self.installed_packages for name in names)

    @cached_property
    def flatpak_remotes(self) -> frozenset[str]:
        out = _capture(["flatpak", "remotes", "--columns=name"])
        return frozenset(line.strip() for line in out.splitlines() if line.strip())

    @cached_property
    def flatpak_apps(self) -> frozenset[str]:
        out = _capture(["flatpak", "list", "--app", "--columns=application"])
        return frozenset(line.strip() for line in out.splitlines() if line.strip())

    def has_flatpak_app(self, app_id: str) -> bool:
        return app_id in self.flatpak_apps

    @cached_property
    def dnf_repos(self) -> frozenset[str]:
        out = _capture(["dnf", "repolist", "--enabled", "--quiet"], 40.0)
        repos = set()
        for line in out.splitlines()[1:]:
            parts = line.split()
            if parts:
                repos.add(parts[0])
        return frozenset(repos)

    @cached_property
    def apt_sources(self) -> str:
        chunks: list[str] = []
        for path in (Path("/etc/apt/sources.list"),):
            if path.is_file():
                chunks.append(read_file(path))
        directory = Path("/etc/apt/sources.list.d")
        if directory.is_dir():
            for entry in sorted(directory.iterdir()):
                if entry.suffix in (".list", ".sources"):
                    chunks.append(read_file(entry))
        return "\n".join(chunks)

    @cached_property
    def pacman_conf(self) -> str:
        return read_file(Path("/etc/pacman.conf"))

    @cached_property
    def zypper_repos(self) -> frozenset[str]:
        out = _capture(["zypper", "--non-interactive", "lr", "--name"], 40.0)
        names = set()
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) > 2 and parts[0].isdigit():
                names.add(parts[1].lower())
                names.add(parts[2].lower())
        return frozenset(names)

    @cached_property
    def enabled_units(self) -> frozenset[str]:
        out = _capture(
            ["systemctl", "list-unit-files", "--state=enabled", "--no-legend", "--no-pager"],
            30.0,
        )
        return frozenset(line.split()[0] for line in out.splitlines() if line.split())

    def unit_enabled(self, unit: str) -> bool:
        return unit in self.enabled_units

    @cached_property
    def snap_packages(self) -> frozenset[str]:
        out = _capture(["snap", "list"])
        return frozenset(line.split()[0] for line in out.splitlines()[1:] if line.split())

    @cached_property
    def loaded_modules(self) -> frozenset[str]:
        try:
            raw = Path("/proc/modules").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return frozenset()
        return frozenset(line.split()[0] for line in raw.splitlines() if line.split())

    @cached_property
    def login_shell(self) -> str:
        """Shell recorded for the user in /etc/passwd, not the running one."""
        try:
            return pwd.getpwuid(os.getuid()).pw_shell
        except KeyError:
            return ""

    @cached_property
    def valid_shells(self) -> frozenset[str]:
        shells = set()
        for line in read_file(Path("/etc/shells")).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                shells.add(line)
        return frozenset(shells)

    def file_contains(self, path: str | Path, needle: str) -> bool:
        return needle in read_file(Path(path))

    def path_exists(self, path: str | Path) -> bool:
        return Path(path).exists()


def read_file(path: Path) -> str:
    """Read a file without escalating. No access means an empty string."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
