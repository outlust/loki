#!/usr/bin/env bash
# loki-send.sh — inietta un messaggio nella sessione Loki in esecuzione
# Uso: ./loki-send.sh "testo del messaggio"
#      ./loki-send.sh --enter          (manda solo Invio, utile per sbloccare)
#      ./loki-send.sh --ctrl-c         (manda Ctrl+C per interrompere lo stream)

SESSION="loki"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Loki non è in esecuzione (nessuna sessione tmux '$SESSION')."
    exit 1
fi

case "$1" in
    --enter)
        tmux send-keys -t "$SESSION" "" Enter
        echo "[inviato: Enter]"
        ;;
    --ctrl-c)
        tmux send-keys -t "$SESSION" C-c
        echo "[inviato: Ctrl+C]"
        ;;
    "")
        echo "Uso: $0 \"messaggio\" | --enter | --ctrl-c"
        exit 1
        ;;
    *)
        # Invia il testo e poi Invio
        tmux send-keys -t "$SESSION" "$*" Enter
        echo "[inviato: $*]"
        ;;
esac
