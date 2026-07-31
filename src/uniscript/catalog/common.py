"""Distribution independent tasks: Flatpak, Snap, tweaks, gaming."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..core.context import ExecContext
from ..core.probe import Probe
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
    WriteFile,
)

FLATHUB_URL = "https://flathub.org/repo/flathub.flatpakrepo"

SYSCTL_PATH = "/etc/sysctl.d/99-uniscript.conf"
GAMING_SYSCTL_PATH = "/etc/sysctl.d/99-uniscript-gaming.conf"
GAMING_LIMITS_PATH = "/etc/security/limits.d/99-uniscript-gaming.conf"
IO_SCHEDULER_PATH = "/etc/udev/rules.d/60-uniscript-scheduler.rules"
JOURNALD_PATH = "/etc/systemd/journald.conf.d/99-uniscript.conf"
ZRAM_PATH = "/etc/systemd/zram-generator.conf"
RESOLVED_PATH = "/etc/systemd/resolved.conf.d/99-uniscript-dns.conf"
NOSNAP_PATH = "/etc/apt/preferences.d/99-uniscript-nosnap.pref"
MANGOHUD_PATH = "~/.config/MangoHud/MangoHud.conf"
SHELLS_PATH = "/etc/shells"

STARSHIP_MARKER = "# uniscript: starship prompt"

HEADER = "# Written by uniscript. The original is kept in ~/.local/share/uniscript/backups.\n"


def by_family(
    system: System,
    *,
    rhel: list[str] | None = None,
    debian: list[str] | None = None,
    arch: list[str] | None = None,
    suse: list[str] | None = None,
    default: list[str] | None = None,
) -> list[str]:
    """Pick the package names that belong to this distribution family."""
    mapping = {"rhel": rhel, "debian": debian, "arch": arch, "suse": suse}
    return mapping.get(system.family) or default or []


# ---------------------------------------------------------------- Flatpak


def _flatpak_tasks(system: System) -> list[Task]:
    flatpak_packages = by_family(
        system,
        rhel=["flatpak"],
        debian=["flatpak"],
        arch=["flatpak"],
        suse=["flatpak"],
        default=["flatpak"],
    )
    plugin_packages = by_family(
        system,
        debian=["gnome-software-plugin-flatpak"] if "GNOME" in system.desktop.upper() else [],
        default=[],
    )

    tasks: list[Task] = [
        Task(
            id="flatpak-flathub",
            title="Flatpak and the Flathub remote",
            summary="Installs flatpak and adds Flathub as a system wide remote.",
            category=Category.PACKAGING,
            details=[
                "Flathub is the largest repository of Flatpak applications.",
                "The remote is added system wide (--system), not only for the current user.",
                "Flathub applications run sandboxed and do not depend on the library versions "
                "installed in the system.",
            ],
            risk=Risk.SAFE,
            default=True,
            steps=[
                Install(flatpak_packages + plugin_packages, optional=True),
                Run(
                    [
                        "flatpak",
                        "remote-add",
                        "--if-not-exists",
                        "--system",
                        "flathub",
                        FLATHUB_URL,
                    ]
                ),
            ],
            detect=lambda probe, sys_: "flathub" in probe.flatpak_remotes,
        ),
        Task(
            id="flatpak-verified-subset",
            title="Flathub limited to verified applications",
            summary="Limits Flathub to packages published by the application authors themselves.",
            category=Category.PACKAGING,
            details=[
                "The 'verified' subset only shows applications confirmed by their authors.",
                "Third party packages disappear, including popular but unofficial ones.",
                "To revert: flatpak remote-modify --subset='' flathub",
            ],
            risk=Risk.MEDIUM,
            steps=[
                Run(["flatpak", "remote-modify", "--system", "--subset=verified", "flathub"]),
            ],
            available=lambda sys_: True,
        ),
        Task(
            id="flatpak-theme-access",
            title="Flatpak access to the system theme",
            summary=(
                "Lets Flatpak applications read the GTK configuration so they match the desktop."
            ),
            category=Category.PACKAGING,
            details=[
                "Without it Flatpak applications use the default Adwaita theme whatever the "
                "desktop is.",
                "Access is granted read only (:ro).",
            ],
            risk=Risk.SAFE,
            default=True,
            steps=[
                Run(
                    [
                        "flatpak",
                        "override",
                        "--system",
                        "--filesystem=xdg-config/gtk-3.0:ro",
                        "--filesystem=xdg-config/gtk-4.0:ro",
                        "--filesystem=/usr/share/themes:ro",
                        "--filesystem=xdg-data/themes:ro",
                        "--filesystem=xdg-data/icons:ro",
                    ]
                ),
            ],
        ),
        Task(
            id="flatpak-tools",
            title="Tools for managing Flatpaks",
            summary="Flatseal (permissions), Warehouse (cleanup), Gear Lever (AppImage).",
            category=Category.PACKAGING,
            details=[
                "com.github.tchx84.Flatseal edits the sandbox permissions of an application.",
                "io.github.flattool.Warehouse removes data left behind by applications.",
                "it.mijorus.gearlever integrates AppImage files into the system menu.",
            ],
            risk=Risk.SAFE,
            steps=[
                _flatpak_install(
                    [
                        "com.github.tchx84.Flatseal",
                        "io.github.flattool.Warehouse",
                        "it.mijorus.gearlever",
                    ]
                ),
            ],
            detect=lambda probe, sys_: probe.has_flatpak_app("com.github.tchx84.Flatseal"),
        ),
    ]
    return tasks


def _flatpak_install(app_ids: list[str], remote: str = "flathub") -> Step:
    return Run(
        ["flatpak", "install", "--system", "-y", "--noninteractive", remote, *app_ids],
        timeout=3600.0,
    )


# ------------------------------------------------------------------- Snap


async def _purge_snaps(ctx: ExecContext) -> None:
    """Remove snaps in order, applications first, core last."""
    listing = await ctx.capture(["snap", "list"])
    names = [line.split()[0] for line in listing.splitlines()[1:] if line.split()]
    core_last = [
        n
        for n in names
        if n
        not in (
            "snapd",
            "core",
            "core18",
            "core20",
            "core22",
            "core24",
            "bare",
            "snapd-desktop-integration",
        )
    ]
    tail = [
        n
        for n in names
        if n
        in ("snapd-desktop-integration", "bare", "core24", "core22", "core20", "core18", "core")
    ]
    if not names:
        ctx.log("no snaps installed", "skip")
        return
    for name in core_last + tail:
        await ctx.run(["snap", "remove", "--purge", name], root=True, allow_fail=True)


def _snap_tasks(system: System) -> list[Task]:
    tasks: list[Task] = []

    if system.family == "debian":
        tasks.append(
            Task(
                id="snap-purge",
                title="Remove snapd and block it from coming back",
                summary=(
                    "Deletes every snap, removes snapd and blocks apt from installing it again."
                ),
                category=Category.PACKAGING,
                risk=Risk.MEDIUM,
                warning=(
                    "On Ubuntu, Firefox and Thunderbird are snaps. Remove snapd together with "
                    "the 'Firefox from the Mozilla repository' task, otherwise you end up "
                    "without a browser."
                ),
                details=[
                    "Order: applications, then the core packages, then snapd itself.",
                    f"The apt block goes to {NOSNAP_PATH} with Pin-Priority: -10.",
                    "The ~/snap directory and /var/cache/snapd are deleted.",
                    "To revert: delete the pin file and install snapd again.",
                ],
                steps=[
                    Custom("removes every installed snap in order", _purge_snaps),
                    Unit("snapd.service", "disable", now=True),
                    Unit("snapd.socket", "disable", now=True),
                    Unit("snapd.seeded.service", "disable", now=True),
                    Run(["apt-get", "purge", "-y", "snapd"], allow_fail=True),
                    Run(["rm", "-rf", "/var/cache/snapd", "/var/snap", "/snap"], allow_fail=True),
                    Custom(
                        "removes the ~/snap directory of the current user",
                        _remove_user_snap_dir,
                    ),
                    WriteFile(
                        NOSNAP_PATH,
                        _nosnap_pin,
                        "apt pin blocking the snapd package",
                    ),
                    Note("snapd removed. Replace snap applications with Flatpak or deb packages."),
                ],
                detect=lambda probe, sys_: (
                    "snapd" not in probe.installed_packages and Path(NOSNAP_PATH).exists()
                ),
                available=lambda sys_: sys_.family == "debian",
            )
        )

    tasks.append(
        Task(
            id="snap-install",
            title="Install snapd (not recommended)",
            summary="Adds snap support to a system that does not have it.",
            category=Category.PACKAGING,
            risk=Risk.HIGH,
            warning=(
                "Highly not recommended. Snap mounts every application as a separate loop "
                "device, slows down the first start of a program, updates in the background "
                "without asking, and outside Ubuntu it has a single proprietary store with no "
                "alternative. Flatpak does the same job better."
            ),
            details=[
                "Installs the snapd package and enables its systemd socket.",
                "Fedora and openSUSE additionally need the /snap symlink.",
                "If you are after applications outside the distribution repository, pick "
                "Flatpak instead.",
            ],
            steps=[
                Install(
                    by_family(
                        system,
                        rhel=["snapd"],
                        debian=["snapd"],
                        arch=["snapd"],
                        suse=["snapd"],
                        default=["snapd"],
                    )
                ),
                Unit("snapd.socket", "enable", now=True),
                Custom("creates the /snap symlink where the distribution needs it", _snap_symlink),
                Note("Log out and back in so the snap paths land in PATH."),
            ],
            detect=lambda probe, sys_: "snapd" in probe.installed_packages,
        )
    )
    return tasks


async def _remove_user_snap_dir(ctx: ExecContext) -> None:
    target = ctx.system.home / "snap"
    if not target.exists():
        ctx.log("no ~/snap directory", "skip")
        return
    await ctx.run(["rm", "-rf", str(target)], root=False, allow_fail=True)


async def _snap_symlink(ctx: ExecContext) -> None:
    if ctx.system.family == "debian":
        ctx.log("Debian and Ubuntu do not need the symlink", "skip")
        return
    if Path("/snap").exists():
        ctx.log("/snap already exists", "skip")
        return
    await ctx.run(["ln", "-s", "/var/lib/snapd/snap", "/snap"], root=True, allow_fail=True)


async def _nosnap_pin(ctx: ExecContext) -> str:
    return (
        HEADER + "# Blocks installing snapd, including as a dependency of another package.\n"
        "Package: snapd\n"
        "Pin: release a=*\n"
        "Pin-Priority: -10\n"
    )


# ------------------------------------------------------------------ Tweaks


async def _swap_devices(ctx: ExecContext) -> list[str]:
    content = await ctx.read_file("/proc/swaps") or ""
    return [line.split()[0] for line in content.splitlines()[1:] if line.split()]


async def _sysctl_content(ctx: ExecContext) -> str:
    devices = await _swap_devices(ctx)
    zram_only = bool(devices) and all(dev.startswith("/dev/zram") for dev in devices)
    if zram_only:
        swappiness, page_cluster = 150, 0
        reason = "swap lives in zram only, compression in RAM is cheap, so swap eagerly"
    elif devices:
        swappiness, page_cluster = 10, 3
        reason = "swap on disk, keep page movement down"
    else:
        swappiness, page_cluster = 60, 3
        reason = "no swap, kernel default"

    return (
        HEADER + f"# {reason}\n"
        f"vm.swappiness = {swappiness}\n"
        f"vm.page-cluster = {page_cluster}\n"
        "\n"
        "# Evict directory and inode metadata from the cache less eagerly.\n"
        "vm.vfs_cache_pressure = 50\n"
        "\n"
        "# Smaller dirty page window, shorter stalls on large writes.\n"
        "vm.dirty_background_ratio = 5\n"
        "vm.dirty_ratio = 15\n"
        "\n"
        "# fq queueing and BBR: lower latency on a saturated link.\n"
        "net.core.default_qdisc = fq\n"
        "net.ipv4.tcp_congestion_control = bbr\n"
        "\n"
        "# Faster detection of broken TCP connections.\n"
        "net.ipv4.tcp_fastopen = 3\n"
        "\n"
        "# More inotify watches: IDEs and file synchronization need them.\n"
        "fs.inotify.max_user_watches = 524288\n"
        "fs.inotify.max_user_instances = 512\n"
    )


async def _gaming_sysctl_content(ctx: ExecContext) -> str:
    return (
        HEADER + "# Memory mapping limit. Game engines under Proton can exceed the default.\n"
        "vm.max_map_count = 2147483642\n"
        "\n"
        "# Split lock mitigation costs frames in some engines.\n"
        "kernel.split_lock_mitigate = 0\n"
    )


async def _gaming_limits_content(ctx: ExecContext) -> str:
    return (
        HEADER + "# Wine esync opens one file descriptor per synchronization object.\n"
        "*               soft    nofile          1048576\n"
        "*               hard    nofile          1048576\n"
    )


async def _io_scheduler_content(ctx: ExecContext) -> str:
    return (
        HEADER + "# NVMe has its own hardware queues, a software scheduler only gets in the way.\n"
        'ACTION=="add|change", KERNEL=="nvme[0-9]*n[0-9]*", ATTR{queue/scheduler}="none"\n'
        "\n"
        "# SATA SSD: mq-deadline. Spinning disk: bfq, which keeps the desktop responsive.\n"
        'ACTION=="add|change", KERNEL=="sd[a-z]", '
        'ATTR{queue/rotational}=="0", ATTR{queue/scheduler}="mq-deadline"\n'
        'ACTION=="add|change", KERNEL=="sd[a-z]", '
        'ATTR{queue/rotational}=="1", ATTR{queue/scheduler}="bfq"\n'
    )


async def _journald_content(ctx: ExecContext) -> str:
    return (
        HEADER + "[Journal]\n"
        "# The journal can grow to a few percent of the partition. Cap it at 500 MiB.\n"
        "SystemMaxUse=500M\n"
        "SystemMaxFileSize=50M\n"
        "SystemMaxFiles=10\n"
    )


async def _zram_content(ctx: ExecContext) -> str:
    total_gib = ctx.system.mem_total_kb / 1024 / 1024
    size_mb = int(min(total_gib, 8) * 1024)
    return (
        HEADER + "[zram0]\n"
        f"# Half of memory, at most 8 GiB. Detected {total_gib:.1f} GiB RAM.\n"
        f"zram-size = min(ram / 2, {size_mb})\n"
        "compression-algorithm = zstd\n"
        "swap-priority = 100\n"
        "fs-type = swap\n"
    )


async def _resolved_content(ctx: ExecContext) -> str:
    return (
        HEADER + "[Resolve]\n"
        "# Cloudflare and Quad9, opportunistic encryption: when the server has no DoT,\n"
        "# the query goes out unencrypted instead of failing.\n"
        "DNS=1.1.1.1#cloudflare-dns.com 9.9.9.9#dns.quad9.net\n"
        "FallbackDNS=1.0.0.1#cloudflare-dns.com 149.112.112.112#dns.quad9.net\n"
        "DNSOverTLS=opportunistic\n"
        "DNSSEC=allow-downgrade\n"
        "Cache=yes\n"
    )


async def _mangohud_content(ctx: ExecContext) -> str:
    return (
        "# Written by uniscript. Full option list: man mangohud\n"
        "fps\n"
        "frametime=0\n"
        "frame_timing=1\n"
        "gpu_stats\n"
        "gpu_temp\n"
        "gpu_load_change\n"
        "cpu_stats\n"
        "cpu_temp\n"
        "cpu_load_change\n"
        "ram\n"
        "vram\n"
        "position=top-left\n"
        "font_size=20\n"
        "background_alpha=0.4\n"
        "toggle_hud=Shift_R+F12\n"
        "toggle_logging=Shift_L+F2\n"
    )


def _tweak_tasks(system: System) -> list[Task]:
    zram_packages = by_family(
        system,
        rhel=["zram-generator-defaults"],
        debian=["systemd-zram-generator"],
        arch=["zram-generator"],
        suse=["zram-generator"],
        default=["zram-generator"],
    )

    tasks: list[Task] = [
        Task(
            id="tweak-sysctl",
            title="Kernel parameters: memory, disk, network",
            summary="Tunes swappiness, write buffers, network queueing and inotify limits.",
            category=Category.TWEAKS,
            risk=Risk.SAFE,
            default=True,
            details=[
                "swappiness is picked automatically: one value for zram, another for swap on disk.",
                "BBR and fq cut latency on a fully saturated link.",
                "The inotify limits fix the usual 'too many open files' error in editors.",
                f"Everything lands in one file, {SYSCTL_PATH}, removable with a single rm.",
            ],
            steps=[
                WriteFile(SYSCTL_PATH, _sysctl_content, "vm, net and fs parameters"),
                Run(["sysctl", "--system"], allow_fail=True),
            ],
            detect=lambda probe, sys_: Path(SYSCTL_PATH).exists(),
        ),
        Task(
            id="tweak-zram",
            title="Compressed swap in RAM (zram)",
            summary="Sets up zram instead of, or next to, swap on disk.",
            category=Category.TWEAKS,
            risk=Risk.SAFE,
            details=[
                "Size: half of memory, at most 8 GiB, zstd compression.",
                "Writes never reach the disk, so swap does not wear out the SSD.",
                "If the configuration file already exists the task is reported as done and "
                "your settings are left alone.",
            ],
            steps=[
                Install(zram_packages, optional=True),
                WriteFile(ZRAM_PATH, _zram_content, "the zram0 device"),
                Run(["systemctl", "daemon-reload"], allow_fail=True),
                Note("zram starts on the next boot."),
            ],
            detect=lambda probe, sys_: Path(ZRAM_PATH).exists(),
            available=lambda sys_: sys_.has_systemd,
        ),
        Task(
            id="tweak-io-scheduler",
            title="IO scheduler matched to the drive",
            summary="none for NVMe, mq-deadline for SATA SSDs, bfq for spinning disks.",
            category=Category.TWEAKS,
            risk=Risk.SAFE,
            details=[
                "A udev rule, active from the next device attach or reboot.",
                "Dropping the scheduler on NVMe removes per request overhead.",
                f"File: {IO_SCHEDULER_PATH}.",
            ],
            steps=[
                WriteFile(IO_SCHEDULER_PATH, _io_scheduler_content, "udev rules for the scheduler"),
                Run(["udevadm", "control", "--reload-rules"], allow_fail=True),
                Run(["udevadm", "trigger", "--subsystem-match=block"], allow_fail=True),
            ],
            detect=lambda probe, sys_: Path(IO_SCHEDULER_PATH).exists(),
        ),
        Task(
            id="tweak-journald",
            title="Cap the size of the systemd journal",
            summary="The journal stops growing without a bound, capped at 500 MiB.",
            category=Category.TWEAKS,
            risk=Risk.SAFE,
            default=True,
            details=[
                "By default journald takes up to 10 percent of the /var partition.",
                "Old entries are removed automatically once the limit is crossed.",
            ],
            steps=[
                WriteFile(JOURNALD_PATH, _journald_content, "journal limits"),
                Run(["systemctl", "restart", "systemd-journald"], allow_fail=True),
            ],
            detect=lambda probe, sys_: Path(JOURNALD_PATH).exists(),
            available=lambda sys_: sys_.has_systemd,
        ),
        Task(
            id="tweak-fstrim",
            title="Weekly SSD TRIM",
            summary="Enables fstrim.timer, which keeps SSD write performance up.",
            category=Category.TWEAKS,
            risk=Risk.SAFE,
            default=True,
            details=["A one off systemd unit enable, no configuration file is touched."],
            steps=[Unit("fstrim.timer", "enable", now=True)],
            detect=lambda probe, sys_: probe.unit_enabled("fstrim.timer"),
            available=lambda sys_: sys_.has_systemd,
        ),
        Task(
            id="tweak-boot-wait-online",
            title="Shorter boot",
            summary="Disables NetworkManager-wait-online, which can add a dozen seconds.",
            category=Category.TWEAKS,
            risk=Risk.MEDIUM,
            details=[
                "The unit holds back network-online.target until an address is assigned.",
                "Disable it only when no service of yours has to start after the network is up.",
                "To revert: systemctl enable NetworkManager-wait-online.service",
            ],
            steps=[Unit("NetworkManager-wait-online.service", "disable", now=False)],
            detect=lambda probe, sys_: not probe.unit_enabled("NetworkManager-wait-online.service"),
            available=lambda sys_: sys_.has_systemd,
        ),
        Task(
            id="tweak-dns",
            title="Encrypted DNS through systemd-resolved",
            summary="Cloudflare and Quad9 with DNS over TLS in opportunistic mode.",
            category=Category.TWEAKS,
            risk=Risk.MEDIUM,
            warning=(
                "Switches DNS to an external provider. On a corporate or home network with a "
                "local name server this can break internal name resolution."
            ),
            details=[
                "Opportunistic mode: when TLS is unavailable the query goes out unencrypted "
                "instead of stalling traffic.",
                f"File: {RESOLVED_PATH}. To revert, delete it and restart systemd-resolved.",
            ],
            steps=[
                WriteFile(RESOLVED_PATH, _resolved_content, "resolver configuration"),
                Run(["systemctl", "restart", "systemd-resolved"], allow_fail=True),
            ],
            detect=lambda probe, sys_: Path(RESOLVED_PATH).exists(),
            available=lambda sys_: sys_.has_systemd,
        ),
        Task(
            id="tweak-firewall",
            title="Firewall enabled",
            summary="Starts firewalld or ufw, depending on the distribution.",
            category=Category.TWEAKS,
            risk=Risk.SAFE,
            default=True,
            details=["The default zone rejects incoming connections, outgoing ones stay open."],
            steps=_firewall_steps(system),
            detect=lambda probe, sys_: (
                probe.unit_enabled("firewalld.service") or probe.unit_enabled("ufw.service")
            ),
        ),
    ]

    if system.is_portable:
        tasks.append(
            Task(
                id="tweak-power-laptop",
                title="Power profile for a laptop",
                summary="Installs power-profiles-daemon and selects the balanced profile.",
                category=Category.TWEAKS,
                risk=Risk.SAFE,
                details=[
                    "A portable chassis or a battery was detected.",
                    "GNOME and KDE support power-profiles-daemon with no extra configuration.",
                    "Do not install TLP alongside it, the two fight over the same settings.",
                ],
                steps=[
                    Install(["power-profiles-daemon"], optional=True),
                    Unit("power-profiles-daemon.service", "enable", now=True),
                ],
                detect=lambda probe, sys_: probe.unit_enabled("power-profiles-daemon.service"),
                available=lambda sys_: sys_.is_portable,
            )
        )

    if system.has("tuned-adm") or system.family in ("rhel", "suse"):
        profile = "balanced" if system.is_portable else "throughput-performance"
        chassis = "laptop" if system.is_portable else "desktop"
        tasks.append(
            Task(
                id="tweak-tuned",
                title=f"tuned profile: {profile}",
                summary="Enables the tuned daemon and selects a profile matching the machine.",
                category=Category.TWEAKS,
                risk=Risk.SAFE,
                details=[
                    f"Selected profile: {profile} (detected a {chassis}).",
                    "The profile can be changed later with tuned-adm profile <name>.",
                    "List of profiles: tuned-adm list",
                ],
                steps=[
                    Install(["tuned"], optional=True),
                    Unit("tuned.service", "enable", now=True),
                    Run(["tuned-adm", "profile", profile], allow_fail=True),
                ],
                detect=lambda probe, sys_: probe.unit_enabled("tuned.service"),
            )
        )

    tasks.append(
        Task(
            id="tweak-mitigations-off",
            title="Disable CPU mitigations (mitigations=off)",
            summary="Removes the Spectre and Meltdown mitigations from the kernel command line.",
            category=Category.TWEAKS,
            risk=Risk.HIGH,
            warning=(
                "This deliberately weakens security. A process in a browser or a virtual "
                "machine can read the memory of another process. Do this only on a machine "
                "with no sensitive data, offline, or used purely for games."
            ),
            details=[
                "The gain depends on the CPU: several to a dozen percent on older Intel "
                "parts, usually below the noise floor on recent AMD ones.",
                "To revert on Fedora: sudo grubby --update-kernel=ALL "
                "--remove-args='mitigations=off'",
                "To revert on Debian and Ubuntu: remove the entry from /etc/default/grub and "
                "run update-grub.",
            ],
            steps=_mitigations_steps(system),
            reboot=True,
            available=lambda sys_: sys_.family in ("rhel", "debian") or bool(sys_.has("grubby")),
        )
    )
    return tasks


def _firewall_steps(system: System) -> list[Step]:
    if system.family == "debian":
        return [
            Install(["ufw"], optional=True),
            Run(["ufw", "--force", "enable"], allow_fail=True),
        ]
    return [
        Install(["firewalld"], optional=True),
        Unit("firewalld.service", "enable", now=True),
    ]


def _mitigations_steps(system: System) -> list[Step]:
    if system.has("grubby"):
        return [
            Run(["grubby", "--update-kernel=ALL", "--args=mitigations=off"]),
            Note("The change takes effect after a reboot. Check with: cat /proc/cmdline"),
        ]
    if system.family == "debian":
        return [
            Custom(
                "appends mitigations=off to GRUB_CMDLINE_LINUX_DEFAULT",
                _debian_add_mitigations,
            ),
            Run(["update-grub"], allow_fail=False),
            Note("The change takes effect after a reboot. Check with: cat /proc/cmdline"),
        ]
    return [
        Note(
            "No supported bootloader was detected. Add mitigations=off to the kernel command "
            "line of your bootloader by hand."
        )
    ]


async def _debian_add_mitigations(ctx: ExecContext) -> None:
    path = "/etc/default/grub"
    current = await ctx.read_file(path)
    if current is None:
        ctx.log(f"no {path}, skipping", "warn")
        return
    if "mitigations=off" in current:
        ctx.log("the parameter is already there", "skip")
        return
    lines = current.splitlines()
    key = "GRUB_CMDLINE_LINUX_DEFAULT="
    for index, line in enumerate(lines):
        if line.startswith(key):
            value = line[len(key) :].strip().strip('"').strip()
            merged = f"{value} mitigations=off".strip()
            lines[index] = f'{key}"{merged}"'
            break
    else:
        lines.append(f'{key}"mitigations=off"')
    await ctx.write_file(path, "\n".join(lines) + "\n", root=True)


# ----------------------------------------------------------------- Gaming


def _gaming_tasks(system: System) -> list[Task]:
    return [
        Task(
            id="gaming-kernel-limits",
            title="Kernel limits for gaming",
            summary="vm.max_map_count and the open file limit that Proton and esync need.",
            category=Category.GAMING,
            risk=Risk.SAFE,
            tags=frozenset({"gaming"}),
            details=[
                "Without a raised max_map_count some Proton games crash while loading.",
                "The nofile limit of 1048576 is required by esync in Wine.",
                f"Files: {GAMING_SYSCTL_PATH} and {GAMING_LIMITS_PATH}.",
            ],
            steps=[
                WriteFile(GAMING_SYSCTL_PATH, _gaming_sysctl_content, "memory limits for games"),
                WriteFile(GAMING_LIMITS_PATH, _gaming_limits_content, "file descriptor limit"),
                Run(["sysctl", "--system"], allow_fail=True),
                Note("The descriptor limit applies after you log in again."),
            ],
            detect=lambda probe, sys_: (
                Path(GAMING_SYSCTL_PATH).exists() and Path(GAMING_LIMITS_PATH).exists()
            ),
        ),
        Task(
            id="gaming-mangohud-config",
            title="MangoHud overlay configuration",
            summary="Sets up a readable overlay with FPS, temperatures and load.",
            category=Category.GAMING,
            risk=Risk.SAFE,
            tags=frozenset({"gaming"}),
            details=[
                "Right Shift with F12 toggles the overlay.",
                f"A user file: {MANGOHUD_PATH}, no root needed.",
                "To start a game with the overlay: mangohud %command% in the Steam launch options.",
            ],
            steps=[
                WriteFile(
                    MANGOHUD_PATH,
                    _mangohud_content,
                    "overlay settings",
                    root=False,
                ),
            ],
            detect=lambda probe, sys_: (sys_.home / ".config/MangoHud/MangoHud.conf").exists(),
        ),
        Task(
            id="gaming-protonup",
            title="ProtonUp-Qt for managing Proton GE",
            summary=(
                "Installs the tool that downloads Proton GE and Wine GE for Steam, Lutris "
                "and Heroic."
            ),
            category=Category.GAMING,
            risk=Risk.SAFE,
            tags=frozenset({"gaming"}),
            details=[
                "Proton GE carries patches and codecs that are missing from Valve's Proton.",
                "After installing, start ProtonUp-Qt, pick Steam and install the newest GE-Proton.",
                "In the game properties in Steam, select that version under Compatibility.",
            ],
            steps=[_flatpak_install(["net.davidotek.pupgui2"])],
            detect=lambda probe, sys_: probe.has_flatpak_app("net.davidotek.pupgui2"),
        ),
        Task(
            id="gaming-heroic",
            title="Heroic Games Launcher",
            summary="A client for the Epic Games Store, GOG and Amazon Games.",
            category=Category.GAMING,
            risk=Risk.SAFE,
            tags=frozenset({"gaming"}),
            details=["The Flatpak build, independent of the system libraries."],
            steps=[_flatpak_install(["com.heroicgameslauncher.hgl"])],
            detect=lambda probe, sys_: probe.has_flatpak_app("com.heroicgameslauncher.hgl"),
        ),
        Task(
            id="gaming-bottles",
            title="Bottles",
            summary="Manages Wine prefixes for programs outside Steam.",
            category=Category.GAMING,
            risk=Risk.SAFE,
            tags=frozenset({"gaming"}),
            details=["Useful for launchers that neither Heroic nor Lutris handles."],
            steps=[_flatpak_install(["com.usebottles.bottles"])],
            detect=lambda probe, sys_: probe.has_flatpak_app("com.usebottles.bottles"),
        ),
    ]


# ------------------------------------------------------------ Shell


async def _shell_path(ctx: ExecContext, name: str) -> str | None:
    """Absolute path of an installed shell, or None when it is not there."""
    found = (await ctx.capture(["sh", "-c", f"command -v {name}"], timeout=15.0)).strip()
    return found if found.startswith("/") else None


async def _register_shell(ctx: ExecContext, target: str) -> None:
    """Add the shell to /etc/shells, which chsh and some services read."""
    content = await ctx.read_file(SHELLS_PATH)
    if content is None:
        ctx.log(f"{SHELLS_PATH} is missing, skipping the registration", "warn")
        return
    if any(line.strip() == target for line in content.splitlines()):
        return
    await ctx.write_file(SHELLS_PATH, content.rstrip("\n") + f"\n{target}\n", root=True)


def _switch_shell(name: str) -> Callable[[ExecContext], Awaitable[None]]:
    """Set the login shell of the invoking user, through chsh or usermod."""

    async def switch(ctx: ExecContext) -> None:
        user = ctx.system.user
        if user in ("", "unknown"):
            ctx.log("the user name could not be determined, the login shell is unchanged", "warn")
            return
        target = await _shell_path(ctx, name)
        if target is None:
            if ctx.dry_run:
                ctx.log(f"$ sudo chsh -s <path to {name}> {user}", "dry")
            else:
                ctx.log(f"{name} was not found in PATH, the login shell is unchanged", "warn")
            return
        current = ctx.probe.login_shell
        if current and os.path.realpath(current) == os.path.realpath(target):
            ctx.log(f"the login shell is already {current}", "skip")
            return
        await _register_shell(ctx, target)
        argv = (
            ["chsh", "-s", target, user]
            if ctx.system.has("chsh")
            else ["usermod", "-s", target, user]
        )
        await ctx.run(argv, root=True)
        ctx.note(f"The new login shell ({target}) applies after you log out and back in.")

    return switch


def _starship_init_line(shell: str) -> tuple[str, str] | None:
    """The startup file of a shell and the line that starts starship in it."""
    lines = {
        "bash": (
            "~/.bashrc",
            'command -v starship >/dev/null 2>&1 && eval "$(starship init bash)"',
        ),
        "zsh": (
            "~/.zshrc",
            'command -v starship >/dev/null 2>&1 && eval "$(starship init zsh)"',
        ),
        "fish": (
            "~/.config/fish/config.fish",
            "if type -q starship\n    starship init fish | source\nend",
        ),
    }
    return lines.get(shell)


def _starship_target(system: System, login_shell: str) -> tuple[Path, str] | None:
    entry = _starship_init_line(Path(login_shell).name)
    if entry is None:
        return None
    relative, line = entry
    return system.home / relative[2:], line


async def _add_starship_init(ctx: ExecContext) -> None:
    shell = Path(ctx.probe.login_shell).name or "bash"
    target = _starship_target(ctx.system, ctx.probe.login_shell)
    if target is None:
        ctx.note(
            f"Unknown login shell ({shell}): add the starship init line to its startup file "
            "yourself, see starship.rs."
        )
        return
    path, line = target
    current = await ctx.read_file(path) or ""
    if STARSHIP_MARKER in current:
        ctx.log(f"{path} already starts starship", "skip")
        return
    separator = "" if not current or current.endswith("\n") else "\n"
    await ctx.write_file(
        path,
        f"{current}{separator}\n{STARSHIP_MARKER}\n{line}\n",
        root=False,
    )


def _starship_applied(probe: Probe, system: System) -> bool:
    target = _starship_target(system, probe.login_shell)
    return target is not None and probe.file_contains(target[0], STARSHIP_MARKER)


def _shell_tasks(system: System) -> list[Task]:
    switch_details = [
        "The change goes through chsh (or usermod when chsh is missing), for the user who "
        "started uniscript, never for root.",
        "If the shell is not listed in /etc/shells yet, it is added there first. The original "
        "file is backed up.",
        "The new shell starts working on the next login, the current session keeps the old one.",
        "Try the shell with its own name first, before you make it the login shell.",
    ]

    return [
        Task(
            id="shell-zsh",
            title="zsh as the login shell",
            summary="Installs zsh and makes it the login shell for your account.",
            category=Category.SHELL,
            risk=Risk.MEDIUM,
            details=[
                "zsh is compatible with bash scripts and adds better completion and history.",
                "Frameworks like oh-my-zsh install on top of it, uniscript does not install them.",
                *switch_details,
            ],
            steps=[
                Install(["zsh"], optional=True),
                Custom("sets zsh as the login shell", _switch_shell("zsh")),
            ],
            detect=lambda probe, sys_: Path(probe.login_shell).name == "zsh",
        ),
        Task(
            id="shell-fish",
            title="fish as the login shell",
            summary="Installs fish and makes it the login shell for your account.",
            category=Category.SHELL,
            risk=Risk.MEDIUM,
            warning=(
                "fish is not POSIX compatible. Scripts starting with #!/bin/sh keep working, "
                "but a snippet copied from the internet and pasted into the prompt often will "
                "not."
            ),
            details=[
                "fish has completion from man pages and history based suggestions with no setup.",
                "It is a poor fit if you paste shell one-liners from documentation a lot.",
                *switch_details,
            ],
            steps=[
                Install(["fish"], optional=True),
                Custom("sets fish as the login shell", _switch_shell("fish")),
            ],
            detect=lambda probe, sys_: Path(probe.login_shell).name == "fish",
        ),
        Task(
            id="shell-bash",
            title="Back to bash",
            summary="Restores bash as the login shell.",
            category=Category.SHELL,
            risk=Risk.SAFE,
            details=[
                "The way back out of zsh or fish.",
                "bash is present on every distribution here, nothing is installed.",
                *switch_details,
            ],
            steps=[Custom("sets bash as the login shell", _switch_shell("bash"))],
            detect=lambda probe, sys_: Path(probe.login_shell).name == "bash",
        ),
        Task(
            id="shell-starship",
            title="starship prompt",
            summary="A prompt showing the git branch, the language version and the exit code.",
            category=Category.SHELL,
            risk=Risk.SAFE,
            details=[
                "One prompt for bash, zsh and fish, configured in ~/.config/starship.toml.",
                "The startup line is added to the file of your current login shell, guarded by a "
                "check for the binary, so a missing starship cannot break the shell.",
                "If the package is not in your repositories, the install step is skipped and the "
                "install instructions are at starship.rs.",
            ],
            steps=[
                Install(["starship"], optional=True),
                Custom(
                    "adds the starship init to the shell startup file",
                    _add_starship_init,
                    root=False,
                ),
                Note("The prompt appears in a newly opened terminal."),
            ],
            detect=_starship_applied,
        ),
    ]


# ----------------------------------------------------------- Applications


def _app_tasks(system: System) -> list[Task]:
    groups: list[tuple[str, str, str, list[str], list[str]]] = [
        (
            "apps-media",
            "Multimedia",
            "VLC, OBS Studio and Audacity as Flatpaks.",
            ["org.videolan.VLC", "com.obsproject.Studio", "org.audacityteam.Audacity"],
            [
                "VLC plays practically any format without adding system codecs.",
                "OBS Studio records and streams, with VAAPI and NVENC support.",
            ],
        ),
        (
            "apps-office",
            "Office and documents",
            "LibreOffice and an ebook reader.",
            ["org.libreoffice.LibreOffice", "com.github.johnfactotum.Foliate"],
            [
                "Foliate reads EPUB and MOBI.",
                "The Flatpak LibreOffice is usually newer than the one in the repository.",
            ],
        ),
        (
            "apps-system",
            "System tools",
            "Mission Center, a system monitor.",
            ["io.missioncenter.MissionCenter"],
            ["Mission Center shows CPU, GPU, disk and network load in one window."],
        ),
        (
            "apps-comms",
            "Messengers",
            "Discord, Signal and Telegram.",
            ["com.discordapp.Discord", "org.signal.Signal", "org.telegram.desktop"],
            ["All as Flatpaks, updated independently of the system."],
        ),
        (
            "apps-browsers",
            "Browsers",
            "Brave, Chromium and LibreWolf.",
            [
                "com.brave.Browser",
                "org.chromium.Chromium",
                "io.gitlab.librewolf-community",
            ],
            [
                "Brave is Chromium with an ad blocker built in.",
                "Chromium is the plain upstream browser without Google's additions.",
                "LibreWolf is Firefox with the telemetry stripped out.",
            ],
        ),
        (
            "apps-music",
            "Music players",
            "Spotify, YouTube Music and Cider.",
            ["com.spotify.Client", "app.ytmdesktop.ytmdesktop", "sh.cider.Cider"],
            [
                "YTMDesktop is a desktop client for YouTube Music.",
                "Cider is an Apple Music client.",
                "Spotify and the streaming clients need their accounts to play anything.",
            ],
        ),
        (
            "apps-terminals",
            "Terminals",
            "Ptyxis, WezTerm and Black Box.",
            [
                "app.devsuite.Ptyxis",
                "org.wezfurlong.wezterm",
                "com.raggesilver.BlackBox",
            ],
            [
                "Ptyxis is a GNOME terminal built for containers and host shells.",
                "WezTerm is GPU-accelerated and configured in Lua.",
                "A Flatpak terminal reaches the host shell through flatpak-spawn.",
            ],
        ),
    ]

    tasks: list[Task] = []
    for task_id, title, summary, app_ids, details in groups:
        first = app_ids[0]
        tasks.append(
            Task(
                id=task_id,
                title=title,
                summary=summary,
                category=Category.APPS,
                risk=Risk.SAFE,
                details=[*details, "Source: Flathub.", "Applications: " + ", ".join(app_ids)],
                steps=[_flatpak_install(app_ids)],
                detect=(lambda app: lambda probe, sys_: probe.has_flatpak_app(app))(first),
            )
        )
    return tasks


# ------------------------------------------------------------- Maintenance


def _maintenance_tasks(system: System) -> list[Task]:
    pm = system.package_manager
    steps: list[Step] = []
    if pm and pm.autoremove:
        steps.append(Run([pm.binary, *pm.autoremove], allow_fail=True))
    if pm and pm.clean:
        steps.append(Run([pm.binary, *pm.clean], allow_fail=True))
    steps.append(Run(["journalctl", "--vacuum-size=500M"], allow_fail=True))
    steps.append(
        Run(
            ["flatpak", "uninstall", "--system", "--unused", "-y", "--noninteractive"],
            allow_fail=True,
        )
    )

    return [
        Task(
            id="maint-cleanup",
            title="Cleanup: cache, orphaned packages, journal",
            summary="Frees disk space without touching user configuration.",
            category=Category.MAINTENANCE,
            risk=Risk.SAFE,
            details=[
                "Removes packages installed as dependencies that nothing needs any more.",
                "Clears downloaded packages and trims the systemd journal to 500 MiB.",
                "Removes unused Flatpak runtimes.",
            ],
            steps=steps,
        ),
    ]


def build(system: System) -> list[Task]:
    return [
        *_flatpak_tasks(system),
        *_snap_tasks(system),
        *_tweak_tasks(system),
        *_gaming_tasks(system),
        *_shell_tasks(system),
        *_app_tasks(system),
        *_maintenance_tasks(system),
    ]
