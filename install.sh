#!/bin/sh
# openField installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/jpbraga/open_field/main/install.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/jpbraga/open_field/main/install.sh | sh -s -- --with-tools --port 8765
#
# Options (flags or environment variables):
#   --dir DIR        OPENFIELD_DIR         where the app goes (default ~/.openfield); running it again updates it
#   --ref REF        OPENFIELD_REF         git branch or tag (default main)
#   --port N         OPENFIELD_PORT        port of the daemon (default 8765)
#   --with-tools     OPENFIELD_WITH_TOOLS=1  also download Real-ESRGAN and RIFE (~80 MB) for the video services
#   --no-service     OPENFIELD_NO_SERVICE=1  do not start it at login (launchd / systemd --user)
#   OPENFIELD_REPO   git URL to install from (default https://github.com/jpbraga/open_field.git)
#   OPENFIELD_BIN_DIR  where the `openfield` command is linked (default ~/.local/bin)
set -eu

REPO=${OPENFIELD_REPO:-https://github.com/jpbraga/open_field.git}
REF=${OPENFIELD_REF:-main}
DIR=${OPENFIELD_DIR:-$HOME/.openfield}
PORT=${OPENFIELD_PORT:-}
WITH_TOOLS=${OPENFIELD_WITH_TOOLS:-0}
NO_SERVICE=${OPENFIELD_NO_SERVICE:-0}
BIN_DIR=${OPENFIELD_BIN_DIR:-$HOME/.local/bin}

while [ $# -gt 0 ]; do
  case $1 in
    --dir) DIR=$2; shift 2 ;;
    --ref) REF=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --with-tools) WITH_TOOLS=1; shift ;;
    --no-service) NO_SERVICE=1; shift ;;
    -h|--help) sed -n '2,17p' "$0" 2>/dev/null || true; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then SUN=$(printf '\033[38;5;208m'); DIM=$(printf '\033[2m'); OFF=$(printf '\033[0m'); else SUN=; DIM=; OFF=; fi
say() { printf '%s==>%s %s\n' "$SUN" "$OFF" "$*"; }
warn() { printf '%swarning:%s %s\n' "$SUN" "$OFF" "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

case $(uname -s) in
  Darwin) OS=macOS ;;
  Linux) OS=Linux ;;
  *) die "this installer supports macOS and Linux; on Windows use install.ps1" ;;
esac

printf '%s  ▁▃▅▃▁%s  openField installer (%s)\n' "$SUN" "$OFF" "$OS"

command -v git >/dev/null 2>&1 || die "git is required (macOS: xcode-select --install; Linux: sudo apt install git)"
command -v curl >/dev/null 2>&1 || die "curl is required"

# uv manages Python (it downloads 3.12 when needed) and the app's dependencies.
UV=$(command -v uv 2>/dev/null || true)
if [ -z "$UV" ]; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    UV=$HOME/.local/bin/uv
  else
    say "installing uv (Python package manager, from astral.sh)"
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
    UV=$HOME/.local/bin/uv
  fi
fi
[ -x "$UV" ] || die "uv was not found after installing it"

if [ -d "$DIR/.git" ]; then
  say "updating $DIR ($REF)"
  git -C "$DIR" fetch --quiet origin "$REF"
  git -C "$DIR" checkout --quiet "$REF"
  git -C "$DIR" pull --quiet --ff-only origin "$REF"
else
  [ -e "$DIR" ] && die "$DIR exists and is not an openField checkout (choose another --dir)"
  say "downloading openField into $DIR"
  git clone --quiet --branch "$REF" "$REPO" "$DIR"
fi

say "installing dependencies (uv sync)"
(cd "$DIR" && "$UV" sync --frozen --no-dev --quiet)
OF=$DIR/.venv/bin/openfield
[ -x "$OF" ] || die "the openfield command was not created"

mkdir -p "$BIN_DIR"
ln -sf "$OF" "$BIN_DIR/openfield"

if [ -n "$PORT" ]; then "$OF" init --port "$PORT"; else "$OF" init; fi

if [ "$WITH_TOOLS" = 1 ]; then
  say "downloading Real-ESRGAN and RIFE"
  "$OF" install-tools || warn "could not download the video tools; try again later with: openfield install-tools"
fi

command -v ffmpeg >/dev/null 2>&1 || warn "ffmpeg is not installed; the media services need it (macOS: brew install ffmpeg; Linux: sudo apt install ffmpeg)"

if [ "$NO_SERVICE" != 1 ]; then
  say "starting openField at login"
  "$OF" service install || warn "could not install the service; run it yourself with: openfield serve"
fi

CONF=${OPENFIELD_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/openfield}
PORT_NOW=$(sed -n 's/^port:[[:space:]]*\([0-9]*\).*/\1/p' "$CONF/daemon.yaml" 2>/dev/null | head -n 1)
PORT_NOW=${PORT_NOW:-8765}

printf '\n%sopenField is installed.%s\n' "$SUN" "$OFF"
printf '  Web UI on this machine:  http://localhost:%s/  (no key needed)\n' "$PORT_NOW"
printf '  From other machines:     http://<this machine>:%s/  with a key (create keys in the web UI,\n' "$PORT_NOW"
printf '                           tab Acesso, or with  openfield key --new NAME)\n'
printf '  Config:                  %s\n' "$CONF"
printf '  App:                     %s\n' "$DIR"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) printf '%s  Add %s to your PATH to use the openfield command.%s\n' "$DIM" "$BIN_DIR" "$OFF" ;;
esac
