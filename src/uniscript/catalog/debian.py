"""Tasks for Debian, Ubuntu, Linux Mint and Pop!_OS (apt)."""

from __future__ import annotations

import re

from ..core.context import ExecContext
from ..core.system import System
from ..core.tasks import (
    Category,
    Custom,
    InputPrompt,
    Install,
    Note,
    Risk,
    Run,
    Shell,
    Step,
    Task,
    Unit,
    WriteFile,
)

APT_CONF = "/etc/apt/apt.conf.d/99-uniscript"
MOZILLA_LIST = "/etc/apt/sources.list.d/mozilla.list"
MOZILLA_PREF = "/etc/apt/preferences.d/mozilla"
MOZILLA_KEY = "/etc/apt/keyrings/packages.mozilla.org.asc"
MOZILLA_KEY_URL = "https://packages.mozilla.org/apt/repo-signing-key.gpg"

HEADER = "# Written by uniscript.\n"

UBUNTU_LIKE = {"ubuntu", "linuxmint", "pop", "elementary", "zorin", "neon"}


def _is_ubuntu(system: System) -> bool:
    return system.os_id in UBUNTU_LIKE or "ubuntu" in system.id_like


async def _apt_conf_content(ctx: ExecContext) -> str:
    return (
        HEADER + "// Skip the package description translations: a shorter apt update.\n"
        'Acquire::Languages "none";\n'
        "\n"
        "// Several HTTP requests over one connection.\n"
        'Acquire::http::Pipeline-Depth "5";\n'
        "\n"
        "// Do not install merely suggested packages.\n"
        'APT::Install-Suggests "false";\n'
    )


async def _mozilla_pref_content(ctx: ExecContext) -> str:
    return (
        HEADER + "# Firefox has to come from the Mozilla repository, not from a repackaged snap.\n"
        "Package: firefox*\n"
        "Pin: origin packages.mozilla.org\n"
        "Pin-Priority: 1000\n"
    )


async def _mozilla_list_content(ctx: ExecContext) -> str:
    return HEADER + f"deb [signed-by={MOZILLA_KEY}] https://packages.mozilla.org/apt mozilla main\n"


_PPA_NAME = re.compile(r"^ppa:[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._+-]*$")


def _ppa_input_valid(value: str) -> str | None:
    tokens = value.replace(",", " ").split()
    ppas = [token for token in tokens if token.startswith("ppa:")]
    if not ppas:
        return "At least one entry has to look like ppa:owner/name."
    bad = [token for token in ppas if not _PPA_NAME.match(token)]
    if bad:
        return f"Not a PPA name: {' '.join(bad)}"
    bad = [
        token
        for token in tokens
        if not token.startswith("ppa:") and not _PACKAGE_NAME.match(token)
    ]
    if bad:
        return f"Not a package name: {' '.join(bad)}"
    return None


async def _enable_ppa(ctx: ExecContext) -> None:
    value = ctx.input_value()
    if not value:
        ctx.log("no PPA name was given", "warn")
        return
    tokens = value.replace(",", " ").split()
    ppas = [token for token in tokens if token.startswith("ppa:")]
    packages = [token for token in tokens if not token.startswith("ppa:")]
    for ppa in ppas:
        await ctx.run(["add-apt-repository", "-y", ppa], root=True, timeout=300.0)
    await ctx.run(["apt-get", "update"], root=True, allow_fail=True)
    if packages:
        await ctx.run(
            ["apt-get", "install", "-y", *packages],
            root=True,
            timeout=min(3600.0, 600.0 + 60.0 * len(packages)),
        )


async def _enable_debian_components(ctx: ExecContext) -> None:
    """Add contrib, non-free and non-free-firmware to the Debian sources.

    Both formats are handled: the classic sources.list and deb822 (.sources).
    """
    wanted = ["contrib", "non-free", "non-free-firmware"]
    changed = False

    classic = "/etc/apt/sources.list"
    content = await ctx.read_file(classic)
    if content:
        lines = content.splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("deb ") and not stripped.startswith("deb-src "):
                continue
            parts = stripped.split()
            if len(parts) < 4 or "main" not in parts:
                continue
            missing = [component for component in wanted if component not in parts]
            if missing:
                lines[index] = line.rstrip() + " " + " ".join(missing)
                changed = True
        if changed:
            await ctx.write_file(classic, "\n".join(lines) + "\n", root=True)

    for name in ("debian.sources", "ubuntu.sources"):
        path = f"/etc/apt/sources.list.d/{name}"
        content = await ctx.read_file(path)
        if not content:
            continue
        lines = content.splitlines()
        touched = False
        for index, line in enumerate(lines):
            if not line.startswith("Components:"):
                continue
            components = line.split(":", 1)[1].split()
            missing = [component for component in wanted if component not in components]
            if missing:
                lines[index] = "Components: " + " ".join(components + missing)
                touched = True
        if touched:
            await ctx.write_file(path, "\n".join(lines) + "\n", root=True)
            changed = True

    if not changed:
        ctx.log("the contrib and non-free components were already enabled", "skip")
        return
    await ctx.run(["apt-get", "update"], root=True, allow_fail=True)


async def _enable_i386(ctx: ExecContext) -> None:
    architectures = await ctx.capture(["dpkg", "--print-foreign-architectures"])
    if "i386" in architectures.split():
        ctx.log("the i386 architecture is already enabled", "skip")
        return
    await ctx.run(["dpkg", "--add-architecture", "i386"], root=True)
    await ctx.run(["apt-get", "update"], root=True, allow_fail=True)


def _driver_steps(system: System) -> list[Step]:
    steps: list[Step] = []
    if "nvidia" in system.gpu_vendors and system.has("ubuntu-drivers"):
        steps.append(Run(["ubuntu-drivers", "install"], timeout=3600.0))
    elif "nvidia" in system.gpu_vendors:
        steps.append(Install(["nvidia-driver", "firmware-misc-nonfree"], optional=True))
    if "amd" in system.gpu_vendors:
        steps.append(
            Install(
                ["mesa-va-drivers", "mesa-vulkan-drivers", "libvulkan1", "vainfo", "vulkan-tools"],
                optional=True,
            )
        )
    if "intel" in system.gpu_vendors:
        steps.append(
            Install(
                ["intel-media-va-driver-non-free", "i965-va-driver", "vainfo"],
                optional=True,
            )
        )
    return steps


def build(system: System) -> list[Task]:
    ubuntu = _is_ubuntu(system)

    tasks: list[Task] = [
        Task(
            id="apt-tuning",
            title="Faster apt",
            summary=(
                "Skips description translations and suggested packages, enables HTTP pipelining."
            ),
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            default=True,
            details=[
                "apt update stops fetching the Translation-* files, which can weigh more than "
                "the indexes themselves.",
                "APT::Install-Suggests false stops merely suggested packages from being pulled in.",
                f"Everything lands in a single file, {APT_CONF}.",
            ],
            steps=[WriteFile(APT_CONF, _apt_conf_content, "apt settings")],
            detect=lambda probe, sys_: probe.path_exists(APT_CONF),
        ),
        Task(
            id="apt-update",
            title="Full system upgrade",
            summary="apt update followed by full-upgrade.",
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            default=True,
            details=[
                "full-upgrade also resolves the upgrades that require removing a package.",
                "The debconf frontend is set to noninteractive, so the install will not stop on "
                "a question.",
            ],
            steps=[
                Run(["apt-get", "update"]),
                Run(["apt-get", "full-upgrade", "-y"], timeout=5400.0),
            ],
        ),
    ]

    if ubuntu:
        tasks.append(
            Task(
                id="ubuntu-components",
                title="universe and multiverse repositories",
                summary=(
                    "Unlocks the larger part of the Ubuntu catalogue, Steam and codecs included."
                ),
                category=Category.REPOS,
                risk=Risk.SAFE,
                default=True,
                details=[
                    "universe holds community maintained packages, multiverse holds software "
                    "under a closed licence.",
                    "Without multiverse you cannot install ubuntu-restricted-extras or Steam "
                    "from the repository.",
                ],
                steps=[
                    Install(["software-properties-common"], optional=True),
                    Run(["add-apt-repository", "-y", "universe"], allow_fail=True),
                    Run(["add-apt-repository", "-y", "multiverse"], allow_fail=True),
                    Run(["apt-get", "update"], allow_fail=True),
                ],
                detect=lambda probe, sys_: "multiverse" in probe.apt_sources,
                available=lambda sys_: _is_ubuntu(sys_),
            )
        )
        tasks.append(
            Task(
                id="ubuntu-codecs",
                title="Microsoft codecs and fonts",
                summary="ubuntu-restricted-extras with the font licence accepted automatically.",
                category=Category.MULTIMEDIA,
                risk=Risk.MEDIUM,
                default=True,
                details=[
                    "The package pulls in GStreamer codecs, RAR support and the Microsoft fonts.",
                    "The font installer normally stops on the EULA screen. uniscript presets "
                    "the answer with debconf-set-selections, so the install goes through "
                    "without stopping. You are accepting the Microsoft licence by doing so.",
                ],
                steps=[
                    Shell(
                        "echo ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula "
                        "select true | debconf-set-selections"
                    ),
                    Install(["ubuntu-restricted-extras"], optional=True),
                ],
                detect=lambda probe, sys_: probe.has_package("ubuntu-restricted-extras"),
                available=lambda sys_: _is_ubuntu(sys_),
            )
        )
        tasks.append(
            Task(
                id="ubuntu-firefox-mozilla",
                title="Firefox from the Mozilla repository instead of the snap",
                summary="Adds the official Mozilla deb repository and pins Firefox to it.",
                category=Category.PACKAGING,
                risk=Risk.MEDIUM,
                warning=(
                    "Run this together with the snapd removal. Ubuntu ships Firefox only as a "
                    "snap, so removing snapd without this task leaves you with no browser."
                ),
                details=[
                    "Repository: https://packages.mozilla.org/apt, distribution mozilla, "
                    "component main.",
                    "The signing key goes to /etc/apt/keyrings and is referenced through "
                    "signed-by, without the deprecated apt-key.",
                    "A pin with priority 1000 makes sure apt does not swap the package back "
                    "for the repackaged snap.",
                    "The deb build updates along with the rest of the system through apt.",
                ],
                steps=[
                    Install(["curl", "ca-certificates"], optional=True),
                    Run(["install", "-d", "-m", "0755", "/etc/apt/keyrings"]),
                    Run(["curl", "-fsSL", MOZILLA_KEY_URL, "-o", MOZILLA_KEY]),
                    Run(["chmod", "0644", MOZILLA_KEY]),
                    WriteFile(MOZILLA_LIST, _mozilla_list_content, "the Mozilla apt source"),
                    WriteFile(
                        MOZILLA_PREF, _mozilla_pref_content, "a pin for the firefox packages"
                    ),
                    Run(["apt-get", "update"]),
                    Run(["apt-get", "install", "-y", "firefox"], timeout=1800.0),
                    Note(
                        "The profile from the snap build does not move over by itself. Sign in "
                        "to your Mozilla account in Firefox or copy the "
                        "~/snap/firefox/common/.mozilla directory."
                    ),
                ],
                detect=lambda probe, sys_: probe.path_exists(MOZILLA_LIST),
                available=lambda sys_: _is_ubuntu(sys_),
            )
        )
        tasks.append(
            Task(
                id="ubuntu-ppa",
                title="Enable a PPA and install from it",
                summary="Adds the Launchpad PPAs you name and installs the packages you list.",
                category=Category.REPOS,
                risk=Risk.MEDIUM,
                warning=(
                    "A PPA is one person's package archive on Launchpad. Nobody reviews "
                    "the contents. Only enable archives whose owner you trust."
                ),
                details=[
                    "Entries starting with ppa: are archives, the rest are package names "
                    "installed after the archives are enabled.",
                    "Example: ppa:mozillateam/ppa firefox-esr adds the archive and "
                    "installs the package in one go.",
                    "Remove one later with: sudo add-apt-repository --remove ppa:owner/name.",
                ],
                prompt=InputPrompt(
                    label="PPAs (ppa:owner/name) and packages to install",
                    placeholder="for example ppa:mozillateam/ppa firefox-esr",
                    validator=_ppa_input_valid,
                ),
                steps=[
                    Install(["software-properties-common"], optional=True),
                    Custom(
                        "enables the PPAs you named and installs the packages",
                        _enable_ppa,
                    ),
                ],
                available=lambda sys_: _is_ubuntu(sys_),
            )
        )
    if not _is_ubuntu(system):
        tasks.append(
            Task(
                id="debian-components",
                title="contrib, non-free and non-free-firmware components",
                summary="Unlocks hardware firmware and software under a closed licence.",
                category=Category.REPOS,
                risk=Risk.MEDIUM,
                details=[
                    "Without non-free-firmware some Wi-Fi cards and GPUs will not get the "
                    "firmware they need.",
                    "Both source formats are handled: the classic sources.list and deb822 "
                    "(.sources).",
                    "The original files are backed up before the change.",
                ],
                steps=[
                    Custom("appends the components to the apt sources", _enable_debian_components),
                ],
                detect=lambda probe, sys_: "non-free-firmware" in probe.apt_sources,
                available=lambda sys_: not _is_ubuntu(sys_),
            )
        )

    driver_steps = _driver_steps(system)
    if driver_steps:
        vendors = ", ".join(sorted(system.gpu_vendors))
        tasks.append(
            Task(
                id="apt-drivers",
                title="Graphics drivers",
                summary=f"Drivers and video decoders for the detected chips: {vendors}.",
                category=Category.DRIVERS,
                risk=Risk.MEDIUM,
                default=True,
                details=[
                    (
                        "On Ubuntu, ubuntu-drivers install is used, which picks the "
                        "recommended NVIDIA driver version itself."
                        if system.has("ubuntu-drivers")
                        else "On Debian the nvidia-driver package from the non-free component "
                        "is installed."
                    ),
                    "For AMD and Intel the VA-API drivers for hardware video decoding are added.",
                    "Check afterwards with: vainfo and vulkaninfo --summary.",
                ],
                steps=[
                    *driver_steps,
                    Note("A reboot is required after installing the NVIDIA driver."),
                ],
                reboot="nvidia" in system.gpu_vendors,
                available=lambda sys_: bool(sys_.gpu_vendors),
            )
        )

    tasks.append(
        Task(
            id="apt-gaming",
            title="Gaming setup",
            summary="Steam, Lutris, MangoHud, GameMode, Wine and the 32-bit libraries.",
            category=Category.GAMING,
            risk=Risk.MEDIUM,
            tags=frozenset({"gaming"}),
            warning=(
                "On Ubuntu, Steam needs the multiverse component. Run the universe and "
                "multiverse repository task first."
            ),
            details=[
                "The i386 architecture is enabled, without it Steam will not start.",
                "The steam-installer package downloads the real Valve client on first run.",
                "GameMode raises the priority of the game, MangoHud shows FPS and temperatures.",
                "Package names depend on the release, missing ones are skipped instead of "
                "failing the task.",
            ],
            steps=[
                Custom("enables the i386 architecture", _enable_i386),
                Install(
                    [
                        "steam-installer",
                        "lutris",
                        "mangohud",
                        "gamemode",
                        "gamescope",
                        "wine",
                        "winetricks",
                        "libvulkan1",
                        "libvulkan1:i386",
                        "mesa-vulkan-drivers",
                        "mesa-vulkan-drivers:i386",
                        "vulkan-tools",
                    ],
                    optional=True,
                ),
                Note(
                    "In Steam enable Proton for every game: Settings, Compatibility, Enable "
                    "Steam Play for all other titles."
                ),
            ],
            detect=lambda probe, sys_: probe.has_any_package("steam", "steam-installer"),
        )
    )

    tasks.append(
        Task(
            id="apt-earlyoom",
            title="earlyoom: a way out of an out-of-memory freeze",
            summary=(
                "Kills the greediest process before the machine locks up for a quarter of an hour."
            ),
            category=Category.TWEAKS,
            risk=Risk.SAFE,
            details=[
                "The kernel only fires its own OOM killer once memory is completely exhausted, "
                "and by then the machine has stopped responding.",
                "earlyoom acts earlier, at a free memory threshold, so the desktop stays usable.",
                "Fedora uses systemd-oomd for this, enabled out of the box.",
            ],
            steps=[
                Install(["earlyoom"], optional=True),
                Unit("earlyoom.service", "enable", now=True),
            ],
            detect=lambda probe, sys_: probe.unit_enabled("earlyoom.service"),
        )
    )

    tasks.append(
        Task(
            id="apt-archives",
            title="Archive and AppImage support",
            summary="7-Zip, RAR and the FUSE library AppImage files need.",
            category=Category.APPS,
            risk=Risk.SAFE,
            steps=[
                Install(
                    ["p7zip-full", "p7zip-rar", "unrar", "libfuse2t64", "libfuse2"],
                    optional=True,
                )
            ],
            detect=lambda probe, sys_: probe.has_package("p7zip-full"),
        )
    )

    tasks.append(
        Task(
            id="apt-dualboot-rtc",
            title="Hardware clock in UTC (dual boot with Windows)",
            summary="Removes the time offset between Linux and Windows.",
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            details=[
                "Windows keeps local time in the hardware clock, Linux keeps UTC.",
                "After this change the time in Linux is correct, and Windows needs the "
                "RealTimeIsUniversal registry entry.",
            ],
            steps=[
                Run(["timedatectl", "set-local-rtc", "0", "--adjust-system-clock"], allow_fail=True)
            ],
        )
    )

    return tasks
