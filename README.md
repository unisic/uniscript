# uniscript

Post-install Linux configuration, driven from the terminal.
The script detects the distribution, the package manager and the hardware, then
shows a list of tasks matched to that particular machine: repositories, drivers,
codecs, Flatpak, a gaming set, shell switching and conservative tweaks.

Nothing happens on its own. You pick the tasks, you read the full plan of
commands, and only then you run it.

## Requirements

- Linux with systemd (another init is detected, but the tasks touching services
  are skipped)
- Python 3.11 or newer
- `sudo` or `doas` for the tasks that change the system
- a network connection on the first run (to install Textual)

The only dependency is [Textual](https://github.com/Textualize/textual)
(`textual>=8.0,<9.0`). The launcher script puts it in a separate virtual
environment in `${XDG_DATA_HOME:-$HOME/.local/share}/uniscript/venv` and never
touches the system packages.

## Running it

One line, clones the repository into `~/uniscript` and starts the interface:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/unisic/uniscript/main/install.sh)
```

The target path, the repository and the branch can be overridden with
`UNISCRIPT_DIR`, `UNISCRIPT_REPO` and `UNISCRIPT_BRANCH`. Running the same line
again updates the existing clone with `git merge --ff-only` instead of cloning
it a second time.

The `bash <(curl ...)` form matters more here than usual: with `curl | bash` the
pipe takes standard input, and the interface needs a terminal. The installer
handles that case by attaching `/dev/tty`, but the process substitution form is
cleaner.

Without the installer, if you already have the repository:

```sh
cd uniscript
./uniscript
```

The first start creates the environment and installs Textual. Later starts go
straight to the interface. If you would rather use the system Python with
Textual already installed:

```sh
UNISCRIPT_SYSTEM_PYTHON=1 ./uniscript
```

Dry run, recommended for the first look. It changes nothing and prints every
command and every file diff:

```sh
./uniscript --dry-run
```

## The interface

```
Categories on the left, tasks in the middle, the description below them, the log at the bottom.
```

| Key | Does |
| --- | --- |
| arrows, tab | move through the list and between panels |
| space | select or deselect a task |
| `e` | the essentials set |
| `g` | the gaming set |
| `a` | select the whole category |
| `n` | deselect everything |
| `r` | show the plan and run |
| `d` | toggle dry run |
| `s` | details of the detected system |
| `c` | clear the log |
| `?` | help |
| `esc` | abort the work in progress |
| `q` | quit |

Tasks are marked: `done` (detected as already applied), `care` (changes system
behaviour), `risk` (can break the system or weaken its security). Risky tasks
are never selected by default.

## Without the interface

```sh
./uniscript --system                       # what was detected
./uniscript --list                         # the available tasks with their ids
./uniscript --plan fedora-rpmfusion        # the commands that would go to the shell
./uniscript --run fedora-rpmfusion --yes   # run without asking
./uniscript --run fedora-copr --input fedora-copr=solopasha/telegram-desktop
```

`--dry-run` works together with `--run`.

## Safety

- **Passwords never go through the interface.** Authorisation happens once, on
  the real terminal, with the TUI suspended. After that every command runs
  through `sudo -n`, and a background task refreshes the timestamp. Privileges
  are only requested when the plan actually contains a system task.
- **Every file change is backed up first**, into
  `~/.local/share/uniscript/backups/<session>/`, together with a `manifest.json`
  and a generated `restore.sh` that reverts every change from that session. The
  restore script remembers the ownership and the mode of each file and does not
  stop at the first error.
- **Dry run** shows the full list of commands and the file diffs without running
  anything.
- **Tasks are idempotent**, as far as that can be detected. What is already done
  is marked `done` and skipped by default.
- **Nothing is downloaded on trust.** Repositories are added from the official
  sources of the distribution, and where the GPG key arrives later than the
  package (Terra), the task description says so outright.

## What is in the task catalogue

77 tasks in the catalogue. Only the ones matching the detected system and
hardware are shown. On the test machine (NVIDIA, KDE, Btrfs, desktop) that comes
out as 44 tasks on Fedora, 40 on Ubuntu, 39 on Arch and 34 on openSUSE.

**Shared by all four families** (31 tasks): Flathub, the verified subset,
Flatpak access to the system theme, tools for managing Flatpaks, four
application sets from Flathub (multimedia, office, system tools, messengers),
removing snapd, installing snapd marked as not recommended, Heroic, Bottles,
ProtonUp-Qt, MangoHud configuration, kernel limits for gaming, the `vm`, `net`
and `fs` parameters, zram, an IO scheduler matched to the drive, a size cap on
the systemd journal, a weekly TRIM, a shorter boot, encrypted DNS through
systemd-resolved, a firewall, a power profile for a laptop, a tuned profile,
turning the CPU mitigations off (marked risky), cache cleanup, switching the
login shell to zsh, fish or back to bash, and the starship prompt. Some of them
are conditional: the power profile only appears on a portable chassis, the
tuned profile where `tuned-adm` is available or the family ships it, and
`mitigations=off` only where the kernel command line can be edited.

**Fedora and RHEL only** (18): a full system upgrade, RPM Fusion free and
nonfree, RPM Fusion tainted, the Cisco openh264 codec, COPR with a prompt for
the repository name, Terra, faster dnf, the full ffmpeg and codecs, the NVIDIA
driver (the open module for Turing and newer, the proprietary one for older
cards), hardware video decoding for AMD and for Intel (one task each), a gaming
set, Btrfs snapshots, firmware updates through fwupd, removing old kernels, the
clock in UTC for dual boot, archive support, and a warning shown when an atomic
variant is detected.

**Debian, Ubuntu and Mint only** (11): a full system upgrade, universe and
multiverse, contrib, non-free and non-free-firmware on Debian, Microsoft codecs
and fonts, Firefox from the Mozilla repository instead of the snap, drivers
through `ubuntu-drivers` or the `nvidia-driver` package, a gaming set,
earlyoom, apt tuning, archive support, the clock in UTC for dual boot.

**Arch, CachyOS and EndeavourOS only** (11): a full system upgrade, sorting out
the mirror list, an AUR helper (paru or yay, one task each), multilib, codecs,
drivers, a gaming set, pacman tuning, a cap on the package cache, the clock in
UTC for dual boot.

**openSUSE only** (6): a full system upgrade, Packman with
`--allow-vendor-change`, drivers, a gaming set, archive support, the clock in
UTC for dual boot.

Snaps: on Ubuntu there is a task that removes every snap, uninstalls snapd and
blocks its return through apt, together with swapping Firefox for the build from
the Mozilla repository, so you are not left without a browser. On the other
systems installing snapd is available, but marked as risky and argued against in
the task description.

Shells: zsh and fish are installed and set as the login shell through `chsh`
(`usermod` when `chsh` is missing), for the user who started uniscript, never for
root. If the shell is not listed in `/etc/shells` yet, it is added there first
and the original file is backed up. There is a task back to bash as well. The
starship line is added to the startup file of your current login shell, guarded
by a check for the binary, so a missing starship cannot break the shell.

## What is deliberately missing

- **Chaotic-AUR.** I could not verify the signing key of that repository, and
  adding a binary package source with an unverified key to a system is bad
  advice. There is a task installing an AUR helper instead.
- Anything picking choices for the user. The `e` and `g` sets only select, the
  plan always has to be confirmed.
- Tasks that can be neither detected nor reverted.

## Structure

```
install.sh             the one-line installer: clones the repository and runs it
uniscript              the launcher, creates the environment and runs the module
src/uniscript/
  cli.py               the mode without an interface
  core/system.py       detecting the distribution, the package manager and the hardware
  core/probe.py        state checks (packages, services, repositories)
  core/tasks.py        the task and step model
  core/runner.py       running commands with streamed output
  core/privileges.py   elevation without passwords in the interface
  core/backup.py       backups and generating restore.sh
  core/context.py      the execution context, dry run, writing files
  catalog/             the task catalogue, shared and per distribution family
  tui/                 the Textual interface
```

The resource limits are explicit: the log keeps 2000 lines, process output is
read in 8 KiB chunks with lines truncated at 4000 characters, the last 200 lines
are kept for the error report, every command has a timeout and is killed
together with its whole process group.

## Verification status

Honestly, because it matters for a script that touches system configuration:

- **Fedora 44 (dnf5, KDE, NVIDIA RTX 4060 Ti): checked on a live machine.**
  System detection, detection of the already applied tasks, dry run, the diff
  preview, writing a file with a backup, writing the same content again, a
  working `restore.sh`, running a system task and a user task, and the shell
  section (the switch to a shell that is not installed, the switch to the shell
  that is already the login one, and adding the starship line to the startup
  file against a test HOME, followed by restoring it).
- **The installer: run end to end** against a local `file://` clone. The first
  run cloned the repository, created the environment and installed Textual, the
  second one fast-forwarded the clone and reused the environment. The four guards
  (a target that is not a repository, a target pointing at a different
  repository, a run through `sudo`, a branch that does not exist) were triggered
  on purpose and each one stopped with its own message.
- **Debian, Ubuntu, Arch, openSUSE: written, not run.** I did not have those
  systems at hand. The package names and the commands come from the
  documentation of each distribution, but no path there has been executed. Start
  with `--dry-run` on those systems and read the plan.
- The `dnf group upgrade multimedia` syntax in dnf5 has not been confirmed on a
  live system.
- The starship init lines for bash, zsh and fish come from the starship
  documentation and have not been run here, because starship is not in the
  Fedora 44 repositories.

## Development

```sh
ruff check src/
ruff format src/
```

The configuration lives in `ruff.toml`, 100 character lines.

## Licence

MIT, the `LICENSE` file.
