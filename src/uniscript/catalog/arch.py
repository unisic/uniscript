"""Tasks for Arch Linux and its derivatives: CachyOS, EndeavourOS, Manjaro."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from ..core.context import ExecContext
from ..core.system import System
from ..core.tasks import (
    Category,
    Custom,
    Install,
    Note,
    Risk,
    Run,
    Step,
    Task,
    Unit,
)

PACMAN_CONF = "/etc/pacman.conf"

AUR_WARNING = (
    "AUR packages are build scripts written by users. Nobody reviews them. Read the "
    "PKGBUILD before you install."
)


async def _tune_pacman_conf(ctx: ExecContext) -> None:
    """Enable parallel downloads, colours and the detailed package lists."""
    content = await ctx.read_file(PACMAN_CONF)
    if content is None:
        ctx.log(f"{PACMAN_CONF} is missing", "warn")
        return

    lines = content.splitlines()
    wanted = {
        "ParallelDownloads": "ParallelDownloads = 10",
        "Color": "Color",
        "VerbosePkgLists": "VerbosePkgLists",
    }
    found: set[str] = set()
    for index, line in enumerate(lines):
        stripped = line.lstrip("#").strip()
        key = stripped.split("=")[0].strip()
        if key in wanted and (line.startswith("#") or stripped != wanted[key]):
            lines[index] = wanted[key]
            found.add(key)
        elif key in wanted:
            found.add(key)

    missing = [value for key, value in wanted.items() if key not in found]
    if missing:
        try:
            anchor = lines.index("[options]") + 1
        except ValueError:
            anchor = 0
        lines[anchor:anchor] = missing

    await ctx.write_file(PACMAN_CONF, "\n".join(lines) + "\n", root=True)


async def _enable_multilib(ctx: ExecContext) -> None:
    """Uncomment the [multilib] section together with its Include line."""
    content = await ctx.read_file(PACMAN_CONF)
    if content is None:
        ctx.log(f"{PACMAN_CONF} is missing", "warn")
        return
    if re.search(r"^\[multilib\]", content, re.MULTILINE):
        ctx.log("multilib is already enabled", "skip")
        return

    lines = content.splitlines()
    changed = False
    for index, line in enumerate(lines):
        if line.strip() == "#[multilib]":
            lines[index] = "[multilib]"
            for follow in range(index + 1, min(index + 4, len(lines))):
                if lines[follow].strip().startswith("#Include"):
                    lines[follow] = lines[follow].replace("#Include", "Include", 1)
                    break
            changed = True
            break

    if not changed:
        lines.extend(["", "[multilib]", "Include = /etc/pacman.d/mirrorlist"])

    await ctx.write_file(PACMAN_CONF, "\n".join(lines) + "\n", root=True)
    await ctx.run(["pacman", "-Sy", "--noconfirm"], root=True, allow_fail=True)


def _aur_helper_builder(binary: str, package: str) -> Callable[[ExecContext], Awaitable[None]]:
    """Build one AUR helper from source. makepkg refuses to run as root."""

    async def build_helper(ctx: ExecContext) -> None:
        if ctx.system.has(binary):
            ctx.log(f"{binary} is already installed", "skip")
            return
        clone = f"https://aur.archlinux.org/{package}.git"
        if ctx.system.is_root:
            ctx.note(
                f"makepkg refuses to run as root. Build {binary} from a normal user account: "
                f"git clone {clone} && cd {package} && makepkg -si"
            )
            return
        script = (
            "set -euo pipefail; "
            'dir="$(mktemp -d)"; '
            "trap 'rm -rf \"$dir\"' EXIT; "
            f'git clone --depth 1 {clone} "$dir/{package}"; '
            f'cd "$dir/{package}"; '
            "makepkg -si --noconfirm"
        )
        await ctx.run_interactive(
            ["bash", "-c", script],
            f"Building {binary} from the AUR, makepkg will ask for the sudo password",
        )

    return build_helper


def _driver_steps(system: System) -> list[Step]:
    steps: list[Step] = []
    if "nvidia" in system.gpu_vendors:
        nvidia = next(gpu for gpu in system.gpus if gpu.vendor == "nvidia")
        package = "nvidia-open-dkms" if nvidia.open_kernel_module_capable else "nvidia-dkms"
        steps.append(Install(["linux-headers", "dkms"], optional=True))
        steps.append(
            Install(
                [
                    package,
                    "nvidia-utils",
                    "lib32-nvidia-utils",
                    "nvidia-settings",
                    "libva-nvidia-driver",
                ],
                optional=True,
            )
        )
    if "amd" in system.gpu_vendors:
        steps.append(
            Install(
                [
                    "mesa",
                    "lib32-mesa",
                    "vulkan-radeon",
                    "lib32-vulkan-radeon",
                    "libva-mesa-driver",
                    "lib32-libva-mesa-driver",
                ],
                optional=True,
            )
        )
    if "intel" in system.gpu_vendors:
        steps.append(
            Install(
                ["vulkan-intel", "lib32-vulkan-intel", "intel-media-driver", "libva-utils"],
                optional=True,
            )
        )
    return steps


def build(system: System) -> list[Task]:
    tasks: list[Task] = [
        Task(
            id="pacman-tuning",
            title="Faster and more readable pacman",
            summary="Parallel downloads, coloured output, detailed package lists.",
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            default=True,
            details=[
                "ParallelDownloads = 10 instead of fetching packages one at a time.",
                "VerbosePkgLists shows the sizes before you confirm the transaction.",
                f"{PACMAN_CONF} is backed up first.",
            ],
            steps=[Custom("sets the options in the [options] section", _tune_pacman_conf)],
            detect=lambda probe, sys_: (
                "ParallelDownloads" in probe.pacman_conf
                and not re.search(r"^#\s*ParallelDownloads", probe.pacman_conf, re.MULTILINE)
            ),
        ),
        Task(
            id="pacman-multilib",
            title="multilib repository",
            summary="The 32-bit libraries Steam and many games do not work without.",
            category=Category.REPOS,
            risk=Risk.SAFE,
            default=True,
            details=[
                "Uncomments the [multilib] section together with its Include line.",
                "Once enabled, the lib32-* packages become available, 32-bit graphics drivers "
                "among them.",
            ],
            steps=[Custom("uncomments the [multilib] section", _enable_multilib)],
            detect=lambda probe, sys_: bool(
                re.search(r"^\[multilib\]", probe.pacman_conf, re.MULTILINE)
            ),
        ),
        Task(
            id="pacman-update",
            title="Full system upgrade",
            summary="pacman -Syu. On Arch a partial upgrade is never allowed.",
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            default=True,
            details=[
                "Arch is a rolling release, mixing old and new packages ends in library errors.",
                "Before a large upgrade check archlinux.org, sometimes manual intervention is "
                "required.",
            ],
            steps=[Run(["pacman", "-Syu", "--noconfirm"], timeout=5400.0)],
        ),
        Task(
            id="pacman-mirrors",
            title="Sort out the mirror list",
            summary="reflector picks the fastest mirrors and refreshes the list every week.",
            category=Category.SYSTEM,
            risk=Risk.MEDIUM,
            details=[
                "Overwrites /etc/pacman.d/mirrorlist. The previous version goes to reflector's "
                "own backup.",
                "The 20 most recently synced HTTPS mirrors are picked, sorted by speed.",
                "The reflector.timer timer repeats this periodically.",
            ],
            steps=[
                Install(["reflector"], optional=True),
                Run(
                    [
                        "reflector",
                        "--latest",
                        "20",
                        "--protocol",
                        "https",
                        "--sort",
                        "rate",
                        "--save",
                        "/etc/pacman.d/mirrorlist",
                    ],
                    timeout=600.0,
                    allow_fail=True,
                ),
                Unit("reflector.timer", "enable", now=True),
            ],
            detect=lambda probe, sys_: probe.unit_enabled("reflector.timer"),
        ),
        Task(
            id="arch-aur-paru",
            title="AUR helper: paru",
            summary=(
                "Builds paru-bin from the AUR, to install packages from outside the official "
                "repositories."
            ),
            category=Category.REPOS,
            risk=Risk.MEDIUM,
            warning=AUR_WARNING,
            details=[
                "paru is written in Rust, its interface follows pacman closely and it shows "
                "the PKGBUILD diff before every build.",
                "paru-bin is the prebuilt package, so nothing has to be compiled from source.",
                "makepkg refuses to run as root, so the build runs from your user account.",
                "The interface is suspended for the build, because makepkg asks for the sudo "
                "password on the terminal.",
                "Pick one helper. paru and yay do the same job and having both only causes "
                "confusion.",
            ],
            steps=[
                Install(["base-devel", "git"], optional=True),
                Custom(
                    "builds and installs paru-bin from the AUR",
                    _aur_helper_builder("paru", "paru-bin"),
                ),
            ],
            detect=lambda probe, sys_: sys_.has("paru"),
        ),
        Task(
            id="arch-aur-yay",
            title="AUR helper: yay",
            summary="Builds yay-bin from the AUR, the alternative to paru.",
            category=Category.REPOS,
            risk=Risk.MEDIUM,
            warning=AUR_WARNING,
            details=[
                "yay is written in Go, it is the older and the most widely documented helper.",
                "yay-bin is the prebuilt package, so nothing has to be compiled from source.",
                "makepkg refuses to run as root, so the build runs from your user account.",
                "The interface is suspended for the build, because makepkg asks for the sudo "
                "password on the terminal.",
                "Other helpers (aura, pikaur, trizen) install the same way, from their own AUR "
                "package: git clone https://aur.archlinux.org/<name>.git && makepkg -si",
            ],
            steps=[
                Install(["base-devel", "git"], optional=True),
                Custom(
                    "builds and installs yay-bin from the AUR",
                    _aur_helper_builder("yay", "yay-bin"),
                ),
            ],
            detect=lambda probe, sys_: sys_.has("yay"),
        ),
        Task(
            id="arch-codecs",
            title="Multimedia codecs",
            summary="The full set of GStreamer plugins plus ffmpeg.",
            category=Category.MULTIMEDIA,
            risk=Risk.SAFE,
            default=True,
            details=[
                "Arch does not strip codecs over patents, the plugins only have to be installed.",
                "gst-plugins-ugly and gst-libav cover the formats you meet in practice.",
            ],
            steps=[
                Install(
                    [
                        "gst-plugins-base",
                        "gst-plugins-good",
                        "gst-plugins-bad",
                        "gst-plugins-ugly",
                        "gst-libav",
                        "ffmpeg",
                        "libva-utils",
                    ],
                    optional=True,
                )
            ],
            detect=lambda probe, sys_: probe.has_package("gst-libav"),
        ),
    ]

    driver_steps = _driver_steps(system)
    if driver_steps:
        vendors = ", ".join(sorted(system.gpu_vendors))
        tasks.append(
            Task(
                id="arch-drivers",
                title="Graphics drivers",
                summary=f"Drivers for the detected chips: {vendors}.",
                category=Category.DRIVERS,
                risk=Risk.MEDIUM,
                default=True,
                details=[
                    "For NVIDIA the DKMS variant is picked, it rebuilds the module on every "
                    "kernel change.",
                    "Turing and newer cards get nvidia-open-dkms, older ones nvidia-dkms.",
                    "The matching lib32-* packages, required by Steam and 32-bit games, are added.",
                    "The lib32 packages need the multilib repository enabled.",
                ],
                steps=[
                    *driver_steps,
                    Install(
                        ["vulkan-icd-loader", "lib32-vulkan-icd-loader", "vulkan-tools"],
                        optional=True,
                    ),
                ],
                reboot="nvidia" in system.gpu_vendors,
            )
        )

    tasks.append(
        Task(
            id="arch-gaming",
            title="Gaming setup",
            summary="Steam, Lutris, MangoHud, GameMode, Gamescope and Wine.",
            category=Category.GAMING,
            risk=Risk.MEDIUM,
            tags=frozenset({"gaming"}),
            warning="Needs the multilib repository enabled.",
            details=[
                "Steam comes from the multilib repository.",
                "wine-staging carries the patches that have not reached the stable release yet.",
                "lib32-mangohud and lib32-gamemode cover 32-bit games.",
            ],
            steps=[
                Install(
                    [
                        "steam",
                        "lutris",
                        "mangohud",
                        "lib32-mangohud",
                        "gamemode",
                        "lib32-gamemode",
                        "gamescope",
                        "wine-staging",
                        "winetricks",
                        "vkbasalt",
                        "goverlay",
                    ],
                    optional=True,
                ),
                Note(
                    "In Steam enable Proton for every game: Settings, Compatibility, Enable "
                    "Steam Play for all other titles."
                ),
            ],
            detect=lambda probe, sys_: probe.has_package("steam"),
        )
    )

    tasks.append(
        Task(
            id="arch-paccache",
            title="Cap the package cache",
            summary="paccache keeps the last three versions of every package and clears the rest.",
            category=Category.MAINTENANCE,
            risk=Risk.SAFE,
            details=[
                "The /var/cache/pacman/pkg directory can grow to tens of GiB.",
                "The paccache.timer timer repeats the cleanup every week.",
                "The versions kept let you roll a package upgrade back.",
            ],
            steps=[
                Install(["pacman-contrib"], optional=True),
                Run(["paccache", "-rk3"], allow_fail=True),
                Unit("paccache.timer", "enable", now=True),
            ],
            detect=lambda probe, sys_: probe.unit_enabled("paccache.timer"),
        )
    )

    tasks.append(
        Task(
            id="arch-dualboot-rtc",
            title="Hardware clock in UTC (dual boot with Windows)",
            summary="Removes the time offset between Linux and Windows.",
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            steps=[
                Run(["timedatectl", "set-local-rtc", "0", "--adjust-system-clock"], allow_fail=True)
            ],
        )
    )

    return tasks
