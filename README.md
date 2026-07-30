# uniscript

Post-install Linux configuration, driven from the terminal.
The script detects the distribution, the package manager and the hardware, then
shows a list of tasks matched to that particular machine: repositories, drivers,
codecs, Flatpak, a gaming set, shell switching and conservative tweaks.

Nothing happens on its own. You pick the tasks, you read the full plan of
commands, and only then you run it.

## Early version, read this first

This is version 0.1.0 and it should be treated as such. It installs packages,
adds repositories, edits files under `/etc` and changes your login shell, so a
mistake here costs more than a mistake in an ordinary program.

Only Fedora has been run on a live machine. Debian, Ubuntu, Arch and openSUSE
are written but never executed. The details are in
[Verification status](#verification-status).

- Start with `./uniscript --dry-run` and read the plan. Nothing runs until you
  confirm it.
- Have a way back. Every file change is backed up with a generated `restore.sh`,
  but that is not a substitute for a system snapshot.
- Do not run this on a production server or a machine you cannot reinstall.
- Tasks marked `risk` can break the system or weaken its security. They are
  never selected by default.

Bug reports are welcome, especially from the untested distributions.

## Requirements

- Linux with systemd (another init works, the tasks touching services are
  skipped)
- Python 3.11 or newer
- `sudo` or `doas` for the tasks that change the system
- a network connection on the first run, to install
  [Textual](https://github.com/Textualize/textual) (`textual>=8.0,<9.0`), the
  only dependency

The launcher puts Textual in a separate virtual environment in
`${XDG_DATA_HOME:-$HOME/.local/share}/uniscript/venv` and never touches the
system packages.

## Running it

One line, clones the repository into `~/uniscript` and starts the interface:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/unisic/uniscript/main/install.sh)
```

Running it again updates the clone with `git merge --ff-only`. The target path,
the repository and the branch can be overridden with `UNISCRIPT_DIR`,
`UNISCRIPT_REPO` and `UNISCRIPT_BRANCH`. The `bash <(curl ...)` form matters:
with `curl | bash` the pipe takes standard input and the interface needs a
terminal.

With the repository already cloned:

```sh
./uniscript                       # first start also creates the environment
./uniscript --dry-run             # changes nothing, prints commands and diffs
UNISCRIPT_SYSTEM_PYTHON=1 ./uniscript   # use the system Python instead
```

## The interface

Category tabs across the top, the task list on the left, the description of the
highlighted task on the right, action buttons at the bottom, and a log that
appears when something starts running. The layout and the palette follow
[WinUtil](https://github.com/ChrisTitusTech/winutil), translated to a terminal.

| Key | Does |
| --- | --- |
| up, down | move through the list |
| left, right | switch the category tab |
| space | select or deselect a task |
| `/` | search everywhere, `esc` clears it |
| shift+arrows | scroll the description (the wheel works too) |
| `e` | the essentials set |
| `g` | the gaming set |
| `a` | select the whole group the cursor is in |
| `n` | deselect everything |
| `r` | show the plan and run |
| `d` | toggle dry run |
| `s` | details of the detected system |
| `t` | switch between the dark and the light palette |
| `l` | show or hide the log |
| `c` | clear the log |
| `?` | help |
| `esc` | abort the work in progress |
| `q` | quit |

Tasks are marked: a green `✓` (detected as already applied), an amber `●`
(changes system behaviour), a red `▲` (can break the system or weaken its
security).

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
  through `sudo -n`. Privileges are only requested when the plan actually
  contains a system task.
- **Every file change is backed up first**, into
  `~/.local/share/uniscript/backups/<session>/`, with a `manifest.json` and a
  generated `restore.sh` that reverts the whole session, remembering ownership
  and mode and not stopping at the first error.
- **Dry run** shows every command and every file diff without running anything.
- **Tasks are idempotent** as far as that can be detected. What is already done
  is marked `done` and skipped by default.
- **Nothing is downloaded on trust.** Repositories come from the official
  sources of the distribution, and where the GPG key arrives later than the
  package (Terra), the task description says so.

## What is in the task catalogue

77 tasks. Only the ones matching the detected system and hardware are shown. On
the test machine (NVIDIA, KDE, Btrfs, desktop) that is 44 tasks on Fedora, 40 on
Ubuntu, 39 on Arch and 34 on openSUSE.

| Where | Tasks | What they cover |
| --- | --- | --- |
| all families | 31 | Flatpak and Flathub, application sets, snapd, the gaming stack (Heroic, Bottles, ProtonUp-Qt, MangoHud, kernel limits), kernel and IO tuning, zram, journal cap, TRIM, encrypted DNS, firewall, power profiles, cache cleanup, zsh, fish, bash and starship |
| Fedora, RHEL | 18 | RPM Fusion free, nonfree and tainted, openh264, COPR, Terra, faster dnf, codecs, the NVIDIA driver, AMD and Intel video decoding, Btrfs snapshots, fwupd, old kernels |
| Debian, Ubuntu, Mint | 11 | universe and multiverse, Debian components, Microsoft codecs and fonts, Firefox from Mozilla instead of the snap, drivers, earlyoom, apt tuning |
| Arch and derivatives | 11 | mirror list, an AUR helper (paru or yay), multilib, codecs, drivers, pacman tuning, a package cache cap |
| openSUSE | 6 | Packman and the full codecs with `--allow-vendor-change`, drivers |

Every family also has a full system upgrade, a gaming set and the clock in UTC
for a dual boot; Fedora, Debian and openSUSE additionally get archive support.
`./uniscript --list` prints the exact set for the machine you are on.

Two details worth knowing. On Ubuntu the snap task removes every snap,
uninstalls snapd and blocks its return, swapping Firefox for the Mozilla build
so you are not left without a browser; elsewhere installing snapd is offered but
marked risky. Shells are switched with `chsh` (`usermod` as a fallback) for the
user who started uniscript, never for root.

## What is deliberately missing

- **Chaotic-AUR.** I could not verify the signing key of that repository. There
  is a task installing an AUR helper instead.
- Anything picking choices for the user. The `e` and `g` sets only select, the
  plan always has to be confirmed.
- Tasks that can be neither detected nor reverted.

## Structure

```
install.sh             the one-line installer
uniscript              the launcher, creates the environment and runs the module
src/uniscript/
  cli.py               the mode without an interface
  core/                detection, probes, the task model, running, privileges,
                       backups and restore.sh, the execution context
  catalog/             the task catalogue, shared and per distribution family
  tui/                 the Textual interface
```

The resource limits are explicit: the log keeps 2000 lines, process output is
read in 8 KiB chunks with lines truncated at 4000 characters, the last 200 lines
are kept for the error report, and every command has a timeout and is killed
together with its whole process group.

## Verification status

- **Fedora 44 (dnf5, KDE, NVIDIA RTX 4060 Ti): checked on a live machine.**
  Detection, dry run, the diff preview, writing a file with a backup, rewriting
  the same content, a working `restore.sh`, running a system task and a user
  task, and the whole shell section against a test HOME.
- **The installer: run end to end** against a local `file://` clone, including
  the four guards (target that is not a repository, target pointing elsewhere, a
  run through `sudo`, a branch that does not exist).
- **Debian, Ubuntu, Arch, openSUSE: written, not run.** The package names and
  commands come from the documentation of each distribution, but no path there
  has been executed.
- Not confirmed on a live system: the `dnf group upgrade multimedia` syntax in
  dnf5, and the starship init lines, because starship is not in the Fedora 44
  repositories.

## Development

```sh
ruff check src/
ruff format src/
```

The configuration lives in `ruff.toml`, 100 character lines.

## Licence

MIT, the `LICENSE` file.
