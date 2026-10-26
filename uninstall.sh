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

py="$PREFIX/venv/bin/python"
if [ -x "$py" ]; then
  if ! "$py" -c 'from bandctl.shaper import Shaper
notes = Shaper("lo").recover()
for note in notes:
    print(note)'; then
    echo "bandctl uninstall: could not clear kernel state. Removing the program anyway." >&2
    echo "If a limit is still in place, install bandctl again, start it, and quit." >&2
  fi
fi

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
