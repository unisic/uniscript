"""Assembling the task list for the detected system."""

from __future__ import annotations

from ..core.system import System
from ..core.tasks import Category, Note, Risk, Task
from . import arch, common, debian, fedora, suse

_BUILDERS = {
    "rhel": fedora.build,
    "debian": debian.build,
    "arch": arch.build,
    "suse": suse.build,
}


def _unknown_distro_task(system: System) -> Task:
    return Task(
        id="unknown-distro",
        title="Unrecognized distribution",
        summary="Only tasks that do not depend on a package manager are available.",
        category=Category.SYSTEM,
        risk=Risk.SAFE,
        details=[
            f"ID read from /etc/os-release: {system.os_id or 'none'}.",
            "No supported package manager found (dnf, apt, pacman, zypper).",
            "Flatpak tasks, kernel tweaks and file configuration still work, "
            "they do not depend on the distribution.",
            "System package installs have to be done by hand.",
        ],
        steps=[
            Note("Unrecognized distribution: tasks installing system packages are unavailable.")
        ],
    )


def build_tasks(system: System) -> list[Task]:
    """Return the tasks available for this system, sorted by category."""
    tasks: list[Task] = []
    builder = _BUILDERS.get(system.family)
    if builder is None:
        tasks.append(_unknown_distro_task(system))
    else:
        tasks.extend(builder(system))

    tasks.extend(common.build(system))

    available = [task for task in tasks if task.is_available(system)]
    available.sort(key=lambda task: (task.category.order, task.title))

    seen: set[str] = set()
    unique: list[Task] = []
    for task in available:
        if task.id in seen:
            continue
        seen.add(task.id)
        unique.append(task)
    return unique


def categories_of(tasks: list[Task]) -> list[Category]:
    present = {task.category for task in tasks}
    return sorted(present, key=lambda category: category.order)
