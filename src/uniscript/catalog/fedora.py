"""Tasks for Fedora and its derivatives (dnf, dnf5).

Package names verified with dnf repoquery on Fedora 44.
What changed compared to older guides:
  - p7zip and p7zip-plugins are gone, 7z archives are handled by the 7zip package,
  - mesa-vdpau-drivers-freeworld was absorbed into mesa-va-drivers-freeworld,
  - the open NVIDIA module has its own package, akmod-nvidia-open, the
    %_with_kmod_nvidia_open macro no longer has to be set by hand.
"""

from __future__ import annotations

from ..core.context import ExecContext
from ..core.system import System
from ..core.tasks import (
    Category,
    Custom,
    InputPrompt,
    Install,
    Interactive,
    Note,
    Risk,
    Run,
    Step,
    Task,
    WriteFile,
)

DNF_CONF = "/etc/dnf/dnf.conf"

RPMFUSION_FREE = (
    "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
)
RPMFUSION_NONFREE = (
    "https://mirrors.rpmfusion.org/nonfree/fedora/"
    "rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm"
)

TERRA_REPO = "terra,https://repos.fyralabs.com/terra$releasever"


def _dnf_release(system: System) -> str:
    return system.version_id or "rawhide"


async def _dnf_conf_content(ctx: ExecContext) -> str:
    """Merge the settings into [main], keeping whatever is already there."""
    current = await ctx.read_file(DNF_CONF) or "[main]\n"
    wanted = {
        "max_parallel_downloads": "10",
        "fastestmirror": "True",
        "defaultyes": "True",
        "keepcache": "False",
        "install_weak_deps": "True",
    }
    lines = current.splitlines()
    if "[main]" not in lines:
        lines.insert(0, "[main]")

    seen: set[str] = set()
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in wanted:
            lines[index] = f"{key}={wanted[key]}"
            seen.add(key)

    missing = [f"{key}={value}" for key, value in wanted.items() if key not in seen]
    if missing:
        main_index = lines.index("[main]")
        end = len(lines)
        for index in range(main_index + 1, len(lines)):
            if lines[index].startswith("["):
                end = index
                break
        lines[end:end] = missing
    return "\n".join(lines) + "\n"


async def _swap_va_drivers(ctx: ExecContext) -> None:
    """Swap the VA drivers for the RPM Fusion build.

    On Fedora 42 and newer mesa-va-drivers is no longer a separate package, so
    dnf swap has nothing to replace and a plain install is needed.
    """
    if "mesa-va-drivers-freeworld" in ctx.probe.installed_packages:
        ctx.log("mesa-va-drivers-freeworld is already installed", "skip")
        return
    if "mesa-va-drivers" in ctx.probe.installed_packages:
        await ctx.run(
            ["dnf", "swap", "-y", "mesa-va-drivers", "mesa-va-drivers-freeworld"],
            root=True,
        )
    else:
        await ctx.run(
            ["dnf", "install", "-y", "--allowerasing", "mesa-va-drivers-freeworld"],
            root=True,
        )


async def _swap_ffmpeg(ctx: ExecContext) -> None:
    if "ffmpeg" in ctx.probe.installed_packages:
        ctx.log("the full ffmpeg is already installed", "skip")
        return
    if "ffmpeg-free" in ctx.probe.installed_packages:
        await ctx.run(["dnf", "swap", "-y", "--allowerasing", "ffmpeg-free", "ffmpeg"], root=True)
    else:
        await ctx.run(["dnf", "install", "-y", "--allowerasing", "ffmpeg"], root=True)


async def _enable_copr(ctx: ExecContext) -> None:
    value = ctx.input_value()
    if not value:
        ctx.log("no repository name was given", "warn")
        return
    for repo in value.replace(",", " ").split():
        await ctx.run(["dnf", "-y", "copr", "enable", repo], root=True)


async def _remove_old_kernels(ctx: ExecContext) -> None:
    output = await ctx.capture(["dnf", "repoquery", "--installonly", "--latest-limit=-2", "-q"])
    packages = [line.strip() for line in output.splitlines() if line.strip()]
    if not packages:
        ctx.log("no old kernels to remove", "skip")
        return
    ctx.log(f"to remove: {len(packages)} kernel packages", "info")
    await ctx.run(["dnf", "remove", "-y", *packages], root=True)


def _nvidia_steps(system: System) -> list[Step]:
    nvidia = next((gpu for gpu in system.gpus if gpu.vendor == "nvidia"), None)
    open_module = bool(nvidia and nvidia.open_kernel_module_capable)
    akmod = "akmod-nvidia-open" if open_module else "akmod-nvidia"

    steps: list[Step] = [
        Install(["kernel-devel", "kernel-headers", "gcc", "make", "acpid", "akmods", "kmodtool"]),
        Install([akmod, "xorg-x11-drv-nvidia-cuda", "xorg-x11-drv-nvidia-power"]),
        Install(["libva-nvidia-driver", "vulkan-loader", "vulkan-loader.i686"], optional=True),
    ]
    if system.secure_boot:
        steps.extend(
            [
                Install(["mokutil", "openssl"]),
                Run(["kmodgenca", "-a"]),
                Interactive(
                    ["mokutil", "--import", "/etc/pki/akmods/certs/public_key.der"],
                    "Secure Boot: importing the module signing key",
                ),
                Note(
                    "After the reboot the blue MOK Manager screen appears. Pick Enroll MOK, "
                    "Continue, Yes and enter the password you have just set. Without this "
                    "step the NVIDIA module will not load with Secure Boot on."
                ),
            ]
        )
    steps.append(
        Note(
            "Building the module with akmods takes a few minutes and finishes after "
            "uniscript exits. Progress: journalctl -f -u akmods. Reboot only once it is done."
        )
    )
    return steps


def build(system: System) -> list[Task]:
    release = _dnf_release(system)
    has_nvidia = "nvidia" in system.gpu_vendors
    has_amd = "amd" in system.gpu_vendors
    has_intel = "intel" in system.gpu_vendors

    tasks: list[Task] = [
        Task(
            id="fedora-dnf-tuning",
            title="Faster dnf",
            summary="Parallel downloads, fastest mirror selection, yes as the default answer.",
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            default=True,
            details=[
                "max_parallel_downloads=10 instead of the default 3.",
                "fastestmirror=True measures mirror latency on every metadata refresh.",
                "keepcache=False frees the space taken by downloaded packages.",
                f"Only {DNF_CONF} is touched, the other entries in it are left alone.",
            ],
            steps=[WriteFile(DNF_CONF, _dnf_conf_content, "settings in the [main] section")],
            detect=lambda probe, sys_: probe.file_contains(DNF_CONF, "max_parallel_downloads"),
        ),
        Task(
            id="fedora-update",
            title="Full system upgrade",
            summary="dnf upgrade for every installed package.",
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            default=True,
            details=[
                "Worth doing before installing drivers, so the modules build against the "
                "current kernel.",
                "If the upgrade brings a new kernel, a reboot is needed.",
            ],
            steps=[
                Run(["dnf", "upgrade", "-y", "--refresh"], timeout=5400.0),
            ],
        ),
        Task(
            id="fedora-rpmfusion",
            title="RPM Fusion repositories (free and nonfree)",
            summary=(
                "Codecs, NVIDIA drivers, Steam and the rest of what Fedora cannot ship itself."
            ),
            category=Category.REPOS,
            risk=Risk.SAFE,
            default=True,
            details=[
                "free holds free software encumbered by patents (codecs).",
                "nonfree holds proprietary software (NVIDIA drivers, Steam).",
                "AppStream metadata is installed too, so the packages show up in the software "
                "centre.",
                f"Fedora release detected as {release}.",
            ],
            steps=[
                Run(
                    [
                        "dnf",
                        "install",
                        "-y",
                        f"https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-{release}.noarch.rpm",
                        f"https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-{release}.noarch.rpm",
                    ]
                ),
                Install(
                    ["rpmfusion-free-appstream-data", "rpmfusion-nonfree-appstream-data"],
                    optional=True,
                ),
                Run(["dnf", "makecache", "--refresh"], allow_fail=True),
            ],
            detect=lambda probe, sys_: probe.has_package(
                "rpmfusion-free-release", "rpmfusion-nonfree-release"
            ),
        ),
        Task(
            id="fedora-rpmfusion-tainted",
            title="RPM Fusion tainted: extra firmware and codecs",
            summary="The tainted repositories with hardware firmware and DVD libraries.",
            category=Category.REPOS,
            risk=Risk.MEDIUM,
            details=[
                "free-tainted holds libraries with an unclear legal status, libdvdcss for one.",
                "nonfree-tainted holds proprietary firmware missing from linux-firmware.",
                "Enable it only when firmware for your network card is missing or you play DVDs.",
            ],
            steps=[
                Install(
                    ["rpmfusion-free-release-tainted", "rpmfusion-nonfree-release-tainted"],
                    optional=True,
                ),
                Install(["libdvdcss"], optional=True),
            ],
            detect=lambda probe, sys_: probe.has_package("rpmfusion-free-release-tainted"),
        ),
        Task(
            id="fedora-openh264",
            title="H.264 codec for browsers",
            summary=(
                "Enables the Cisco repository and installs openh264 for Firefox and "
                "Chromium based browsers."
            ),
            category=Category.MULTIMEDIA,
            risk=Risk.SAFE,
            default=True,
            details=[
                "Without this codec video calls in the browser can run with no picture.",
                "The fedora-cisco-openh264 repository ships with Fedora, it only has to be "
                "enabled.",
            ],
            steps=[
                Run(["dnf", "config-manager", "enable", "fedora-cisco-openh264"], allow_fail=True),
                Install(
                    ["openh264", "gstreamer1-plugin-openh264", "mozilla-openh264"],
                    optional=True,
                ),
                Note("In Firefox open about:addons and enable the OpenH264 plugin."),
            ],
            detect=lambda probe, sys_: probe.has_package("mozilla-openh264"),
        ),
        Task(
            id="fedora-multimedia",
            title="Full ffmpeg and multimedia codecs",
            summary="Replaces the stripped down ffmpeg-free with the full ffmpeg from RPM Fusion.",
            category=Category.MULTIMEDIA,
            risk=Risk.MEDIUM,
            default=True,
            warning="Needs RPM Fusion enabled. Run the repository task first.",
            details=[
                "Fedora ships ffmpeg-free without the patent encumbered codecs.",
                "The swap brings H.264, H.265, AAC and the rest of the formats you meet in "
                "practice.",
                "GStreamer plugins and libavcodec-freeworld for browsers are added too.",
            ],
            steps=[
                Custom("swaps ffmpeg-free for ffmpeg (dnf swap)", _swap_ffmpeg),
                Install(
                    [
                        "gstreamer1-plugins-bad-freeworld",
                        "gstreamer1-plugins-ugly",
                        "libavcodec-freeworld",
                    ],
                    optional=True,
                ),
                Run(
                    [
                        "dnf",
                        "group",
                        "upgrade",
                        "-y",
                        "--setopt=install_weak_deps=False",
                        "--exclude=PackageKit-gstreamer-plugin",
                        "multimedia",
                    ],
                    allow_fail=True,
                    timeout=3600.0,
                ),
            ],
            detect=lambda probe, sys_: probe.has_package("libavcodec-freeworld"),
        ),
        Task(
            id="fedora-terra",
            title="Terra repository (Fyra Labs)",
            summary=(
                "Packages that are in neither Fedora nor RPM Fusion, among them a fresher "
                "Mesa and desktop tooling."
            ),
            category=Category.REPOS,
            risk=Risk.MEDIUM,
            warning=(
                "A third party repository. It can replace system packages with newer builds, "
                "which sometimes breaks upgrades. Do not mix it with COPRs shipping the same "
                "thing."
            ),
            details=[
                "The command follows the official Terra documentation (docs.terrapkg.com).",
                "The first install runs with --nogpgcheck, because the keys only arrive with "
                "terra-gpg-keys.",
                "After that, every further package is signature checked.",
            ],
            steps=[
                Run(
                    [
                        "dnf",
                        "install",
                        "-y",
                        "--nogpgcheck",
                        "--repofrompath",
                        TERRA_REPO,
                        "terra-release",
                        "terra-gpg-keys",
                    ]
                ),
            ],
            detect=lambda probe, sys_: probe.has_package("terra-release"),
        ),
        Task(
            id="fedora-copr",
            title="Enable a COPR repository",
            summary="Adds the COPR repository you name, for example user/project.",
            category=Category.REPOS,
            risk=Risk.MEDIUM,
            warning=(
                "COPR lets any Fedora user build packages. Nobody reviews them. Only enable "
                "repositories whose owner you trust."
            ),
            details=[
                "Format: owner/project. Separate several repositories with a space or a comma.",
                "List the enabled ones: dnf copr list. Disable: dnf copr disable owner/project.",
            ],
            prompt=InputPrompt(
                label="COPR repositories (owner/project)",
                placeholder="for example atim/starship solopasha/hyprland",
                validator=lambda value: (
                    None
                    if all("/" in part for part in value.replace(",", " ").split())
                    else "Every entry has to look like owner/project."
                ),
            ),
            steps=[
                Install(
                    ["dnf5-plugins"]
                    if system.package_manager and system.package_manager.name == "dnf5"
                    else ["dnf-plugins-core"],
                    optional=True,
                ),
                Custom("enables the COPR repositories you named", _enable_copr),
            ],
        ),
        Task(
            id="fedora-firmware",
            title="Firmware updates through fwupd",
            summary="Downloads and installs the firmware updates available for your hardware.",
            category=Category.SYSTEM,
            risk=Risk.MEDIUM,
            warning=(
                "A firmware update is the one operation here that can brick hardware if "
                "interrupted. On a laptop, plug in the charger and do not power off."
            ),
            details=[
                "List the detected devices: fwupdmgr get-devices.",
                "Some updates are only applied during a reboot.",
            ],
            steps=[
                Install(["fwupd"], optional=True),
                Run(["fwupdmgr", "refresh", "--force"], allow_fail=True),
                Run(["fwupdmgr", "get-updates"], allow_fail=True),
                Run(["fwupdmgr", "update", "-y"], allow_fail=True, timeout=3600.0),
            ],
        ),
        Task(
            id="fedora-archives",
            title="Archive and AppImage support",
            summary="7zip, unrar and the FUSE libraries AppImage files need.",
            category=Category.APPS,
            risk=Risk.SAFE,
            details=[
                "On Fedora 42 and newer the p7zip package is gone, 7zip took over its role.",
                "unrar comes from RPM Fusion nonfree.",
            ],
            steps=[Install(["7zip", "unrar", "fuse", "fuse-libs"], optional=True)],
            detect=lambda probe, sys_: probe.has_package("7zip"),
        ),
        Task(
            id="fedora-dualboot-rtc",
            title="Hardware clock in UTC (dual boot with Windows)",
            summary="Removes the time offset between Linux and Windows.",
            category=Category.SYSTEM,
            risk=Risk.SAFE,
            details=[
                "Windows keeps local time in the hardware clock, Linux keeps UTC.",
                "The command switches to the UTC standard. On the Windows side you then add "
                "the RealTimeIsUniversal registry entry or live with the offset there.",
            ],
            steps=[
                Run(
                    ["timedatectl", "set-local-rtc", "0", "--adjust-system-clock"], allow_fail=True
                ),
            ],
        ),
        Task(
            id="fedora-old-kernels",
            title="Remove old kernels",
            summary="Keeps the two newest kernels, deletes the rest along with their modules.",
            category=Category.MAINTENANCE,
            risk=Risk.MEDIUM,
            details=[
                "Fedora keeps 3 kernels by default, each one takes a few hundred MiB in /boot.",
                "At least two always remain, so there is a way back if the newest one misbehaves.",
            ],
            steps=[
                Custom("finds and removes kernels older than the last two", _remove_old_kernels)
            ],
        ),
    ]

    if has_nvidia:
        nvidia = next(gpu for gpu in system.gpus if gpu.vendor == "nvidia")
        open_module = nvidia.open_kernel_module_capable
        secure_boot_note = (
            "on, the MOK key will be imported"
            if system.secure_boot
            else "off, module signing is not needed"
        )
        tasks.append(
            Task(
                id="fedora-nvidia",
                title="NVIDIA driver from RPM Fusion",
                summary=(
                    f"akmod-nvidia-open for {nvidia.name}"
                    if open_module
                    else f"akmod-nvidia (proprietary module) for {nvidia.name}"
                ),
                category=Category.DRIVERS,
                risk=Risk.MEDIUM,
                warning=(
                    "Needs RPM Fusion nonfree enabled. After the install the module builds in "
                    "the background for a few minutes. Reboot only once that finishes, "
                    "otherwise the system comes up on nouveau or in a fallback mode."
                ),
                details=[
                    f"Detected card: {nvidia.name} (PCI {nvidia.vendor_id}:{nvidia.device_id}).",
                    (
                        "A Turing or newer chip, so the open kernel module "
                        "(akmod-nvidia-open) is used, the one NVIDIA recommends for recent "
                        "cards."
                        if open_module
                        else "A chip older than Turing, the open module does not support it, "
                        "so the proprietary module (akmod-nvidia) is installed."
                    ),
                    "xorg-x11-drv-nvidia-power adds the services that fix suspend and resume.",
                    "libva-nvidia-driver enables hardware video decoding in the browser.",
                    f"Secure Boot: {secure_boot_note}.",
                ],
                steps=_nvidia_steps(system),
                reboot=True,
                detect=lambda probe, sys_: probe.has_any_package(
                    "akmod-nvidia", "akmod-nvidia-open"
                ),
                available=lambda sys_: "nvidia" in sys_.gpu_vendors,
            )
        )

    if has_amd:
        tasks.append(
            Task(
                id="fedora-amd",
                title="AMD hardware video decoding",
                summary="The VA drivers from RPM Fusion plus Vulkan and diagnostic tools.",
                category=Category.DRIVERS,
                risk=Risk.SAFE,
                default=True,
                details=[
                    "Fedora ships Mesa without the H.264 and H.265 decoders because of patents.",
                    "mesa-va-drivers-freeworld brings hardware decoding back to browsers and "
                    "players.",
                    "On Fedora 42 and newer that package also absorbed the VDPAU part.",
                    "Check afterwards with: vainfo.",
                ],
                steps=[
                    Custom("installs mesa-va-drivers-freeworld", _swap_va_drivers),
                    Install(
                        [
                            "mesa-vulkan-drivers",
                            "mesa-vulkan-drivers.i686",
                            "vulkan-loader",
                            "vulkan-loader.i686",
                            "libva-utils",
                            "vulkan-tools",
                        ],
                        optional=True,
                    ),
                ],
                detect=lambda probe, sys_: probe.has_package("mesa-va-drivers-freeworld"),
                available=lambda sys_: "amd" in sys_.gpu_vendors,
            )
        )

    if has_intel:
        tasks.append(
            Task(
                id="fedora-intel",
                title="Intel hardware video decoding",
                summary="intel-media-driver for 8th generation chips and newer.",
                category=Category.DRIVERS,
                risk=Risk.SAFE,
                default=True,
                details=[
                    "intel-media-driver covers Gen 8 and newer, in practice everything from "
                    "Broadwell on.",
                    "For much older chips (Gen 7 and earlier) the right package is "
                    "libva-intel-driver.",
                    "Both come from RPM Fusion.",
                    "Check afterwards with: vainfo.",
                ],
                steps=[
                    Install(["intel-media-driver", "libva-utils"], optional=True),
                    Note(
                        "For chips older than the 8th generation install libva-intel-driver "
                        "instead."
                    ),
                ],
                detect=lambda probe, sys_: probe.has_any_package(
                    "intel-media-driver", "libva-intel-media-driver"
                ),
                available=lambda sys_: "intel" in sys_.gpu_vendors,
            )
        )

    tasks.append(
        Task(
            id="fedora-gaming",
            title="Gaming setup",
            summary="Steam, Lutris, MangoHud, GameMode, Gamescope, Wine and the 32-bit libraries.",
            category=Category.GAMING,
            risk=Risk.MEDIUM,
            tags=frozenset({"gaming"}),
            warning="Needs RPM Fusion nonfree enabled, that is where Steam comes from.",
            details=[
                "Steam from the repository, not Flatpak: easier access to drives and controllers.",
                "GameMode raises the priority of the game process and switches the CPU "
                "governor to performance.",
                "Gamescope runs a game in a micro compositor, with its own resolution and scaling.",
                "vkBasalt adds image sharpening, goverlay configures MangoHud in a window.",
                "steam-devices are the udev rules for gamepads, Steam Controller and DualSense "
                "included.",
                "The 32-bit Vulkan and Mesa libraries older games need are installed too.",
            ],
            steps=[
                Install(
                    [
                        "steam",
                        "steam-devices",
                        "lutris",
                        "mangohud",
                        "mangohud.i686",
                        "gamemode",
                        "gamemode.i686",
                        "gamescope",
                        "goverlay",
                        "vkBasalt",
                        "wine",
                        "winetricks",
                        "protontricks",
                        "vulkan-loader",
                        "vulkan-loader.i686",
                        "mesa-vulkan-drivers",
                        "mesa-vulkan-drivers.i686",
                        "mesa-dri-drivers.i686",
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

    tasks.append(
        Task(
            id="fedora-btrfs-snapshots",
            title="Btrfs filesystem snapshots",
            summary="snapper and btrfs-assistant, to roll the system back after a bad upgrade.",
            category=Category.MAINTENANCE,
            risk=Risk.MEDIUM,
            details=[
                "A btrfs filesystem was detected on the root partition.",
                "After the install a configuration still has to be created: snapper -c root "
                "create-config /",
                "Snapshots take space in proportion to the number of changes, not to the size "
                "of the system.",
            ],
            steps=[
                Install(["btrfs-assistant", "snapper"], optional=True),
                Note(
                    "Create the configuration: sudo snapper -c root create-config / , then "
                    "enable the snapper-timeline.timer and snapper-cleanup.timer timers."
                ),
            ],
            detect=lambda probe, sys_: probe.has_package("snapper"),
            available=lambda sys_: sys_.root_fs == "btrfs",
        )
    )

    if system.is_atomic:
        tasks.insert(
            0,
            Task(
                id="fedora-atomic-warning",
                title="An atomic Fedora variant was detected",
                summary=(
                    "On Silverblue, Kinoite and Bazzite packages are installed with rpm-ostree."
                ),
                category=Category.SYSTEM,
                risk=Risk.SAFE,
                details=[
                    "/usr is read only, dnf cannot change the system image.",
                    "Tasks installing packages through dnf will not work. Use rpm-ostree "
                    "install or, better, Flatpak and a toolbox container.",
                    "Flatpak tasks, file configuration and tweaks work normally.",
                ],
                steps=[Note("Atomic variant: install packages with rpm-ostree install, not dnf.")],
                available=lambda sys_: sys_.is_atomic,
            ),
        )

    return tasks
