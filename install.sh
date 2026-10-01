#!/usr/bin/env bash
# Install RepoAtlas from this checkout, in one command.
#
#   ./install.sh                    install; asks which directory to index
#   ./install.sh --root ~/work      install, then index every Git repository under ~/work
#   ./install.sh --yes              never ask: take the defaults (installs uv if missing)
#   ./install.sh --no-explorer      skip the explorer (no Node.js needed)
#   ./install.sh --no-agent         leave Claude Code's configuration alone
#
# What it does, each step skipped when it is already done:
#
#   1. checks for git, and for uv (offering to install it);
#   2. installs RepoAtlas and its pinned dependencies into .venv (uv sync --locked);
#   3. links `repoatlas` into ~/.local/bin, so it is on PATH;
#   4. installs and builds the graph explorer, when Node.js 22+ is present;
#   5. registers the MCP server with Claude Code and links the five skills,
#      when the `claude` command is present;
#   6. indexes a directory of repositories, when you name one.
#
# Running it again is safe: nothing it finds already in place is replaced, and
# nothing it did not create is touched. See docs/installation.md.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPOATLAS="$HERE/.venv/bin/repoatlas"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
SKILLS_DIR="$HOME/.claude/skills"

ROOT=""
ASSUME_YES=0
WITH_EXPLORER=1
WITH_AGENT=1

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --root)
      [ $# -ge 2 ] || { echo "--root needs a directory" >&2; exit 2; }
      ROOT="$2"
      shift 2
      ;;
    --root=*) ROOT="${1#--root=}"; shift ;;
    --yes | -y) ASSUME_YES=1; shift ;;
    --no-explorer) WITH_EXPLORER=0; shift ;;
    --no-agent) WITH_AGENT=0; shift ;;
    --help | -h) usage; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; RESET=""
fi

step() { printf '\n%s==>%s %s%s%s\n' "$GREEN" "$RESET" "$BOLD" "$*" "$RESET"; }
ok() { printf '    %s\n' "$*"; }
note() { printf '    %s%s%s\n' "$YELLOW" "$*" "$RESET"; }
fail() { printf '\n%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# Ask a yes/no question; the default is yes. With --yes the answer is yes, and
# with no terminal to ask on (and no --yes) it is no.
confirm() {
  if [ "$ASSUME_YES" -eq 1 ]; then
    return 0
  fi
  if [ ! -t 0 ]; then
    return 1
  fi
  local reply
  printf '    %s [Y/n] ' "$1"
  read -r reply || reply=""
  case "$reply" in
    [nN]*) return 1 ;;
    *) return 0 ;;
  esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# A link at $2 pointing at $1: made when nothing is there, kept when it is
# already this link, and left alone (with a note) when it is anything else.
link() {
  local target="$1" at="$2"
  if [ -L "$at" ] && [ "$(readlink "$at")" = "$target" ]; then
    ok "already linked: $at"
  elif [ -e "$at" ] || [ -L "$at" ]; then
    note "left alone: $at already exists and is not this installation's link"
  else
    mkdir -p "$(dirname "$at")"
    ln -s "$target" "$at"
    ok "linked $at -> $target"
  fi
}

# --- 1. prerequisites -------------------------------------------------------

step "Checking prerequisites"
have git || fail "git is required: install it, then run this again."
ok "git: $(git --version)"

if ! have uv; then
  # The installer's usual places, in case it ran before without changing PATH.
  for candidate in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    if [ -x "$candidate/uv" ]; then
      PATH="$candidate:$PATH"
      break
    fi
  done
fi
if ! have uv; then
  note "uv (https://docs.astral.sh/uv/) installs RepoAtlas and the Python it needs."
  if confirm "Install uv now, with its official installer?"; then
    have curl || fail "installing uv needs curl; install curl or uv yourself, then run this again."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$PATH"
    have uv || fail "uv was installed but is not on PATH; open a new shell and run this again."
  else
    fail "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
  fi
fi
ok "uv: $(uv --version)"

# --- 2. RepoAtlas -------------------------------------------------------------

step "Installing RepoAtlas into $HERE/.venv"
# Exactly the versions uv.lock records; uv fetches Python 3.13 itself if needed.
(cd "$HERE" && uv sync --locked)
[ -x "$REPOATLAS" ] || fail "the install did not produce $REPOATLAS"
"$REPOATLAS" --help >/dev/null || fail "$REPOATLAS does not run"
ok "installed: $REPOATLAS"

# --- 3. on PATH ---------------------------------------------------------------

step "Putting repoatlas on your PATH"
link "$REPOATLAS" "$BIN_DIR/repoatlas"
case ":$PATH:" in
  *":$BIN_DIR:"*) ok "$BIN_DIR is on PATH" ;;
  *)
    note "$BIN_DIR is not on PATH. Add this line to your shell's startup file (~/.bashrc, ~/.zshrc):"
    note "  export PATH=\"$BIN_DIR:\$PATH\""
    ;;
esac

# --- 4. the explorer ------------------------------------------------------------

EXPLORER_STATE="skipped"
if [ "$WITH_EXPLORER" -eq 1 ]; then
  step "Setting up the graph explorer (repoatlas ui)"
  NODE_MAJOR=""
  if have node; then
    NODE_MAJOR="$(node --version 2>/dev/null | sed -n 's/^v\([0-9][0-9]*\).*/\1/p')"
  fi
  if [ -z "$NODE_MAJOR" ]; then
    note "skipped: the explorer needs Node.js 22 or later, and node is not on PATH."
    note "Install Node.js (https://nodejs.org), then run \`repoatlas ui\`: it sets itself up."
  elif [ "$NODE_MAJOR" -lt 22 ]; then
    note "skipped: the explorer needs Node.js 22 or later, and node is $(node --version)."
  elif ! have pnpm && ! have corepack; then
    note "skipped: the explorer's packages are installed with pnpm, which is not on PATH."
    note "Install it (npm install -g pnpm@11), then run \`repoatlas ui\`."
  else
    "$REPOATLAS" ui --prepare
    EXPLORER_STATE="ready"
  fi
fi

# --- 5. the agent -------------------------------------------------------------

AGENT_STATE="skipped"
if [ "$WITH_AGENT" -eq 1 ]; then
  step "Connecting your agent"
  if have claude; then
    if claude mcp get repoatlas >/dev/null 2>&1; then
      ok "Claude Code already has an MCP server named repoatlas; left as it is"
    else
      claude mcp add --scope user repoatlas -- "$REPOATLAS" mcp
      ok "registered the MCP server with Claude Code (user scope)"
    fi
    AGENT_STATE="claude"
  else
    note "the \`claude\` command is not on PATH, so nothing was registered."
    note "Register this MCP server in your agent: command $REPOATLAS, argument: mcp"
  fi
  if [ -d "$HOME/.claude" ] || [ "$AGENT_STATE" = "claude" ]; then
    for skill in "$HERE"/skills/repoatlas-*/; do
      skill="${skill%/}"
      link "$skill" "$SKILLS_DIR/$(basename "$skill")"
    done
  else
    note "skills for your agent are in $HERE/skills (one directory each)"
  fi
fi

# --- 6. a workspace -----------------------------------------------------------

step "Indexing your repositories"
if [ -z "$ROOT" ] && "$REPOATLAS" repos >/dev/null 2>&1; then
  ok "a workspace is already indexed; \`repoatlas sync\` brings it up to date"
else
  if [ -z "$ROOT" ] && [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
    printf '    Which directory holds your Git repositories? (Enter to skip) '
    read -r ROOT || ROOT=""
  fi
  if [ -n "$ROOT" ]; then
    case "$ROOT" in "~"*) ROOT="$HOME${ROOT#\~}" ;; esac
    "$REPOATLAS" init "$ROOT"
  else
    note "skipped: run \`repoatlas init <directory>\` when you are ready."
  fi
fi

# --- done ---------------------------------------------------------------------

step "RepoAtlas is installed"
ok "repoatlas init <dir>   index every Git repository under <dir> (once)"
ok "repoatlas sync         update the index after you change code"
if [ "$EXPLORER_STATE" = "ready" ]; then
  ok "repoatlas ui           open the graph explorer"
fi
ok "repoatlas --help       everything else"
if [ "$AGENT_STATE" = "claude" ]; then
  ok "${DIM}Restart Claude Code to load the MCP server and the skills.${RESET}"
fi
