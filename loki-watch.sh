#!/usr/bin/env bash
# loki-watch.sh — mostra in tempo reale quello che Loki sta producendo
# Uso: ./loki-watch.sh [righe_da_mostrare]
#      ./loki-watch.sh --snapshot   (una foto statica del pane corrente)

SESSION="loki"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Loki non è in esecuzione (nessuna sessione tmux '$SESSION')."
    exit 1
fi

if [ "$1" = "--snapshot" ]; then
    tmux capture-pane -t "$SESSION" -p
    exit 0
fi

# Segui il pane in modalità read-only (Ctrl+C per uscire)
exec tmux attach-session -t "$SESSION" -r
