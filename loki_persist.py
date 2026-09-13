"""Session persistence for Loki: per-turn autosave + resume-last + prune.

Zero UI dependency. Each function takes paths/dicts and returns data.
The point: no long session should be lost if the terminal dies or Ollama
crashes. Every turn is atomically serialized to `_autosave.json` (tmp +
rename, so a mid-write crash never leaves a corrupted file behind).
"""
import json
import os
import tempfile
import time


AUTOSAVE_NAME = '_autosave'
AUTOSAVE_TTL_HOURS = 12
SESSION_MAX_AGE_DAYS = 60


def _serialize_msg(msg):
    """Serializable copy of a message: normalizes tool_calls (which can be
    pydantic-like objects returned by ollama, not just dicts)."""
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


def autosave_session(messages, model, sessions_dir, name=AUTOSAVE_NAME):
    """Atomically write the autosave. Skip if only the system prompt is present.

    Returns the written path (or None if skipped/error).
    """
    if not messages or len(messages) <= 1:
        return None
    os.makedirs(sessions_dir, exist_ok=True)
    path = os.path.join(sessions_dir, f"{name}.json")
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
        return path
    except Exception:
        try:
            os.unlink(tmp_path)  # noqa: F821 (only defined if mkstemp succeeded)
        except Exception:
            pass
        return None


def load_autosave_if_fresh(sessions_dir, name=AUTOSAVE_NAME,
                           ttl_hours=AUTOSAVE_TTL_HOURS):
    """Return the autosave payload if it exists and is recent, else None."""
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
    """Age of the autosave in seconds, or None if not present."""
    path = os.path.join(sessions_dir, f"{name}.json")
    if not os.path.isfile(path):
        return None
    try:
        return int(time.time() - os.path.getmtime(path))
    except Exception:
        return None


def prune_old_sessions(sessions_dir, max_age_days=SESSION_MAX_AGE_DAYS,
                       skip_prefixes=('_',)):
    """Remove .json session files older than max_age_days.

    Files whose names start with any of `skip_prefixes` (default: '_' ->
    autosave) are never touched. Returns the list of removed names.
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
