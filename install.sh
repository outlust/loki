#!/usr/bin/env bash
# Loki shell-agent — from-zero installer.
# Usage:
#   bash install.sh              # interactive
#   bash install.sh -y           # assume yes on all prompts (default model)
#   LOKI_HOME=/elsewhere bash install.sh
#
# Does: prereqs -> ollama -> venv -> pip -> pull model ->
#       write ~/LOKI/loki.sh + `loki` alias in .bashrc/.zshrc.
# Idempotent: re-running updates what's there without breaking anything.

set -e
umask 022

# ── colors
G='\033[0;32m'; Y='\033[1;33m'; RED='\033[0;31m'
C='\033[0;36m'; D='\033[2m'; B='\033[1m'; N='\033[0m'

step() { echo -e "\n${C}▶${N} ${B}${1}${N}"; }
ok()   { echo -e "  ${G}✓${N} $1"; }
warn() { echo -e "  ${Y}!${N} $1"; }
err()  { echo -e "  ${RED}✗${N} $1"; }

# ── flags
YES=0
for a in "$@"; do
  case "$a" in
    -y|--yes) YES=1 ;;
    -h|--help)
      cat <<EOF
Loki installer

Options:
  -y, --yes     auto-confirm all prompts
  -h, --help    this help

Environment variables:
  LOKI_HOME     destination directory (default: \$HOME/LOKI)
  LOKI_MODEL    Ollama model to use (default: asked interactively)
EOF
      exit 0
      ;;
  esac
done

confirm() {
  [ "$YES" -eq 1 ] && return 0
  local prompt="$1"
  local ans
  read -r -p "$(echo -e "  ${Y}?${N} $prompt [y/N] ")" ans
  [[ "$ans" =~ ^[yYsS] ]]
}

# ── destination and bundle
LOKI_HOME="${LOKI_HOME:-$HOME/LOKI}"
BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_MODEL="${LOKI_MODEL:-orcarouter/Qwen3.8-27B-Uncensored:latest}"

echo -e "${B}Loki installer${N}  ${D}(destination: $LOKI_HOME)${N}"

# ═══════════════════════════════════════════════════════════════════════
step "1/9 · Prerequisites"
# ═══════════════════════════════════════════════════════════════════════
if [ "$(uname -s)" != "Linux" ]; then
  err "Loki requires Linux (uses systemd + ollama)."
  exit 1
fi
ok "OS: Linux"

if ! command -v python3 &>/dev/null; then
  err "python3 not found. Install it (e.g. sudo apt install python3 python3-venv python3-pip)."
  exit 1
fi
PY_V="$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
PY_MAJ="${PY_V%.*}"; PY_MIN="${PY_V#*.}"
if [ "$PY_MAJ" -lt 3 ] || { [ "$PY_MAJ" -eq 3 ] && [ "$PY_MIN" -lt 9 ]; }; then
  err "Python 3.9+ required (found $PY_V)"
  exit 1
fi
ok "Python $PY_V"

if ! python3 -c 'import venv' 2>/dev/null; then
  err "'venv' module missing. Install: sudo apt install python3-venv"
  exit 1
fi
ok "venv module present"

for cmd in curl grep sed awk; do
  command -v "$cmd" &>/dev/null || { err "missing $cmd"; exit 1; }
done
ok "base tools (curl, grep, sed, awk)"

# ═══════════════════════════════════════════════════════════════════════
step "2/9 · GPU detection"
# ═══════════════════════════════════════════════════════════════════════
GPU_KIND="cpu"
if command -v nvidia-smi &>/dev/null && nvidia-smi -L >/dev/null 2>&1; then
  GPU_KIND="nvidia"
  ok "NVIDIA GPU: $(nvidia-smi -L | head -1)"
elif command -v rocm-smi &>/dev/null && rocm-smi >/dev/null 2>&1; then
  GPU_KIND="amd_rocm"
  ok "AMD GPU with ROCm active"
else
  LSPCI_OUT=""
  command -v lspci &>/dev/null && LSPCI_OUT="$(lspci 2>/dev/null || true)"
  if echo "$LSPCI_OUT" | grep -iE 'vga|3d|display' | grep -qi nvidia; then
    warn "NVIDIA GPU present but nvidia-smi does not work — driver missing?"
  elif echo "$LSPCI_OUT" | grep -iE 'vga|3d|display' | grep -qi amd; then
    warn "AMD GPU present but ROCm not active — Ollama will run on CPU"
  else
    warn "No GPU detected — Ollama will run on CPU (slow with large models)"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════
step "3/9 · Ollama"
# ═══════════════════════════════════════════════════════════════════════
if command -v ollama &>/dev/null; then
  ok "ollama already installed ($(ollama --version 2>&1 | head -1 | tr -d '\n'))"
else
  warn "ollama not installed"
  if confirm "Download and install Ollama (curl from ollama.com, needs sudo)?"; then
    curl -fsSL https://ollama.com/install.sh | sh
    if command -v ollama &>/dev/null; then
      ok "ollama installed"
    else
      err "ollama install failed — install manually from https://ollama.com/download"
      exit 1
    fi
  else
    err "Loki cannot run without Ollama. Aborted."
    exit 1
  fi
fi

# ── systemd service (best effort)
if command -v systemctl &>/dev/null && systemctl list-unit-files 2>/dev/null | grep -q '^ollama\.service'; then
  if systemctl is-active --quiet ollama; then
    ok "ollama service active"
  else
    if confirm "Start the ollama service (sudo systemctl start ollama)?"; then
      sudo systemctl start ollama || warn "start failed — start it manually"
      sleep 1
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════════════
step "4/9 · Prepare $LOKI_HOME and copy files"
# ═══════════════════════════════════════════════════════════════════════
mkdir -p "$LOKI_HOME"

PY_FILES=(loki.py loki_hw.py loki_persist.py loki_mem.py loki_sec.py)
# install.sh and uninstall.sh also land in LOKI_HOME so the user can
# reinstall/uninstall later without keeping the original bundle around.
AUX_FILES=(install.sh uninstall.sh requirements.txt)
if [ "$BUNDLE_DIR" != "$LOKI_HOME" ]; then
  for f in "${PY_FILES[@]}"; do
    if [ -f "$BUNDLE_DIR/$f" ]; then
      cp "$BUNDLE_DIR/$f" "$LOKI_HOME/$f"
      ok "copied $f"
    else
      err "missing file in bundle: $f (expected at: $BUNDLE_DIR/$f)"
      exit 1
    fi
  done
  for f in "${AUX_FILES[@]}"; do
    if [ -f "$BUNDLE_DIR/$f" ]; then
      cp "$BUNDLE_DIR/$f" "$LOKI_HOME/$f"
      [ "$f" != "requirements.txt" ] && chmod +x "$LOKI_HOME/$f"
      ok "copied $f"
    fi
  done
else
  # In place: verify that all .py files exist
  for f in "${PY_FILES[@]}"; do
    [ -f "$LOKI_HOME/$f" ] || { err "missing $LOKI_HOME/$f"; exit 1; }
  done
  ok "bundle already at $LOKI_HOME"
fi

# ═══════════════════════════════════════════════════════════════════════
step "5/9 · Python venv"
# ═══════════════════════════════════════════════════════════════════════
VENV="$LOKI_HOME/venv"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
  ok "venv created at $VENV"
else
  ok "venv already present at $VENV"
fi

# ═══════════════════════════════════════════════════════════════════════
step "6/9 · Python dependencies (ollama, prompt_toolkit)"
# ═══════════════════════════════════════════════════════════════════════
"$VENV/bin/pip" install --quiet --upgrade pip
if [ -f "$LOKI_HOME/requirements.txt" ]; then
  "$VENV/bin/pip" install --quiet -r "$LOKI_HOME/requirements.txt"
else
  "$VENV/bin/pip" install --quiet "ollama>=0.3" "prompt_toolkit>=3.0"
fi
ok "dependencies installed"

# ═══════════════════════════════════════════════════════════════════════
step "7/9 · LLM model"
# ═══════════════════════════════════════════════════════════════════════
CHOSEN_MODEL="$DEFAULT_MODEL"
echo -e "  Default: ${C}$DEFAULT_MODEL${N}  ${D}(~17 GB, needs a lot of RAM/VRAM)${N}"
if [ "$YES" -ne 1 ]; then
  echo -e "  ${D}Lighter alternatives: qwen3:8b (~5GB), qwen2.5:7b-instruct (~4.5GB), llama3.1:8b${N}"
  if confirm "Use a different model?"; then
    read -r -p "  Ollama model name: " user_choice
    [ -n "$user_choice" ] && CHOSEN_MODEL="$user_choice"
  fi
fi

if command -v ollama &>/dev/null; then
  if ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | grep -qxF "$CHOSEN_MODEL"; then
    ok "model $CHOSEN_MODEL already downloaded"
  else
    if confirm "Pull ${C}$CHOSEN_MODEL${N} now (may be several GB)?"; then
      ollama pull "$CHOSEN_MODEL" || warn "pull failed — retry later with: ollama pull $CHOSEN_MODEL"
    else
      warn "Skipping pull. Do it later with: ollama pull $CHOSEN_MODEL"
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════════════
step "8/9 · Launcher $LOKI_HOME/loki.sh"
# ═══════════════════════════════════════════════════════════════════════
cat > "$LOKI_HOME/loki.sh" <<EOF
#!/usr/bin/env bash
# Loki launcher — generated by install.sh
set -e
LOKI_HOME="$LOKI_HOME"
export LOKI_MODEL="\${LOKI_MODEL:-$CHOSEN_MODEL}"

if [ ! -d "\$LOKI_HOME/venv" ]; then
  echo "Error: venv not found at \$LOKI_HOME/venv"
  echo "Re-run install.sh"
  exit 1
fi
if [ ! -f "\$LOKI_HOME/loki.py" ]; then
  echo "Error: loki.py not found at \$LOKI_HOME"
  exit 1
fi

# Start Ollama if available via systemd
if command -v systemctl &>/dev/null && systemctl list-unit-files 2>/dev/null | grep -q '^ollama\\.service' && ! systemctl is-active --quiet ollama; then
  echo "Ollama not running, starting it..."
  sudo systemctl start ollama
  sleep 2
fi

cd "\$LOKI_HOME"
exec "\$LOKI_HOME/venv/bin/python3" "\$LOKI_HOME/loki.py" "\$@"
EOF
chmod +x "$LOKI_HOME/loki.sh"
ok "launcher written and made executable"

# ═══════════════════════════════════════════════════════════════════════
step "9/9 · 'loki' shell alias"
# ═══════════════════════════════════════════════════════════════════════
ALIAS_LINE="alias loki='$LOKI_HOME/loki.sh'"
ALIAS_MARK="# >>> loki alias (installer) >>>"
ALIAS_END="# <<< loki alias (installer) <<<"

add_alias_to() {
  local rc="$1"
  [ -f "$rc" ] || return 0
  if grep -qF "$ALIAS_MARK" "$rc"; then
    # Refresh the existing block (in case LOKI_HOME has changed)
    sed -i "/$(printf '%s' "$ALIAS_MARK" | sed 's/[]\/$*.^[]/\\&/g')/,/$(printf '%s' "$ALIAS_END" | sed 's/[]\/$*.^[]/\\&/g')/d" "$rc"
  fi
  {
    echo ""
    echo "$ALIAS_MARK"
    echo "$ALIAS_LINE"
    echo "$ALIAS_END"
  } >> "$rc"
  ok "alias updated in $rc"
}
add_alias_to "$HOME/.bashrc"
add_alias_to "$HOME/.zshrc"

# ═══════════════════════════════════════════════════════════════════════
echo
echo -e "${G}════════════════════════════════════════════════════════${N}"
echo -e "${G}  Installation complete${N}"
echo -e "${G}════════════════════════════════════════════════════════${N}"
echo
echo -e "  ${B}Loki:${N}      $LOKI_HOME"
echo -e "  ${B}Model:${N}     $CHOSEN_MODEL"
echo -e "  ${B}Venv:${N}      $LOKI_HOME/venv"
echo -e "  ${B}Sessions:${N}  ~/.loki_sessions/  ${D}(shared across installs)${N}"
echo -e "  ${B}Memory:${N}    ~/.loki_memory.md"
echo
echo -e "  ${Y}Next steps:${N}"
echo -e "    1. Reload your shell:  ${C}source ~/.bashrc${N}  (or open a new terminal)"
echo -e "    2. Run:                ${C}loki${N}"
echo
echo -e "  ${D}To uninstall:          bash $LOKI_HOME/uninstall.sh${N}"
echo
