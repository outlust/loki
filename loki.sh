#!/usr/bin/env bash
set -e

VENV_PATH="$HOME/venv-shell-agent"
SCRIPT_PATH="$HOME/loki.py"
SESSION="loki"

if [ ! -d "$VENV_PATH" ]; then
    echo "Errore: venv non trovato in $VENV_PATH"
    exit 1
fi

if [ ! -f "$SCRIPT_PATH" ]; then
    echo "Errore: script non trovato in $SCRIPT_PATH"
    exit 1
fi

if ! systemctl is-active --quiet ollama; then
    echo "Ollama non attivo, lo avvio..."
    sudo systemctl start ollama
    sleep 2
fi

# Se già esiste una sessione tmux "loki", ci si attacca direttamente
if tmux has-session -t "$SESSION" 2>/dev/null; then
    exec tmux attach-session -t "$SESSION"
fi

# Nuova sessione tmux detached, poi attach
tmux new-session -d -s "$SESSION" -x 220 -y 50 \
    "$VENV_PATH/bin/python3" "$SCRIPT_PATH" "$@"
exec tmux attach-session -t "$SESSION"
