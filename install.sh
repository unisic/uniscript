#!/usr/bin/env bash
# Fetches uniscript and runs it. Usage:
#   bash <(curl -fsSL https://raw.githubusercontent.com/UZYTKOWNIK/uniscript/main/install.sh)
set -euo pipefail

REPO="${UNISCRIPT_REPO:-https://github.com/UZYTKOWNIK/uniscript.git}"
BRANCH="${UNISCRIPT_BRANCH:-main}"
TARGET="${UNISCRIPT_DIR:-$HOME/uniscript}"

die() {
    printf 'install: %s\n' "$1" >&2
    exit 1
}

info() {
    printf 'install: %s\n' "$1" >&2
}

git_hint() {
    if command -v apt-get >/dev/null 2>&1; then
        printf '  sudo apt-get install git\n' >&2
    elif command -v dnf >/dev/null 2>&1; then
        printf '  sudo dnf install git\n' >&2
    elif command -v pacman >/dev/null 2>&1; then
        printf '  sudo pacman -S git\n' >&2
    elif command -v zypper >/dev/null 2>&1; then
        printf '  sudo zypper install git\n' >&2
    fi
}

if [ -n "${SUDO_USER:-}" ]; then
    die "you are running this through sudo. The installer belongs in your user account, uniscript asks for privileges itself when it needs them."
fi

command -v git >/dev/null 2>&1 || {
    printf 'install: git is required:\n' >&2
    git_hint
    exit 1
}

if [ -e "$TARGET" ]; then
    [ -d "$TARGET/.git" ] || die "$TARGET already exists and is not a git repository. Remove it or set UNISCRIPT_DIR."
    origin="$(git -C "$TARGET" remote get-url origin 2>/dev/null || true)"
    [ "$origin" = "$REPO" ] || die "$TARGET points at a different repository ($origin). Set UNISCRIPT_DIR to another directory."
    info "updating $TARGET"
    git -C "$TARGET" fetch --quiet origin "$BRANCH"
    git -C "$TARGET" checkout --quiet "$BRANCH"
    if ! git -C "$TARGET" merge --ff-only --quiet "origin/$BRANCH"; then
        die "$TARGET has local changes that cannot be fast-forwarded. Take a look there yourself."
    fi
else
    info "cloning into $TARGET"
    git clone --quiet --branch "$BRANCH" --depth 1 "$REPO" "$TARGET" ||
        die "the clone failed, check the repository address and your network connection"
fi

[ -x "$TARGET/uniscript" ] || die "$TARGET/uniscript is missing"

# With "curl | bash" standard input is taken by the pipe, and the interface needs
# a terminal. Attach /dev/tty when it is available.
if [ ! -t 0 ] && [ -e /dev/tty ]; then
    exec "$TARGET/uniscript" "$@" </dev/tty
fi

exec "$TARGET/uniscript" "$@"
