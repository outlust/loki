#!/usr/bin/env bash
# Loki uninstall.
# Removes: venv, launcher, alias in rc files.
# Does NOT touch: ~/.loki_sessions/, ~/.loki_memory.md, ollama, the downloaded model.
# Asks before deleting the .py files and the LOKI folder.
set -e

G='\033[0;32m'; Y='\033[1;33m'; RED='\033[0;31m'; C='\033[0;36m'; D='\033[2m'; N='\033[0m'

LOKI_HOME="${LOKI_HOME:-$HOME/LOKI}"
ALIAS_MARK="# >>> loki alias (installer) >>>"
ALIAS_END="# <<< loki alias (installer) <<<"

confirm() {
  local ans
  read -r -p "$(echo -e "  ${Y}?${N} $1 [y/N] ")" ans
  [[ "$ans" =~ ^[yYsS] ]]
}

echo -e "Uninstall Loki  ${D}(target: $LOKI_HOME)${N}"
echo

# 1. Remove alias
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  if [ -f "$rc" ] && grep -qF "$ALIAS_MARK" "$rc"; then
    sed -i "/$(printf '%s' "$ALIAS_MARK" | sed 's/[]\/$*.^[]/\\&/g')/,/$(printf '%s' "$ALIAS_END" | sed 's/[]\/$*.^[]/\\&/g')/d" "$rc"
    echo -e "  ${G}✓${N} alias removed from $rc"
  fi
done

# 2. Venv
if [ -d "$LOKI_HOME/venv" ]; then
  if confirm "Remove venv $LOKI_HOME/venv?"; then
    rm -rf "$LOKI_HOME/venv"
    echo -e "  ${G}✓${N} venv removed"
  fi
fi

# 3. Launcher and Loki files (does not touch non-Loki subdirs)
LOKI_FILES=(loki.sh loki.py loki_hw.py loki_persist.py loki_mem.py install.sh uninstall.sh)
FOUND=()
for f in "${LOKI_FILES[@]}"; do
  [ -f "$LOKI_HOME/$f" ] && FOUND+=("$f")
done
if [ ${#FOUND[@]} -gt 0 ]; then
  echo -e "  Loki files in $LOKI_HOME: ${C}${FOUND[*]}${N}"
  if confirm "Remove these files?"; then
    for f in "${FOUND[@]}"; do rm -f "$LOKI_HOME/$f"; done
    echo -e "  ${G}✓${N} Loki files removed"
  fi
fi

# 4. LOKI folder empty?
if [ -d "$LOKI_HOME" ] && [ -z "$(ls -A "$LOKI_HOME" 2>/dev/null)" ]; then
  rmdir "$LOKI_HOME" && echo -e "  ${G}✓${N} folder $LOKI_HOME removed (was empty)"
fi

echo
echo -e "  ${Y}Not touched (remove by hand if you want):${N}"
echo -e "    ~/.loki_sessions/     ${D}(session history)${N}"
echo -e "    ~/.loki_memory.md     ${D}(persistent memory)${N}"
echo -e "    ollama + models       ${D}(ollama rm <model> for the model)${N}"
echo
