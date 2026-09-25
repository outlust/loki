"""Loki Web UI — aiohttp + WebSocket server.

Avvio:  LOKI_UI=web loki.sh          (porta default 8080)
        LOKI_PORT=9090 LOKI_UI=web loki.sh
Accesso: http://<tailscale-ip>:<porta>   — nessun dominio necessario

Richiede: pip install aiohttp  (aggiunto in requirements.txt)
"""

import asyncio
import json
import os
import re
import sys
import threading

_ANSI_RE = re.compile(r'\033\[[0-9;]*[mKHJA-Za-z]|\033\].*?\x07')
_ws_clients: set = set()
_main_loop = None   # asyncio event loop, settato da run()

# ── Confirm mechanism (thread-safe) ──────────────────────────────────────────
_confirm_event  = threading.Event()
_confirm_result = [False]


def confirm_web(command: str) -> bool:
    """Inviato dal thread executor. Mostra modal nel browser e attende risposta."""
    _confirm_event.clear()
    _confirm_result[0] = False
    _broadcast_sync({"type": "confirm", "command": command})
    _confirm_event.wait(timeout=300)  # 5 minuti
    return _confirm_result[0]


# ── WebOutputProxy ────────────────────────────────────────────────────────────
class WebOutputProxy:
    """Intercetta sys.stdout, rimuove ANSI, manda testo al browser via WS."""
    def __init__(self, orig):
        self._orig = orig

    def write(self, text):
        if isinstance(text, bytes):
            text = text.decode('utf-8', errors='replace')
        clean = _ANSI_RE.sub('', text)
        if '\r' in clean:
            # Progress bars: tieni solo dopo l'ultimo \r
            clean = clean.split('\r')[-1]
        if clean.strip('\n'):
            _broadcast_sync({"type": "out", "text": clean})
        return len(text)

    def flush(self): pass
    def isatty(self): return False
    def writable(self): return True

    def fileno(self):
        try:
            return self._orig.fileno()
        except Exception:
            return -1


# ── Broadcast ─────────────────────────────────────────────────────────────────
def _broadcast_sync(msg: dict):
    if _main_loop is None or not _ws_clients:
        return
    asyncio.run_coroutine_threadsafe(_broadcast_async(msg), _main_loop)


async def _broadcast_async(msg: dict):
    txt = json.dumps(msg, ensure_ascii=False)
    dead = set()
    for ws in list(_ws_clients):
        try:
            await ws.send_str(txt)
        except Exception:
            dead.add(ws)
    for d in dead:
        _ws_clients.discard(d)


# ── State & processing ────────────────────────────────────────────────────────
_state: dict = {}
_processing_lock = threading.Lock()


def _send_stats():
    try:
        import loki as _l
        ctx_pct = (int(100 * _l.stats['ctx_used'] / _l.stats['working_ctx'])
                   if _l.stats.get('ctx_used') and _l.stats.get('working_ctx') else 0)
        _broadcast_sync({
            "type": "stats",
            "model":    _l.MODEL.split('/')[-1][:32],
            "messages": _l.stats['messages'],
            "ctx_pct":  ctx_pct,
            "working_ctx": _l.stats['working_ctx'],
            "uptime":   int((
                __import__('datetime').datetime.now() - _l.stats['start_time']
            ).total_seconds()),
        })
    except Exception:
        pass


def _do_turn(text: str):
    import loki as _l
    if text.startswith('/'):
        action, payload = _l.parse_slash(text, _state['messages'])
        if action == 'exit':
            _broadcast_sync({"type": "server_exit"})
        elif action == 'clear':
            _state['messages'] = _state['messages'][:1]
            _broadcast_sync({"type": "clear"})
        elif action in ('resume', 'trim'):
            _state['messages'] = payload
        elif action == 'compress':
            _state['messages'] = _l.compress_context(_state['messages'])
        return
    _l.stats['messages'] += 1
    _state['messages'].append({"role": "user", "content": text})
    _l._run_turn(_state)


# ── WebSocket handler ─────────────────────────────────────────────────────────
async def _ws_handler(request):
    from aiohttp import web, WSMsgType
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _ws_clients.add(ws)
    _send_stats()
    try:
        async for raw in ws:
            if raw.type != WSMsgType.TEXT:
                break
            try:
                msg = json.loads(raw.data)
            except Exception:
                continue
            t = msg.get("type")
            if t == "input":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                if _processing_lock.locked():
                    await ws.send_str(json.dumps({"type": "busy"}))
                    continue
                loop = asyncio.get_event_loop()
                loop.create_task(_handle_input(text))
            elif t == "confirm_response":
                _confirm_result[0] = bool(msg.get("approved"))
                _confirm_event.set()
    finally:
        _ws_clients.discard(ws)
    return ws


async def _handle_input(text: str):
    if not _processing_lock.acquire(blocking=False):
        return
    try:
        _broadcast_sync({"type": "processing", "value": True})
        await asyncio.get_event_loop().run_in_executor(None, _do_turn, text)
    finally:
        _processing_lock.release()
        _broadcast_sync({"type": "processing", "value": False})
        _send_stats()


# ── HTTP ──────────────────────────────────────────────────────────────────────
async def _index(request):
    from aiohttp import web
    return web.Response(text=_HTML, content_type='text/html', charset='utf-8')


# ── Entry point ───────────────────────────────────────────────────────────────
def run(state: dict, host: str = '0.0.0.0', port: int = 8080):
    """Avvia il server web. Blocca finché il processo non esce."""
    global _main_loop, _state
    try:
        from aiohttp import web
    except ImportError:
        sys.stderr.write("ERROR: aiohttp mancante. Esegui: pip install aiohttp\n")
        sys.exit(1)

    _state = state

    import loki as _l
    _l._web_confirm_fn = confirm_web   # hook per confirm_command

    app = web.Application()
    app.router.add_get('/',   _index)
    app.router.add_get('/ws', _ws_handler)

    orig_stdout = sys.stdout
    sys.stdout  = WebOutputProxy(orig_stdout)

    async def _serve():
        global _main_loop
        _main_loop = asyncio.get_event_loop()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        orig_stdout.write(
            f"\n  ◈ Loki Web UI  →  http://localhost:{port}\n"
            f"  Tailscale:        http://<tailscale-ip>:{port}\n\n"
        )
        orig_stdout.flush()
        await asyncio.Event().wait()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout = orig_stdout
        _l._web_confirm_fn = None


# ── HTML ──────────────────────────────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Loki</title>
<style>
:root {
  --bg:        #0b0505;
  --bg2:       #140808;
  --bg3:       #1c0c0c;
  --accent:    #7d1020;
  --accent2:   #b01828;
  --text:      #e2d6d6;
  --text2:     #8a6e6e;
  --border:    #2a1010;
  --tool-bg:   #0e0606;
  --think-fg:  #6e4a4a;
  --red:       #c41020;
  --green:     #5a8040;
  --mono: 'JetBrains Mono','Fira Mono','Cascadia Code',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{
  background:var(--bg);color:var(--text);
  font-family:var(--mono);font-size:14px;
  display:flex;flex-direction:column;
}

/* ── Header ── */
#hdr{
  background:var(--bg2);border-bottom:1px solid var(--border);
  padding:9px 20px;display:flex;align-items:center;gap:12px;flex-shrink:0;
}
.logo{color:var(--accent2);font-weight:700;font-size:17px;letter-spacing:3px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--green);display:inline-block;transition:background .3s}
.dot.off{background:var(--red)}
#status{color:var(--text2);font-size:12px}
.spacer{flex:1}
#busy-label{color:var(--accent2);font-size:12px;display:none}
#busy-label.on{display:block}

/* ── Messages ── */
#msgs{
  flex:1;overflow-y:auto;padding:14px 20px;
  display:flex;flex-direction:column;gap:10px;
}
#msgs::-webkit-scrollbar{width:3px}
#msgs::-webkit-scrollbar-track{background:var(--bg)}
#msgs::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

.msg{display:flex;flex-direction:column;gap:3px;max-width:96%}

/* User */
.msg-user{
  background:var(--bg2);border-left:3px solid var(--accent);
  padding:9px 14px;border-radius:0 4px 4px 0;align-self:flex-start;
}
.role-user{color:var(--accent2);font-size:11px;margin-bottom:3px}

/* Assistant */
.msg-ai{align-self:flex-start}
.role-ai{color:var(--text2);font-size:11px;margin-bottom:3px}
.ai-content{white-space:pre-wrap;line-height:1.65;word-break:break-word}

/* Thinking */
.think-wrap{
  background:var(--bg2);border-left:2px solid var(--border);
  padding:5px 12px;border-radius:0 3px 3px 0;margin-bottom:5px;
}
.think-toggle{
  color:var(--think-fg);font-size:11px;cursor:pointer;user-select:none;
  display:flex;align-items:center;gap:6px;
}
.think-toggle:hover{color:var(--text2)}
.think-body{
  color:var(--think-fg);font-size:12px;margin-top:5px;
  white-space:pre-wrap;line-height:1.5;max-height:300px;
  overflow-y:auto;display:none;
}
.think-body.open{display:block}

/* Tool */
.tool-call{
  background:var(--tool-bg);border:1px solid var(--border);
  border-radius:4px;padding:7px 12px;margin:3px 0;
}
.tc-header{color:var(--text2);font-size:11px;margin-bottom:3px}
.tc-cmd{color:var(--accent2);white-space:pre-wrap;word-break:break-all;font-size:13px}
.tool-out{
  background:var(--tool-bg);border:1px solid var(--border);
  border-radius:4px;padding:7px 12px;margin:2px 0 4px;
  max-height:180px;overflow-y:auto;white-space:pre-wrap;
  font-size:12px;color:var(--text2);word-break:break-all;
}

/* Streaming cursor */
.cursor{
  display:inline-block;width:7px;height:13px;
  background:var(--accent2);animation:blink .75s step-end infinite;
  vertical-align:text-bottom;margin-left:1px;
}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}

/* System note */
.sys-note{color:var(--text2);font-size:12px;padding:2px 0;font-style:italic}

/* ── Status bar ── */
#sb{
  background:var(--bg2);border-top:1px solid var(--border);
  padding:4px 20px;font-size:11px;color:var(--text2);
  display:flex;gap:16px;flex-shrink:0;
}

/* ── Input ── */
#ibar{
  background:var(--bg2);border-top:1px solid var(--border);
  padding:10px 16px;display:flex;gap:10px;align-items:flex-end;flex-shrink:0;
}
#inp{
  flex:1;background:var(--bg3);border:1px solid var(--border);
  border-radius:4px;color:var(--text);font-family:var(--mono);font-size:14px;
  padding:9px 13px;resize:none;min-height:40px;max-height:150px;
  outline:none;line-height:1.4;
}
#inp:focus{border-color:var(--accent)}
#inp::placeholder{color:var(--text2)}
#sbtn{
  background:var(--accent);color:var(--text);border:none;
  border-radius:4px;padding:9px 16px;font-family:var(--mono);font-size:14px;
  cursor:pointer;transition:background .15s;white-space:nowrap;flex-shrink:0;
}
#sbtn:hover{background:var(--accent2)}
#sbtn:disabled{opacity:.35;cursor:default}

/* ── Confirm modal ── */
#overlay{
  display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.72);z-index:100;
  align-items:center;justify-content:center;
}
#overlay.on{display:flex}
#modal{
  background:var(--bg2);border:1px solid var(--accent);
  border-radius:6px;padding:22px 26px;max-width:560px;width:92%;
}
#modal h3{color:var(--accent2);margin-bottom:11px;font-size:13px}
.cmd-box{
  background:var(--tool-bg);border:1px solid var(--border);
  border-radius:4px;padding:9px 13px;font-size:13px;
  color:var(--accent2);white-space:pre-wrap;word-break:break-all;
  margin-bottom:16px;max-height:190px;overflow-y:auto;
}
.mbtn-row{display:flex;gap:10px;justify-content:flex-end}
.mbtn{
  padding:8px 18px;border-radius:4px;border:none;
  font-family:var(--mono);font-size:13px;cursor:pointer;
}
#byes{background:var(--accent);color:var(--text)}
#byes:hover{background:var(--accent2)}
#bno{background:var(--bg3);color:var(--text2);border:1px solid var(--border)}
#bno:hover{color:var(--text)}
</style>
</head>
<body>

<div id="hdr">
  <span class="logo">◈ LOKI</span>
  <span class="dot off" id="dot"></span>
  <span id="status">connessione...</span>
  <span class="spacer"></span>
  <span id="busy-label">⏺ elaborando...</span>
</div>

<div id="msgs"></div>

<div id="sb">
  <span id="sb-model">—</span>
  <span id="sb-msgs">0 msg</span>
  <span id="sb-ctx"></span>
  <span id="sb-up"></span>
</div>

<div id="ibar">
  <textarea id="inp" rows="1"
    placeholder="Messaggio... (Enter invia · Shift+Enter a capo · /help per comandi)"></textarea>
  <button id="sbtn">Invia</button>
</div>

<div id="overlay">
  <div id="modal">
    <h3>⏺ Conferma esecuzione comando</h3>
    <div class="cmd-box" id="modal-cmd"></div>
    <div class="mbtn-row">
      <button class="mbtn" id="bno">✕ No</button>
      <button class="mbtn" id="byes">✓ Esegui</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const msgs    = $('msgs');
const inp     = $('inp');
const sbtn    = $('sbtn');
const dot     = $('dot');
const statusEl= $('status');
const overlay = $('overlay');
const busyLbl = $('busy-label');

let ws, reconnTimer;
let processing = false;

// ── WebSocket ────────────────────────────────────────────────────────────────
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    dot.className = 'dot';
    statusEl.textContent = 'connesso';
    clearTimeout(reconnTimer);
  };
  ws.onclose = () => {
    dot.className = 'dot off';
    statusEl.textContent = 'disconnesso — riconnetto...';
    reconnTimer = setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = e => handle(JSON.parse(e.data));
}

function send(data) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(data));
}

// ── Current streaming AI message ─────────────────────────────────────────────
let _aiDiv = null;        // current .msg-ai
let _contentEl = null;    // .ai-content inside _aiDiv
let _cursorEl = null;
let _buf = '';            // accumulated streamed text

function resetStream() {
  _aiDiv = null; _contentEl = null; _cursorEl = null; _buf = '';
}

function ensureAiDiv() {
  if (_aiDiv) return;
  _aiDiv = document.createElement('div');
  _aiDiv.className = 'msg msg-ai';
  const role = document.createElement('div');
  role.className = 'role-ai';
  role.textContent = '● loki';
  _aiDiv.appendChild(role);
  _contentEl = document.createElement('div');
  _contentEl.className = 'ai-content';
  _aiDiv.appendChild(_contentEl);
  msgs.appendChild(_aiDiv);
}

function appendStream(text) {
  ensureAiDiv();
  _buf += text;
  if (_cursorEl) _cursorEl.remove();
  _contentEl.textContent = _buf;
  if (!_cursorEl) {
    _cursorEl = document.createElement('span');
    _cursorEl.className = 'cursor';
  }
  _contentEl.appendChild(_cursorEl);
  scrollBottom();
}

function endStream() {
  if (_cursorEl) { _cursorEl.remove(); _cursorEl = null; }
  resetStream();
}

// ── Message types ─────────────────────────────────────────────────────────────
function handle(msg) {
  switch (msg.type) {
    case 'out':
      appendStream(msg.text);
      break;

    case 'processing':
      processing = msg.value;
      sbtn.disabled = processing;
      sbtn.textContent = processing ? '...' : 'Invia';
      busyLbl.className = processing ? 'on' : '';
      if (!processing) endStream();
      break;

    case 'confirm':
      showConfirm(msg.command);
      break;

    case 'clear':
      msgs.innerHTML = '';
      resetStream();
      appendSys('Conversazione cancellata.');
      break;

    case 'busy':
      appendSys('⚠ Loki sta già elaborando — attendi.');
      break;

    case 'server_exit':
      appendSys('Loki si è spento.');
      break;

    case 'stats':
      $('sb-model').textContent = msg.model || '—';
      $('sb-msgs').textContent  = (msg.messages || 0) + ' msg';
      $('sb-ctx').textContent   = msg.ctx_pct ? 'ctx ' + msg.ctx_pct + '%' : '';
      if (msg.uptime !== undefined) {
        const m = Math.floor(msg.uptime / 60), s = msg.uptime % 60;
        $('sb-up').textContent = `${m}m ${s}s`;
      }
      break;
  }
}

// ── User message ──────────────────────────────────────────────────────────────
function addUser(text) {
  const div = document.createElement('div');
  div.className = 'msg msg-user';
  const role = document.createElement('div');
  role.className = 'role-user';
  role.textContent = '❯ tu';
  div.appendChild(role);
  const c = document.createElement('div');
  c.textContent = text;
  div.appendChild(c);
  msgs.appendChild(div);
}

function appendSys(text) {
  const d = document.createElement('div');
  d.className = 'sys-note';
  d.textContent = text;
  msgs.appendChild(d);
  scrollBottom();
}

function scrollBottom() {
  msgs.scrollTop = msgs.scrollHeight;
}

// ── Confirm modal ─────────────────────────────────────────────────────────────
function showConfirm(cmd) {
  $('modal-cmd').textContent = cmd;
  overlay.className = 'on';
  $('byes').focus();
}
function hideConfirm() { overlay.className = ''; }

$('byes').onclick = () => { hideConfirm(); send({type:'confirm_response',approved:true}); };
$('bno').onclick  = () => { hideConfirm(); send({type:'confirm_response',approved:false}); };
overlay.addEventListener('click', e => { if (e.target === overlay) { hideConfirm(); send({type:'confirm_response',approved:false}); } });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && overlay.className === 'on') { hideConfirm(); send({type:'confirm_response',approved:false}); }});

// ── Input ─────────────────────────────────────────────────────────────────────
function submit() {
  const text = inp.value.trim();
  if (!text || processing) return;
  inp.value = '';
  inp.style.height = 'auto';
  addUser(text);
  scrollBottom();
  send({type: 'input', text});
}

inp.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
});
inp.addEventListener('input', () => {
  inp.style.height = 'auto';
  inp.style.height = Math.min(inp.scrollHeight, 150) + 'px';
});
sbtn.onclick = submit;

connect();
inp.focus();
</script>
</body>
</html>"""
