# uniscript

Post-install Linux configuration, driven from the terminal. It detects the
distribution, the package manager and the hardware, then shows only the tasks
that fit the machine: repositories, drivers, codecs, applications, gaming,
shell switching, conservative tweaks and maintenance. Nothing happens on its
own: you pick the tasks, read the exact commands, then confirm.

**Version 0.2.0, read this first.** It installs packages, adds repositories and
edits files under `/etc`, so treat it accordingly: start with `--dry-run`, have
a snapshot or another way back, and keep it off machines you cannot reinstall.
Only Fedora has been run on a live machine; Debian, Ubuntu, Arch and openSUSE
are written but never executed ([details](#verification-status)). Tasks marked
`risk` are never selected by default. Bug reports are welcome, especially from
the untested distributions.

## Running it

Requirements: Linux with systemd, Python 3.11+, `sudo` or `doas`, a network
connection on the first run. The launcher installs the only dependency,
[Textual](https://github.com/Textualize/textual), into its own venv under
`~/.local/share/uniscript/venv` and never touches the system packages
(`UNISCRIPT_SYSTEM_PYTHON=1` skips the venv).

One line, clones into `~/uniscript` and starts; the `bash <(curl ...)` form
matters, because the interface needs a terminal on standard input. Running it
again updates the clone; `UNISCRIPT_DIR`, `UNISCRIPT_REPO` and
`UNISCRIPT_BRANCH` override the defaults:

```sh
bash <(curl -fsSL https://raw.githubusercontent.com/unisic/uniscript/main/install.sh)
```

From a clone:

```sh
./uniscript                              # asks: TUI or browser GUI; first start creates the venv
./uniscript --tui                        # straight to the terminal interface
./uniscript --gui                        # straight to the browser GUI
./uniscript-gui                          # the GUI without the venv (standard library only)
./uniscript --dry-run                    # changes nothing, prints commands and diffs
./uniscript --system                     # what was detected
./uniscript --list                       # the available tasks and their ids
./uniscript --plan fedora-rpmfusion      # the commands a task would run
./uniscript --run fedora-rpmfusion --yes # headless; --dry-run and --input work here too
```

## The interface

Three panes, laid out like
[linutil](https://github.com/ChrisTitusTech/linutil): categories on the left,
tasks in the middle, the description on the right. Applications is browsed
like a directory tree (Browsers, Development and so on), `..` leads back up.
The palette is [Tokyo Night](https://github.com/folke/tokyonight.nvim); `t`
switches between the dark and the day variant.

| Keys | Action |
| --- | --- |
| arrows | move; right/enter opens a group, left backs out to the categories |
| space, `a`, `n` | select a task, the whole group, nothing |
| `/` | search everywhere, `esc` clears it |
| `f` | switch an application between Flatpak and the system package |
| `e`, `g` | the essentials or the gaming preset |
| `r`, `d` | show the plan and run; toggle dry run |
| shift+arrows | scroll the description |
| `s`, `l`, `c`, `?`, `q` | system details, log, clear log, help, quit |

The mouse works everywhere: clicks tick tasks and open groups, the wheel
scrolls the panel under the pointer. The terminal has to forward mouse events
(in tmux: `set -g mouse on`); `./uniscript --input-probe` prints what actually
arrives. Task markers: green `✓` already applied, amber `●` changes system
behaviour, red `▲` can break the system.

The browser GUI serves the same catalogue and the same engine on
`127.0.0.1`, guarded by a one-off token, so only this machine can reach it.
It adds a Quick setup tour: the post-install baseline for the detected
hardware (repositories, the NVIDIA driver when the card is there, codecs, a
full update, the safe tweaks) selected in one click, always behind the same
plan-and-confirm step. Keep the terminal open: the administrator password is
still asked there, never in the browser.

## Safety

- Passwords never pass through the interface: one prompt on the real terminal,
  then `sudo -n`, and only when the plan actually contains a system task.
- Every file change is first backed up to `~/.local/share/uniscript/backups/`,
  with a generated `restore.sh` that reverts the whole session.
- Dry run prints every command and every file diff without running anything.
- Tasks detected as already applied are marked `done` and skipped; repositories
  come only from official sources.

## The catalogue

About 140 tasks; only those matching the machine are shown. On the test
machine that is 104 on Fedora, 101 on Ubuntu, 100 on Arch and 95 on openSUSE;
`--list` prints the exact set for yours. Shared across families: Flatpak and
Flathub, applications one per task in groups (including Helium from the
developers' own repo or AppImage; where an app also lives in the system
repositories, each install can be switched between Flatpak and the native
package), the gaming stack, kernel and IO tuning,
zram, encrypted DNS, firewall, package and Flatpak maintenance, shells. Per
family: RPM Fusion, Terra, the NVIDIA driver and COPR with search and installs
on Fedora; PPAs with installs, Firefox from Mozilla instead of the snap,
universe/multiverse and drivers on Debian and Ubuntu; an AUR helper plus AUR
installs with search, multilib and pacman tuning on Arch; Packman codecs and
opi on openSUSE.

Worth knowing: the Ubuntu snap-removal task removes every snap and swaps
Firefox for the Mozilla build, so you are not left without a browser. Shells
are switched with `chsh` for the user who started uniscript, never for root.
Deliberately missing: Chaotic-AUR (its signing key could not be verified),
anything that picks choices for the user, and tasks that can be neither
detected nor reverted.

## Structure

```
install.sh             the one-line installer
uniscript              the launcher, creates the environment and runs the module
src/uniscript/
  cli.py               the mode without an interface
  core/                detection, probes, the task model, runner, privileges,
                       backups and restore.sh
  catalog/             the task catalogue, shared and per distribution family
  tui/                 the Textual interface
```

Resource limits are explicit: the log keeps 2000 lines, process output is read
in 8 KiB chunks with lines capped at 4000 characters, and every command has a
timeout and is killed together with its process group.

## Verification status

- Fedora 44 (dnf5, KDE, NVIDIA): checked on a live machine, including dry run,
  backups with a working `restore.sh`, system and user tasks and the shell
  section.
- The installer: run end to end against a local clone, including its guards.
- Debian, Ubuntu, Arch, openSUSE: written, not run. The commands come from
  each distribution's documentation.
- Not confirmed live: the `dnf group upgrade multimedia` syntax under dnf5, the
  starship init lines, the AppImage install paths (Helium, Unisic) with their
  Gear Lever hand-off, and a live root run through the browser GUI since the
  sudo-ticket fix; the GUI itself has run real tasks on Fedora.

## Development

`ruff check src/` and `ruff format src/`, configuration in `ruff.toml`.
Licence: MIT, the `LICENSE` file.
