"""Memoria persistente di Loki: file capato con rotazione.

Il file `~/.loki_memory.md` mescola:
- header con note utente (`/remember`);
- blocchi `### Sessione compressa <data>` prodotti dal compress.

Ruotiamo i blocchi mantenendo gli ultimi N; se il file supera comunque
il cap hard (troppi `/remember`, sommari giganti), tagliamo dalla cima
mettendo un marker esplicito.
"""
import os
import re


MEMORY_HARD_CAP_BYTES = 50 * 1024  # 50 KB tetto duro


def load_memory(path):
    if os.path.isfile(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def append_memory(path, text):
    with open(path, 'a') as f:
        f.write(f"\n\n{text.strip()}")


def rotate_summaries(path, max_summaries=5):
    """Mantiene al piu' `max_summaries` blocchi `### Sessione compressa`.
    Le note utente in cima non vengono toccate.
    """
    if not os.path.exists(path):
        return
    with open(path) as f:
        content = f.read()
    parts = re.split(r'(?m)^### Sessione compressa ', content)
    if len(parts) - 1 <= max_summaries:
        return
    header = parts[0].rstrip()
    kept = parts[-max_summaries:]
    body = "\n\n".join(f"### Sessione compressa {p.rstrip()}" for p in kept)
    new = (header + "\n\n" + body).strip() + "\n"
    with open(path, 'w') as f:
        f.write(new)


def enforce_hard_cap(path, cap_bytes=MEMORY_HARD_CAP_BYTES):
    """Se il file supera cap_bytes anche dopo rotate_summaries, taglia dalla
    cima al primo boundary di paragrafo utile e mette un marker.
    """
    if not os.path.isfile(path):
        return
    size = os.path.getsize(path)
    if size <= cap_bytes:
        return
    with open(path) as f:
        content = f.read()
    target = int(cap_bytes * 0.9)  # scendi sotto il cap, evita retrigger
    excess = len(content) - target
    if excess <= 0:
        return
    cut_from = excess
    nl = content.find('\n\n', cut_from)
    if nl >= 0:
        cut_from = nl + 2
    trimmed = content[cut_from:].lstrip()
    marker = (f"[memoria auto-troncata: {excess} byte piu' vecchi "
              f"rimossi per rientrare nel cap]\n\n")
    with open(path, 'w') as f:
        f.write(marker + trimmed)
