#!/bin/bash
# Install bandctl system-wide so `sudo bandctl` works.
# From a checkout: sudo ./install.sh
# From the internet: curl -fsSL .../install.sh | sudo bash

set -euo pipefail

INSTALL_URL="https://raw.githubusercontent.com/DanielMellen-University/bandctl/main/install.sh"
TARBALL_URL="https://github.com/DanielMellen-University/bandctl/archive/refs/heads/main.tar.gz"
PREFIX="/opt/bandctl"
BINDIR="/usr/local/bin"
SKIP_DEPS=0
ORIG_ARGS=("$@")

die() {
  echo "bandctl install: $*" >&2
  exit 1
}

usage() {
  cat << 'EOF'
Usage: sudo ./install.sh [--prefix DIR] [--bindir DIR] [--skip-deps]

Installs bandctl into /opt/bandctl and links these commands:
  bandctl
  bandctl-uninstall

--prefix and --bindir together install somewhere else (no root needed
when both directories are writable). --skip-deps does not install
python3, iproute2, or nftables.
EOF
}

# Piped into a shell: $0 is bash, not this file. Download the repo and re-run.
if [ ! -f "$0" ] || [ ! -f "$(dirname "$0")/pyproject.toml" ]; then
  if [ "$(id -u)" -ne 0 ]; then
    die "pipe the installer to sudo: curl -fsSL $INSTALL_URL | sudo bash"
  fi
  command -v curl >/dev/null 2>&1 || die "curl is required to download bandctl"
  command -v tar >/dev/null 2>&1 || die "tar is required to download bandctl"
  tmp=$(mktemp -d)
  curl -fsSL "$TARBALL_URL" | tar -xz -C "$tmp" --strip-components=1
  exec "$tmp/install.sh" "$@"
fi

SRC=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
[ -f "$SRC/pyproject.toml" ] || die "pyproject.toml is missing next to install.sh"

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
    --skip-deps)
      SKIP_DEPS=1
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

case "$PREFIX" in
  /|/usr|/usr/bin|/usr/local|/usr/local/bin|/opt|/bin|/sbin|/etc|/home|/root|/tmp|/var)
    die "refusing to install into $PREFIX"
    ;;
  /*) ;;
  *) die "prefix must be an absolute path" ;;
esac
case "$BINDIR" in
  /*) ;;
  *) die "bindir must be an absolute path" ;;
esac
case "$PREFIX$BINDIR" in
  *[!A-Za-z0-9._/-]*) die "prefix and bindir may only use letters, numbers, and ._/-" ;;
esac

if [ "$(id -u)" -ne 0 ] && { [ "$PREFIX" = /opt/bandctl ] || [ "$BINDIR" = /usr/local/bin ]; }; then
  exec sudo "$0" "${ORIG_ARGS[@]}"
fi

bandctl_is_running() {
  local hits
  hits=$(ps -eo args= | grep -E '^([^[:space:]]*/)?bandctl([[:space:]]|$)|^([^[:space:]]*/)?python[0-9.]*[[:space:]]+(-m[[:space:]]+bandctl([.[:space:]]|$)|([^[:space:]]*/)?bandctl([[:space:]]|$))' || true)
  [ -n "$hits" ]
}

if bandctl_is_running; then
  die "bandctl is running. Quit it with q first, then install again."
fi

need_deps() {
  command -v python3 >/dev/null 2>&1 || return 0
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || return 0
  python3 -c 'import ensurepip' >/dev/null 2>&1 || return 0
  command -v tc >/dev/null 2>&1 || return 0
  command -v nft >/dev/null 2>&1 || return 0
  return 1
}

install_deps() {
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y --no-install-recommends python3 python3-venv python3-pip iproute2 nftables ca-certificates
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip iproute nftables ca-certificates
  elif command -v pacman >/dev/null 2>&1; then
    pacman -Sy --noconfirm python python-pip iproute2 nftables ca-certificates
  else
    die "install python3 (>= 3.10), python3-venv, iproute2, and nftables, then re-run"
  fi
}

if [ "$SKIP_DEPS" -eq 0 ] && need_deps; then
  install_deps
fi

command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' || die "python3 is older than 3.10"

mkdir -p "$PREFIX" "$BINDIR"
chmod 755 "$PREFIX"

place_command() {
  local dest=$1
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    local current
    current=$(readlink -f "$dest" 2>/dev/null || true)
    case "$current" in
      "$PREFIX"/*) ;;
      *) die "$dest already exists and is not a bandctl install" ;;
    esac
  fi
  ln -sfn "$PREFIX/venv/bin/bandctl" "$dest"
}

rm -rf "$PREFIX/venv"
python3 -m venv "$PREFIX/venv" || die "could not create a virtualenv. On Debian or Ubuntu install python3-venv, or re-run without --skip-deps."
"$PREFIX/venv/bin/pip" install --disable-pip-version-check "$SRC"

place_command "$BINDIR/bandctl"
cp "$SRC/uninstall.sh" "$PREFIX/uninstall.sh"
chmod 755 "$PREFIX/uninstall.sh"

wrapper="$BINDIR/bandctl-uninstall"
if [ -e "$wrapper" ] && ! grep -F -q "$PREFIX/uninstall.sh" "$wrapper"; then
  die "$wrapper already exists and is not a bandctl install"
fi
cat > "$wrapper" << EOF
#!/bin/sh
exec "$PREFIX/uninstall.sh" --prefix "$PREFIX" --bindir "$BINDIR" "\$@"
EOF
chmod 755 "$wrapper"

cat > "$PREFIX/install-stamp" << EOF
PREFIX=$PREFIX
BINDIR=$BINDIR
EOF
chmod 644 "$PREFIX/install-stamp"

version=$("$BINDIR/bandctl" --version)
echo "Installed $version"
echo "  sudo bandctl"
echo "  sudo bandctl --iface wlan0"
echo "  sudo bandctl-uninstall"
