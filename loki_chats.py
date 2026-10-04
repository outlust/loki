"""Loki Chats — persistent chat sessions linked to projects."""
import json, threading, time, uuid
from pathlib import Path

CHATS_DIR = Path.home() / '.loki' / 'chats'


def _ensure():
    CHATS_DIR.mkdir(parents=True, exist_ok=True)


def _path(chat_id: str) -> Path:
    return CHATS_DIR / f'{chat_id}.json'


def create(project: str | None = None) -> dict:
    _ensure()
    cid = f'ch_{int(time.time())}_{uuid.uuid4().hex[:6]}'
    chat = {
        'id':         cid,
        'title':      'Nuova chat',
        'project':    project,
        'messages':   [],
        'created_at': time.time(),
        'updated_at': time.time(),
    }
    _write(chat)
    return chat


def _write(chat: dict):
    _ensure()
    chat['updated_at'] = time.time()
    tmp = _path(chat['id']).with_suffix('.tmp')
    tmp.write_text(json.dumps(chat, ensure_ascii=False, indent=2), 'utf-8')
    tmp.replace(_path(chat['id']))


def save_messages(chat_id: str, messages: list):
    chat = load(chat_id)
    if chat is None:
        return
    # exclude system prompt (role == 'system') so we rebuild it fresh on load
    chat['messages'] = [m for m in messages if m.get('role') != 'system']
    _write(chat)


def load(chat_id: str) -> dict | None:
    p = _path(chat_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text('utf-8'))
    except Exception:
        return None


def list_chats() -> list:
    _ensure()
    chats = []
    for p in CHATS_DIR.glob('*.json'):
        try:
            c = json.loads(p.read_text('utf-8'))
            msgs = [m for m in c.get('messages', []) if m.get('role') != 'system']
            chats.append({
                'id':            c['id'],
                'title':         c.get('title', 'Chat'),
                'project':       c.get('project'),
                'message_count': len(msgs),
                'updated_at':    c.get('updated_at', 0),
                'created_at':    c.get('created_at', 0),
            })
        except Exception:
            pass
    return sorted(chats, key=lambda x: x['updated_at'], reverse=True)


def delete(chat_id: str):
    p = _path(chat_id)
    if p.exists():
        p.unlink()


def update_title(chat_id: str, title: str) -> dict | None:
    chat = load(chat_id)
    if chat:
        chat['title'] = title[:80]
        _write(chat)
    return chat


def gen_title_async(chat_id: str, first_user_msg: str, callback=None):
    """Generate a title in background; calls callback(chat_id, title) when done."""
    def _run():
        title = _gen_title(first_user_msg)
        if title:
            update_title(chat_id, title)
            if callback:
                callback(chat_id, title)
    threading.Thread(target=_run, daemon=True).start()


def _gen_title(user_msg: str) -> str | None:
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=12,
            messages=[{
                'role': 'user',
                'content': (
                    f'Genera un titolo brevissimo (3-5 parole in italiano) per questa conversazione: '
                    f'"{user_msg[:300]}". '
                    f'Rispondi SOLO con il titolo, niente altro, niente virgolette.'
                ),
            }],
        )
        return resp.content[0].text.strip().strip('"').strip("'")[:60] or None
    except Exception:
        return None
