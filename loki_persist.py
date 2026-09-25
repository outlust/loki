"""Persistenza sessioni per Loki: autosave per turno + resume-last + prune.

Zero dipendenze dall'UI. Ogni funzione prende paths/dict e ritorna dati.
Il senso: nessuna sessione lunga deve andare persa se il terminale muore
o se ollama va giu'. Ogni turno viene serializzato atomicamente su
`_autosave.json` (tmp + rename, cosi' un crash a meta' scrittura non
lascia un file corrotto).
"""
import json
import os
import tempfile
import time


AUTOSAVE_NAME = '_autosave'
AUTOSAVE_TTL_HOURS = 12
SESSION_MAX_AGE_DAYS = 60
AUTOSAVE_MIN_INTERVAL_S = 30  # don't write more often than this


def _serialize_msg(msg):
    """Copia serializzabile di un messaggio: normalizza tool_calls (che possono
    essere oggetti pydantic-like ritornati da ollama, non solo dict)."""
    m = dict(msg)
    if 'tool_calls' in m and m['tool_calls']:
        tcs = []
        for tc in m['tool_calls']:
            if isinstance(tc, dict):
                tcs.append(tc)
            else:
                fn = tc.function if hasattr(tc, 'function') else {}
                tcs.append({'function': {
                    'name':      getattr(fn, 'name', ''),
                    'arguments': getattr(fn, 'arguments', {}),
                }})
        m['tool_calls'] = tcs
    return m


_autosave_last_written: dict = {}  # key=path → (timestamp, message_count)


def autosave_session(messages, model, sessions_dir, name=AUTOSAVE_NAME):
    """Atomically write the autosave. Skips if only system prompt or
    if the last save was recent AND message count hasn't changed.

    Returns the path written (or None if skipped/error).
    """
    if not messages or len(messages) <= 1:
        return None
    path = os.path.join(sessions_dir, f"{name}.json")
    now = time.time()
    last_ts, last_count = _autosave_last_written.get(path, (0, -1))
    msg_count = len(messages)
    if msg_count == last_count and (now - last_ts) < AUTOSAVE_MIN_INTERVAL_S:
        return None  # nothing changed recently, skip disk write
    os.makedirs(sessions_dir, exist_ok=True)
    payload = {
        'saved_at': time.strftime("%Y-%m-%d %H:%M:%S"),
        'model':    model,
        'autosave': True,
        'messages': [_serialize_msg(m) for m in messages[1:]],
    }
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(prefix='.autosave-',
                                            dir=sessions_dir)
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, path)
        _autosave_last_written[path] = (time.time(), msg_count)
        return path
    except Exception:
        try:
            os.unlink(tmp_path)  # noqa: F821 (defined only if mkstemp succeeded)
        except Exception:
            pass
        return None


def load_autosave_if_fresh(sessions_dir, name=AUTOSAVE_NAME,
                           ttl_hours=AUTOSAVE_TTL_HOURS):
    """Ritorna il payload dell'autosave se esiste ed e' recente, altrimenti None."""
    path = os.path.join(sessions_dir, f"{name}.json")
    if not os.path.isfile(path):
        return None
    try:
        age = time.time() - os.path.getmtime(path)
        if age > ttl_hours * 3600:
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def autosave_age_seconds(sessions_dir, name=AUTOSAVE_NAME):
    """Eta' dell'autosave in secondi, o None se non esiste."""
    path = os.path.join(sessions_dir, f"{name}.json")
    if not os.path.isfile(path):
        return None
    try:
        return int(time.time() - os.path.getmtime(path))
    except Exception:
        return None


def prune_old_sessions(sessions_dir, max_age_days=SESSION_MAX_AGE_DAYS,
                       skip_prefixes=('_',)):
    """Rimuove file di sessione .json piu' vecchi di max_age_days.

    File con nome che inizia per skip_prefixes (default: '_' -> autosave)
    non vengono mai toccati. Ritorna la lista di nomi rimossi.
    """
    if not os.path.isdir(sessions_dir):
        return []
    cutoff = time.time() - max_age_days * 86400
    removed = []
    for fname in os.listdir(sessions_dir):
        if not fname.endswith('.json'):
            continue
        if any(fname.startswith(p) for p in skip_prefixes):
            continue
        path = os.path.join(sessions_dir, fname)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed.append(fname)
        except Exception:
            pass
    return removed
