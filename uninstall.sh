#!/bin/bash
# Remove a bandctl install and any qdisc, nft table, or cgroup a crash left behind.
# Quit a running bandctl first. q removes the limits. This script will not
# kill a live UI out from under you.

set -euo pipefail

# The installed copy sits next to install-stamp. Use that prefix so
# `bandctl-uninstall` removes the right directory, not only /opt/bandctl.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
DEFAULT_PREFIX=/opt/bandctl
if [ -f "$SCRIPT_DIR/install-stamp" ]; then
  while IFS= read -r line; do
    case "$line" in
      PREFIX=/*) DEFAULT_PREFIX=${line#PREFIX=} ;;
    esac
  done < "$SCRIPT_DIR/install-stamp"
fi
PREFIX="${PREFIX:-$DEFAULT_PREFIX}"
BINDIR="${BINDIR:-/usr/local/bin}"
ORIG_ARGS=("$@")

die() {
  echo "bandctl uninstall: $*" >&2
  exit 1
}

usage() {
  cat << 'EOF'
Usage: sudo bandctl-uninstall
       sudo ./uninstall.sh [--prefix DIR] [--bindir DIR]

Removes the install directory and the bandctl commands. If bandctl is
not running, it also clears rules left in /run/bandctl.json. iproute2
and nftables stay installed.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)
      [ $# -ge 2 ] || die "--prefix needs a directory"
      PREFIX=$2
      shift 2
      ;;
    --bindir)
      [ $# -ge 2 ] || die "--bindir needs a directory"
      BINDIR=$2
      shift 2
      ;;
    --prefix=*)
      PREFIX=${1#--prefix=}
      shift
      ;;
    --bindir=*)
      BINDIR=${1#--bindir=}
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

if [ -f "$PREFIX/install-stamp" ]; then
  stamp_prefix=""
  stamp_bindir=""
  while IFS= read -r line; do
    case "$line" in
      PREFIX=/*) stamp_prefix=${line#PREFIX=} ;;
      BINDIR=/*) stamp_bindir=${line#BINDIR=} ;;
    esac
  done < "$PREFIX/install-stamp"
  [ "$stamp_prefix" = "$PREFIX" ] || die "install stamp does not match $PREFIX"
  [ -n "$stamp_bindir" ] && BINDIR=$stamp_bindir
fi

case "$PREFIX" in
  /|/usr|/usr/bin|/usr/local|/usr/local/bin|/opt|/bin|/sbin|/etc|/home|/root|/tmp|/var)
    die "refusing to remove $PREFIX"
    ;;
esac

if [ "$(id -u)" -ne 0 ] && { [ "$PREFIX" = /opt/bandctl ] || [ "$BINDIR" = /usr/local/bin ]; }; then
  if [ ! -f "$0" ]; then
    die "run: sudo bandctl-uninstall"
  fi
  exec sudo "$0" "${ORIG_ARGS[@]}"
fi

if [ ! -f "$PREFIX/install-stamp" ] && [ ! -d "$PREFIX" ]; then
  echo "bandctl is not installed"
  exit 0
fi

[ -f "$PREFIX/install-stamp" ] || die "$PREFIX is not a bandctl install (no install-stamp)"

bandctl_is_running() {
  local hits
  hits=$(ps -eo args= | grep -E '(^|/)bandctl( |$)|python[0-9.]* -m bandctl' | grep -v uninstall | grep -v install.sh || true)
  [ -n "$hits" ]
}

if bandctl_is_running; then
  die "bandctl is still running. Press q in that window so it can remove the limits, then uninstall."
fi

remove_install() {
local PREFIX=$1 BINDIR=$2 link current wrapper
link="$BINDIR/bandctl"
if [ -L "$link" ] || [ -e "$link" ]; then
  current=$(readlink -f "$link" 2>/dev/null || true)
  case "$current" in
    "$PREFIX"/*) rm -f "$link" ;;
    *) echo "bandctl uninstall: left $link in place (it is not this install)" >&2 ;;
  esac
fi

wrapper="$BINDIR/bandctl-uninstall"
if [ -f "$wrapper" ] && grep -F -q "$PREFIX/uninstall.sh" "$wrapper"; then
  rm -f "$wrapper"
fi

rm -rf "$PREFIX"
echo "Uninstalled bandctl"
}
export -f remove_install

if [ "$(id -u)" -ne 0 ]; then
  echo "bandctl uninstall: not root; recovery was skipped." >&2
  echo "Run bandctl as root after reinstalling if cleanup is needed." >&2
  remove_install "$PREFIX" "$BINDIR"
  exit 0
fi

py="$PREFIX/venv/bin/python"
recover=1
if [ ! -x "$py" ]; then
  py=$(command -v python3) || die "python3 is required to lock the session"
  recover=0
fi

"$py" -c 'import contextlib
import fcntl
import os
import stat
import subprocess
import sys

prefix, bindir, recover = sys.argv[1:]

@contextlib.contextmanager
def fallback_lock():
    fd = os.open("/run/bandctl.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("session lock must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)

def safe(text):
    return "".join("?" if ord(c) < 32 or 127 <= ord(c) <= 159 else c for c in text)

try:
    if recover == "1":
        from bandctl.session_lock import SessionLock
        lock = SessionLock()
    else:
        lock = fallback_lock()
    with lock:
        if recover == "0":
            print("bandctl uninstall: installed Python is missing; recovery was skipped.", file=sys.stderr)
            print("Reinstall bandctl and start it to retry cleanup.", file=sys.stderr)
        if recover == "1":
            try:
                from bandctl.shaper import Shaper
                shaper = Shaper("lo")
                for note in shaper.recover():
                    print(safe(note), flush=True)
                failed = bool(shaper.recovery_error)
            except Exception as exc:
                print("bandctl uninstall: " + safe(str(exc)), file=sys.stderr)
                failed = True
            if failed:
                print("bandctl uninstall: could not clear kernel state. Removing the program anyway.", file=sys.stderr)
                print("If a limit is still in place, install bandctl again, start it, and quit.", file=sys.stderr)
        result = subprocess.run(["bash", "-e", "-c", "remove_install \"$1\" \"$2\"", "bandctl-uninstall", prefix, bindir])
except (OSError, RuntimeError) as exc:
    print("bandctl uninstall: " + safe(str(exc)), file=sys.stderr)
    raise SystemExit(1)
raise SystemExit(result.returncode)' "$PREFIX" "$BINDIR" "$recover"
