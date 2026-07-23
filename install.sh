#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/guigu856/huayang.git"
REPO_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/huayang/repo"
UV_BIN="$HOME/.local/bin"

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }

# 0. Fail fast when the CLI is in use: running processes keep serving the old build.
#    Match the installed CLI paths only, so the script's own path never self-matches.
if pids="$(pgrep -f '/\.local/bin/huayang' 2>/dev/null)"; then
  echo "huayang / huayang-mcp is running (pids: $(echo $pids | tr '\n' ' '))." >&2
  echo "Close those processes (admin console, MCP server) and re-run." >&2
  exit 1
fi

# 1. Locate project: use current directory when it is the huayang repo, otherwise clone.
if [ -f pyproject.toml ] && grep -q '^name = "huayang"' pyproject.toml; then
  PROJECT_DIR="$(pwd)"
  step "Using current directory: $PROJECT_DIR"
else
  if ! command -v git >/dev/null 2>&1; then
    step "git not found; installing..."
    if command -v brew >/dev/null 2>&1; then brew install git
    elif command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y git
    elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y git
    elif command -v pacman >/dev/null 2>&1; then sudo pacman -S --noconfirm git
    else echo "git is required; install it and re-run." >&2; exit 1
    fi
  fi
  if [ -d "$REPO_DIR" ]; then
    step "Repo already at $REPO_DIR; pulling latest..."
    git -C "$REPO_DIR" pull --ff-only || echo "    pull failed; using existing checkout."
  else
    step "Cloning $REPO_URL -> $REPO_DIR"
    mkdir -p "$(dirname "$REPO_DIR")"
    git clone "$REPO_URL" "$REPO_DIR"
  fi
  PROJECT_DIR="$REPO_DIR"
fi

# 2. Ensure uv.
if ! command -v uv >/dev/null 2>&1; then
  step "uv not found; installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$UV_BIN:$PATH"
fi
step "uv $(uv --version)"

# 3. Ensure FFmpeg (required by download/render features).
if ! command -v ffmpeg >/dev/null 2>&1; then
  step "FFmpeg not found; installing..."
  if command -v brew >/dev/null 2>&1; then brew install ffmpeg
  elif command -v apt-get >/dev/null 2>&1; then sudo apt-get update && sudo apt-get install -y ffmpeg
  elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y ffmpeg
  elif command -v pacman >/dev/null 2>&1; then sudo pacman -S --noconfirm ffmpeg
  else echo "    FFmpeg could not be installed automatically; download/render features need it." >&2
  fi
fi

# 4. Install dependencies, browser, and the huayang CLI.
cd "$PROJECT_DIR"
step "Syncing Python dependencies (uv sync)..."
uv sync
step "Installing Playwright Chromium..."
uv run playwright install chromium
step "Installing huayang CLI (uv tool)..."
uv tool install --force --editable .

# 5. Verify.
if [ -x "$UV_BIN/huayang-mcp" ]; then
  step "Done. CLI installed to $UV_BIN"
else
  echo "WARNING: huayang-mcp not found; check the output above." >&2
fi
case ":$PATH:" in
  *":$UV_BIN:"*) ;;
  *) echo "NOTE: add $UV_BIN to your PATH so 'huayang' and 'huayang-mcp' work in new terminals." ;;
esac
echo
echo "Next steps:"
echo "  huayang        # start the admin console at http://127.0.0.1:8788"
echo "  huayang-mcp    # start the MCP server"
