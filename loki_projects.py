"""Loki Projects — persistent per-project key-value memory."""
import json, re
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / '.loki' / 'projects'


def _slug(name: str) -> str:
    return re.sub(r'[^a-z0-9_-]', '_', name.strip().lower())[:64]


def _path(name: str) -> Path:
    return PROJECTS_DIR / f"{_slug(name)}.json"


def list_projects() -> list:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(PROJECTS_DIR.glob('*.json')):
        try:
            out.append(json.loads(f.read_text('utf-8')))
        except Exception:
            pass
    return out


def load(name: str) -> dict | None:
    p = _path(name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text('utf-8'))
    except Exception:
        return None


def save(project: dict):
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    _path(project['name']).write_text(
        json.dumps(project, indent=2, ensure_ascii=False), 'utf-8'
    )


def create(name: str, description: str = '') -> dict:
    p = {'name': name, 'description': description,
         'memory': {}, 'created_at': datetime.now().isoformat()}
    save(p)
    return p


def set_mem(name: str, key: str, value: str) -> dict:
    p = load(name) or create(name)
    p['memory'][key] = value
    save(p)
    return p


def del_mem(name: str, key: str) -> bool:
    p = load(name)
    if not p or key not in p['memory']:
        return False
    del p['memory'][key]
    save(p)
    return True


def delete(name: str) -> bool:
    f = _path(name)
    if not f.exists():
        return False
    f.unlink()
    return True


def memory_block(project: dict) -> str:
    """Returns the memory section to inject into the system prompt."""
    mem = project.get('memory', {})
    if not mem:
        return ''
    lines = [f"## Project: {project['name']}"]
    if project.get('description'):
        lines.append(f"Description: {project['description']}")
    lines.append("Persistent memory (do NOT re-derive these — they are already known facts):")
    for k, v in mem.items():
        lines.append(f"  • {k}: {v}")
    return '\n'.join(lines)
