#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/guigu856/huayang.git"
REPO_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/huayang/repo"

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
fail() { echo "ERROR: $1" >&2; exit 1; }

is_huayang_source() {
  local root="$1"
  [ -f "$root/pyproject.toml" ] &&
    grep -q '^name = "huayang"' "$root/pyproject.toml" &&
    [ -f "$root/.mcp.json" ] &&
    [ -f "$root/.codex-plugin/plugin.json" ] &&
    [ -f "$root/rules/main-agent.md" ] &&
    [ -d "$root/skills" ] &&
    [ -d "$root/schemas" ]
}

is_expected_origin() {
  case "$1" in
    https://github.com/guigu856/huayang|https://github.com/guigu856/huayang.git|git@github.com:guigu856/huayang|git@github.com:guigu856/huayang.git|ssh://git@github.com/guigu856/huayang|ssh://git@github.com/guigu856/huayang.git)
      return 0
      ;;
    *) return 1 ;;
  esac
}

validate_git_origin() {
  local root="$1"
  local origin
  origin="$(git -C "$root" remote get-url origin 2>/dev/null || true)"
  is_expected_origin "$origin" || fail "Existing checkout has unexpected origin: ${origin:-<missing>}"
}

ensure_git() {
  if command -v git >/dev/null 2>&1; then return; fi
  step "git not found; installing..."
  if command -v brew >/dev/null 2>&1; then brew install git
  elif command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y git
  elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y git
  elif command -v pacman >/dev/null 2>&1; then sudo pacman -S --noconfirm git
  else fail "git is required; install it and re-run."
  fi
  command -v git >/dev/null 2>&1 || fail "git installation did not place git on PATH."
}

# Running processes keep serving or locking the old build.
if pids="$(pgrep -f '/huayang(-mcp)?$' 2>/dev/null)" && [ -n "$pids" ]; then
  echo "huayang / huayang-mcp is running (pids: $(echo "$pids" | tr '\n' ' '))." >&2
  echo "Close those processes and re-run." >&2
  exit 1
fi

# Use a valid current source tree, otherwise maintain a dedicated checkout.
if is_huayang_source "$(pwd)"; then
  PROJECT_DIR="$(pwd)"
  if [ -d "$PROJECT_DIR/.git" ]; then validate_git_origin "$PROJECT_DIR"; fi
  step "Using current source directory: $PROJECT_DIR"
else
  ensure_git
  if [ -e "$REPO_DIR" ]; then
    [ -d "$REPO_DIR/.git" ] || fail "$REPO_DIR exists but is not a Git checkout. Move it aside and re-run."
    validate_git_origin "$REPO_DIR"
    if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
      fail "$REPO_DIR has local changes. Commit or remove them before upgrading."
    fi
    step "Updating existing checkout at $REPO_DIR..."
    git -C "$REPO_DIR" pull --ff-only
  else
    step "Cloning $REPO_URL -> $REPO_DIR"
    mkdir -p "$(dirname "$REPO_DIR")"
    git clone "$REPO_URL" "$REPO_DIR"
  fi
  is_huayang_source "$REPO_DIR" || fail "Downloaded checkout is missing required Huayang files."
  PROJECT_DIR="$REPO_DIR"
fi

# Ensure uv and resolve its configured tool executable directory dynamically.
if ! command -v uv >/dev/null 2>&1; then
  step "uv not found; installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || fail "uv installation completed but uv is not on PATH."
step "uv $(uv --version)"
UV_BIN="$(uv tool dir --bin)"
mkdir -p "$UV_BIN"
export PATH="$UV_BIN:$PATH"
uv tool update-shell >/dev/null 2>&1 || true

# FFmpeg and ffprobe are required by download, analysis and rendering features.
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  step "FFmpeg not found; installing..."
  if command -v brew >/dev/null 2>&1; then brew install ffmpeg
  elif command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y ffmpeg
  elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y ffmpeg
  elif command -v pacman >/dev/null 2>&1; then sudo pacman -S --noconfirm ffmpeg
  else fail "FFmpeg could not be installed automatically. Install ffmpeg and ffprobe, then re-run."
  fi
fi
command -v ffmpeg >/dev/null 2>&1 || fail "ffmpeg is still unavailable after installation."
command -v ffprobe >/dev/null 2>&1 || fail "ffprobe is still unavailable after installation."

# Install dependencies/browser for the source checkout, then expose the user commands.
cd "$PROJECT_DIR"
step "Syncing locked Python dependencies..."
uv sync
step "Installing Playwright Chromium..."
uv run playwright install chromium
step "Installing Huayang commands..."
uv tool install --force --editable .

HUAYANG_BIN="$UV_BIN/huayang"
HUAYANG_MCP_BIN="$UV_BIN/huayang-mcp"
[ -x "$HUAYANG_BIN" ] || fail "huayang was not installed to $UV_BIN."
[ -x "$HUAYANG_MCP_BIN" ] || fail "huayang-mcp was not installed to $UV_BIN."

step "Running installation doctor..."
"$HUAYANG_BIN" doctor --json

if command -v codex >/dev/null 2>&1; then
  step "Codex detected; installing Huayang Plugin..."
  "$HUAYANG_BIN" plugin install codex
else
  echo "NOTE: Codex was not found. Install it later with: huayang plugin install codex"
fi

echo
echo "Installation complete."
echo "  huayang                         # start admin console at http://127.0.0.1:8788"
echo "  huayang-mcp                     # start MCP server"
echo "  huayang doctor                  # verify runtime"
echo "  huayang plugin install codex    # install/refresh Codex Plugin"
