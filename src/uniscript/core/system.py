"""System detection: distribution, package manager, hardware, environment."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

PROBE_TIMEOUT = 8.0

# PCI class 0x03xxxx is a display controller.
_PCI_DISPLAY_CLASS = "0x03"
_PCI_VENDORS = {
    "0x10de": "nvidia",
    "0x1002": "amd",
    "0x1022": "amd",
    "0x8086": "intel",
    "0x1af4": "virtio",
    "0x15ad": "vmware",
    "0x1234": "qemu",
}

# Lowest PCI device id of the Turing generation. The open NVIDIA kernel module
# supports Turing and newer, earlier chips need the proprietary module.
_NVIDIA_TURING_MIN_DEVICE_ID = 0x1E00

_CHASSIS_PORTABLE = {8, 9, 10, 11, 14, 30, 31, 32}

_SECURE_BOOT_EFIVAR = "/sys/firmware/efi/efivars/SecureBoot-8be4df61-93ca-11d2-aa0d-00e098032b8c"


def _read_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _run(argv: list[str], timeout: float = PROBE_TIMEOUT) -> str:
    """Run a probe without elevating privileges. On failure return an empty string."""
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
    return proc.stdout if proc.returncode == 0 else ""


def _parse_os_release() -> dict[str, str]:
    raw = _read_text("/etc/os-release") or _read_text("/usr/lib/os-release")
    data: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        data[key.strip()] = value
    return data


@dataclass(frozen=True)
class PackageManager:
    """Normalized package manager interface."""

    name: str
    binary: str
    family: str
    install: tuple[str, ...]
    remove: tuple[str, ...]
    upgrade: tuple[str, ...]
    refresh: tuple[str, ...]
    autoremove: tuple[str, ...] | None
    clean: tuple[str, ...] | None

    def install_cmd(self, packages: list[str]) -> list[str]:
        return [self.binary, *self.install, *packages]

    def remove_cmd(self, packages: list[str]) -> list[str]:
        return [self.binary, *self.remove, *packages]


_PACKAGE_MANAGERS: dict[str, PackageManager] = {
    "dnf5": PackageManager(
        name="dnf5",
        binary="dnf",
        family="rhel",
        install=("install", "-y"),
        remove=("remove", "-y"),
        upgrade=("upgrade", "-y"),
        refresh=("makecache", "--refresh"),
        autoremove=("autoremove", "-y"),
        clean=("clean", "all"),
    ),
    "dnf": PackageManager(
        name="dnf",
        binary="dnf",
        family="rhel",
        install=("install", "-y"),
        remove=("remove", "-y"),
        upgrade=("upgrade", "-y"),
        refresh=("makecache", "--refresh"),
        autoremove=("autoremove", "-y"),
        clean=("clean", "all"),
    ),
    "apt": PackageManager(
        name="apt",
        binary="apt-get",
        family="debian",
        install=("install", "-y"),
        remove=("remove", "-y"),
        upgrade=("full-upgrade", "-y"),
        refresh=("update",),
        autoremove=("autoremove", "--purge", "-y"),
        clean=("clean",),
    ),
    "pacman": PackageManager(
        name="pacman",
        binary="pacman",
        family="arch",
        install=("-S", "--needed", "--noconfirm"),
        remove=("-Rns", "--noconfirm"),
        upgrade=("-Syu", "--noconfirm"),
        refresh=("-Sy",),
        autoremove=None,
        clean=("-Sc", "--noconfirm"),
    ),
    "zypper": PackageManager(
        name="zypper",
        binary="zypper",
        family="suse",
        install=("--non-interactive", "install"),
        remove=("--non-interactive", "remove"),
        upgrade=("--non-interactive", "dup"),
        refresh=("refresh",),
        autoremove=None,
        clean=("clean", "--all"),
    ),
}


@dataclass(frozen=True)
class Gpu:
    vendor: str
    vendor_id: str
    device_id: str
    name: str
    driver: str | None
    pci_slot: str

    @property
    def open_kernel_module_capable(self) -> bool:
        """Heuristic: Turing (2018) and newer support the open NVIDIA module."""
        if self.vendor != "nvidia":
            return False
        try:
            return int(self.device_id, 16) >= _NVIDIA_TURING_MIN_DEVICE_ID
        except ValueError:
            return False


def _detect_gpus() -> list[Gpu]:
    gpus: list[Gpu] = []
    lspci_names = _lspci_names()
    devices = Path("/sys/bus/pci/devices")
    if not devices.is_dir():
        return gpus
    for entry in sorted(devices.iterdir()):
        pci_class = _read_text(entry / "class").strip()
        if not pci_class.startswith(_PCI_DISPLAY_CLASS):
            continue
        vendor_id = _read_text(entry / "vendor").strip().lower()
        device_id = _read_text(entry / "device").strip().lower()
        driver_link = entry / "driver"
        driver = driver_link.resolve().name if driver_link.is_symlink() else None
        slot = entry.name
        short = slot[5:] if slot.startswith("0000:") else slot
        name = lspci_names.get(short) or lspci_names.get(slot) or ""
        gpus.append(
            Gpu(
                vendor=_PCI_VENDORS.get(vendor_id, "other"),
                vendor_id=vendor_id.removeprefix("0x"),
                device_id=device_id.removeprefix("0x"),
                name=name or f"PCI {vendor_id.removeprefix('0x')}:{device_id.removeprefix('0x')}",
                driver=driver,
                pci_slot=short,
            )
        )
    return gpus


def _lspci_names() -> dict[str, str]:
    out = _run(["lspci", "-mm"])
    names: dict[str, str] = {}
    for line in out.splitlines():
        fields = re.findall(r'"([^"]*)"|(\S+)', line)
        parts = [a or b for a, b in fields]
        if len(parts) < 4:
            continue
        slot, _cls, vendor, device = parts[0], parts[1], parts[2], parts[3]
        names[slot] = f"{vendor} {device}"
    return names


def _detect_secure_boot() -> bool | None:
    path = Path(_SECURE_BOOT_EFIVAR)
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    # Four bytes of EFI attributes, then a single value byte.
    if len(data) < 5:
        return None
    return data[4] == 1


def _detect_root_filesystem() -> tuple[str, str]:
    for line in _read_text("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "/":
            return parts[2], parts[3] if len(parts) > 3 else ""
    return "unknown", ""


def _detect_meminfo() -> tuple[int, int]:
    total = swap = 0
    for line in _read_text("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1])
        elif line.startswith("SwapTotal:"):
            swap = int(line.split()[1])
    return total, swap


def _detect_cpu() -> tuple[str, str, int]:
    vendor = model = ""
    for line in _read_text("/proc/cpuinfo").splitlines():
        if not vendor and line.startswith("vendor_id"):
            vendor = line.split(":", 1)[1].strip()
        elif not model and line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
        if vendor and model:
            break
    if not model:
        model = platform.processor() or "unknown"
    return vendor or "unknown", model, os.cpu_count() or 1


def _detect_portable() -> bool:
    chassis = _read_text("/sys/class/dmi/id/chassis_type").strip()
    if chassis.isdigit() and int(chassis) in _CHASSIS_PORTABLE:
        return True
    power = Path("/sys/class/power_supply")
    if power.is_dir():
        return any(p.name.startswith("BAT") for p in power.iterdir())
    return False


def _detect_container() -> str | None:
    if Path("/run/.containerenv").exists():
        return "podman"
    if Path("/.dockerenv").exists():
        return "docker"
    # The systemd container interface defines this variable in lower case.
    runtime = os.environ.get("container")  # noqa: SIM112
    if runtime:
        return runtime
    return None


def _detect_package_manager(os_id: str, id_like: list[str]) -> PackageManager | None:
    families = [os_id, *id_like]
    if any(f in ("fedora", "rhel", "centos", "almalinux", "rocky") for f in families):
        if shutil.which("dnf5"):
            return _PACKAGE_MANAGERS["dnf5"]
        if shutil.which("dnf"):
            return _PACKAGE_MANAGERS["dnf"]
    if any(f in ("debian", "ubuntu", "linuxmint", "pop") for f in families):
        if shutil.which("apt-get"):
            return _PACKAGE_MANAGERS["apt"]
    if any(f in ("arch", "archlinux", "cachyos", "endeavouros", "manjaro") for f in families):
        if shutil.which("pacman"):
            return _PACKAGE_MANAGERS["pacman"]
    if any(
        f in ("opensuse", "suse", "opensuse-tumbleweed", "opensuse-leap", "sles") for f in families
    ):
        if shutil.which("zypper"):
            return _PACKAGE_MANAGERS["zypper"]
    # Unknown distribution: fall back to whichever binary is present.
    for candidate in ("dnf5", "dnf", "apt-get", "pacman", "zypper"):
        if shutil.which(candidate):
            key = {"dnf5": "dnf5", "dnf": "dnf", "apt-get": "apt"}.get(candidate, candidate)
            return _PACKAGE_MANAGERS[key]
    return None


@dataclass
class System:
    """Collected facts about the machine. All read, none guessed."""

    os_id: str
    id_like: list[str]
    version_id: str
    pretty_name: str
    variant_id: str | None
    package_manager: PackageManager | None
    kernel: str
    architecture: str
    init_system: str
    desktop: str
    session_type: str
    cpu_vendor: str
    cpu_model: str
    cpu_threads: int
    mem_total_kb: int
    swap_total_kb: int
    gpus: list[Gpu]
    root_fs: str
    root_fs_options: str
    is_portable: bool
    is_atomic: bool
    container: str | None
    secure_boot: bool | None
    euid: int
    user: str
    home: Path
    tools: dict[str, str] = field(default_factory=dict)

    @property
    def family(self) -> str:
        return self.package_manager.family if self.package_manager else "unknown"

    @property
    def is_root(self) -> bool:
        return self.euid == 0

    @property
    def has_systemd(self) -> bool:
        return self.init_system == "systemd"

    @property
    def gpu_vendors(self) -> set[str]:
        return {gpu.vendor for gpu in self.gpus}

    @property
    def mem_total_gib(self) -> float:
        return self.mem_total_kb / 1024 / 1024

    @property
    def version_major(self) -> int | None:
        match = re.match(r"(\d+)", self.version_id)
        return int(match.group(1)) if match else None

    def has(self, tool: str) -> bool:
        return tool in self.tools

    def summary_rows(self) -> list[tuple[str, str]]:
        gpu_text = ", ".join(f"{g.name} [{g.vendor}]" for g in self.gpus) or "none detected"
        mem = f"{self.mem_total_gib:.1f} GiB RAM"
        if self.swap_total_kb:
            mem += f", swap {self.swap_total_kb / 1024 / 1024:.1f} GiB"
        else:
            mem += ", no swap"
        secure_boot = {True: "enabled", False: "disabled", None: "no EFI"}[self.secure_boot]
        rows = [
            ("System", f"{self.pretty_name} ({self.os_id} {self.version_id})"),
            (
                "Package manager",
                self.package_manager.name if self.package_manager else "unknown",
            ),
            ("Kernel", f"{self.kernel} {self.architecture}"),
            ("Init", self.init_system),
            ("Desktop", f"{self.desktop or 'none'} / {self.session_type or 'unknown session'}"),
            ("CPU", f"{self.cpu_model} ({self.cpu_threads} threads)"),
            ("Memory", mem),
            ("Graphics", gpu_text),
            ("Root filesystem", f"{self.root_fs}"),
            ("Secure Boot", secure_boot),
            ("Chassis", "laptop" if self.is_portable else "desktop"),
        ]
        if self.is_atomic:
            rows.append(("Variant", "atomic/immutable (rpm-ostree)"))
        if self.container:
            rows.append(("Container", self.container))
        rows.append(("User", f"{self.user} (uid {self.euid})"))
        return rows


_INTERESTING_TOOLS = (
    "flatpak",
    "snap",
    "rpm-ostree",
    "systemctl",
    "sudo",
    "doas",
    "pkexec",
    "git",
    "curl",
    "wget",
    "lspci",
    "fwupdmgr",
    "tuned-adm",
    "mokutil",
    "ubuntu-drivers",
    "paru",
    "yay",
    "makepkg",
    "nvidia-smi",
    "gpg",
    "chsh",
    "bash",
    "zsh",
    "fish",
    "starship",
)


def detect_system() -> System:
    """Collect the facts about the system. File reads and cheap probes only."""
    release = _parse_os_release()
    os_id = release.get("ID", "").lower() or "unknown"
    id_like = release.get("ID_LIKE", "").lower().split()
    cpu_vendor, cpu_model, cpu_threads = _detect_cpu()
    mem_total, swap_total = _detect_meminfo()
    root_fs, root_fs_options = _detect_root_filesystem()
    tools = {name: path for name in _INTERESTING_TOOLS if (path := shutil.which(name))}
    is_atomic = (
        Path("/run/ostree-booted").exists()
        or bool(release.get("VARIANT_ID", "").endswith(("silverblue", "kinoite", "sericea")))
        or (shutil.which("transactional-update") is not None and not os.access("/usr", os.W_OK))
    )

    return System(
        os_id=os_id,
        id_like=id_like,
        version_id=release.get("VERSION_ID", ""),
        pretty_name=release.get("PRETTY_NAME") or release.get("NAME") or "Linux",
        variant_id=release.get("VARIANT_ID") or None,
        package_manager=_detect_package_manager(os_id, id_like),
        kernel=platform.release(),
        architecture=platform.machine(),
        init_system="systemd" if Path("/run/systemd/system").is_dir() else "other",
        desktop=os.environ.get("XDG_CURRENT_DESKTOP", ""),
        session_type=os.environ.get("XDG_SESSION_TYPE", ""),
        cpu_vendor=cpu_vendor,
        cpu_model=cpu_model,
        cpu_threads=cpu_threads,
        mem_total_kb=mem_total,
        swap_total_kb=swap_total,
        gpus=_detect_gpus(),
        root_fs=root_fs,
        root_fs_options=root_fs_options,
        is_portable=_detect_portable(),
        is_atomic=is_atomic,
        container=_detect_container(),
        secure_boot=_detect_secure_boot(),
        euid=os.geteuid(),
        user=os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown",
        home=Path(os.path.expanduser("~")),
        tools=tools,
    )
