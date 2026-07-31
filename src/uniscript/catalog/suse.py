"""Tasks for openSUSE Tumbleweed and Leap (zypper)."""

from __future__ import annotations

from ..core.system import System
from ..core.tasks import (
    Category,
    Install,
    Note,
    Risk,
    Run,
    Step,
    Task,
)

PACKMAN_TUMBLEWEED = "https://ftp.gwdg.de/pub/linux/misc/packman/suse/openSUSE_Tumbleweed/"
PACKMAN_LEAP = "https://ftp.gwdg.de/pub/linux/misc/packman/suse/openSUSE_Leap_$releasever/"
NVIDIA_TUMBLEWEED = "https://download.nvidia.com/opensuse/tumbleweed"
NVIDIA_LEAP = "https://download.nvidia.com/opensuse/leap/$releasever"


def _is_tumbleweed(system: System) -> bool:
    return "tumbleweed" in system.os_id or "tumbleweed" in system.pretty_name.lower()


def _packman_url(system: System) -> str:
    return PACKMAN_TUMBLEWEED if _is_tumbleweed(system) else PACKMAN_LEAP


def _nvidia_url(system: System) -> str:
    return NVIDIA_TUMBLEWEED if _is_tumbleweed(system) else NVIDIA_LEAP


def _driver_steps(system: System) -> list[Step]:
    steps: list[Step] = []
    if "nvidia" in system.gpu_vendors:
        steps.extend(
            [
                Run(
                    ["zypper", "--non-interactive", "ar", "-f", _nvidia_url(system), "NVIDIA"],
                    allow_fail=True,
                ),
                Run(["zypper", "--non-interactive", "--gpg-auto-import-keys", "refresh"]),
                Run(
                    ["zypper", "--non-interactive", "install-new-recommends", "--repo", "NVIDIA"],
                    timeout=3600.0,
                ),
            ]
        )
    if "amd" in system.gpu_vendors or "intel" in system.gpu_vendors:
        steps.append(
            Install(
                ["libva-utils", "vulkan-tools", "Mesa-dri", "libvulkan1"],
                optional=True,
            )
        )
    if "intel" in system.gpu_vendors:
        steps.append(Install(["intel-media-driver", "libva-vdpau-driver"], optional=True))
    return steps


def build(system: System) -> list[Task]:
    packman = _packman_url(system)

    tasks: list[Task] = [
        Task(
            id="zypper-update",
            title="Full system upgrade",
            summary="zypper dup, the right upgrade method for Tumbleweed.",
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            default=True,
            details=[
                "dup also resolves package vendor changes and dropped dependencies.",
                "On Leap, dup is safe within a single release.",
            ],
            steps=[
                Run(["zypper", "--non-interactive", "refresh"]),
                Run(["zypper", "--non-interactive", "dup"], timeout=5400.0),
            ],
        ),
        Task(
            id="suse-opi",
            title="opi: install packages from OBS and Packman",
            summary="Installs opi, the community installer that searches the Build Service.",
            category=Category.REPOS,
            risk=Risk.MEDIUM,
            details=[
                "opi <name> searches the openSUSE Build Service and Packman, installs "
                "your pick and adds the repository it came from.",
                "Community OBS repositories are unreviewed; read what opi offers "
                "before accepting.",
                "Run it in a terminal afterwards: opi <package>, or opi codecs.",
            ],
            steps=[Install(["opi"])],
            detect=lambda probe, sys_: probe.has_package("opi"),
        ),
        Task(
            id="suse-packman",
            title="Packman repository and the full codecs",
            summary="Adds Packman and switches the multimedia packages over to its builds.",
            category=Category.MULTIMEDIA,
            risk=Risk.MEDIUM,
            default=True,
            warning=(
                "Switching the vendor of the multimedia packages to Packman is permanent. "
                "Going back needs another dup from the main repository."
            ),
            details=[
                f"Repository: {packman} with priority 90, higher than the default.",
                "zypper dup --from packman --allow-vendor-change swaps ffmpeg and the "
                "GStreamer plugins for the builds carrying the full codec set.",
                "An alternative to all of this: the opi package and the opi codecs command.",
            ],
            steps=[
                Run(
                    ["zypper", "--non-interactive", "ar", "-cfp", "90", packman, "packman"],
                    allow_fail=True,
                ),
                Run(["zypper", "--non-interactive", "--gpg-auto-import-keys", "refresh"]),
                Run(
                    [
                        "zypper",
                        "--non-interactive",
                        "dup",
                        "--from",
                        "packman",
                        "--allow-vendor-change",
                    ],
                    timeout=5400.0,
                ),
                Install(
                    [
                        "ffmpeg",
                        "gstreamer-plugins-good",
                        "gstreamer-plugins-bad",
                        "gstreamer-plugins-ugly",
                        "gstreamer-plugins-libav",
                    ],
                    optional=True,
                ),
            ],
            detect=lambda probe, sys_: "packman" in probe.zypper_repos,
        ),
        Task(
            id="suse-archives",
            title="Archive support",
            summary="7-Zip and RAR.",
            category=Category.APPS,
            risk=Risk.SAFE,
            steps=[Install(["p7zip-full", "unrar"], optional=True)],
            detect=lambda probe, sys_: probe.has_any_package("p7zip-full", "7zip"),
        ),
        Task(
            id="suse-dualboot-rtc",
            title="Hardware clock in UTC (dual boot with Windows)",
            summary="Removes the time offset between Linux and Windows.",
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            steps=[
                Run(["timedatectl", "set-local-rtc", "0", "--adjust-system-clock"], allow_fail=True)
            ],
        ),
    ]

    driver_steps = _driver_steps(system)
    if driver_steps:
        vendors = ", ".join(sorted(system.gpu_vendors))
        tasks.append(
            Task(
                id="suse-drivers",
                title="Graphics drivers",
                summary=f"Drivers for the detected chips: {vendors}.",
                category=Category.DRIVERS,
                risk=Risk.MEDIUM,
                details=[
                    f"For NVIDIA the official {_nvidia_url(system)} repository is added.",
                    "install-new-recommends matches the driver package to the detected card "
                    "itself, instead of guessing whether the variant is G06 or G05.",
                    "A reboot is required after installing the NVIDIA driver.",
                ],
                steps=[*driver_steps, Note("Check after the reboot: nvidia-smi and vainfo.")],
                reboot="nvidia" in system.gpu_vendors,
            )
        )

    tasks.append(
        Task(
            id="suse-gaming",
            title="Gaming setup",
            summary="Steam, Lutris, MangoHud, GameMode and Wine.",
            category=Category.GAMING,
            risk=Risk.MEDIUM,
            tags=frozenset({"gaming"}),
            details=[
                "Steam is in the main openSUSE repository.",
                "Missing packages are skipped instead of failing the whole task.",
            ],
            steps=[
                Install(
                    [
                        "steam",
                        "lutris",
                        "mangohud",
                        "gamemoded",
                        "wine",
                        "winetricks",
                        "vulkan-tools",
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

    return tasks
