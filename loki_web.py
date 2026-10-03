"""Loki Web Server v2
Multi-client aiohttp + WebSocket, history replay, mobile-first UI.
Start: LOKI_UI=web [LOKI_PORT=8080] python loki.py
"""
import asyncio, json, re, sys, threading, time
from datetime import datetime

# ── ANSI stripping ──────────────────────────────────────────────────────────────
_ANSI = re.compile(r'\033(?:\[[0-9;]*[mKHJA-Za-z]|\][^\x07]*\x07|.)')
def _strip(s: str) -> str:
    return _ANSI.sub('', s)

# ── Globals ─────────────────────────────────────────────────────────────────────
_ws_clients: set  = set()
_main_loop        = None
_history: list    = []   # [{type,text|data,ts}, …]  — user + ai_turn + sys only
_pending: list    = []   # accumulates AI output chunks during a turn
_state: dict      = {}
_lock             = threading.Lock()

# ── Confirm bridge (sync executor → async WS) ───────────────────────────────────
_confirm_ev = threading.Event()
_confirm_ok = [False]

def confirm_web(command: str) -> bool:
    _confirm_ev.clear()
    _confirm_ok[0] = False
    _broadcast_sync({"type": "confirm", "command": command})
    _confirm_ev.wait(timeout=300)
    return _confirm_ok[0]

# ── Broadcast helpers ───────────────────────────────────────────────────────────
def _broadcast_sync(msg: dict):
    """Call from any thread."""
    if _main_loop and _ws_clients:
        asyncio.run_coroutine_threadsafe(_broadcast(msg), _main_loop)

async def _broadcast(msg: dict, skip=None):
    data = json.dumps(msg, ensure_ascii=False)
    dead = set()
    for ws in list(_ws_clients):
        if ws is skip:
            continue
        try:
            await ws.send_str(data)
        except Exception:
            dead.add(ws)
    _ws_clients.difference_update(dead)

# ── Output proxy ────────────────────────────────────────────────────────────────
class WebOutputProxy:
    def __init__(self, orig):
        self._orig = orig

    def write(self, text):
        if isinstance(text, bytes):
            text = text.decode('utf-8', errors='replace')
        clean = _strip(text)
        if '\r' in clean:
            clean = clean.split('\r')[-1]
        if clean.strip('\n'):
            if _lock.locked():
                _pending.append(clean)
            _broadcast_sync({"type": "out", "text": clean})
        return len(text)

    def flush(self): pass
    def isatty(self): return False
    def writable(self): return True
    def fileno(self):
        try:   return self._orig.fileno()
        except: return -1

# ── Project helpers ──────────────────────────────────────────────────────────────
def _project_payload():
    try:
        import loki as _l
        import loki_projects
        p = _l._active_project
        all_p = [{"name": x['name'], "description": x.get('description',''),
                  "mem_count": len(x.get('memory',{}))}
                 for x in loki_projects.list_projects()]
        return {
            "type":        "project_state",
            "active":      p['name'] if p else None,
            "description": p.get('description','') if p else '',
            "memory":      p.get('memory',{}) if p else {},
            "projects":    all_p,
        }
    except Exception:
        return {"type": "project_state", "active": None, "memory": {}, "projects": []}

def _send_project_state():
    _broadcast_sync(_project_payload())

def _handle_project_action(msg: dict):
    """Called from WS handler for project_action messages (sync, runs in executor)."""
    import loki as _l
    import loki_projects
    action = msg.get('action','')
    name   = (msg.get('name') or '').strip()
    key    = (msg.get('key') or '').strip()
    value  = (msg.get('value') or '').strip()

    if action == 'list':
        pass  # just send state

    elif action == 'create':
        if name:
            existing = loki_projects.load(name)
            _l._active_project = existing or loki_projects.create(name, msg.get('description',''))
            if _state.get('messages') and _state['messages'][0]['role'] == 'system':
                _state['messages'][0]['content'] = _l.build_system_prompt()

    elif action == 'switch':
        if name:
            p = loki_projects.load(name)
            if p:
                _l._active_project = p
                if _state.get('messages') and _state['messages'][0]['role'] == 'system':
                    _state['messages'][0]['content'] = _l.build_system_prompt()

    elif action == 'close':
        _l._active_project = None
        if _state.get('messages') and _state['messages'][0]['role'] == 'system':
            _state['messages'][0]['content'] = _l.build_system_prompt()

    elif action == 'set_mem':
        if key and _l._active_project:
            _l._active_project = loki_projects.set_mem(_l._active_project['name'], key, value)
            if _state.get('messages') and _state['messages'][0]['role'] == 'system':
                _state['messages'][0]['content'] = _l.build_system_prompt()

    elif action == 'del_mem':
        if key and _l._active_project:
            loki_projects.del_mem(_l._active_project['name'], key)
            _l._active_project = loki_projects.load(_l._active_project['name']) or _l._active_project
            if _state.get('messages') and _state['messages'][0]['role'] == 'system':
                _state['messages'][0]['content'] = _l.build_system_prompt()

    elif action == 'delete':
        if name:
            loki_projects.delete(name)
            if _l._active_project and _l._active_project['name'] == name:
                _l._active_project = None
                if _state.get('messages') and _state['messages'][0]['role'] == 'system':
                    _state['messages'][0]['content'] = _l.build_system_prompt()

    _send_project_state()


# ── Stats ────────────────────────────────────────────────────────────────────────
def _send_stats():
    try:
        import loki as _l
        cu = _l.stats.get('ctx_used', 0) or 0
        wc = _l.stats.get('working_ctx', 1) or 1
        _broadcast_sync({
            "type":    "stats",
            "model":   _l.MODEL.split('/')[-1][:36],
            "msgs":    _l.stats.get('messages', 0),
            "ctx_pct": int(100 * cu / wc) if cu else 0,
            "uptime":  int((datetime.now() - _l.stats['start_time']).total_seconds()),
        })
    except Exception:
        pass

# ── Turn execution ───────────────────────────────────────────────────────────────
def _do_turn(text: str):
    import loki as _l
    if text.startswith('/'):
        action, payload = _l.parse_slash(text, _state['messages'])
        if action == 'exit':
            _history.clear()
            _broadcast_sync({"type": "server_exit"})
        elif action == 'clear':
            _state['messages'] = _state['messages'][:1]
            _history.clear()
            _broadcast_sync({"type": "clear"})
        elif action in ('resume', 'trim'):
            _state['messages'] = payload
        elif action == 'compress':
            _state['messages'] = _l.compress_context(_state['messages'])
        _send_project_state()
        return
    _l.stats['messages'] += 1
    _state['messages'].append({"role": "user", "content": text})
    _l._run_turn(_state)

async def _handle_input(text: str, sender=None):
    if not _lock.acquire(blocking=False):
        if sender:
            try:
                await sender.send_str(json.dumps({"type": "busy"}))
            except Exception:
                pass
        return
    _pending.clear()
    try:
        await _broadcast({"type": "processing", "value": True})
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _do_turn, text)
    finally:
        _lock.release()
        if _pending:
            _history.append({
                "type": "ai_turn",
                "text": "".join(_pending),
                "ts":   time.time(),
            })
        _pending.clear()
        await _broadcast({"type": "processing", "value": False})
        _send_stats()
        _send_project_state()

# ── WebSocket handler ────────────────────────────────────────────────────────────
async def _ws_handler(request):
    from aiohttp import web, WSMsgType
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    _ws_clients.add(ws)

    if _history:
        await ws.send_str(json.dumps({"type": "history", "events": _history}))
    _send_stats()
    _send_project_state()

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
                ev = {"type": "user", "text": text, "ts": time.time()}
                _history.append(ev)
                loop = asyncio.get_event_loop()
                # Broadcast user event to other clients only (sender shows it locally)
                loop.create_task(_broadcast(ev, skip=ws))
                loop.create_task(_handle_input(text, sender=ws))
            elif t == "project_action":
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, _handle_project_action, msg)
            elif t == "confirm_response":
                _confirm_ok[0] = bool(msg.get("approved"))
                _confirm_ev.set()
    finally:
        _ws_clients.discard(ws)
    return ws

# ── HTTP ─────────────────────────────────────────────────────────────────────────
async def _index(request):
    from aiohttp import web
    return web.Response(text=_HTML, content_type='text/html', charset='utf-8')

# ── Entry point ──────────────────────────────────────────────────────────────────
def run(state: dict, host: str = '0.0.0.0', port: int = 8080):
    global _main_loop, _state
    try:
        from aiohttp import web
    except ImportError:
        sys.stderr.write("ERROR: pip install aiohttp\n")
        sys.exit(1)

    _state = state

    import loki as _l
    _l._web_confirm_fn = confirm_web

    orig = sys.stdout
    sys.stdout = WebOutputProxy(orig)

    async def _serve():
        global _main_loop
        _main_loop = asyncio.get_event_loop()
        app = web.Application()
        app.router.add_get('/',   _index)
        app.router.add_get('/ws', _ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        orig.write(f"\n  ◈ Loki Web  →  http://localhost:{port}\n\n")
        orig.flush()
        await asyncio.Event().wait()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout = orig
        _l._web_confirm_fn = None


# ── HTML ──────────────────────────────────────────────────────────────────────────
_HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0505">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<title>Loki</title>
<style>
/* ── Reset ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}

/* ── Tokens ── */
:root{
  --bg:      #0b0505;
  --bg2:     #120606;
  --bg3:     #1b0b0b;
  --bg4:     #231010;
  --accent:  #7d1020;
  --accent2: #b01828;
  --text:    #e0d4d4;
  --text2:   #8a6e6e;
  --dim:     #4a3030;
  --border:  #281010;
  --tool-bg: #0d0505;
  --green:   #4d7a3a;
  --red:     #b02020;
  --yellow:  #8a5c1a;
  --mono:    'JetBrains Mono','Cascadia Code','Fira Mono',ui-monospace,monospace;
  --r:       5px;
}

/* ── Layout ── */
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:var(--mono);font-size:14px;line-height:1.5}
#app{display:flex;flex-direction:column;height:100dvh}

/* ── Header ── */
#hdr{
  background:var(--bg2);border-bottom:1px solid var(--border);
  padding:0 16px;height:46px;
  display:flex;align-items:center;gap:10px;flex-shrink:0;
}
.logo{color:var(--accent2);font-weight:700;font-size:15px;letter-spacing:4px;flex-shrink:0}
#conn-dot{width:7px;height:7px;border-radius:50%;background:var(--red);flex-shrink:0;transition:background .3s}
#conn-dot.ok{background:var(--green)}
#conn-txt{color:var(--text2);font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#busy{display:flex;align-items:center;gap:6px;visibility:hidden}
#busy.on{visibility:visible}
.spin{width:10px;height:10px;border-radius:50%;border:2px solid var(--border);border-top-color:var(--accent2);animation:spin .7s linear infinite;flex-shrink:0}
@keyframes spin{to{transform:rotate(360deg)}}
#busy-txt{color:var(--accent2);font-size:11px}

/* ── Messages ── */
#msgs{
  flex:1;overflow-y:auto;
  padding:16px 16px 8px;
  display:flex;flex-direction:column;gap:16px;
}
#msgs::-webkit-scrollbar{width:3px}
#msgs::-webkit-scrollbar-track{background:transparent}
#msgs::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

/* User bubble */
.m-user{display:flex;flex-direction:column;align-items:flex-end;align-self:flex-end;max-width:min(82%,640px)}
.m-user .role{color:var(--text2);font-size:10px;margin-bottom:3px;padding-right:2px}
.m-user .bubble{
  background:var(--bg3);border:1px solid var(--accent);
  border-radius:var(--r) var(--r) 2px var(--r);
  padding:10px 14px;white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.6;
}

/* AI bubble */
.m-ai{display:flex;flex-direction:column;align-self:flex-start;max-width:min(96%,820px);width:100%}
.m-ai .role{color:var(--dim);font-size:10px;margin-bottom:4px;padding-left:1px}
.m-ai .body{white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.75;color:var(--text)}

/* Line-level colouring (AI output) */
.lt{color:var(--accent2)}   /* tool call  ⏺ ◆ */
.lo{color:var(--green)}     /* ok         ✓ ✔  */
.le{color:var(--red)}       /* error      ✗ ✘  */
.lw{color:var(--yellow)}    /* warn       ⚠    */
.ld{color:var(--text2)}     /* dim        │ ╭  */

/* Streaming cursor */
.cur{display:inline-block;width:6px;height:13px;background:var(--accent2);vertical-align:text-bottom;margin-left:1px;animation:blink .8s step-end infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}

/* System note */
.m-sys{align-self:center;color:var(--dim);font-size:11px;padding:3px 12px;border-radius:12px;background:var(--bg2);border:1px solid var(--border);font-style:italic;text-align:center}

/* ── Status bar ── */
#sb{
  background:var(--bg2);border-top:1px solid var(--border);
  height:26px;padding:0 16px;
  display:flex;align-items:center;gap:0;
  font-size:10px;color:var(--text2);flex-shrink:0;overflow:hidden;
}
.si{padding:0 10px;border-right:1px solid var(--border);white-space:nowrap;line-height:26px;height:100%}
.si:first-child{padding-left:0}
.si:last-child{border-right:none}
#ctx-bar{height:2px;background:var(--accent);border-radius:1px;transition:width .5s ease;align-self:center}

/* ── Input bar ── */
#ibar{
  background:var(--bg2);border-top:1px solid var(--border);
  padding:10px 12px;display:flex;align-items:flex-end;gap:8px;flex-shrink:0;
  padding-bottom:max(10px,env(safe-area-inset-bottom));
}
#inp{
  flex:1;background:var(--bg3);border:1px solid var(--border);
  border-radius:var(--r);color:var(--text);font-family:var(--mono);font-size:14px;
  padding:10px 12px;resize:none;min-height:42px;max-height:160px;
  outline:none;line-height:1.5;transition:border-color .15s;
}
#inp:focus{border-color:var(--accent)}
#inp::placeholder{color:var(--dim)}
#sbtn{
  flex-shrink:0;background:var(--accent);color:var(--text);
  border:none;border-radius:var(--r);
  min-width:46px;height:42px;
  font-family:var(--mono);font-size:18px;cursor:pointer;
  transition:background .15s,opacity .15s;
  display:flex;align-items:center;justify-content:center;
}
#sbtn:hover:not(:disabled){background:var(--accent2)}
#sbtn:disabled{opacity:.35;cursor:default}

/* ── Scroll-to-bottom button ── */
#scrollbtn{
  position:fixed;right:16px;bottom:80px;
  background:var(--bg4);border:1px solid var(--border);
  color:var(--text2);border-radius:50%;width:34px;height:34px;
  display:flex;align-items:center;justify-content:center;
  cursor:pointer;font-size:14px;opacity:0;pointer-events:none;
  transition:opacity .2s;z-index:50;
}
#scrollbtn.on{opacity:1;pointer-events:auto}
#scrollbtn:hover{background:var(--bg3);color:var(--text)}

/* ── Confirm modal ── */
#overlay{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.82);
  z-index:100;align-items:center;justify-content:center;padding:16px;
}
#overlay.on{display:flex}
#modal{
  background:var(--bg2);border:1px solid var(--accent);
  border-radius:var(--r);padding:22px;width:100%;max-width:520px;
}
#modal-hdr{display:flex;align-items:center;gap:8px;margin-bottom:14px}
.mpulse{width:8px;height:8px;border-radius:50%;background:var(--accent2);animation:pulse 1.4s ease-in-out infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
#modal-hdr span{color:var(--accent2);font-size:12px}
.cmd-box{
  background:var(--tool-bg);border:1px solid var(--border);border-radius:4px;
  padding:10px 13px;font-size:13px;color:var(--accent2);
  white-space:pre-wrap;word-break:break-all;max-height:200px;overflow-y:auto;
  margin-bottom:18px;line-height:1.5;
}
.mbtns{display:flex;gap:10px;justify-content:flex-end}
.mbtn{
  padding:9px 22px;border-radius:4px;border:none;
  font-family:var(--mono);font-size:13px;cursor:pointer;min-height:40px;
}
#byes{background:var(--accent);color:var(--text)}
#byes:hover{background:var(--accent2)}
#bno{background:var(--bg3);color:var(--text2);border:1px solid var(--border)}
#bno:hover{color:var(--text)}

/* ── Projects panel ── */
#proj-btn{
  flex-shrink:0;background:none;border:1px solid var(--border);
  border-radius:4px;color:var(--text2);font-family:var(--mono);font-size:11px;
  padding:4px 9px;cursor:pointer;display:flex;align-items:center;gap:5px;
  white-space:nowrap;transition:border-color .15s,color .15s;
}
#proj-btn:hover{border-color:var(--accent);color:var(--text)}
#proj-btn.active{border-color:var(--accent2);color:var(--accent2)}
#proj-dot{width:6px;height:6px;border-radius:50%;background:var(--dim);flex-shrink:0;transition:background .3s}
#proj-dot.on{background:var(--accent2)}

#proj-panel{
  position:fixed;top:0;right:0;bottom:0;
  width:min(360px,100vw);
  background:var(--bg2);border-left:1px solid var(--border);
  display:flex;flex-direction:column;
  transform:translateX(100%);transition:transform .25s ease;
  z-index:90;
}
#proj-panel.open{transform:translateX(0)}

#pp-hdr{
  background:var(--bg3);border-bottom:1px solid var(--border);
  padding:12px 16px;display:flex;align-items:center;gap:8px;flex-shrink:0;
}
#pp-hdr-title{color:var(--accent2);font-size:13px;font-weight:700;letter-spacing:1px;flex:1}
#pp-close{
  background:none;border:none;color:var(--text2);cursor:pointer;
  font-size:18px;padding:2px 6px;border-radius:3px;
}
#pp-close:hover{color:var(--text)}

#pp-body{flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:16px}
#pp-body::-webkit-scrollbar{width:2px}
#pp-body::-webkit-scrollbar-thumb{background:var(--border)}

.pp-section-title{color:var(--text2);font-size:10px;letter-spacing:1px;margin-bottom:6px;text-transform:uppercase}

/* Active project card */
#pp-active{background:var(--bg3);border:1px solid var(--accent);border-radius:var(--r);padding:12px}
#pp-active-name{color:var(--accent2);font-size:14px;font-weight:700;margin-bottom:3px}
#pp-active-desc{color:var(--text2);font-size:11px;margin-bottom:10px;min-height:14px}
#pp-no-project{color:var(--dim);font-size:12px;font-style:italic}

/* Memory table */
#pp-mem-list{display:flex;flex-direction:column;gap:4px}
.mem-row{
  background:var(--tool-bg);border:1px solid var(--border);border-radius:3px;
  padding:6px 10px;display:flex;align-items:flex-start;gap:8px;
}
.mem-key{color:var(--accent2);font-size:12px;flex-shrink:0;min-width:100px;word-break:break-word}
.mem-val{color:var(--text);font-size:12px;flex:1;word-break:break-word}
.mem-del{
  background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;
  padding:0 2px;flex-shrink:0;transition:color .15s;
}
.mem-del:hover{color:var(--red)}
.mem-empty{color:var(--dim);font-size:11px;font-style:italic}

/* Add memory form */
#pp-add-form{display:flex;flex-direction:column;gap:6px}
.pp-input{
  background:var(--bg3);border:1px solid var(--border);border-radius:3px;
  color:var(--text);font-family:var(--mono);font-size:12px;
  padding:7px 10px;outline:none;width:100%;
  transition:border-color .15s;
}
.pp-input:focus{border-color:var(--accent)}
.pp-input::placeholder{color:var(--dim)}
#pp-add-btn{
  background:var(--accent);color:var(--text);border:none;border-radius:3px;
  font-family:var(--mono);font-size:12px;padding:7px 14px;cursor:pointer;align-self:flex-end;
}
#pp-add-btn:hover{background:var(--accent2)}

/* Projects list */
#pp-proj-list{display:flex;flex-direction:column;gap:4px}
.proj-row{
  background:var(--tool-bg);border:1px solid var(--border);border-radius:3px;
  padding:8px 10px;display:flex;align-items:center;gap:8px;cursor:pointer;
  transition:border-color .15s;
}
.proj-row:hover{border-color:var(--accent)}
.proj-row.current{border-color:var(--accent2)}
.proj-row-name{color:var(--text);font-size:12px;font-weight:600;flex:1}
.proj-row-desc{color:var(--text2);font-size:10px}
.proj-row-mem{color:var(--dim);font-size:10px;flex-shrink:0}
.proj-del-btn{
  background:none;border:none;color:var(--dim);cursor:pointer;font-size:13px;padding:0 3px;
}
.proj-del-btn:hover{color:var(--red)}

/* New project form */
#pp-new-form{display:flex;flex-direction:column;gap:6px}

/* ── Mobile tweaks ── */
@media(max-width:480px){
  .m-user,.m-ai{max-width:100%}
  .mbtn{flex:1;padding:12px}
  #sb .si:nth-child(n+4){display:none}
}
</style>
</head>
<body>
<div id="app">

  <div id="hdr">
    <span class="logo">◈ LOKI</span>
    <span id="conn-dot"></span>
    <span id="conn-txt">connessione...</span>
    <div id="busy"><div class="spin"></div><span id="busy-txt">elaborando</span></div>
    <button id="proj-btn" title="Progetti" onclick="togglePanel()">
      <span id="proj-dot"></span>
      <span id="proj-label">progetti</span>
    </button>
  </div>

  <div id="msgs"></div>

  <div id="sb">
    <div class="si" id="sb-model">—</div>
    <div class="si" id="sb-msgs">0 msg</div>
    <div class="si" id="sb-ctx">ctx —</div>
    <div id="ctx-bar" style="width:0;margin:0 8px"></div>
    <div class="si" id="sb-up">0s</div>
  </div>

  <div id="ibar">
    <textarea id="inp" rows="1"
      placeholder="Messaggio… (Enter invia · Shift+Enter a capo · /help)"></textarea>
    <button id="sbtn" title="Invia (Enter)">↑</button>
  </div>

</div>

<div id="scrollbtn" title="Scorri in basso">↓</div>

<!-- Projects panel -->
<div id="proj-panel">
  <div id="pp-hdr">
    <span id="pp-hdr-title">◈ PROGETTI</span>
    <button id="pp-close" onclick="togglePanel()">✕</button>
  </div>
  <div id="pp-body">

    <!-- Active project -->
    <div>
      <div class="pp-section-title">Progetto attivo</div>
      <div id="pp-active">
        <div id="pp-no-project">Nessun progetto attivo</div>
        <div id="pp-active-name" style="display:none"></div>
        <div id="pp-active-desc"></div>
        <!-- Memory -->
        <div id="pp-mem-section" style="display:none">
          <div class="pp-section-title" style="margin-top:10px">Memoria</div>
          <div id="pp-mem-list"></div>
          <!-- Add key-value -->
          <div id="pp-add-form" style="margin-top:8px">
            <input id="pp-key" class="pp-input" placeholder="chiave" autocomplete="off">
            <input id="pp-val" class="pp-input" placeholder="valore" autocomplete="off">
            <button id="pp-add-btn" onclick="addMem()">+ Salva</button>
          </div>
        </div>
        <!-- Close project -->
        <button id="pp-close-proj" onclick="wsSend({type:'project_action',action:'close'})"
          style="display:none;margin-top:10px;background:none;border:1px solid var(--border);
                 color:var(--text2);font-family:var(--mono);font-size:11px;padding:5px 10px;
                 border-radius:3px;cursor:pointer;width:100%">
          Chiudi progetto
        </button>
      </div>
    </div>

    <!-- All projects -->
    <div>
      <div class="pp-section-title">Tutti i progetti</div>
      <div id="pp-proj-list"></div>
      <!-- New project form -->
      <div id="pp-new-form" style="margin-top:8px">
        <input id="pp-new-name" class="pp-input" placeholder="nome nuovo progetto" autocomplete="off">
        <input id="pp-new-desc" class="pp-input" placeholder="descrizione (opzionale)" autocomplete="off">
        <button id="pp-add-btn" onclick="createProject()"
          style="background:var(--bg3);border:1px solid var(--border);color:var(--text2);
                 font-family:var(--mono);font-size:12px;padding:7px 14px;border-radius:3px;
                 cursor:pointer;align-self:flex-end">
          + Crea progetto
        </button>
      </div>
    </div>

  </div>
</div>

<div id="overlay">
  <div id="modal">
    <div id="modal-hdr"><div class="mpulse"></div><span>Conferma esecuzione comando</span></div>
    <div class="cmd-box" id="modal-cmd"></div>
    <div class="mbtns">
      <button class="mbtn" id="bno">✕ No</button>
      <button class="mbtn" id="byes">✓ Esegui</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id)
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
const msgs = $('msgs'), inp = $('inp'), sbtn = $('sbtn'), overlay = $('overlay')

/* ── Line classifier ── */
function paintLine(line) {
  const e = esc(line)
  if (/^\s*[⏺◆▸►]/.test(line))                    return `<span class="lt">${e}</span>`
  if (/^\s*[✓✔]/.test(line))                         return `<span class="lo">${e}</span>`
  if (/^\s*[✗✘✕]/.test(line))                        return `<span class="le">${e}</span>`
  if (/^\s*⚠/.test(line))                             return `<span class="lw">${e}</span>`
  if (/^\s{3,}[│╭╰├─╴]/.test(line))                  return `<span class="ld">${e}</span>`
  return e
}
function renderText(text) {
  return text.split('\n').map(paintLine).join('\n')
}

/* ── Scroll management ── */
let pinned = true
msgs.addEventListener('scroll', () => {
  pinned = msgs.scrollTop + msgs.clientHeight >= msgs.scrollHeight - 80
  $('scrollbtn').className = pinned ? '' : 'on'
})
function scrollBot(force=false) {
  if (pinned || force) {
    msgs.scrollTop = msgs.scrollHeight
    pinned = true
    $('scrollbtn').className = ''
  }
}
$('scrollbtn').onclick = () => { msgs.scrollTop = msgs.scrollHeight; pinned = true; $('scrollbtn').className = '' }

/* ── Message builders ── */
function mkUser(text) {
  const d = document.createElement('div')
  d.className = 'm-user'
  d.innerHTML = `<div class="role">❯ tu</div><div class="bubble">${esc(text)}</div>`
  msgs.appendChild(d)
  scrollBot(true)
}
function mkSys(text) {
  const d = document.createElement('div')
  d.className = 'm-sys'
  d.textContent = text
  msgs.appendChild(d)
  scrollBot()
}

/* ── AI streaming ── */
let _aDiv=null, _aBody=null, _aCur=null, _aBuf=''

function ensureAi() {
  if (_aDiv) return
  _aDiv = document.createElement('div')
  _aDiv.className = 'm-ai'
  _aBody = document.createElement('div')
  _aBody.className = 'body'
  const role = document.createElement('div')
  role.className = 'role'
  role.textContent = '● loki'
  _aDiv.appendChild(role)
  _aDiv.appendChild(_aBody)
  msgs.appendChild(_aDiv)
}
function appendChunk(text) {
  ensureAi()
  _aBuf += text
  if (_aCur) _aCur.remove()
  _aBody.innerHTML = renderText(_aBuf)
  if (!_aCur) { _aCur = document.createElement('span'); _aCur.className = 'cur' }
  _aBody.appendChild(_aCur)
  scrollBot()
}
function endAi() {
  if (_aCur) { _aCur.remove(); _aCur=null }
  _aDiv=null; _aBody=null; _aBuf=''
}

/* ── History replay ── */
function replay(events) {
  msgs.innerHTML = ''
  endAi()
  for (const ev of events) {
    if (ev.type === 'user') {
      mkUser(ev.text)
    } else if (ev.type === 'ai_turn') {
      const d = document.createElement('div')
      d.className = 'm-ai'
      d.innerHTML = `<div class="role">● loki</div><div class="body">${renderText(ev.text)}</div>`
      msgs.appendChild(d)
    } else if (ev.type === 'sys') {
      mkSys(ev.text)
    }
  }
  scrollBot(true)
}

/* ── Stats & uptime ── */
let _upSec=0, _upTimer=null
function startUptime(base) {
  _upSec = base
  clearInterval(_upTimer)
  _upTimer = setInterval(() => {
    _upSec++
    const m=Math.floor(_upSec/60), s=_upSec%60
    $('sb-up').textContent = m ? `${m}m ${s}s` : `${s}s`
  }, 1000)
}
function applyStats(m) {
  $('sb-model').textContent = m.model || '—'
  $('sb-msgs').textContent  = (m.msgs||0)+' msg'
  const p = m.ctx_pct || 0
  $('sb-ctx').textContent   = `ctx ${p}%`
  $('ctx-bar').style.width  = Math.min(p,100)*0.6+'px'
  if (m.uptime !== undefined) startUptime(m.uptime)
}

/* ── WS message handler ── */
let processing = false

function handle(msg) {
  switch (msg.type) {
    case 'history':
      replay(msg.events || [])
      break
    case 'user':
      // Another device sent a message — show it here too
      mkUser(msg.text)
      break
    case 'out':
      appendChunk(msg.text)
      break
    case 'processing':
      processing = msg.value
      sbtn.disabled = processing
      $('busy').className = processing ? 'on' : ''
      if (!processing) endAi()
      break
    case 'confirm':
      showConfirm(msg.command)
      break
    case 'clear':
      msgs.innerHTML = ''
      endAi()
      mkSys('Conversazione cancellata.')
      break
    case 'busy':
      mkSys('⚠ Loki sta già elaborando — attendi.')
      break
    case 'server_exit':
      mkSys('Loki si è spento.')
      break
    case 'stats':
      applyStats(msg)
      break
    case 'project_state':
      applyProjectState(msg)
      break
  }
}

/* ── WebSocket ── */
let ws=null, _reconn=null

function connect() {
  clearTimeout(_reconn)
  const proto = location.protocol==='https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/ws`)

  ws.onopen = () => {
    $('conn-dot').className = 'ok'
    $('conn-txt').textContent = 'connesso'
  }
  ws.onclose = () => {
    $('conn-dot').className = ''
    $('conn-txt').textContent = 'disconnesso — riconnetto...'
    ws = null
    _reconn = setTimeout(connect, 2000)
  }
  ws.onerror = () => ws && ws.close()
  ws.onmessage = e => { try { handle(JSON.parse(e.data)) } catch(ex){ console.error(ex) } }
}

function wsSend(data) {
  if (ws && ws.readyState===1) ws.send(JSON.stringify(data))
}

/* ── Confirm modal ── */
function showConfirm(cmd) {
  $('modal-cmd').textContent = cmd
  overlay.className = 'on'
  $('byes').focus()
}
function hideConfirm() { overlay.className = '' }

$('byes').onclick = () => { hideConfirm(); wsSend({type:'confirm_response',approved:true}) }
$('bno').onclick  = () => { hideConfirm(); wsSend({type:'confirm_response',approved:false}) }
overlay.addEventListener('click', e => {
  if (e.target===overlay) { hideConfirm(); wsSend({type:'confirm_response',approved:false}) }
})
document.addEventListener('keydown', e => {
  if (e.key==='Escape' && overlay.className==='on') {
    hideConfirm(); wsSend({type:'confirm_response',approved:false})
  }
})

/* ── Input ── */
function submit() {
  const text = inp.value.trim()
  if (!text || processing) return
  inp.value = ''
  inp.style.height = 'auto'
  mkUser(text)
  wsSend({type:'input', text})
}

inp.addEventListener('keydown', e => {
  if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); submit() }
})
inp.addEventListener('input', () => {
  inp.style.height = 'auto'
  inp.style.height = Math.min(inp.scrollHeight,160)+'px'
})
sbtn.onclick = submit

/* ── Projects panel ── */
let _projState = {active: null, memory: {}, projects: []}

function togglePanel() {
  const p = $('proj-panel')
  p.classList.toggle('open')
  $('proj-btn').classList.toggle('active', p.classList.contains('open'))
}

function applyProjectState(s) {
  _projState = s

  // Header button
  const dot = $('proj-dot'), lbl = $('proj-label')
  if (s.active) {
    dot.className = 'on'
    lbl.textContent = s.active.length > 14 ? s.active.slice(0,13)+'…' : s.active
  } else {
    dot.className = ''
    lbl.textContent = 'progetti'
  }

  // Active card
  const noProj = $('pp-no-project')
  const nameEl = $('pp-active-name')
  const descEl = $('pp-active-desc')
  const memSec = $('pp-mem-section')
  const closeBtn = $('pp-close-proj')

  if (s.active) {
    noProj.style.display = 'none'
    nameEl.style.display = 'block'
    nameEl.textContent = s.active
    descEl.textContent = s.description || ''
    memSec.style.display = 'block'
    closeBtn.style.display = 'block'
    renderMemory(s.memory || {})
  } else {
    noProj.style.display = 'block'
    nameEl.style.display = 'none'
    descEl.textContent = ''
    memSec.style.display = 'none'
    closeBtn.style.display = 'none'
  }

  // Projects list
  renderProjectsList(s.projects || [])
}

function renderMemory(mem) {
  const list = $('pp-mem-list')
  list.innerHTML = ''
  const keys = Object.keys(mem)
  if (!keys.length) {
    list.innerHTML = '<div class="mem-empty">Nessuna chiave salvata</div>'
    return
  }
  for (const k of keys) {
    const row = document.createElement('div')
    row.className = 'mem-row'
    row.innerHTML = `
      <span class="mem-key">${esc(k)}</span>
      <span class="mem-val">${esc(String(mem[k]))}</span>
      <button class="mem-del" title="Rimuovi" onclick="delMem('${esc(k).replace(/'/g,"\\'")}')">✕</button>
    `
    list.appendChild(row)
  }
}

function renderProjectsList(projects) {
  const list = $('pp-proj-list')
  list.innerHTML = ''
  if (!projects.length) {
    list.innerHTML = '<div class="mem-empty">Nessun progetto</div>'
    return
  }
  for (const p of projects) {
    const row = document.createElement('div')
    row.className = 'proj-row' + (_projState.active === p.name ? ' current' : '')
    row.innerHTML = `
      <div style="flex:1" onclick="switchProject('${esc(p.name).replace(/'/g,"\\'")}')">
        <div class="proj-row-name">${esc(p.name)}</div>
        ${p.description ? `<div class="proj-row-desc">${esc(p.description)}</div>` : ''}
      </div>
      <span class="proj-row-mem">${p.mem_count} chiavi</span>
      <button class="proj-del-btn" title="Elimina" onclick="deleteProject('${esc(p.name).replace(/'/g,"\\'")}')">✕</button>
    `
    list.appendChild(row)
  }
}

function switchProject(name) {
  wsSend({type:'project_action', action:'switch', name})
}

function createProject() {
  const name = $('pp-new-name').value.trim()
  const desc = $('pp-new-desc').value.trim()
  if (!name) { $('pp-new-name').focus(); return }
  wsSend({type:'project_action', action:'create', name, description: desc})
  $('pp-new-name').value = ''
  $('pp-new-desc').value = ''
}

function addMem() {
  const k = $('pp-key').value.trim()
  const v = $('pp-val').value.trim()
  if (!k || !v) { (!k ? $('pp-key') : $('pp-val')).focus(); return }
  wsSend({type:'project_action', action:'set_mem', key: k, value: v})
  $('pp-key').value = ''
  $('pp-val').value = ''
}

function delMem(key) {
  wsSend({type:'project_action', action:'del_mem', key})
}

function deleteProject(name) {
  if (!confirm(`Eliminare il progetto "${name}"?`)) return
  wsSend({type:'project_action', action:'delete', name})
}

// Enter key on new-project inputs
$('pp-new-name').addEventListener('keydown', e => { if(e.key==='Enter') $('pp-new-desc').focus() })
$('pp-new-desc').addEventListener('keydown', e => { if(e.key==='Enter') createProject() })
$('pp-key').addEventListener('keydown', e => { if(e.key==='Enter') $('pp-val').focus() })
$('pp-val').addEventListener('keydown', e => { if(e.key==='Enter') addMem() })

// Close panel on backdrop click (mobile)
document.addEventListener('click', e => {
  const panel = $('proj-panel')
  if (panel.classList.contains('open') && !panel.contains(e.target) && e.target !== $('proj-btn') && !$('proj-btn').contains(e.target))
    panel.classList.remove('open')
})

connect()
inp.focus()
</script>
</body>
</html>"""
