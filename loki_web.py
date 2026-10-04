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
.logo{display:flex;align-items:center;gap:8px;flex-shrink:0}
.logo-txt{color:var(--accent2);font-weight:700;font-size:15px;letter-spacing:4px}
#conn-dot{width:7px;height:7px;border-radius:50%;background:var(--red);flex-shrink:0;transition:background .3s}
#conn-dot.ok{background:var(--green)}
#conn-txt{color:var(--text2);font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#busy{display:flex;align-items:center;gap:6px;visibility:hidden}
#busy.on{visibility:visible}
.spin-svg{flex-shrink:0;animation:spin .7s linear infinite}
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
    <div class="logo"><div id="logo-svg-wrap" style="width:26px;height:26px"></div><span class="logo-txt">LOKI</span></div>
    <span id="conn-dot"></span>
    <span id="conn-txt">connessione...</span>
    <div id="busy"><div id="busy-spin-wrap" class="spin-svg" style="width:16px;height:16px"></div><span id="busy-txt">elaborando</span></div>
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

// ── Loki SVG Logo ──
const _PR='M250.00,61.00 L247.19,61.04 L244.38,61.13 L241.57,61.26 L238.77,61.43 L235.97,61.64 L233.17,61.89 L230.38,62.18 L227.59,62.52 L224.80,62.89 L222.03,63.31 L219.26,63.77 L216.50,64.27 L213.74,64.81 L211.00,65.39 L208.26,66.02 L205.53,66.68 L202.82,67.38 L200.11,68.13 L197.42,68.91 L194.74,69.74 L192.07,70.60 L189.42,71.50 L186.78,72.44 L184.15,73.42 L181.54,74.44 L178.95,75.50 L176.37,76.60 L173.81,77.74 L171.26,78.91 L168.74,80.12 L166.23,81.37 L163.74,82.65 L161.27,83.97 L158.83,85.33 L156.40,86.72 L153.99,88.15 L151.61,89.62 L149.25,91.12 L146.91,92.65 L144.59,94.22 L142.30,95.83 L140.03,97.46 L137.79,99.14 L135.57,100.84 L133.38,102.58 L131.21,104.34 L129.08,106.14 L126.96,107.98 L124.88,109.84 L122.83,111.73 L120.80,113.65 L118.80,115.61 L116.83,117.59 L114.90,119.60 L112.99,121.64 L111.11,123.71 L109.27,125.80 L107.45,127.92 L105.67,130.07 L103.92,132.24 L102.20,134.44 L100.52,136.67 L98.87,138.92 L97.25,141.19 L95.67,143.49 L94.12,145.81 L92.61,148.15 L91.13,150.51 L89.69,152.90 L88.28,155.31 L86.91,157.73 L85.57,160.18 L84.28,162.64 L83.02,165.13 L81.79,167.63 L80.61,170.15 L79.46,172.69 L78.35,175.24 L77.28,177.81 L76.24,180.40 L75.25,183.00 L74.29,185.61 L73.38,188.24 L72.50,190.88 L71.66,193.53 L70.86,196.20 L70.11,198.88 L69.39,201.56 L68.71,204.26 L68.07,206.97 L67.47,209.68 L66.92,212.40 L66.40,215.14 L65.92,217.87 L65.49,220.62 L65.10,223.37 L64.74,226.13 L64.43,228.89 L64.16,231.65 L63.93,234.42 L63.74,237.19 L63.59,239.96 L63.49,242.74 L63.42,245.51 L63.40,248.29 L63.42,251.06 L63.48,253.84 L63.58,256.61 L63.72,259.38 L63.90,262.15 L64.13,264.91 L64.39,267.67 L64.70,270.43 L65.04,273.18 L65.43,275.92 L65.86,278.66 L66.33,281.39 L66.84,284.12 L67.39,286.83 L67.98,289.54 L68.61,292.24 L69.28,294.92 L69.99,297.60 L70.74,300.27 L71.53,302.92 L72.36,305.56 L73.22,308.19 L74.13,310.81 L75.08,313.41 L76.06,315.99 L77.08,318.56 L78.14,321.12 L79.24,323.66 L80.37,326.18 L81.55,328.69 L82.76,331.17 L84.00,333.64 L85.28,336.09 L86.60,338.52 L87.96,340.93 L89.35,343.32 L90.77,345.68 L92.23,348.03 L93.73,350.35 L95.26,352.65 L96.82,354.93 L98.41,357.18 L100.04,359.41 L101.71,361.62 L103.40,363.79 L105.13,365.95 L106.88,368.08 L108.67,370.18 L110.49,372.25 L112.34,374.30 L114.22,376.31 L116.13,378.30 L118.07,380.26 L120.04,382.19 L122.04,384.10 L124.06,385.97 L126.11,387.81 L128.19,389.62 L130.30,391.40 L132.43,393.14 L134.58,394.86 L136.76,396.54 L138.97,398.19 L141.20,399.81 L143.45,401.39 L145.73,402.94 L148.03,404.45 L150.35,405.94 L152.69,407.38 L155.05,408.79 L157.43,410.17 L159.84,411.50 L162.26,412.81 L164.70,414.07 L167.16,415.30 L169.64,416.50 L172.13,417.65 L174.65,418.77 L177.17,419.85 L179.72,420.90 L182.27,421.90 L184.85,422.87 L187.43,423.80 L190.03,424.69 L192.64,425.54 L195.27,426.35 L197.90,427.12 L200.55,427.86 L203.20,428.55 L205.87,429.20 L208.54,429.82 L211.23,430.39 L213.92,430.93 L216.62,431.42 L219.32,431.87 L222.04,432.29 L224.75,432.66 L227.47,432.99 L230.20,433.29 L232.93,433.54 L235.66,433.75 L238.40,433.92 L241.14,434.05 L243.88,434.14 L246.62,434.18 L249.36,434.19 L252.09,434.16 L254.83,434.08 L257.57,433.97 L260.30,433.81 L263.03,433.62 L265.76,433.38 L268.48,433.10 L271.20,432.78 L273.91,432.40 L276.61,431.95 L279.30,431.45 L281.98,430.92 L284.66,430.35 L287.32,429.74 L289.97,429.09 L292.61,428.40 L295.24,427.67 L297.86,426.91 L300.46,426.10 L303.05,425.26 L305.63,424.38 L308.19,423.46 L310.74,422.50 L313.27,421.50 L315.79,420.47 L318.29,419.40 L320.77,418.29 L323.23,417.15 L325.68,415.97 L328.10,414.76 L330.51,413.51 L332.90,412.22 L335.26,410.90 L337.61,409.54 L339.93,408.15 L342.23,406.73 L344.51,405.27 L346.77,403.78 L349.00,402.26 L351.21,400.71 L353.40,399.12 L355.56,397.50 L357.69,395.85 L359.80,394.17 L361.88,392.45 L363.94,390.71 L365.96,388.94 L367.96,387.14 L369.94,385.31 L371.88,383.45 L373.80,381.56 L375.68,379.65 L377.54,377.71 L379.37,375.74 L381.16,373.74 L382.93,371.72 L384.66,369.68 L386.36,367.61 L388.03,365.52 L389.67,363.40 L391.28,361.26 L392.85,359.09 L394.39,356.91 L395.90,354.70 L397.37,352.47 L398.81,350.22 L400.21,347.95 L401.58,345.66 L402.91,343.35 L404.21,341.02 L405.47,338.68 L406.70,336.32 L407.89,333.94 L409.05,331.54 L410.17,329.13 L411.25,326.70 L412.29,324.26 L413.30,321.80 L414.27,319.33 L415.20,316.85 L416.10,314.35 L416.96,311.84 L417.77,309.33 L418.56,306.80 L419.30,304.26 L420.00,301.71 L420.67,299.15 L421.30,296.58 L421.88,294.01 L422.43,291.43 L422.94,288.84 L423.41,286.24 L423.85,283.64 L424.24,281.04 L424.59,278.43 L424.91,275.82 L425.18,273.20 L425.42,270.58 L425.62,267.96 L425.77,265.34 L425.89,262.72 L425.97,260.09 L426.01,257.47 L426.01,254.85 L425.97,252.23 L425.89,249.62 L425.77,247.00 L425.61,244.39 L425.42,241.78 L425.18,239.18 L424.91,236.58 L424.59,233.99 L424.24,231.41 L423.85,228.83 L423.42,226.26 L422.96,223.70 L422.45,221.15 L421.91,218.60 L421.33,216.07 L420.71,213.54 L420.06,211.03 L419.36,208.53 L418.63,206.04 L417.86,203.56 L417.06,201.10 L416.22,198.65 L415.34,196.21 L414.43,193.79 L413.48,191.39 L412.50,189.00 L411.48,186.62 L410.42,184.27 L409.33,181.93 L408.21,179.61 L407.05,177.31 L405.86,175.03 L404.63,172.76 L403.38,170.52 L402.08,168.30 L400.76,166.10 L399.40,163.92 L398.01,161.76 L396.59,159.62 L395.14,157.51 L393.66,155.42 L392.15,153.36 L390.60,151.32 L389.03,149.30 L387.43,147.31 L385.80,145.35 L384.14,143.41 L382.45,141.50 L380.74,139.62 L378.99,137.76 L377.22,135.93 L375.43,134.13 L373.60,132.36 L371.76,130.62 L369.88,128.90 L367.99,127.22 L366.06,125.57 L364.12,123.95 L362.15,122.36 L360.16,120.80 L358.14,119.27 L356.11,117.78 L354.05,116.32 L351.97,114.89 L349.87,113.50 L347.75,112.14 L345.61,110.81 L343.46,109.52 L341.28,108.26 L339.09,107.04 L336.88,105.86 L334.65,104.71 L332.40,103.60 L330.14,102.53 L327.87,101.49 L325.58,100.50 L323.27,99.54 L320.95,98.63 L318.62,97.75 L316.27,96.93 L313.91,96.15 L311.53,95.42 L309.13,94.76 L306.69,94.25 L306.18,95.66 L308.33,96.86 L310.51,97.98 L312.68,99.10 L314.84,100.23 L316.98,101.39 L319.10,102.57 L321.21,103.77 L323.30,104.99 L325.37,106.25 L327.43,107.52 L329.46,108.82 L331.48,110.15 L333.47,111.51 L335.44,112.89 L337.40,114.29 L339.33,115.73 L341.24,117.18 L343.12,118.67 L344.99,120.17 L346.83,121.71 L348.64,123.26 L350.44,124.85 L352.20,126.45 L353.95,128.08 L355.67,129.74 L357.36,131.41 L359.03,133.11 L360.67,134.83 L362.29,136.58 L363.87,138.35 L365.44,140.13 L366.97,141.94 L368.48,143.77 L369.96,145.62 L371.41,147.49 L372.83,149.38 L374.22,151.29 L375.59,153.22 L376.93,155.16 L378.23,157.13 L379.51,159.11 L380.75,161.10 L381.97,163.12 L383.16,165.15 L384.31,167.20 L385.44,169.26 L386.53,171.33 L387.59,173.42 L388.62,175.53 L389.62,177.65 L390.59,179.78 L391.52,181.92 L392.43,184.08 L393.30,186.25 L394.13,188.42 L394.94,190.61 L395.71,192.81 L396.45,195.02 L397.16,197.24 L397.83,199.47 L398.47,201.70 L399.08,203.94 L399.65,206.19 L400.19,208.45 L400.70,210.71 L401.17,212.98 L401.61,215.26 L402.01,217.54 L402.38,219.82 L402.72,222.11 L403.02,224.40 L403.29,226.69 L403.53,228.98 L403.73,231.28 L403.89,233.58 L404.03,235.88 L404.13,238.18 L404.19,240.48 L404.22,242.78 L404.22,245.07 L404.18,247.37 L404.11,249.66 L404.01,251.95 L403.87,254.24 L403.70,256.53 L403.49,258.81 L403.25,261.08 L402.98,263.35 L402.67,265.61 L402.33,267.87 L401.96,270.12 L401.56,272.37 L401.12,274.61 L400.65,276.83 L400.14,279.06 L399.61,281.27 L399.04,283.47 L398.44,285.66 L397.81,287.84 L397.14,290.01 L396.44,292.17 L395.72,294.32 L394.96,296.46 L394.17,298.58 L393.35,300.69 L392.50,302.78 L391.61,304.87 L390.70,306.93 L389.76,308.99 L388.79,311.02 L387.79,313.04 L386.76,315.05 L385.70,317.04 L384.61,319.01 L383.49,320.96 L382.35,322.90 L381.18,324.82 L379.98,326.72 L378.75,328.60 L377.49,330.46 L376.21,332.30 L374.91,334.12 L373.57,335.92 L372.21,337.70 L370.83,339.46 L369.42,341.20 L367.98,342.91 L366.52,344.60 L365.04,346.27 L363.53,347.92 L362.00,349.54 L360.45,351.14 L358.87,352.72 L357.27,354.27 L355.65,355.79 L354.01,357.29 L352.35,358.77 L350.66,360.22 L348.96,361.64 L347.24,363.04 L345.49,364.41 L343.73,365.76 L341.95,367.07 L340.15,368.36 L338.33,369.63 L336.49,370.86 L334.64,372.07 L332.77,373.25 L330.89,374.40 L328.98,375.52 L327.07,376.61 L325.14,377.68 L323.19,378.71 L321.23,379.71 L319.25,380.69 L317.27,381.63 L315.27,382.55 L313.26,383.43 L311.23,384.29 L309.20,385.11 L307.15,385.91 L305.09,386.67 L303.03,387.40 L300.95,388.10 L298.86,388.77 L296.77,389.41 L294.67,390.01 L292.56,390.59 L290.44,391.13 L288.32,391.64 L286.19,392.12 L284.05,392.57 L281.91,392.98 L279.77,393.37 L277.62,393.72 L275.46,394.04 L273.31,394.33 L271.15,394.58 L268.98,394.80 L266.82,395.02 L264.66,395.23 L262.50,395.41 L260.33,395.56 L258.16,395.67 L256.00,395.75 L253.83,395.80 L251.66,395.82 L249.49,395.81 L247.32,395.76 L245.16,395.68 L242.99,395.57 L240.83,395.43 L238.67,395.25 L236.51,395.04 L234.36,394.81 L232.21,394.53 L230.06,394.23 L227.92,393.90 L225.79,393.53 L223.66,393.13 L221.54,392.70 L219.43,392.24 L217.32,391.75 L215.22,391.22 L213.13,390.67 L211.05,390.08 L208.98,389.47 L206.91,388.82 L204.86,388.14 L202.82,387.43 L200.79,386.69 L198.77,385.93 L196.76,385.13 L194.77,384.30 L192.78,383.44 L190.82,382.56 L188.86,381.64 L186.92,380.70 L184.99,379.72 L183.08,378.72 L181.18,377.69 L179.30,376.64 L177.44,375.55 L175.59,374.44 L173.76,373.30 L171.95,372.13 L170.15,370.94 L168.38,369.72 L166.62,368.47 L164.88,367.20 L163.16,365.90 L161.46,364.58 L159.78,363.23 L158.12,361.86 L156.48,360.47 L154.86,359.05 L153.27,357.60 L151.69,356.14 L150.14,354.65 L148.61,353.13 L147.11,351.60 L145.62,350.04 L144.16,348.46 L142.73,346.86 L141.32,345.24 L139.93,343.60 L138.57,341.94 L137.23,340.25 L135.92,338.55 L134.63,336.83 L133.37,335.09 L132.14,333.34 L130.93,331.56 L129.75,329.77 L128.60,327.96 L127.47,326.13 L126.37,324.29 L125.30,322.43 L124.25,320.56 L123.24,318.67 L122.25,316.77 L121.29,314.85 L120.36,312.92 L119.46,310.97 L118.59,309.02 L117.75,307.05 L116.94,305.07 L116.15,303.07 L115.40,301.07 L114.68,299.05 L113.98,297.03 L113.32,294.99 L112.69,292.95 L112.09,290.89 L111.52,288.83 L110.98,286.76 L110.47,284.69 L109.99,282.60 L109.54,280.51 L109.13,278.41 L108.74,276.31 L108.39,274.20 L108.07,272.09 L107.78,269.98 L107.52,267.86 L107.29,265.73 L107.09,263.61 L106.93,261.48 L106.80,259.35 L106.70,257.21 L106.63,255.08 L106.59,252.95 L106.59,250.82 L106.61,248.68 L106.67,246.55 L106.76,244.42 L106.88,242.29 L107.04,240.17 L107.22,238.04 L107.44,235.92 L107.69,233.81 L107.96,231.70 L108.28,229.59 L108.62,227.49 L108.99,225.39 L109.39,223.30 L109.83,221.22 L110.29,219.14 L110.79,217.07 L111.32,215.01 L111.88,212.96 L112.46,210.91 L113.08,208.88 L113.73,206.85 L114.41,204.84 L115.12,202.84 L115.85,200.84 L116.62,198.86 L117.42,196.89 L118.24,194.93 L119.10,192.99 L119.98,191.06 L120.89,189.14 L121.83,187.24 L122.80,185.35 L123.80,183.48 L124.82,181.62 L125.87,179.77 L126.95,177.95 L128.05,176.14 L129.18,174.34 L130.34,172.57 L131.53,170.81 L132.74,169.07 L133.97,167.35 L135.23,165.64 L136.52,163.96 L137.83,162.30 L139.16,160.65 L140.52,159.03 L141.90,157.43 L143.31,155.84 L144.74,154.28 L146.19,152.74 L147.66,151.23 L149.16,149.73 L150.68,148.26 L152.22,146.81 L153.78,145.38 L155.36,143.98 L156.96,142.60 L158.58,141.25 L160.23,139.92 L161.89,138.61 L163.57,137.33 L165.27,136.08 L166.98,134.85 L168.72,133.65 L170.47,132.47 L172.24,131.32 L174.02,130.19 L175.83,129.10 L177.64,128.03 L179.48,126.98 L181.33,125.97 L183.19,124.98 L185.07,124.02 L186.96,123.09 L188.86,122.19 L190.78,121.31 L192.71,120.47 L194.65,119.65 L196.60,118.86 L198.57,118.10 L200.54,117.37 L202.53,116.68 L204.52,116.01 L206.53,115.37 L208.54,114.76 L210.56,114.18 L212.59,113.63 L214.63,113.11 L216.68,112.62 L218.73,112.16 L220.79,111.73 L222.85,111.34 L224.92,110.97 L226.99,110.64 L229.07,110.33 L231.16,110.06 L233.24,109.82 L235.33,109.60 L237.42,109.42 L239.52,109.28 L241.61,109.16 L243.71,109.07 L245.81,109.02 L247.90,108.99 L250.00,109.00Z';
const _PN='M232.99,61.94 L233.78,61.76 L234.58,61.56 L235.37,61.36 L236.17,61.14 L236.97,60.91 L237.76,60.67 L238.56,60.41 L239.36,60.16 L240.16,59.92 L240.97,59.67 L241.77,59.43 L242.58,59.19 L243.39,58.96 L244.20,58.73 L245.01,58.50 L245.83,58.28 L246.64,58.06 L247.46,57.84 L248.28,57.63 L249.10,57.42 L249.93,57.21 L250.75,57.02 L251.58,56.84 L252.41,56.67 L253.24,56.52 L254.07,56.37 L254.90,56.25 L255.74,56.13 L256.57,56.03 L257.40,55.94 L258.24,55.86 L259.07,55.80 L259.91,55.75 L260.74,55.71 L261.58,55.67 L262.42,55.64 L263.25,55.61 L264.09,55.59 L264.93,55.57 L265.77,55.55 L266.61,55.54 L267.45,55.53 L268.28,55.52 L269.12,55.52 L269.97,55.52 L270.80,55.53 L271.64,55.56 L272.48,55.60 L273.32,55.66 L274.15,55.73 L274.99,55.81 L275.82,55.90 L276.65,56.01 L277.48,56.13 L278.31,56.26 L279.13,56.41 L279.95,56.57 L280.77,56.74 L281.59,56.92 L282.41,57.10 L283.23,57.29 L284.05,57.47 L284.86,57.66 L285.68,57.86 L286.49,58.06 L287.30,58.26 L288.11,58.46 L288.92,58.68 L289.73,58.91 L290.52,59.17 L291.32,59.45 L292.11,59.75 L292.89,60.08 L293.66,60.43 L294.43,60.80 L295.19,61.19 L295.95,61.60 L296.69,62.03 L297.43,62.49 L298.16,62.96 L298.89,63.46 L299.61,63.95 L300.33,64.45 L301.04,64.95 L301.75,65.46 L302.46,65.98 L303.15,66.53 L303.84,67.09 L304.52,67.67 L305.19,68.26 L305.85,68.87 L306.50,69.50 L307.15,70.15 L307.78,70.81 L308.41,71.49 L309.02,72.19 L309.63,72.90 L310.22,73.62 L310.82,74.35 L311.39,75.12 L311.95,75.92 L312.49,76.75 L313.01,77.62 L313.51,78.52 L314.00,79.46 L314.46,80.42 L314.91,81.42 L315.32,82.50 L315.69,83.66 L316.01,84.92 L316.28,86.26 L316.52,87.69 L316.71,89.18 L316.92,90.60 L317.15,91.95 L317.40,93.23 L317.67,94.45 L317.95,95.60 L317.11,99.26 L317.11,99.26 L315.10,102.09 L314.12,102.61 L313.12,103.19 L312.10,103.83 L311.06,104.55 L310.01,105.33 L308.94,106.19 L307.89,106.99 L306.89,107.72 L305.93,108.37 L305.00,108.95 L304.12,109.45 L303.26,109.88 L302.42,110.29 L301.60,110.68 L300.79,111.04 L300.00,111.38 L299.22,111.69 L298.45,111.97 L297.70,112.23 L296.96,112.46 L296.23,112.69 L295.50,112.92 L294.78,113.14 L294.07,113.34 L293.36,113.54 L292.66,113.73 L291.96,113.90 L291.28,114.07 L290.60,114.23 L289.92,114.37 L289.25,114.51 L288.59,114.63 L287.93,114.75 L287.28,114.85 L286.63,114.95 L285.98,115.06 L285.34,115.17 L284.69,115.28 L284.05,115.38 L283.42,115.47 L282.79,115.55 L282.16,115.61 L281.55,115.66 L280.93,115.69 L280.32,115.71 L279.72,115.71 L279.12,115.70 L278.53,115.67 L277.93,115.63 L277.35,115.58 L276.76,115.51 L276.18,115.43 L275.60,115.36 L275.01,115.29 L274.43,115.23 L273.84,115.17 L273.26,115.10 L272.68,115.05 L272.09,114.99 L271.51,114.94 L270.92,114.89 L270.34,114.83 L269.76,114.77 L269.18,114.70 L268.60,114.62 L268.02,114.54 L267.44,114.45 L266.86,114.35 L266.29,114.25 L265.71,114.14 L265.14,114.03 L264.56,113.91 L263.98,113.78 L263.41,113.64 L262.83,113.51 L262.26,113.38 L261.68,113.25 L261.10,113.13 L260.52,113.01 L259.94,112.89 L259.36,112.77 L258.77,112.65 L258.19,112.54 L257.61,112.43 L257.02,112.32 L256.44,112.21 L255.85,112.09 L255.27,111.97 L254.68,111.84 L254.09,111.70 L253.50,111.55 L252.91,111.41 L252.32,111.25 L251.73,111.09 L251.14,110.92 L250.54,110.75 L249.95,110.57 L249.35,110.38 L248.75,110.20 L248.15,110.02 L247.55,109.84 L246.95,109.66 L246.34,109.49 L245.73,109.32 L245.13,109.15 L244.52,108.98 L243.90,108.82 L243.29,108.66 L242.68,108.50 L242.06,108.35 L241.44,108.19 L240.82,108.04 L240.21,107.91 L239.59,107.80 L238.97,107.70 L238.35,107.62 L237.73,107.55 L237.11,107.51Z';
const _PT='M367.85,127.11 L367.17,126.52 L366.49,125.94 L365.81,125.36 L365.12,124.78 L364.43,124.21 L363.74,123.64 L363.05,123.08 L362.35,122.52 L361.65,121.96 L360.94,121.41 L360.24,120.86 L359.53,120.32 L358.82,119.78 L358.10,119.24 L357.38,118.71 L356.66,118.19 L355.94,117.66 L355.22,117.14 L354.49,116.63 L353.76,116.12 L353.02,115.61 L352.29,115.11 L351.55,114.61 L350.81,114.11 L350.06,113.62 L349.32,113.14 L348.57,112.66 L347.82,112.18 L347.06,111.71 L346.31,111.24 L345.55,110.77 L344.79,110.31 L344.02,109.86 L343.26,109.40 L342.49,108.96 L341.72,108.51 L340.95,108.08 L340.17,107.64 L339.40,107.21 L338.62,106.79 L337.84,106.37 L337.06,105.95 L336.27,105.54 L335.48,105.14 L334.69,104.73 L333.90,104.34 L333.11,103.94 L332.31,103.56 L331.51,103.17 L330.72,102.79 L329.91,102.42 L329.11,102.05 L328.30,101.69 L327.50,101.33 L326.69,100.97 L325.88,100.63 L325.06,100.28 L324.25,99.94 L323.43,99.61 L322.62,99.28 L321.80,98.95 L320.97,98.64 L320.15,98.32 L319.32,98.01 L318.50,97.71 L317.67,97.41 L316.84,97.12 L316.01,96.84 L315.17,96.56 L314.34,96.28 L313.50,96.02 L312.66,95.76 L311.82,95.50 L310.97,95.26 L310.13,95.02 L309.28,94.80 L308.43,94.58 L307.57,94.39 L306.69,94.25 L306.18,95.66 L306.93,96.11 L307.70,96.52 L308.47,96.92 L309.24,97.32 L310.01,97.72 L310.77,98.11 L311.54,98.51 L312.31,98.90 L313.07,99.30 L313.84,99.70 L314.60,100.11 L315.36,100.51 L316.11,100.92 L316.87,101.33 L317.62,101.74 L318.37,102.16 L319.12,102.58 L319.87,103.00 L320.62,103.43 L321.36,103.85 L322.10,104.29 L322.84,104.72 L323.58,105.16 L324.31,105.60 L325.04,106.04 L325.77,106.49 L326.50,106.94 L327.22,107.39 L327.94,107.85 L328.66,108.31 L329.38,108.77 L330.10,109.24 L330.81,109.71 L331.52,110.18 L332.23,110.66 L332.93,111.14 L333.63,111.62 L334.33,112.10 L335.03,112.59 L335.72,113.08 L336.41,113.58 L337.10,114.08 L337.79,114.58 L338.47,115.09 L339.15,115.59 L339.83,116.11 L340.51,116.62 L341.18,117.14 L341.85,117.66 L342.52,118.18 L343.18,118.71 L343.84,119.24 L344.50,119.78 L345.15,120.31 L345.81,120.85 L346.46,121.40 L347.10,121.94 L347.75,122.49 L348.39,123.04 L349.03,123.60 L349.66,124.16 L350.29,124.72 L350.92,125.28 L351.55,125.85 L352.17,126.42 L352.79,126.99 L353.41,127.57 L354.02,128.15 L354.63,128.73 L355.24,129.32 L355.84,129.91 L356.44,130.50 L357.04,131.09 L357.63,131.69 L358.22,132.29 L358.81,132.89 L359.40,133.49 L359.98,134.10 L360.56,134.71Z';
const _PX='M115.44,71.04C113.08,70.94 111.48,70.73 109.71,70.30C107.65,69.81 104.77,68.88 103.88,68.42C103.57,68.27 103.00,67.99 102.62,67.82C101.00,67.08 100.40,66.69 98.44,65.11C96.31,63.39 93.68,60.20 93.00,58.50C92.91,58.26 92.70,57.86 92.55,57.61C92.39,57.36 92.17,56.82 92.06,56.42C91.94,56.02 91.71,55.35 91.53,54.94C91.36,54.53 91.08,53.38 90.91,52.38C90.65,50.85 90.60,50.14 90.60,47.88C90.59,45.52 90.63,44.94 90.92,43.21C91.11,42.12 91.39,40.91 91.55,40.52C91.72,40.13 91.92,39.56 92.00,39.25C92.18,38.54 93.23,36.47 93.95,35.38C95.06,33.70 95.40,33.28 96.66,32.02C98.36,30.30 100.14,28.95 102.06,27.93C102.47,27.71 102.98,27.44 103.19,27.33C104.93,26.39 108.77,25.19 110.84,24.94C111.72,24.83 112.66,24.67 112.94,24.59C113.28,24.49 114.63,24.44 117.25,24.44C120.35,24.44 121.26,24.48 122.12,24.65C122.71,24.77 123.72,24.93 124.38,25.01C125.03,25.09 125.96,25.29 126.44,25.45C126.92,25.62 127.65,25.84 128.06,25.95C128.47,26.06 129.22,26.34 129.71,26.57C130.20,26.81 130.67,27.00 130.76,27.00C130.94,27.00 133.08,28.14 134.00,28.73C136.12,30.08 138.85,32.59 139.94,34.16C141.72,36.76 142.55,38.47 143.33,41.12C143.81,42.80 143.90,43.30 144.01,44.88C144.07,45.87 144.15,46.88 144.18,47.11C144.29,47.89 143.90,52.40 143.64,53.31C142.65,56.87 141.71,58.93 140.01,61.25C138.54,63.27 135.64,65.88 133.65,66.99C132.22,67.79 132.09,67.86 131.06,68.32C128.85,69.31 126.90,69.91 123.50,70.63C122.48,70.84 118.19,71.15 116.92,71.10C116.58,71.09 115.92,71.06 115.44,71.04ZM30.79,70.04C30.38,69.96 30.08,69.55 30.08,69.03C30.07,68.77 30.08,58.96 30.10,47.21L30.12,25.86L30.43,25.56L30.74,25.25L36.98,25.25C42.94,25.25 43.24,25.26 43.52,25.49L43.81,25.73L43.88,41.50L43.94,57.27L44.29,57.57L44.64,57.88L57.56,57.88L70.48,57.88L70.74,58.20C70.99,58.53 71.00,58.66 71.00,64.00C71.00,69.20 70.99,69.49 70.76,69.77L70.52,70.06L50.79,70.08C39.94,70.08 30.94,70.07 30.79,70.04ZM169.14,70.00C168.79,69.95 168.50,69.81 168.35,69.63C168.13,69.36 168.13,68.55 168.13,48.02C168.13,35.79 168.18,26.48 168.24,26.20C168.46,25.26 168.04,25.32 175.01,25.28L181.25,25.24L181.56,25.55C181.80,25.79 181.87,25.99 181.87,26.40C181.87,26.70 181.89,32.20 181.91,38.64C181.94,51.09 181.92,50.75 182.52,50.75C182.67,50.75 182.97,50.45 183.35,49.92C183.67,49.47 184.19,48.77 184.50,48.38C184.81,47.98 185.47,47.13 185.97,46.48C186.46,45.84 187.56,44.41 188.41,43.31C189.26,42.21 190.56,40.52 191.30,39.56C193.48,36.71 194.68,35.15 196.00,33.44C196.27,33.09 196.89,32.27 197.40,31.61C200.51,27.53 201.14,26.73 201.82,26.03L202.58,25.25L209.10,25.25C214.85,25.25 215.63,25.27 215.80,25.45C216.19,25.83 215.97,26.33 214.76,27.75C214.44,28.13 213.92,28.77 213.59,29.19C212.75,30.25 211.04,32.39 208.44,35.63C207.20,37.18 205.71,39.03 205.13,39.75C204.55,40.47 203.61,41.64 203.04,42.35C201.77,43.93 201.71,44.25 202.47,45.40C202.73,45.79 203.15,46.47 203.41,46.90C203.67,47.33 204.08,48.02 204.33,48.44C204.58,48.85 204.88,49.36 205.00,49.56C205.47,50.37 206.06,51.34 206.44,51.94C206.67,52.28 206.92,52.70 207.00,52.88C207.08,53.05 207.34,53.47 207.56,53.81C207.78,54.16 208.21,54.86 208.50,55.38C208.80,55.89 209.24,56.63 209.49,57.02C209.74,57.42 210.19,58.17 210.50,58.69C210.81,59.21 211.19,59.83 211.34,60.07C211.49,60.30 211.75,60.74 211.92,61.03C212.30,61.70 213.00,62.88 213.51,63.69C214.31,64.97 214.98,66.09 215.54,67.06C215.85,67.61 216.29,68.30 216.50,68.59C216.97,69.22 216.97,69.73 216.50,69.96C216.27,70.08 214.50,70.11 209.19,70.09C201.20,70.06 201.67,70.12 201.03,68.97C200.83,68.61 200.43,67.90 200.14,67.41C199.86,66.91 199.41,66.12 199.15,65.66C198.89,65.19 198.53,64.59 198.34,64.31C198.15,64.04 198.00,63.76 198.00,63.70C198.00,63.63 197.78,63.24 197.52,62.82C197.07,62.11 196.65,61.39 195.46,59.31C195.19,58.83 194.78,58.13 194.56,57.75C194.34,57.37 194.04,56.82 193.89,56.53C193.57,55.90 193.14,55.59 192.67,55.65C192.38,55.68 191.83,56.30 189.51,59.20C182.22,68.27 181.16,69.56 180.83,69.80C180.49,70.06 180.35,70.06 175.08,70.08C172.12,70.08 169.44,70.05 169.14,70.00ZM239.51,69.99C238.85,69.73 238.88,71.04 238.88,47.86C238.88,32.42 238.91,26.17 239.01,25.89C239.09,25.67 239.25,25.44 239.38,25.37C239.52,25.30 241.98,25.25 245.73,25.25L251.86,25.25L252.21,25.55L252.56,25.85L252.62,47.52C252.69,70.24 252.70,69.70 252.16,69.95C251.84,70.10 239.88,70.13 239.51,69.99ZM121.63,58.27C122.70,57.99 123.83,57.63 124.15,57.46C124.47,57.30 124.88,57.10 125.08,57.03C125.72,56.81 127.37,55.38 128.14,54.40C129.00,53.30 129.78,51.66 130.01,50.49C130.21,49.47 130.21,45.93 130.02,44.92C129.84,43.99 129.04,42.24 128.42,41.41C127.17,39.74 125.56,38.54 123.42,37.65C122.32,37.20 121.88,37.09 120.17,36.88C119.08,36.74 117.79,36.63 117.31,36.63C115.93,36.63 112.74,37.08 111.99,37.38C111.62,37.53 111.03,37.76 110.69,37.90C108.90,38.61 106.78,40.51 105.71,42.36C105.21,43.22 105.06,43.65 104.81,44.86C104.21,47.73 104.54,50.97 105.63,52.94C106.28,54.13 107.25,55.28 108.30,56.12C109.25,56.87 111.02,57.77 112.00,57.99C112.38,58.08 113.08,58.26 113.56,58.40C114.83,58.77 115.85,58.87 117.88,58.81C119.50,58.77 119.89,58.72 121.63,58.27Z';

function lokiSVG(uid){
  const m='lm'+uid;
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500" width="100%" height="100%" style="display:block">'
    +'<defs><mask id="'+m+'" maskUnits="userSpaceOnUse" x="0" y="0" width="500" height="500">'
    +'<rect width="500" height="500" fill="black"/>'
    +'<path d="'+_PR+'" fill="white"/>'
    +'<path d="'+_PN+'" fill="white"/>'
    +'<path d="M284.85,88.72 L327.18,87.38 L314.31,114.49Z" fill="black"/>'
    +'<path d="'+_PT+'" fill="white"/>'
    +'<circle cx="274.85" cy="73.74" r="9.5" fill="black"/>'
    +'<g transform="translate(250,252) translate(-141.3,-47.7)">'
    +'<path d="'+_PX+'" fill="white" fill-rule="evenodd"/>'
    +'</g></mask></defs>'
    +'<rect width="500" height="500" fill="#b01828" mask="url(#'+m+')"/>'
    +'<circle cx="274.85" cy="73.74" r="6.5" fill="#5EE06A"/>'
    +'</svg>';
}

function lokiSVGNoText(uid){
  const m='lm'+uid;
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500" width="100%" height="100%" style="display:block">'
    +'<defs><mask id="'+m+'" maskUnits="userSpaceOnUse" x="0" y="0" width="500" height="500">'
    +'<rect width="500" height="500" fill="black"/>'
    +'<path d="'+_PR+'" fill="white"/>'
    +'<path d="'+_PN+'" fill="white"/>'
    +'<path d="M284.85,88.72 L327.18,87.38 L314.31,114.49Z" fill="black"/>'
    +'<path d="'+_PT+'" fill="white"/>'
    +'<circle cx="274.85" cy="73.74" r="9.5" fill="black"/>'
    +'</mask></defs>'
    +'<rect width="500" height="500" fill="#b01828" mask="url(#'+m+')"/>'
    +'<circle cx="274.85" cy="73.74" r="6.5" fill="#5EE06A"/>'
    +'</svg>';
}

// Init logos
document.getElementById('logo-svg-wrap').innerHTML = lokiSVG('hdr');
document.getElementById('busy-spin-wrap').innerHTML = lokiSVGNoText('bs');

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
