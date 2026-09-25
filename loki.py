import ollama
import subprocess
import os
import re
import sys
import random
import json
import time
import asyncio
import threading
import configparser
import shutil
from datetime import datetime
import loki_plugins
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.styles import Style
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.application import Application, get_app_or_none
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window, ConditionalContainer, FloatContainer, Float
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import BeforeInput
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.filters import Condition
from prompt_toolkit.data_structures import Point

MODEL = os.environ.get('LOKI_MODEL') or "orcarouter/Qwen3.8-27B-Uncensored:latest"
VERSION = "0.7.0"
PREVIEW_LINES = 20
SESSIONS_DIR  = os.path.expanduser("~/.loki_sessions")
MEMORY_FILE   = os.path.expanduser("~/.loki_memory.md")
WORKSPACE_DIR  = os.path.expanduser("~/.loki_workspace")
WORKSPACE_FILES = {"plan.md", "failures.md", "ideas.md", "notes.md", "scratch.md"}
COMPRESS_AT      = 0.80
COMPRESS_PREEMPT = 0.65
MAX_CTX       = 32768
WORKING_CTX   = 8192
KEEP_ALIVE    = '15m'
COMPRESS_CTX  = 8192
OUTPUT_MAX_LINES  = 100
OUTPUT_HEAD_LINES = 40
OUTPUT_TAIL_LINES = 30
OUTPUT_MAX_CHARS  = 6000
KEEP_RECENT_MSG   = 6
MEMORY_MAX_SUMMARIES = 5
SHELL_TIMEOUT     = 30
# Memory efficiency: trim old messages every turn (no LLM needed)
TOOL_OUTPUT_KEEP_CHARS = 400   # chars kept in old tool outputs (head + tail)
MAX_MESSAGES_BEFORE_COMPRESS = 60  # force compress when history grows this long
MEMORY_INJECT_MAX_CHARS = 6000  # max chars of memory file injected into system prompt
CONFIG_FILE = os.path.expanduser("~/.loki.conf")
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ── Personas ─────────────────────────────────────────────────────────────────
PERSONAS = {
    'general': (
        "You are Loki, a highly capable Linux shell agent. "
        "Help with coding, system administration, file management, data analysis, and general tasks. "
        "When executing shell commands, use the run_shell tool. "
        "Use read_file to examine files and write_file to create or modify them. "
        "Be direct, concise, and efficient. Never repeat the executed command in your response. "
        "Never use emoji."
    ),
    'security': (
        "You are an expert red team operator and offensive security specialist with deep Linux sysadmin skills and vision capabilities. "
        "Your mindset is adversarial by default: always think like an attacker. "
        "When analyzing targets, proactively identify attack surfaces, misconfigurations, exposed secrets, weak auth, and exploitable vulnerabilities. "
        "When executing shell commands, use the run_shell tool. Interpret output and extract security-relevant findings. "
        "Use read_file to inspect configs, binaries, and source code. Use write_file to drop payloads or patch files. "
        "Suggest concrete next steps: reconnaissance, exploitation, pivoting, persistence, exfiltration. "
        "Be direct and technical. Skip disclaimers. Never repeat the executed command in the final response. "
        "Never use emoji."
    ),
    'developer': (
        "You are an expert software developer, code reviewer, and debugger. "
        "Help with writing, debugging, refactoring, and understanding code across any programming language. "
        "Use run_shell to run tests, linters, build tools, and git commands. "
        "Use read_file to examine source code, configs, and logs. Use write_file to apply changes and create new files. "
        "Be precise, suggest idiomatic solutions, and explain trade-offs when relevant. "
        "Never use emoji. Never repeat commands you just ran."
    ),
    'sysadmin': (
        "You are an expert Linux sysadmin and DevOps engineer. "
        "Help manage servers, configure services, troubleshoot issues, and automate repetitive tasks. "
        "Use run_shell for system commands, log inspection, and service management. "
        "Use read_file to inspect config files and logs. Use write_file to update config files and scripts. "
        "Be direct and prioritize system stability, security, and reproducibility. Never use emoji."
    ),
}
ACTIVE_PERSONA = 'general'


def load_config():
    """Load ~/.loki.conf and return a dict of settings. Silent on errors."""
    cfg = {}
    if not os.path.isfile(CONFIG_FILE):
        return cfg
    parser = configparser.ConfigParser()
    try:
        parser.read(CONFIG_FILE)
        sect = parser['loki'] if 'loki' in parser else {}
        for key in ('model', 'persona'):
            if key in sect:
                cfg[key] = sect[key].strip()
        for key in ('auto_approve', 'auto_continue', 'show_thinking'):
            if key in sect:
                try:
                    cfg[key] = sect.getboolean(key)
                except Exception:
                    pass
        if 'shell_timeout' in sect:
            try:
                cfg['shell_timeout'] = int(sect['shell_timeout'])
            except Exception:
                pass
    except Exception:
        pass
    return cfg
import loki_hw
import loki_persist
import loki_mem

HW = None  # dict popolato al boot da loki_hw.detect_hardware()

R      = "\033[0m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
ORANGE = "\033[38;5;135m"   # viola medio  (ex arancio - accento principale)
GREEN  = "\033[38;5;93m"    # viola scuro  (ex verde   - stati ok/secondario)
RED    = "\033[38;5;196m"
BLUE   = "\033[38;5;39m"
GRAY   = "\033[38;5;244m"
DGRAY  = "\033[38;5;238m"
CYAN   = "\033[38;5;51m"
YELLOW = "\033[38;5;220m"
PURPLE = "\033[38;5;141m"   # lavanda     (thinking block)

SLASH_COMMANDS = {
    '/help':    'show available commands',
    '/clear':   'clear the conversation',
    '/reset':   'alias of /clear',
    '/model':   'show model; /model <name> switches to a different model live',
    '/persona': 'show or switch persona  [general|security|developer|sysadmin]',
    '/tools':   'list available tools the model can call',
    '/cwd':     'show current directory',
    '/img':     'attach an image to the next message',
    '/auto':    'enable auto-approve for commands',
    '/manual':  'disable auto-approve for commands',
    '/ac':        'toggle auto-continue when tokens run out',
    '/remember':  'save something to persistent memory',
    '/memory':    'show persistent memory',
    '/compress':  'summarize the conversation and save to memory',
    '/think':   'show/hide the model reasoning',
    '/last':    'reprint the last full output',
    '/history': 'session statistics',
    '/cost':    'alias of /history',
    '/save':     'save the current session  [name]',
    '/resume':   'list/resume a saved session (no arg: list; /resume <n|name>: load and reprint history)',
    '/resume-last': "resume the last session's autosave (if < 12h old)",
    '/search':   'search saved sessions by keyword',
    '/delete':   'delete a saved session [name|number]',
    '/clone':    'clone a saved session   [source] [new_name]',
    '/fetch':    'fetch a URL and show its content  [url]',
    '/hw':       'show detected HW (RAM, threads, GPU, working ctx)',
    '/trim':     'free memory now: strip thinking + truncate old tool outputs (no LLM)',
    '/exit':     'exit the shell agent',
    '/quit':     'alias of /exit',
}

stats = {
    'start_time':    datetime.now(),
    'messages':      0,
    'tools_ok':      0,
    'tools_no':      0,
    'auto_approve':   True,
    'show_thinking':  True,
    'auto_continue':  True,
    'last_tokens':    0,   # token GENERATI nell'ultima risposta (eval_count)
    'ctx_used':       0,   # token del PROMPT dell'ultima chiamata (prompt_eval_count) — questo e il segnale di riempimento contesto
    'working_ctx':    WORKING_CTX,  # ctx effettivamente allocato lato Ollama (adattivo)
    'pending_images': [],
    'last_command':   None,
    'last_output':    None,
}

tools_schema = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Run a bash command on Linux and return stdout+stderr. "
                "Use for commands, package management, git, running scripts, etc. "
                "CRITICAL: if you already know the command, call it NOW without extra reasoning. "
                "Real output from a failed command is worth more than any reasoning."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Bash command to run"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file from disk. Use for source code, configs, logs, or any text file. Supports partial reads via start_line/end_line.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or ~-relative path to the file"},
                    "start_line": {"type": "integer", "description": "First line to read (1-indexed, optional)"},
                    "end_line": {"type": "integer", "description": "Last line to read inclusive (optional)"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write or overwrite a file on disk. Use to create new files or replace existing ones. Set append=true to add to an existing file without truncating it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or ~-relative path to the file"},
                    "content": {"type": "string", "description": "Full content to write to the file"},
                    "append": {"type": "boolean", "description": "If true, append to the file instead of overwriting (default false)"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search for information online (tool syntax, documentation, CVEs, workarounds, recent events). "
                "Use BEFORE guessing unknown options or flags, and when a command fails for unclear reasons. "
                "Returns instant answers + search snippets. Follow up with fetch_url to read a result in full."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query in English or Italian"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetch the full text content of a specific URL (documentation, CVE page, GitHub repo, "
                "article, man page online, etc.). Use AFTER web_search to read a promising result in detail, "
                "or when you already have a URL you need to inspect. Returns clean markdown text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch (https://...)"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_write",
            "description": (
                "Write or update a file in the persistent workspace. "
                "Use INSTEAD of reasoning in loops: if considering 2+ options or a step failed, write it to file NOW. "
                "Files survive between turns — your thinking does not. "
                "plan.md=current plan, failures.md=what failed, "
                "ideas.md=options considered, notes.md=free notes, scratch.md=drafts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File name: plan.md | failures.md | ideas.md | notes.md | scratch.md"
                    },
                    "content": {"type": "string", "description": "Content to write"},
                    "mode": {
                        "type": "string",
                        "description": "write=overwrite, append=add to end",
                        "enum": ["write", "append"]
                    }
                },
                "required": ["file", "content", "mode"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_read",
            "description": (
                "Read a file from the workspace. "
                "Call at the start of complex sessions to remember where you were. "
                "Read failures.md before retrying something that may have already failed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "File name: plan.md | failures.md | ideas.md | notes.md | scratch.md"
                    }
                },
                "required": ["file"]
            }
        }
    },
]

_SEC_TOOLS = {
    'http_probe', 'encode_decode', 'hash_data', 'identify_hash',
    'file_entropy', 'check_linux_privesc',
    'port_scan', 'dns_enum', 'web_fingerprint', 'dir_bruteforce',
    'jwt_decode', 'generate_payload', 'net_recon', 'cred_harvest',
    'generate_persist', 'kernel_suggest', 'exfil_payload',
    'sqli_probe', 'lfi_probe',
}

def get_active_tools():
    """Return base tools + security tools (if security persona) + user plugins."""
    schema = list(tools_schema)
    if ACTIVE_PERSONA == 'security':
        try:
            import loki_sec
            schema.extend(loki_sec.SECURITY_TOOLS_SCHEMA)
        except ImportError:
            pass
    # User-defined plugins always active, regardless of persona
    if loki_plugins.tool_names():
        schema.extend(loki_plugins._plugin_tools)
    return schema

class SlashOnlyCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith('/') or ' ' in text or '\n' in text:
            return
        for cmd, desc in SLASH_COMMANDS.items():
            if cmd.startswith(text.lower()):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )

def strip_ansi(s):
    return re.sub(r'\033\[[0-9;]*m', '', s)

def c(text, color):
    return f"{color}{text}{R}"

# Flag globale settato da async_chat_loop: True quando siamo dentro la
# Application prompt_toolkit. get_app_or_none() non funziona da executor thread
# (ContextVar non si propaga), quindi usiamo un flag esplicito.
_PINNED_MODE = False

# Event settato dall'handler Ctrl+C per interrompere lo streaming/turno corrente.
# stream_response lo controlla tra un chunk e l'altro e alza KeyboardInterrupt
# quando e set, cosi riusa il gia esistente handler di interruzione.
_cancel_event = threading.Event()

# Event True quando il modello sta emettendo tool_calls (bash) o quando
# run_shell sta eseguendo un comando: lo spinner mostra "bashing..." invece
# di "cooking...". Set/clear in stream_response e run_shell.
_bashing_event = threading.Event()

# ==================== FULLSCREEN v2 =====================
# Accumulatore condiviso di tutto il testo che va nell'output area.
_output_chunks   = []
_output_lock     = threading.Lock()
_app_ref         = None
_follow_bottom   = [True]   # True = incolla al fondo (default); False = utente ha rotellato su
_scroll_lines    = [0]      # quando NON segue: N righe dall'alto del contenuto
_last_max_scroll = [0]      # ultima quantita scrollabile vista al render (per capping/sync)
_desired_scroll  = [0]      # posizione voluta (aggiornata subito a ogni scroll, per cursore-virtuale)
_output_line_count  = [1]   # numero righe dell'ultimo _get_output_ft() — per _get_output_cursor_pos
_output_window_ref  = [None]  # riferimento al Window dell'output — per leggere render_info
_mouse_enabled   = [True]
# Stato picker sessioni (attivato da /resume senza argomenti)
_picker = dict(active=False, sessions=[], cursor=0,
               mode='list', action=0, clone_buf='')
_picker_state_ref = [None]   # riferimento a state dict per caricare la sessione scelta


def _debug_log(msg):
    """Log a ~/loki_debug.log. Utile per errori altrimenti invisibili in
    fullscreen (dove stdout va nel void o nell'output area)."""
    try:
        with open(os.path.expanduser("~/loki_debug.log"), "a") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def _output_append(text):
    """Accumula testo nell'output area. Gestisce '\\r' (torna a inizio riga
    corrente e sovrascrive) per far funzionare barre di progresso e simili."""
    with _output_lock:
        if '\r' not in text:
            _output_chunks.append(text)
        else:
            existing = ''.join(_output_chunks)
            _output_chunks.clear()
            for i, part in enumerate(text.split('\r')):
                if i > 0:
                    lastnl = existing.rfind('\n')
                    existing = existing[:lastnl + 1] if lastnl >= 0 else ''
                existing += part
            _output_chunks.append(existing)
    if _app_ref is not None:
        try:
            _app_ref.invalidate()
        except Exception:
            pass


class _OutputProxy:
    """File-like che intercetta sys.stdout e alimenta l'output area."""
    def write(self, text):
        if isinstance(text, bytes):
            text = text.decode('utf-8', errors='replace')
        _output_append(text)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return True

    def fileno(self):
        return sys.__stdout__.fileno()

    def writable(self):
        return True


def _scroll_up(step):
    """Scrolla su di N righe. Al primo scroll-up esce dal 'segui-fondo' e
    fissa la posizione manuale al valore attuale (max_scroll conosciuto)."""
    if _follow_bottom[0]:
        _follow_bottom[0] = False
        # Se _last_max_scroll non e ancora aggiornato (primo render non ancora
        # avvenuto), usa il conteggio righe logiche come fallback ragionevole.
        _scroll_lines[0] = _last_max_scroll[0] if _last_max_scroll[0] > 0 else max(0, _output_line_count[0] - 1)
    _scroll_lines[0] = max(0, _scroll_lines[0] - step)
    _desired_scroll[0] = _scroll_lines[0]
    if _app_ref is not None:
        _app_ref.invalidate()


def _scroll_down(step):
    """Scrolla giu di N righe. Se torni al fondo (o oltre) rientri in
    'segui-fondo' cosi il contenuto nuovo appare automaticamente."""
    if _follow_bottom[0]:
        return  # gia al fondo, niente da fare
    _scroll_lines[0] += step
    if _scroll_lines[0] >= _last_max_scroll[0]:
        _follow_bottom[0] = True
        _scroll_lines[0]  = 0
        _desired_scroll[0] = _last_max_scroll[0]
    else:
        _desired_scroll[0] = _scroll_lines[0]   # aggiorna subito
    if _app_ref is not None:
        _app_ref.invalidate()


def _scroll_to_bottom():
    _follow_bottom[0] = True
    _scroll_lines[0]  = 0
    _desired_scroll[0] = _last_max_scroll[0]
    if _app_ref is not None:
        _app_ref.invalidate()


def _get_output_cursor_pos() -> Point:
    """Ritorna la posizione del cursore virtuale del FormattedTextControl.
    Usa _output_line_count (aggiornato da _get_output_ft() nello stesso frame)
    per stare sempre dentro i bounds del contenuto renderizzato.
    Aggiorna _last_max_scroll dal render_info del frame precedente, cosi
    _scroll_up/_scroll_down hanno un cap reale (con wrap_lines=True
    get_vertical_scroll non viene mai chiamata)."""
    try:
        lc = _output_line_count[0]
        # Aggiorna il max scroll leggendo render_info del frame precedente.
        # visible_line_numbers e la lista delle righe logiche visibili; la sua
        # lunghezza (unica) = quante righe logiche stanno nel window adesso.
        win = _output_window_ref[0]
        if win is not None:
            ri = win.render_info
            if ri is not None and ri.displayed_lines:
                vis = len(set(ri.displayed_lines))   # righe logiche uniche visibili
                _last_max_scroll[0] = max(0, lc - vis)
        if _follow_bottom[0]:
            return Point(x=0, y=max(0, lc - 1))
        return Point(x=0, y=min(_desired_scroll[0], max(0, lc - 1)))
    except Exception as e:
        import traceback as _tb
        _debug_log(f"_get_output_cursor_pos ERRORE: {e}\n{_tb.format_exc()}")
        return Point(x=0, y=0)


# ── Session Picker ────────────────────────────────────────────────────────────

def _picker_load_sessions():
    """Carica la lista sessioni ordinata dalla piu recente."""
    out = []
    if not os.path.isdir(SESSIONS_DIR):
        return out
    for fname in sorted(os.listdir(SESSIONS_DIR), reverse=True):
        if not fname.endswith('.json'):
            continue
        name = fname[:-5]
        path = _session_path(name)
        try:
            with open(path) as f:
                data = json.load(f)
            n_msgs   = len([m for m in data.get('messages', []) if m['role'] == 'user'])
            saved_at = data.get('saved_at', '')
        except Exception:
            n_msgs, saved_at = 0, ''
        out.append({'name': name, 'path': path, 'n_msgs': n_msgs, 'saved_at': saved_at})
    return out


def _picker_elapsed(saved_at):
    try:
        dt   = datetime.strptime(saved_at, "%Y-%m-%d %H:%M:%S")
        secs = int((datetime.now() - dt).total_seconds())
        if secs < 60:    return f"{secs}s fa"
        if secs < 3600:  return f"{secs//60}m fa"
        if secs < 86400: return f"{secs//3600}h fa"
        return f"{secs//86400}g fa"
    except Exception:
        return ''


def _picker_activate(state_ref):
    _picker['sessions']  = _picker_load_sessions()
    _picker['cursor']    = 0
    _picker['mode']      = 'list'
    _picker['action']    = 0
    _picker['clone_buf'] = ''
    _picker['active']    = True
    _picker_state_ref[0] = state_ref
    if _app_ref:
        _app_ref.invalidate()


def _picker_deactivate():
    _picker['active'] = False
    if _app_ref:
        _app_ref.invalidate()


def _picker_render():
    """Genera il testo ANSI del picker da mostrare nell'output area."""
    ss  = _picker['sessions']
    cur = _picker['cursor']
    mode= _picker['mode']
    act = _picker['action']
    W   = max(50, term_width() - 6)

    out = []
    out.append(f"\n  {BOLD}{ORANGE}SESSIONI SALVATE{R}  {DIM}{len(ss)} sessioni{R}")
    out.append(f"  {DGRAY}{'─' * (W - 2)}{R}")

    if not ss:
        out.append(f"\n  {DIM}nessuna sessione salvata{R}")
        out.append(f"\n  {GRAY}Esc{R} = chiudi")
        return '\n'.join(out)

    for i, s in enumerate(ss):
        sel    = (i == cur)
        arrow  = f"{ORANGE}▶{R}" if sel else ' '
        name   = s['name']
        elapsed= _picker_elapsed(s['saved_at'])
        n_msg  = s['n_msgs']

        name_str   = f"{BOLD}{ORANGE}{name}{R}" if sel else name
        elapsed_str= f"{DIM}{elapsed:<10}{R}"
        msgs_str   = f"{GRAY}{n_msg} msg{R}"

        # Label azioni: visibili inline solo sulla riga selezionata
        if sel and mode in ('list', 'actions', 'delete_confirm', 'clone_input'):
            del_lbl = f"{BOLD}{RED}[✕ Elim]{R}" if (mode == 'actions' and act == 0) else f"{DGRAY}[✕ Elim]{R}"
            cln_lbl = f"{BOLD}{GREEN}[⎘ Clona]{R}" if (mode == 'actions' and act == 1) else f"{DGRAY}[⎘ Clona]{R}"
            emoji_str = f"  {del_lbl}  {cln_lbl}"
        else:
            emoji_str = ""

        if sel and mode == 'delete_confirm':
            out.append(f"  {arrow} {name_str:<28}  {elapsed_str}  {msgs_str}{emoji_str}")
            d = f"{RED if act==0 else DGRAY}[ Sì, elimina ]{R}"
            a = f"{GREEN if act==1 else DGRAY}[ Annulla ]{R}"
            out.append(f"       {d}  {a}  {DGRAY}(← → scegli, Invio conferma){R}")
        elif sel and mode == 'clone_input':
            out.append(f"  {arrow} {name_str:<28}  {elapsed_str}  {msgs_str}{emoji_str}")
            out.append(f"  {DIM}Scrivi il nuovo nome nel campo qui sotto e premi Invio{R}  {DGRAY}(Esc = annulla){R}")
        else:
            out.append(f"  {arrow} {name_str:<28}  {elapsed_str}  {msgs_str}{emoji_str}")

    out.append(f"\n  {DGRAY}{'─' * (W - 2)}{R}")
    if mode in ('list', 'actions'):
        out.append(f"  {GRAY}↑↓{R} naviga  {GRAY}Invio{R} apri  {GRAY}→{R} seleziona azione  {GRAY}Esc{R} esci")
    return '\n'.join(out)


def _picker_do_load(name):
    """Carica la sessione selezionata: chiude il picker, chiama resume_session."""
    state = _picker_state_ref[0]
    if state is None:
        _picker_deactivate()
        return
    _picker_deactivate()
    result = resume_session(name, state['messages'])
    if result is not None:
        state['messages'] = result
        stats['pending_images'].clear()
    _scroll_to_bottom()


# ── Fine Session Picker ────────────────────────────────────────────────────────

def _wheel_scroll_output(mouse_event):
    """Handler condiviso: rotella su/giu → scrolla output."""
    et = mouse_event.event_type
    if et == MouseEventType.SCROLL_UP:
        _scroll_up(3)
        return None
    if et == MouseEventType.SCROLL_DOWN:
        _scroll_down(3)
        return None
    return NotImplemented


class _ScrollableOutputControl(FormattedTextControl):
    """FormattedTextControl + handler rotella per l'output area stesso."""
    def mouse_handler(self, mouse_event):
        res = _wheel_scroll_output(mouse_event)
        if res is NotImplemented:
            return NotImplemented
        return res


class _WheelFTControl(FormattedTextControl):
    """FormattedTextControl 'passivo' che intercetta la rotella e la manda
    all'output area — usato per toolbar/separator/spinner cosi la rotella
    funziona anche se il cursore e sopra di loro."""
    def mouse_handler(self, mouse_event):
        res = _wheel_scroll_output(mouse_event)
        if res is NotImplemented:
            return super().mouse_handler(mouse_event)
        return res


class _WheelBufferControl(BufferControl):
    """BufferControl che intercetta la rotella e la manda all'output area,
    invece di provare a scrollare dentro il buffer di input (che e piccolo)."""
    def mouse_handler(self, mouse_event):
        res = _wheel_scroll_output(mouse_event)
        if res is NotImplemented:
            return super().mouse_handler(mouse_event)
        return res


def write(text):
    sys.stdout.write(text)
    # In modalita pinned lo StdoutProxy di patch_stdout accumula fino ai '\n'
    # e ridisegna il prompt su ogni emit: se flushiamo char-by-char vediamo un
    # carattere per riga. Fuori da patch_stdout serve il flush esplicito.
    if not _PINNED_MODE:
        sys.stdout.flush()

def term_width():
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80

def box(lines, color=ORANGE, min_width=64, padding=1):
    max_len = max((len(strip_ansi(l)) for l in lines), default=0)
    width = max(max_len + 2 + padding * 2, min_width)
    top = c("╭" + "─" * (width - 2) + "╮", color)
    bot = c("╰" + "─" * (width - 2) + "╯", color)
    print(top)
    for line in lines:
        pad = width - 2 - padding * 2 - len(strip_ansi(line))
        print(c("│", color) + " " * padding + line + " " * pad + " " * padding + c("│", color))
    print(bot)

def welcome():
    # In fullscreen: pulisci il buffer di output invece di scrivere escape "clear" al tty.
    if _PINNED_MODE:
        with _output_lock:
            _output_chunks.clear()
    elif get_app_or_none() is None:
        os.system('clear')
    else:
        print()

    lines = [
        f"{c('◈', GREEN)} {ORANGE}{BOLD}LOKI{R} {c(f'v{VERSION}', DIM)}",
        "",
        f"  {c('/help', ORANGE)} per aiuto  {c('·', DGRAY)}  {c('/exit', ORANGE)} per uscire",
        "",
        f"  {c('cwd', DIM)}    {os.getcwd()}",
        f"  {c('model', DIM)}  {MODEL}",
    ]
    box(lines)
    tips = [
        "Digita '/' per vedere i comandi disponibili",
        "Usa \\ + Invio per andare a capo",
        "Freccia su/giu per navigare la cronologia",
        "Ctrl+R per cercare nella cronologia",
        "/last per rivedere l'ultimo output completo",
        "/think per attivare/disattivare il ragionamento visibile",
        "/auto per approvare automaticamente i comandi",
        "Ctrl+C durante lo streaming ferma il turno corrente",
        "Alt+M per riattivare la selezione col mouse",
    ]
    print(f"\n  {c('Tip', GRAY)} {c(random.choice(tips), DIM)}\n")

def show_help():
    print()
    print(f"  {c('COMANDI', BOLD)}")
    for cmd, desc in SLASH_COMMANDS.items():
        print(f"    {c(cmd.ljust(12), ORANGE)} {c(desc, GRAY)}")
    print()
    print(f"  {c('TASTIERA', BOLD)}")
    keys = [
        ("Invio",          "invia messaggio"),
        ("\\ + Invio",     "nuova riga"),
        ("freccia su/giu", "cronologia comandi"),
        ("Ctrl+R",         "ricerca cronologia"),
        ("Ctrl+C / Ctrl+D","esci"),
    ]
    for k, desc in keys:
        print(f"    {c(k.ljust(18), CYAN)} {c(desc, GRAY)}")
    print()

def show_stats():
    elapsed = datetime.now() - stats['start_time']
    mins = int(elapsed.total_seconds() // 60)
    secs = int(elapsed.total_seconds() % 60)
    age_h = elapsed.total_seconds() / 3600
    auto_state  = c('ON', GREEN) if stats['auto_approve']  else c('OFF', GRAY)
    think_state = c('ON', GREEN) if stats['show_thinking'] else c('OFF', GRAY)
    ac_state    = c('ON', GREEN) if stats['auto_continue'] else c('OFF', GRAY)
    print()
    print(f"  {c('SESSION', BOLD)}")
    print(f"    {c('uptime', DIM):<22} {mins}m {secs}s", end='')
    if age_h >= 1:
        print(f"  {c('⚠ use /compress or /trim if lagging', YELLOW)}", end='')
    print()
    print(f"    {c('messages', DIM):<22} {stats['messages']}")
    print(f"    {c('tools ok', DIM):<22} {c(str(stats['tools_ok']), GREEN)}")
    print(f"    {c('tools failed', DIM):<22} {c(str(stats['tools_no']), RED)}")
    print(f"    {c('auto approve', DIM):<22} {auto_state}")
    print(f"    {c('show thinking', DIM):<22} {think_state}")
    print(f"    {c('auto continue', DIM):<22} {ac_state}")
    print(f"    {c('working ctx', DIM):<22} {stats['working_ctx']} / {MAX_CTX} tk")
    if stats['ctx_used']:
        pct = int(100 * stats['ctx_used'] / stats['working_ctx'])
        bar_filled = int(20 * pct / 100)
        bar = '█' * bar_filled + '░' * (20 - bar_filled)
        bar_color = RED if pct >= 70 else YELLOW if pct >= 45 else GREEN
        print(f"    {c('ctx last turn', DIM):<22} {c(bar, bar_color)} {pct}%  ({stats['ctx_used']} tk)")
    # Show memory file size
    if os.path.isfile(MEMORY_FILE):
        mem_kb = os.path.getsize(MEMORY_FILE) / 1024
        mem_color = YELLOW if mem_kb > 30 else GREEN
        print(f"    {c('memory file', DIM):<22} {c(f'{mem_kb:.1f} KB', mem_color)}"
              f"  {c(f'(inject cap: {MEMORY_INJECT_MAX_CHARS//1000}k chars)', DIM)}")
    print()

def _confirm_sync(command):
    """Modal di conferma con input() da stdin. Vale in modalita legacy o
    dentro run_in_terminal (che detacha stdin dall'app pinned)."""
    print()
    print(f"  {c('⏺', ORANGE)} {c('Bash', BOLD)}  {c(command, CYAN)}")
    print(f"     {c('1', BOLD)} {c('·', DGRAY)} Si, esegui")
    print(f"     {c('2', BOLD)} {c('·', DGRAY)} Si, e non chiedere piu per questa sessione")
    print(f"     {c('3', BOLD)} {c('·', DGRAY)} No, ferma qui")
    while True:
        try:
            choice = input(f"     {c('❯', ORANGE)} ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if choice in ('1', 'y', 'Y', 's', 'S', ''):
            return True
        if choice == '2':
            stats['auto_approve'] = True
            print(c("     auto approve attivato per questa sessione", YELLOW))
            return True
        if choice in ('3', 'n', 'N'):
            return False
        print(c("     scelta non valida", RED))


# Riferimento al main event loop, popolato all'avvio di async_chat_loop.
_MAIN_LOOP = None


def confirm_command(command):
    if stats['auto_approve']:
        print()
        print(f"  {c('⏺', GREEN)} {c('Bash', BOLD)}  {c(command, CYAN)}  {c('[auto]', DIM)}")
        return True

    app = get_app_or_none()
    if app is None or _MAIN_LOOP is None:
        return _confirm_sync(command)

    # Modalita async pinned: sospendi l'app con run_in_terminal e chiedi conferma
    import concurrent.futures
    fut = concurrent.futures.Future()

    async def _do():
        try:
            fut.set_result(await app.run_in_terminal(lambda: _confirm_sync(command)))
        except Exception as e:
            fut.set_exception(e)

    asyncio.run_coroutine_threadsafe(_do(), _MAIN_LOOP)
    try:
        return fut.result(timeout=300)
    except concurrent.futures.TimeoutError:
        print(c("     timeout conferma — comando rifiutato", RED))
        return False

def detect_script_context(command, output):
    if output.startswith('#!'):
        first_line = output.splitlines()[0]
        return f"shell script  {c(first_line, DIM)}"
    patterns = [
        r'(?:cat|tee)\s*>\s*([^\s;|&]+\.(?:sh|bash|py|zsh))',
        r'nano\s+([^\s;|&]+\.(?:sh|bash|py|zsh))',
        r'vim?\s+([^\s;|&]+\.(?:sh|bash|py|zsh))',
        r'echo\s+.+>\s*([^\s;|&]+\.(?:sh|bash|py|zsh))',
        r'>\s*([^\s;|&]+\.(?:sh|bash|py|zsh))',
    ]
    for pat in patterns:
        m = re.search(pat, command)
        if m:
            fname = m.group(1)
            ext = fname.rsplit('.', 1)[-1]
            kind = 'python' if ext == 'py' else 'shell script'
            return f"{kind}  {c(fname, DIM)}"
    return None

def print_output_block(command, full_output, expand=False):
    lines = full_output.splitlines()
    total = len(lines)
    script_label = detect_script_context(command, full_output)
    header = "output" if not script_label else script_label
    header_len = len(strip_ansi(header))
    fill = max(1, term_width() - 12 - header_len)

    print(f"     {c('┌─', DGRAY)} {c(header, GRAY)} {c('─' * fill, DGRAY)}")
    show_lines = lines if expand else lines[:PREVIEW_LINES]
    for line in show_lines:
        print(f"     {c('│', DGRAY)} {line}")
    print(f"     {c('└' + '─' * (term_width() - 8), DGRAY)}")

    if not expand and total > PREVIEW_LINES:
        hidden = total - PREVIEW_LINES
        # Niente input() bloccante: mostriamo un hint e basta.
        # L'utente puo sempre rileggere l'output completo con /last.
        print(f"     {c(f'{hidden} righe nascoste — usa /last per vedere tutto', DIM)}")

def truncate_for_model(output):
    orig_chars = len(output)
    lines = output.splitlines()
    total = len(lines)
    truncated = False

    if total > OUTPUT_MAX_LINES:
        head    = lines[:OUTPUT_HEAD_LINES]
        tail    = lines[-OUTPUT_TAIL_LINES:]
        omitted = total - OUTPUT_HEAD_LINES - OUTPUT_TAIL_LINES
        sep     = f"\n[... {omitted} righe omesse — usa grep/head/tail se ti serve il centro ...]\n"
        result  = "\n".join(head) + sep + "\n".join(tail)
        truncated = True
    else:
        result = output

    # Hard cap in caratteri: cattura righe-monstre (JSON minificato, base64, log su una riga)
    # che il limite per-riga non intercetta.
    if len(result) > OUTPUT_MAX_CHARS:
        keep  = OUTPUT_MAX_CHARS // 2
        cut   = len(result) - OUTPUT_MAX_CHARS
        result = (result[:keep]
                  + f"\n[... {cut} char omessi al centro ...]\n"
                  + result[-keep:])
        truncated = True

    if truncated:
        result += (f"\n[output troncato: {len(result)} char inviati su "
                   f"{orig_chars} totali ({total} righe) — "
                   f"usa grep/head/tail/sed per estrarre solo cio' che serve]")
    return result

def _maybe_sudo_apt(command):
    stripped = command.lstrip()
    if re.match(r'apt(?:-get)?\s', stripped) and not command.startswith('sudo'):
        return 'sudo ' + command
    return command

def run_web_search(query):
    """Multi-source web search: DDG instant answer API + DDG Lite via Jina reader."""
    import urllib.parse
    import json as _json
    query = query.strip()
    if not query:
        return "Errore: query vuota"
    q = urllib.parse.quote_plus(query)
    print(f"  {c('web_search:', BLUE)} {query}")
    results = []

    # 1) DDG Instant Answer API — fast, structured, best for factual/tech queries
    try:
        ddg_api = f"https://api.duckduckgo.com/?q={q}&format=json&no_html=1&skip_disambig=1&t=loki"
        r = subprocess.run(
            f"curl -sL --max-time 8 '{ddg_api}'",
            shell=True, capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            data = _json.loads(r.stdout)
            section = []
            if data.get('Answer'):
                section.append(f"[INSTANT ANSWER] {data['Answer']}")
            if data.get('AbstractText'):
                section.append(f"[SUMMARY] {data['AbstractText']}")
                if data.get('AbstractURL'):
                    section.append(f"Source: {data['AbstractURL']}")
            for topic in data.get('RelatedTopics', [])[:5]:
                if isinstance(topic, dict) and topic.get('Text'):
                    url = topic.get('FirstURL', '')
                    line = f"• {topic['Text']}"
                    if url:
                        line += f"\n  → {url}"
                    section.append(line)
            if section:
                results.append('\n'.join(section))
    except Exception:
        pass

    # 2) DDG Lite via Jina reader — gets actual search snippets + titles
    try:
        jina_url = f"https://r.jina.ai/https://lite.duckduckgo.com/lite/?q={q}"
        r = subprocess.run(
            f"curl -sL --max-time 15 '{jina_url}'",
            shell=True, capture_output=True, text=True, timeout=18,
        )
        text = r.stdout.strip()
        if text:
            _SKIP = ('duckduckgo.com/l/?uddg=', 'URL Source:', 'Title:',
                     'Markdown Content:', 'Web Search', 'Safe Search',
                     'Next Page', '---', '===')
            lines = []
            for line in text.splitlines():
                line = line.strip()
                if not line or len(line) < 16:
                    continue
                if any(s in line for s in _SKIP):
                    continue
                lines.append(line)
            if lines:
                results.append('[SEARCH RESULTS]\n' + '\n'.join(lines[:60]))
    except Exception:
        pass

    if not results:
        return f"Nessun risultato per: {query}"

    combined = '\n\n'.join(results)
    if len(combined) > 3500:
        combined = (combined[:3500]
                    + '\n[...troncato — usa fetch_url <url> per leggere una pagina completa]')
    return combined


def run_fetch_url(url):
    """Fetch full text content of a URL via Jina AI reader. Returns clean markdown."""
    url = url.strip().strip('"\'')
    if not url.startswith('http'):
        url = 'https://' + url
    print(f"  {c('fetch_url:', BLUE)} {url}")
    try:
        jina_url = f"https://r.jina.ai/{url}"
        r = subprocess.run(
            f"curl -sL --max-time 20 '{jina_url}'",
            shell=True, capture_output=True, text=True, timeout=25,
        )
        text = r.stdout.strip()
        if not text:
            return f"Nessun contenuto da: {url}"
        # Strip Jina metadata header lines
        _META = ('URL Source:', 'Title:', 'Markdown Content:', 'Published Time:',
                 'Description:', 'X-Frame-Options:', 'Content-Type:')
        lines = [ln for ln in text.splitlines()
                 if not any(ln.strip().startswith(m) for m in _META)]
        result = '\n'.join(lines).strip()
        if len(result) > 6000:
            result = (result[:6000]
                      + '\n[...troncato — specifica una sezione o usa read_file per file locali]')
        return result if result else f"Contenuto vuoto da: {url}"
    except subprocess.TimeoutExpired:
        return f"TIMEOUT: fetch_url ha superato 20 secondi per {url}"
    except Exception as e:
        return f"Errore fetch_url: {e}"

def run_workspace_write(file, content, mode):
    if file not in WORKSPACE_FILES:
        return f"File non permesso: {file}. Usa: {', '.join(sorted(WORKSPACE_FILES))}"
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    path = os.path.join(WORKSPACE_DIR, file)
    flag = 'a' if mode == 'append' else 'w'
    with open(path, flag, encoding='utf-8') as f:
        if mode == 'append':
            f.write('\n' + content)
        else:
            f.write(content)
    preview = content[:120] + ('...' if len(content) > 120 else '')
    print(f"  {c('workspace_write:', BLUE)} [{mode}] {file}: {preview}")
    return f"OK: {file} aggiornato ({len(content)} caratteri, mode={mode})"

def run_workspace_read(file):
    if file not in WORKSPACE_FILES:
        return f"File non permesso: {file}. Usa: {', '.join(sorted(WORKSPACE_FILES))}"
    path = os.path.join(WORKSPACE_DIR, file)
    if not os.path.exists(path):
        return f"{file}: (vuoto — nessuna nota salvata)"
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    if not content.strip():
        return f"{file}: (vuoto)"
    print(f"  {c('workspace_read:', BLUE)} {file} ({len(content)} chars)")
    return content if len(content) <= 3000 else content[:3000] + '\n[...troncato]'

def run_shell(command):
    command = _maybe_sudo_apt(command)
    if not confirm_command(command):
        stats['tools_no'] += 1
        _bashing_event.clear()
        return "COMANDO RIFIUTATO DALL'UTENTE"
    stats['tools_ok'] += 1
    _bashing_event.set()  # spinner -> "bashing..." per tutta la durata del subprocess
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=SHELL_TIMEOUT
        )
        output = (result.stdout + result.stderr).strip() or "(nessun output)"
        stats['last_command'] = command
        stats['last_output'] = output                # completo per /last
        model_output = truncate_for_model(output)    # troncato per il modello
        print_output_block(command, output)          # display intero (truncato dall'UI a PREVIEW_LINES)
        return model_output
    except subprocess.TimeoutExpired:
        print(c(f"     TIMEOUT (>{SHELL_TIMEOUT}s)", RED))
        return f"TIMEOUT: command exceeded {SHELL_TIMEOUT} seconds"
    finally:
        # Comando finito: torna a "cooking..." per l'eventuale continuazione
        # del modello (che riflette sull'output).
        _bashing_event.clear()

def show_last():
    if not stats['last_output']:
        print(c("  nessun output precedente\n", DIM))
        return
    print()
    print_output_block(stats['last_command'] or '', stats['last_output'], expand=True)
    print()

READ_FILE_MAX_BYTES = 200 * 1024  # 200 KB cap per read_file call


def run_read_file(path, start_line=None, end_line=None):
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}"
    try:
        size = os.path.getsize(path)
        with open(path, 'r', errors='replace') as f:
            lines = f.readlines()
        total = len(lines)
        if start_line is not None or end_line is not None:
            s = max(0, (int(start_line) if start_line else 1) - 1)
            e = min(total, int(end_line) if end_line else total)
            selected = lines[s:e]
        else:
            selected = lines
        content = ''.join(selected)
        truncated = False
        if len(content) > READ_FILE_MAX_BYTES:
            content = content[:READ_FILE_MAX_BYTES]
            truncated = True
        header = f"# {path}  ({total} lines"
        if start_line or end_line:
            s_disp = start_line or 1
            e_disp = end_line or total
            header += f", showing {s_disp}–{e_disp}"
        header += ")\n"
        result = header + content
        if truncated:
            result += f"\n\n[... output truncated at {READ_FILE_MAX_BYTES // 1024}KB — use start_line/end_line for a narrower range ...]"
        return result
    except PermissionError:
        return f"ERROR: permission denied: {path}"
    except Exception as e:
        return f"ERROR reading {path}: {e}"


def run_write_file(path, content, append=False):
    path = os.path.expanduser(path)
    mode_str = 'append to' if append else 'overwrite'
    n_lines = len(content.splitlines())
    cmd_display = f"write_file({mode_str}): {path}  [{n_lines} lines]"
    if not confirm_command(cmd_display):
        stats['tools_no'] += 1
        return "WRITE REJECTED BY THE USER"
    stats['tools_ok'] += 1
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        mode = 'a' if append else 'w'
        with open(path, mode) as f:
            f.write(content)
        verb = 'appended' if append else 'wrote'
        return f"OK: {verb} {n_lines} lines to {path}"
    except Exception as e:
        stats['tools_ok'] -= 1
        stats['tools_no'] += 1
        return f"ERROR writing {path}: {e}"


def load_memory():
    if os.path.isfile(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return f.read().strip()
    return ""

def append_memory(text):
    with open(MEMORY_FILE, 'a') as f:
        f.write(f"\n\n{text.strip()}")
    print(f"  {c('memoria aggiornata:', DIM)} {MEMORY_FILE}\n")

def show_memory():
    mem = load_memory()
    if not mem:
        print(c("  memoria vuota\n", DIM))
        return
    print(f"\n  {c('MEMORIA', BOLD)}\n")
    for line in mem.splitlines():
        print(f"  {line}")
    print()

def build_system_prompt():
    base = PERSONAS.get(ACTIVE_PERSONA, PERSONAS['general'])
    ops = (
        "\n\n## OPERATIVE RULES — context efficiency"
        "\nYour context window is limited: every command output consumes it. Work parsimoniously."
        "\n- Before running, estimate if output will be large. If it might, filter AT THE SOURCE:"
        " `head -50 file`, `grep PATTERN file`, `cmd | head -30`, `wc -l`. Never `cat` large files."
        "\n- Prepend `timeout N` to commands that can block (network, scans, interactive prompts)."
        " Never launch foreground commands without a timeout."
        "\n- For multi-step tasks: write a short 3-5 point plan, then execute one step at a time."
        " Update the plan after each step. No command barrages."
        "\n- Use the MINIMUM command that produces the information you need now."
        " `cmd --help | head -30` instead of the full man. `ls dir | head` instead of recursive."
        "\n- Never repeat an identical failed command: change approach, options, or tool."
        " If output was truncated, narrow it with grep/sed/head instead of relaunching raw."
        "\n- If a flag, syntax or tool is unknown or gave an inexplicable error,"
        " use web_search IMMEDIATELY before guessing. Faster than 10 blind attempts."
        "\n- Reason concisely. Plan, then act. Aim for the answer with the fewest commands."
    )
    think = (
        "\n\n## REASONING DISCIPLINE — absolute constraint"
        "\n**MAIN ENEMY**: reasoning loop — reasoning about 2+ alternatives without executing any,"
        " or considering the same option 2+ times. This burns context window without producing real info."
        " Output from a failed command is worth more than 1000 tokens of prior reasoning."
        "\n\n**MANDATORY CONTRACT — every thinking turn MUST end with ONE of:**"
        "\n  A) a tool call (run_shell, web_search, fetch_url, read_file, write_file, workspace_write/read, or a security tool)"
        "\n  B) a final text response to the user"
        "\nOption C does not exist (thinking without action). If about to choose C, choose A."
        "\n\n**TRIPWIRE — these conditions trigger an IMMEDIATE tool call, no further reasoning:**"
        "\n- You already know the command to run → run it NOW. Don't think about it more."
        "\n- You have a path, PID, env var to verify → use run_shell NOW."
        "\n- You've considered the same option 2+ times → take the most likely, execute NOW."
        "\n- You've done 3+ reasoning steps without a tool call → run `echo 'CP: [state]'` NOW."
        "\n- A command failed with a non-obvious error → diagnose NOW."
        "\n- You don't know the exact syntax of a flag → use web_search NOW, don't guess."
        "\n\n**YOUR THINKING IS NOT SAVED IN CONTEXT.** If truncated it's lost."
        " Every thought that doesn't culminate in a tool call is burned context window."
    )
    workspace = (
        "\n\n## WORKSPACE — persistent memory between turns"
        "\n- workspace_write/read: .md files in ~/.loki_workspace/ that survive between turns."
        "\n- Your thinking is discarded after each turn. Files are not."
        "\n- RULE: if you find yourself thinking the same thing a second time → write to workspace instead."
        "\n- plan.md: current step plan (update after each completed step)"
        "\n- failures.md: what you tried that did NOT work (read BEFORE retrying)"
        "\n- ideas.md: options considered, pros/cons"
        "\n- notes.md: observations, important output to remember"
        "\n- scratch.md: free drafts"
        "\n- TRIPWIRE: considering 3+ options? → workspace_write to ideas.md NOW, then decide."
    )
    plan_path = os.path.join(WORKSPACE_DIR, "plan.md")
    plan_section = ""
    try:
        if os.path.exists(plan_path):
            with open(plan_path, 'r', encoding='utf-8') as _f:
                _plan = _f.read().strip()
            if _plan:
                plan_section = f"\n\n## CURRENT PLAN (from workspace)\n{_plan}"
    except Exception:
        pass
    mem = load_memory()
    if mem and len(mem) > MEMORY_INJECT_MAX_CHARS:
        # Inject only the tail (most recent summaries) to cap system prompt size.
        # Full file stays on disk; only token cost is reduced.
        mem = f"[...older memory truncated: {len(mem) - MEMORY_INJECT_MAX_CHARS} chars...]\n\n" + mem[-MEMORY_INJECT_MAX_CHARS:]
    mem_section = f"\n\n## PERSISTENT MEMORY\n{mem}" if mem else ""
    return base + ops + think + workspace + mem_section + plan_section

def _find_turn_start(messages, idx):
    """Sposta idx all'indietro finche non atterra su un messaggio 'user'.

    Evita che 'recent' inizi con un 'tool' orfano (il cui assistant/tool_call
    e finito in to_compress) o con un assistant risposta a un tool_call ora
    perso. Un 'user' e un confine di turno pulito.
    """
    while idx > 1 and messages[idx]['role'] != 'user':
        idx -= 1
    return idx


def _fmt_msg_for_summary(msg, txt_lim=2000, tool_lim=1500):
    role = msg['role']
    content = msg.get('content', '') or ''
    if role == 'assistant' and msg.get('tool_calls'):
        parts = []
        if content:
            parts.append(content[:txt_lim])
        for tc in msg['tool_calls']:
            fn = tc.get('function', {}) if isinstance(tc, dict) else {}
            name = fn.get('name', '?')
            args = fn.get('arguments', {}) or {}
            cmd  = args.get('command', '') if isinstance(args, dict) else str(args)
            parts.append(f"→ tool_call {name}({cmd})")
        return "[assistant]: " + "\n".join(parts)
    if role == 'tool':
        body = content
        if len(body) > tool_lim:
            half = tool_lim // 2
            omitted = len(body) - tool_lim
            body = f"{body[:half]}\n[... {omitted} char omessi ...]\n{body[-half:]}"
        return f"[tool_output]: {body}"
    return f"[{role}]: {content[:txt_lim]}"


def _rotate_memory_summaries():
    """Mantiene al piu MEMORY_MAX_SUMMARIES riassunti di sessione nel file.

    Le note manuali dell'utente (`/remember`) restano intatte in cima.
    Applica anche un hard cap in byte via loki_mem.enforce_hard_cap: se
    dopo la rotazione il file e ancora troppo grosso (tante note manuali,
    riassunti giganti), taglia dalla cima con marker esplicito.
    """
    loki_mem.rotate_summaries(MEMORY_FILE, max_summaries=MEMORY_MAX_SUMMARIES)
    loki_mem.enforce_hard_cap(MEMORY_FILE)


def detect_max_ctx():
    """Interroga Ollama per la context length reale del modello.

    Se la chiamata fallisce (Ollama down, modello non trovato) lascia il default.
    """
    global MAX_CTX
    try:
        info = ollama.show(MODEL)
        model_info = info.get('modelinfo') or info.get('model_info') or {}
        for k, v in model_info.items():
            if k.endswith('.context_length') and isinstance(v, int) and v > 0:
                MAX_CTX = v
                return
    except Exception:
        pass


def estimate_context_tokens(messages):
    """Stima grezza dei token nel contesto (~4 char/token).

    Serve alla compressione pre-emptiva: decidere PRIMA di inviare invece di
    scoprire a posteriori (done_reason=length) che il prompt era troppo grande.
    stats['ctx_used'] non basta perche' e' il conteggio del turno PRECEDENTE.
    """
    chars = 0
    for m in messages:
        chars += len(m.get('content') or '')
        chars += len(m.get('thinking') or '')
        for tc in (m.get('tool_calls') or []):
            fn   = tc.get('function', {}) if isinstance(tc, dict) else {}
            args = fn.get('arguments', {}) or {}
            chars += len(str(args))
    return chars // 4


def _trim_old_messages(messages):
    """Free memory every turn without calling the LLM.

    For messages older than KEEP_RECENT_MSG:
    - Strips thinking blocks (they are never needed after the turn ends)
    - Truncates tool outputs to TOOL_OUTPUT_KEEP_CHARS (head + tail)

    Returns a new list; the original is not mutated.
    """
    if len(messages) <= KEEP_RECENT_MSG + 1:
        return messages
    cutoff = max(1, len(messages) - KEEP_RECENT_MSG)
    result = list(messages)
    for i in range(1, cutoff):
        m = result[i]
        changed = False
        # Drop thinking — useless after the turn
        if m.get('thinking'):
            m = {k: v for k, v in m.items() if k != 'thinking'}
            changed = True
        # Truncate large tool outputs
        if m.get('role') == 'tool':
            content = m.get('content', '')
            if len(content) > TOOL_OUTPUT_KEEP_CHARS:
                head = content[:TOOL_OUTPUT_KEEP_CHARS // 2]
                tail = content[-(TOOL_OUTPUT_KEEP_CHARS // 4):]
                omitted = len(content) - TOOL_OUTPUT_KEEP_CHARS // 2 - TOOL_OUTPUT_KEEP_CHARS // 4
                m = {**m, 'content': f"{head}\n[...{omitted} chars trimmed...]\n{tail}"}
                changed = True
        if changed:
            result[i] = m
    return result


def compress_context(messages):
    if len(messages) <= 3:
        print(c("  conversazione troppo corta da comprimere\n", DIM))
        return messages

    keep_start = max(1, len(messages) - KEEP_RECENT_MSG)
    keep_start = _find_turn_start(messages, keep_start)
    to_compress = messages[1:keep_start]
    if not to_compress:
        print(c("  niente da comprimere\n", DIM))
        return messages

    MAX_TRANSCRIPT = 12000  # piu spazio: ora includiamo anche tool_output
    raw_transcript = "\n\n".join(_fmt_msg_for_summary(m) for m in to_compress)
    if len(raw_transcript) > MAX_TRANSCRIPT:
        half = MAX_TRANSCRIPT // 2
        transcript = (
            raw_transcript[:half]
            + f"\n\n[... {len(raw_transcript) - MAX_TRANSCRIPT} caratteri omessi ...]\n\n"
            + raw_transcript[-half:]
        )
    else:
        transcript = raw_transcript

    BAR_W   = 24
    EST_MAX = 1500   # token stimati per il riassunto (tetto barra)
    SPIN    = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

    def _render_bar(chars, done=False):
        approx = max(chars // 4, 1)
        filled = min(BAR_W, int(BAR_W * approx / EST_MAX)) if not done else BAR_W
        bar    = '█' * filled + '░' * (BAR_W - filled)
        color  = GREEN if done else ORANGE
        return f"\r  {c('◎ compressione', PURPLE)}  {DGRAY}[{R}{color}{bar}{R}{DGRAY}]{R}  {c(f'~{approx}tk', DGRAY)}  "

    # mostra spinner mentre il modello elabora il prompt (prima del primo token)
    spin_i     = [0]
    first_seen = [False]

    def _spin():
        ch = SPIN[spin_i[0] % len(SPIN)]
        write(f"\r  {c('◎ compressione', PURPLE)}  {DGRAY}{ch} elaborando...{R}        ")
        spin_i[0] += 1

    import threading
    stop_spin = threading.Event()

    def _spinner_thread():
        while not stop_spin.is_set():
            _spin()
            stop_spin.wait(0.12)

    write(f"\n")
    t_spin = threading.Thread(target=_spinner_thread, daemon=True)
    t_spin.start()

    summary_parts = []
    chars = 0
    try:
        stream = ollama.chat(
            model=MODEL,
            messages=[{
                "role": "system",
                "content": "Sei un assistente che riassume conversazioni tra utente e shell agent in modo denso e preciso in italiano."
            }, {
                "role": "user",
                "content": (
                    "Riassumi questa conversazione in un blocco markdown conciso. "
                    "I blocchi [tool_call] indicano comandi eseguiti, i blocchi "
                    "[tool_output] il loro output. Preserva: fatti tecnici scoperti, "
                    "file/percorsi/host/porte incontrati, decisioni prese, obiettivi "
                    "dell'utente non ancora completati, ed eventuali errori rilevanti. "
                    f"Ignora chiacchiere e conferme banali:\n\n{transcript}"
                )
            }],
            stream=True,
            # compress e un one-shot su transcript gia troncato a 12000 char:
            # non serve tutto MAX_CTX, un tetto piccolo evita che Ollama
            # rialloci un KV cache enorme per un compito breve.
            options={
                'num_ctx':     min(COMPRESS_CTX, MAX_CTX),
                'num_predict': 1500,
                'temperature': 0.3,
            },
            keep_alive=KEEP_ALIVE,
        )
        for chunk in stream:
            piece = (chunk.get('message', {}).get('content') or '')
            if piece:
                if not first_seen[0]:
                    stop_spin.set()
                    first_seen[0] = True
                summary_parts.append(piece)
                chars += len(piece)
                write(_render_bar(chars))
    except KeyboardInterrupt:
        stop_spin.set()
        write(f"\r  {c('⚠ compressione interrotta — contesto invariato', YELLOW)}        \n\n")
        return messages
    except Exception as e:
        stop_spin.set()
        write(f"\r  {c(f'errore compressione: {e}', RED)}        \n\n")
        return messages

    stop_spin.set()

    write(_render_bar(chars, done=True))
    write(f"\r  {c('◎ compressione', PURPLE)}  {c('[' + '█' * BAR_W + ']', GREEN)}  {c('✓ fatto', GREEN)}        \n")

    summary = "".join(summary_parts)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    append_memory(f"### Sessione compressa {ts}\n{summary}")
    _rotate_memory_summaries()

    recent = messages[keep_start:]
    recent_clean = []
    for m in recent:
        if m.get('role') == 'assistant' and m.get('thinking'):
            m = {k: v for k, v in m.items() if k != 'thinking'}
        recent_clean.append(m)
    new_messages = [{"role": "system", "content": build_system_prompt()}] + recent_clean
    print(f"  {c('✓ salvato in memoria', GREEN)}  {c(f'{len(to_compress)} messaggi compressi', GRAY)}\n")
    return new_messages

def _session_path(name):
    safe = re.sub(r'[^\w\-]', '_', name)
    return os.path.join(SESSIONS_DIR, f"{safe}.json")

def _generate_session_title(messages):
    """Ask the LLM for a 4-5 word dash-slug title. Falls back to timestamp."""
    samples = []
    for m in messages[1:]:
        if m.get('role') in ('user', 'assistant') and m.get('content'):
            samples.append(m['content'][:200])
        if len(samples) >= 6:
            break
    if not samples:
        return datetime.now().strftime("%Y%m%d_%H%M%S")
    transcript = "\n".join(samples)
    try:
        resp = ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": (
                "Generate a 4-5 word title for this conversation. "
                "Reply with ONLY the title: lowercase english words separated by dashes, "
                "no punctuation, no quotes, no explanation.\n"
                "Good examples: debug-nginx-timeout, setup-python-venv, analyze-auth-logs\n\n"
                f"Conversation:\n{transcript}"
            )}],
            stream=False,
            options={'num_ctx': 4096, 'num_predict': 20, 'temperature': 0.2},
            keep_alive=KEEP_ALIVE,
        )
        raw = (resp.get('message', {}).get('content') or '').strip().lower()
        slug = re.sub(r'[^\w\s-]', '', raw)
        slug = re.sub(r'[\s_]+', '-', slug.strip())[:50]
        if slug and re.search(r'[a-z]', slug):
            return slug
    except Exception:
        pass
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def save_session(messages, name=None, auto_title=False):
    if not name:
        if auto_title and len(messages) > 2:
            write(f"\n  {c('◎ generating title...', PURPLE)}  ")
            name = _generate_session_title(messages)
            write(f"\r  {c('◎ title:', PURPLE)} {c(name, ORANGE)}            \n")
        else:
            name = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        'saved_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'model':    MODEL,
        'persona':  ACTIVE_PERSONA,
        'messages': [loki_persist._serialize_msg(m) for m in messages[1:]],
    }
    path = _session_path(name)
    with open(path, 'w') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n  {c('session saved:', DIM)} {c(name, ORANGE)}  {c(path, DGRAY)}\n")
    return name

def _replay_message(m):
    """Ristampa un singolo messaggio salvato nel formato compatto della chat."""
    role = m.get('role')
    content = m.get('content', '') or ''
    if role == 'user':
        first, *rest = content.splitlines() or ['']
        print(f"\n  {ORANGE}❯{R} {first}")
        for line in rest:
            print(f"    {line}")
    elif role == 'assistant':
        if content:
            first, *rest = content.splitlines() or ['']
            print(f"\n  {c('●', ORANGE)} {first}")
            for line in rest:
                print(f"    {line}")
        for tc in m.get('tool_calls') or []:
            fn = tc.get('function', {}) if isinstance(tc, dict) else {}
            args = fn.get('arguments', {}) or {}
            cmd  = args.get('command', '') if isinstance(args, dict) else str(args)
            print(f"\n  {c('⏺', GREEN)} {c('Bash', BOLD)}  {c(cmd, CYAN)}  {c('[replay]', DIM)}")
    elif role == 'tool':
        lines = content.splitlines()
        w = max(20, term_width() - 8)
        print(f"     {c('┌─', DGRAY)} {c('output (replay)', GRAY)} {c('─' * max(1, w - 20), DGRAY)}")
        for line in lines[:15]:
            print(f"     {c('│', DGRAY)} {line}")
        if len(lines) > 15:
            print(f"     {c(f'     ... {len(lines) - 15} righe omesse', DIM)}")
        print(f"     {c('└' + '─' * w, DGRAY)}")


def resume_session(arg, messages):
    """/resume — se `arg` e vuoto: elenca. Altrimenti: carica + ristampa storia.
    Ritorna la nuova lista messaggi da mettere in state, oppure None."""
    files = sorted(
        [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.json')],
        reverse=True,
    )
    entries = [f[:-5] for f in files]

    # -- nessun argomento: elenca --
    if not arg:
        if not entries:
            print(c("\n  nessuna sessione salvata\n", DIM))
            return None
        print()
        print(f"  {c('SESSIONI SALVATE', BOLD)}")
        for i, name in enumerate(entries, 1):
            path = _session_path(name)
            try:
                with open(path) as f:
                    data = json.load(f)
                n_msg = len([m for m in data.get('messages', []) if m['role'] == 'user'])
                saved = data.get('saved_at', '?')
            except Exception:
                n_msg, saved = 0, '?'
            print(f"    {c(str(i).rjust(2), ORANGE)}  {c(name.ljust(24), BOLD)}  "
                  f"{c(saved, DIM)}  {c(f'{n_msg} msg', GRAY)}")
        print(f"\n  {c('per riprenderne una:', DIM)} {c('/resume <numero|nome>', ORANGE)}\n")
        return None

    # -- con argomento: carica --
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(entries):
            arg = entries[idx]
        else:
            print(c(f"\n  numero non valido: {arg}\n", RED))
            return None
    path = _session_path(arg)
    if not os.path.isfile(path):
        print(c(f"\n  sessione non trovata: {arg}\n", RED))
        return None
    with open(path) as f:
        data = json.load(f)
    loaded = data.get('messages', [])
    n_user = len([m for m in loaded if m['role'] == 'user'])

    # Pulisce l'output area e ristampa banner + storia.
    if _PINNED_MODE:
        with _output_lock:
            _output_chunks.clear()
    fill = max(1, term_width() - 4)
    print(f"\n  {c('◈', GREEN)} {c('Sessione ripresa:', DIM)} {c(arg, ORANGE)}  "
          f"{c(f'{n_user} messaggi utente', GRAY)}")
    print(f"  {c('─' * fill, GRAY)}")
    for m in loaded:
        _replay_message(m)
    print(f"\n  {c('─' * fill, GRAY)}")
    print(f"  {c('▸ continua a scrivere qui sotto', DIM)}\n")

    return messages[:1] + loaded  # system prompt + messaggi ripresi

def parse_slash(text, messages=None):
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    if cmd in ('/exit', '/quit'):
        return 'exit', None
    if cmd == '/help':
        show_help()
        return 'handled', None
    if cmd in ('/clear', '/reset'):
        return 'clear', None
    if cmd == '/model':
        global MODEL
        if arg.strip():
            MODEL = arg.strip()
            print(f"  {c('model switched to:', DIM)} {c(MODEL, ORANGE)}\n")
        else:
            print(f"  {c('model:', DIM)} {MODEL}\n")
        return 'handled', None
    if cmd == '/persona':
        global ACTIVE_PERSONA
        arg = arg.strip().lower()
        if not arg:
            print()
            print(f"  {c('PERSONAS', BOLD)}")
            for name, desc in PERSONAS.items():
                marker = c('►', ORANGE) if name == ACTIVE_PERSONA else ' '
                first_sentence = desc.split('.')[0] + '.'
                print(f"    {marker} {c(name, BOLD if name == ACTIVE_PERSONA else GRAY):<18} {c(first_sentence[:60], DIM)}")
            print(f"\n  {c('current:', DIM)} {ACTIVE_PERSONA}")
            print(f"  {c('switch:', DIM)} /persona <name>\n")
        elif arg in PERSONAS:
            ACTIVE_PERSONA = arg
            if messages and messages[0]['role'] == 'system':
                messages[0]['content'] = build_system_prompt()
            print(f"  {c('persona:', DIM)} {c(ACTIVE_PERSONA, ORANGE)}\n")
        else:
            opts = ', '.join(PERSONAS.keys())
            print(c(f"  unknown persona: {arg}. Available: {opts}\n", RED))
        return 'handled', None
    if cmd == '/tools':
        print()
        print(f"  {c('AVAILABLE TOOLS', BOLD)}")
        for tool in get_active_tools():
            fn = tool['function']
            name = fn['name']
            desc = fn['description'].split('.')[0]
            props = fn['parameters']['properties']
            required = fn['parameters'].get('required', [])
            print(f"\n    {c(name, ORANGE)}")
            print(f"      {c(desc, GRAY)}")
            for pname, pdef in props.items():
                req_mark = '*' if pname in required else ' '
                ptype = pdef.get('type', '?')
                pdesc = pdef.get('description', '')[:60]
                print(f"      {c(req_mark + pname, CYAN):<18} {c(ptype, DIM):<10} {c(pdesc, GRAY)}")
        print()
        return 'handled', None
    if cmd == '/cwd':
        print(f"  {c('cwd:', DIM)} {os.getcwd()}\n")
        return 'handled', None
    if cmd == '/auto':
        stats['auto_approve'] = True
        print(f"  {c('auto approve:', DIM)} {c('ON', GREEN)}\n")
        return 'handled', None
    if cmd == '/manual':
        stats['auto_approve'] = False
        print(f"  {c('auto approve:', DIM)} {c('OFF', GRAY)}\n")
        return 'handled', None
    if cmd == '/ac':
        stats['auto_continue'] = not stats['auto_continue']
        state = c('ON', GREEN) if stats['auto_continue'] else c('OFF', GRAY)
        print(f"  {c('auto-continue:', DIM)} {state}\n")
        return 'handled', None
    if cmd == '/remember':
        if arg.strip():
            append_memory(arg.strip())
        else:
            print(c("  uso: /remember <testo>\n", GRAY))
        return 'handled', None
    if cmd == '/memory':
        show_memory()
        return 'handled', None
    if cmd == '/compress':
        if messages:
            return 'compress', messages
        return 'handled', None
    if cmd == '/trim':
        if messages:
            before = estimate_context_tokens(messages)
            trimmed = _trim_old_messages(messages)
            after = estimate_context_tokens(trimmed)
            saved = max(0, before - after)
            msg_count = len([m for m in messages if m.get('role') == 'tool'])
            print(f"  {c('✓ trim done', GREEN)}  {c(f'~{saved} tokens freed', GRAY)}  "
                  f"{c(f'{msg_count} tool msgs processed', DIM)}\n")
            return 'trim', trimmed
        return 'handled', None
    if cmd == '/think':
        stats['show_thinking'] = not stats['show_thinking']
        state = c('ON', GREEN) if stats['show_thinking'] else c('OFF', GRAY)
        print(f"  {c('mostra thinking:', DIM)} {state}\n")
        return 'handled', None
    if cmd in ('/history', '/cost'):
        show_stats()
        return 'handled', None
    if cmd == '/last':
        show_last()
        return 'handled', None
    if cmd == '/resume':
        if messages is not None:
            arg = arg.strip()
            if not arg and _PINNED_MODE:
                # In fullscreen: attiva il picker interattivo
                return 'picker', None
            result = resume_session(arg, messages)
            if result is not None:
                return 'resume', result
        return 'handled', None
    if cmd == '/resume-last':
        payload = loki_persist.load_autosave_if_fresh(SESSIONS_DIR)
        if payload is None:
            age = loki_persist.autosave_age_seconds(SESSIONS_DIR)
            if age is None:
                print(c("  nessun autosave presente\n", DIM))
            else:
                hrs = age // 3600
                print(c(f"  autosave troppo vecchio ({hrs}h fa) — ignorato\n", DIM))
            return 'handled', None
        age = loki_persist.autosave_age_seconds(SESSIONS_DIR) or 0
        mins_ago = age // 60
        raw = payload.get('messages', [])
        new_msgs = [{"role": "system", "content": build_system_prompt()}] + raw
        n_user = sum(1 for m in raw if m.get('role') == 'user')
        print(f"\n  {c('◈ autosave riesumato', GREEN)}  "
              f"{c(f'{n_user} messaggi utente · {mins_ago}m fa', GRAY)}\n")
        return 'resume', new_msgs
    if cmd == '/hw':
        if HW is None:
            print(c("  HW non ancora rilevato\n", DIM))
        else:
            line = loki_hw.hw_line(HW, stats['working_ctx'], MAX_CTX)
            print(f"\n  {c('HARDWARE', BOLD)}")
            print(f"    {line}")
            print(f"    {c('keep_alive:', DIM)} {KEEP_ALIVE}   "
                  f"{c('compress_ctx:', DIM)} {min(COMPRESS_CTX, MAX_CTX)}   "
                  f"{c('memory cap:', DIM)} {loki_mem.MEMORY_HARD_CAP_BYTES // 1024} KB")
            print()
        return 'handled', None
    if cmd == '/delete':
        arg = arg.strip()
        if not arg:
            print(c("  uso: /delete <nome|numero>\n", GRAY))
            return 'handled', None
        entries = [f[:-5] for f in sorted(os.listdir(SESSIONS_DIR), reverse=True) if f.endswith('.json')]
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(entries):
                arg = entries[idx]
            else:
                print(c(f"  numero non valido: {arg}\n", RED))
                return 'handled', None
        path = _session_path(arg)
        if not os.path.isfile(path):
            print(c(f"  sessione non trovata: {arg}\n", RED))
        else:
            os.remove(path)
            print(f"  {c('eliminata:', DIM)} {c(arg, ORANGE)}\n")
        return 'handled', None
    if cmd == '/save':
        if messages:
            explicit = arg.strip()
            save_session(messages, explicit or None, auto_title=not explicit)
        else:
            print(c("  nessun messaggio da salvare\n", DIM))
        return 'handled', None
    if cmd == '/clone':
        cparts = arg.strip().split(maxsplit=1)
        if len(cparts) < 2:
            print(c("  uso: /clone <sorgente> <nuovo_nome>\n", GRAY))
            return 'handled', None
        src_arg, dst_name = cparts[0], cparts[1].strip()
        entries = [f[:-5] for f in sorted(os.listdir(SESSIONS_DIR), reverse=True) if f.endswith('.json')]
        if src_arg.isdigit():
            idx = int(src_arg) - 1
            if 0 <= idx < len(entries):
                src_arg = entries[idx]
            else:
                print(c(f"  numero non valido: {src_arg}\n", RED))
                return 'handled', None
        src_path = _session_path(src_arg)
        dst_path = _session_path(dst_name)
        if not os.path.isfile(src_path):
            print(c(f"  sessione non trovata: {src_arg}\n", RED))
        elif os.path.isfile(dst_path):
            print(c(f"  esiste già una sessione con nome: {dst_name}\n", RED))
        else:
            import shutil
            shutil.copy2(src_path, dst_path)
            print(f"  {c('clonata:', DIM)} {c(src_arg, GRAY)} {c('→', DGRAY)} {c(dst_name, ORANGE)}\n")
        return 'handled', None
    if cmd == '/search':
        term = arg.strip().lower()
        if not term:
            print(c("  usage: /search <keyword>\n", GRAY))
            return 'handled', None
        matches = []
        if os.path.isdir(SESSIONS_DIR):
            for fname in sorted(os.listdir(SESSIONS_DIR), reverse=True):
                if not fname.endswith('.json'):
                    continue
                fpath = os.path.join(SESSIONS_DIR, fname)
                try:
                    with open(fpath) as sf:
                        data = json.load(sf)
                    found_in = None
                    for m in data.get('messages', []):
                        if term in (m.get('content') or '').lower():
                            found_in = (m.get('content') or '')[:100].replace('\n', ' ')
                            break
                    if found_in is not None:
                        matches.append((fname[:-5], data.get('saved_at', '?'), found_in))
                except Exception:
                    pass
        if not matches:
            print(c(f"\n  no sessions found for: {arg}\n", DIM))
        else:
            print(f"\n  {c('SEARCH RESULTS', BOLD)} for {c(arg, ORANGE)}  {c(f'{len(matches)} found', GRAY)}")
            for name, saved, snippet in matches:
                print(f"\n    {c(name, BOLD)}  {c(saved, DIM)}")
                print(f"    {c(snippet[:80], DGRAY)}")
            print(f"\n  {c('load one:', DIM)} {c('/resume <name>', ORANGE)}\n")
        return 'handled', None
    if cmd == '/fetch':
        url = arg.strip()
        if not url:
            print(c("  uso: /fetch <url>\n", GRAY))
        else:
            result = run_fetch_url(url)
            print(f"\n{result}\n")
        return 'handled', None
    if cmd == '/img':
        path = os.path.expanduser(arg.strip().strip('"\''))
        if os.path.isfile(path):
            stats['pending_images'].append(path)
            print(f"  {c('immagine allegata:', DIM)} {path}")
            print(f"  {c('sara inviata col prossimo messaggio', GRAY)}\n")
        else:
            print(c(f"  file non trovato: {path}\n", RED))
        return 'handled', None
    print(c(f"  comando sconosciuto: {cmd}\n", RED))
    return 'handled', None

def build_session(on_submit=None):
    """Costruisce la PromptSession.

    Se `on_submit` e passato (modalita async pinned), Enter chiama la callback
    e resetta il buffer senza far ritornare prompt_async(): cosi il prompt
    resta sempre visibile con la sua toolbar. Se None, comportamento legacy
    sincrono.
    """
    kb = KeyBindings()

    @kb.add('enter')
    def _(event):
        buf = event.current_buffer
        if buf.text.endswith('\\'):
            buf.delete_before_cursor(1)
            buf.insert_text('\n')
            return
        if on_submit is None:
            buf.validate_and_handle()
            return
        text = buf.text.strip()
        if not text:
            return
        buf.reset()
        on_submit(text)

    def bottom_toolbar():
        elapsed  = datetime.now() - stats['start_time']
        mins     = int(elapsed.total_seconds() // 60)
        secs     = int(elapsed.total_seconds() % 60)
        sep      = f"  {DGRAY}│{R}  "
        mode     = c('⏺ AUTO',   ORANGE) if stats['auto_approve']  else c('⏸ MANUAL', GRAY)
        think    = f"{PURPLE}◆ THINK{R}" if stats['show_thinking'] else f"{DGRAY}◇ THINK{R}"
        ac       = f"{GREEN}AC{R}"      if stats['auto_continue'] else f"{DGRAY}AC{R}"
        n_imgs   = len(stats['pending_images'])
        imgs     = f"{sep}{ORANGE}⬡ img:{n_imgs}{R}" if n_imgs else ''
        tk       = f"  {DGRAY}tk:{stats['last_tokens']}{R}" if stats['last_tokens'] else ''
        if stats['ctx_used']:
            pct = int(100 * stats['ctx_used'] / stats['working_ctx'])
            ctx_col = RED if pct >= 80 else (YELLOW if pct >= 60 else DGRAY)
            ctx = f"  {ctx_col}ctx {pct}%{R}"
        else:
            ctx = ''
        model_short = MODEL.split('/')[-1][:28]
        return ANSI(
            f"  {mode}{sep}"
            f"{model_short}{sep}"
            f"{mins:02d}m {secs:02d}s{sep}"
            f"✉ {stats['messages']}  "
            f"{GREEN}✓ {stats['tools_ok']}{R}  "
            f"{RED}✗ {stats['tools_no']}{R}"
            f"{tk}{ctx}{imgs}{sep}"
            f"{think}  {ac}  "
        )

    toolbar_style = Style.from_dict({
        'bottom-toolbar': 'bg:#0e0e0e fg:#ffffff bold noreverse',
    })

    return PromptSession(
        history=InMemoryHistory(),
        multiline=True,
        key_bindings=kb,
        completer=SlashOnlyCompleter(),
        complete_while_typing=True,
        auto_suggest=AutoSuggestFromHistory(),
        bottom_toolbar=bottom_toolbar,
        mouse_support=False,
        style=toolbar_style,
        refresh_interval=1.0,   # aggiorna la toolbar (timer, ctx%) ogni secondo
    )

class WordFlow:
    """Word-aware wrapper per output streaming.

    Bufferizza i caratteri fino al confine di parola (spazio/newline), poi
    decide se la parola entra nella riga corrente: se no, va a capo PRIMA di
    scriverla. Parole piu lunghe di una riga intera vengono spezzate a forza.
    """
    def __init__(self, width, indent_cols, line_start, newline_prefix=None):
        self.width         = width
        self.indent_cols   = indent_cols
        self.line_start    = line_start
        self.newline_prefix = newline_prefix or (lambda is_wrap: '')
        self.col           = indent_cols
        self.word          = ''

    def _newline(self, is_wrap):
        write(self.newline_prefix(is_wrap) + '\n' + self.line_start)
        self.col = self.indent_cols

    def _emit_word(self):
        w = self.word
        if not w:
            return
        self.word = ''
        if self.col + len(w) <= self.width:
            write(w); self.col += len(w); return
        line_room = self.width - self.indent_cols
        if len(w) <= line_room:
            self._newline(True)
            write(w); self.col += len(w); return
        while w:
            space = max(1, self.width - self.col)
            piece, w = w[:space], w[space:]
            write(piece); self.col += len(piece)
            if w:
                self._newline(True)

    def feed(self, ch):
        if ch == '\n':
            self._emit_word()
            self._newline(False)
        elif ch == ' ':
            self._emit_word()
            if self.col >= self.width:
                self._newline(True)
            else:
                write(' '); self.col += 1
        else:
            self.word += ch

    def finish(self):
        self._emit_word()


def stream_response(messages):
    thinking_buf   = []
    content_buf    = []
    tool_calls_buf = []
    thinking_started = False
    thinking_ended   = False
    content_started  = False
    done_reason      = 'stop'
    eval_count       = 0
    live_chars       = 0

    WRAP_W      = term_width() - 4
    WRAP_INDENT = "    "

    def tk_label():
        approx = max(live_chars // 4, 1)
        return f" {DGRAY}~{approx}tk{R}"

    content_flow = WordFlow(
        width=WRAP_W,
        indent_cols=4,
        line_start=WRAP_INDENT,
        newline_prefix=lambda is_wrap: tk_label() if is_wrap else '',
    )
    think_flow = WordFlow(
        width=WRAP_W,
        indent_cols=4,
        line_start=f"  {DGRAY}│{R} {DIM}",
        newline_prefix=lambda is_wrap: f"{R}{tk_label()}",
    )

    try:
        stream = ollama.chat(
            model=MODEL,
            messages=prepare_messages_for_api(messages),
            tools=get_active_tools(),
            think=True,
            stream=True,
            options={'num_ctx': stats['working_ctx']},
            keep_alive=KEEP_ALIVE,
        )
    except Exception as e:
        print(c(f"  Errore Ollama: {e}\n", RED))
        return None

    try:
        for chunk in stream:
            # Ctrl+C dall'app: interrompiamo lo stream come se fosse un
            # KeyboardInterrupt vero, cosi riusiamo il branch except sotto
            # che chiude i blocchi pending e salva il contenuto parziale.
            if _cancel_event.is_set():
                raise KeyboardInterrupt

            msg = chunk.get('message', {})

            thinking_chunk = msg.get('thinking') or ''
            content_chunk  = msg.get('content')  or ''
            tool_chunk     = msg.get('tool_calls') or []

            if thinking_chunk and stats['show_thinking']:
                if not thinking_started:
                    label = "thinking"
                    fill = max(1, term_width() - 10 - len(label))
                    write(f"\n  {PURPLE}◆{R} {PURPLE}{label}{R} {DGRAY}{'─' * fill}{R}\n  {DGRAY}│{R} {DIM}")
                    thinking_started = True
                    think_flow.col = 4
                for ch in thinking_chunk:
                    live_chars += 1
                    think_flow.feed(ch)
                thinking_buf.append(thinking_chunk)
            elif thinking_chunk:
                thinking_buf.append(thinking_chunk)

            if content_chunk or tool_chunk:
                if thinking_started and not thinking_ended:
                    think_flow.finish()
                    write(f"{R}\n  {DGRAY}{'─' * (term_width() - 4)}{R}\n")
                    thinking_ended = True

            if content_chunk:
                if not content_started:
                    write(f"\n  {ORANGE}●{R} ")
                    content_started = True
                    content_flow.col = 4
                for ch in content_chunk:
                    live_chars += 1
                    content_flow.feed(ch)
                content_buf.append(content_chunk)

            if tool_chunk:
                # Il modello sta emettendo una chiamata a run_shell: lo spinner
                # passa a "bashing..." per segnalare che stiamo per eseguire
                # (o gia' eseguendo) codice shell.
                _bashing_event.set()
                for tc in tool_chunk:
                    tool_calls_buf.append(tc)

            if chunk.get('done'):
                done_reason = chunk.get('done_reason', 'stop')
                eval_count  = chunk.get('eval_count', 0)
                stats['last_tokens'] = eval_count
                stats['ctx_used']    = chunk.get('prompt_eval_count', stats['ctx_used'])

    except KeyboardInterrupt:
        if thinking_started and not thinking_ended:
            think_flow.finish()
            write(f"{R}\n  {DGRAY}{'─' * (term_width() - 4)}{R}\n")
        elif content_started:
            content_flow.finish()
            write("\n")
        write(f"\n  {GRAY}⚠ interrotto{R}\n\n")
        return {
            'role':       'assistant',
            'content':    ''.join(content_buf),
            'thinking':   ''.join(thinking_buf),
            'tool_calls': [],
        }

    if thinking_started and not thinking_ended:
        think_flow.finish()
        write(f"{R}\n  {DGRAY}{'─' * (term_width() - 4)}{R}\n")
    if content_started:
        content_flow.finish()
        write("\n\n")

    return {
        'role':        'assistant',
        'content':     ''.join(content_buf),
        'thinking':    ''.join(thinking_buf),
        'tool_calls':  tool_calls_buf,
        'done_reason': done_reason,
    }

def prepare_messages_for_api(messages):
    """Strips thinking from old assistant messages to reclaim context tokens."""
    assistant_indices = [i for i, m in enumerate(messages) if m.get('role') == 'assistant']
    keep_thinking_from = assistant_indices[-2] if len(assistant_indices) >= 2 else -1
    result = []
    for i, m in enumerate(messages):
        if m.get('role') == 'assistant' and m.get('thinking') and i < keep_thinking_from:
            m = {k: v for k, v in m.items() if k != 'thinking'}
        result.append(m)
    return result

def build_continue_message(last_msg):
    """Builds a smarter 'continua' that tells the model where it left off."""
    thinking = (last_msg.get('thinking') or '').strip()
    content  = (last_msg.get('content')  or '').strip()
    if content:
        tail = content[-500:]
        return (
            "continua la risposta interrotta dal limite di token. "
            f"Stavi scrivendo: «…{tail}» — "
            "prosegui senza ripetere quanto già scritto."
        )
    if thinking:
        thinking_tail = "\n".join(thinking.splitlines()[-12:])
        return (
            "il tuo ragionamento è stato interrotto dal limite di token.\n"
            f"Ultimi pensieri:\n```\n{thinking_tail}\n```\n"
            "Completa il ragionamento e produci la risposta finale."
        )
    return "continua"

def _run_turn(state):
    """Esegue il turno completo (streaming + tool loop) nell'executor thread.

    Chiama stream_response e, se il modello ha prodotto tool_calls, esegue i
    tool e re-invoca stream_response, come faceva il vecchio chat_loop sincrono.
    Tutti gli output passano per print()/write() e patch_stdout li mostra sopra
    il prompt pinnato.
    """
    messages = state['messages']

    # Dynamic COMPRESS_PREEMPT: lower the threshold as the session ages.
    # After 1h → 0.55, after 2h → 0.45. Prevents late-session lag buildup.
    _session_age_h = (datetime.now() - stats['start_time']).total_seconds() / 3600
    if _session_age_h >= 2:
        _eff_preempt = 0.45
    elif _session_age_h >= 1:
        _eff_preempt = 0.55
    else:
        _eff_preempt = COMPRESS_PREEMPT

    # Compressione pre-emptiva: intervieni PRIMA di inviare se il contesto stimato
    # supera _eff_preempt del working_ctx, invece di aspettare done_reason=length.
    est = estimate_context_tokens(messages)
    if est >= int(stats['working_ctx'] * _eff_preempt):
        new_ctx = loki_hw.next_working_ctx(stats['working_ctx'], MAX_CTX)
        if new_ctx > stats['working_ctx']:
            old = stats['working_ctx']
            stats['working_ctx'] = new_ctx
            print(f"\n  {c(f'↗ ctx esteso pre-emptive: {old} → {new_ctx} tk (~{est} tk stimati)', GRAY)}\n")
        else:
            pct = int(100 * est / MAX_CTX)
            print(f"\n  {c(f'⚠ contesto ~{pct}% (~{est}/{MAX_CTX} tk) — compressione pre-emptiva...', YELLOW)}")
            state['messages'] = compress_context(messages)
            messages = state['messages']

    length_retries   = 0   # contatore TOTALE di done_reason=length nel turno
    no_action_strikes = 0  # turni consecutivi con solo thinking, zero azioni
    while True:
        msg = stream_response(messages)
        if msg is None:
            messages.pop()
            break
        messages.append(msg)

        # Se l'utente ha premuto Ctrl+C durante lo stream, stream_response e
        # gia rientrato salvando il parziale: usciamo dal loop tool senza
        # provare a rilanciare il modello.
        if _cancel_event.is_set():
            break

        if msg['tool_calls']:
            no_action_strikes = 0   # tool call eseguita: reset contatore reasoning loop
            for tc in msg['tool_calls']:
                fn = tc.get('function', {})
                tool_name = fn.get('name', '')
                args = fn.get('arguments', {}) or {}
                if not isinstance(args, dict):
                    args = {}
                if tool_name == 'run_shell':
                    cmd_arg = args.get('command', '')
                    output = run_shell(cmd_arg)
                elif tool_name == 'read_file':
                    output = run_read_file(
                        args.get('path', ''),
                        args.get('start_line'),
                        args.get('end_line'),
                    )
                elif tool_name == 'write_file':
                    output = run_write_file(
                        args.get('path', ''),
                        args.get('content', ''),
                        bool(args.get('append', False)),
                    )
                elif tool_name == 'web_search':
                    output = run_web_search(args.get('query', ''))
                elif tool_name == 'fetch_url':
                    output = run_fetch_url(args.get('url', ''))
                elif tool_name == 'workspace_write':
                    output = run_workspace_write(
                        args.get('file', ''),
                        args.get('content', ''),
                        args.get('mode', 'append'),
                    )
                elif tool_name == 'workspace_read':
                    output = run_workspace_read(args.get('file', ''))
                elif tool_name in _SEC_TOOLS:
                    try:
                        import loki_sec
                        sec_fn = getattr(loki_sec, tool_name)
                        output = sec_fn(**args)
                    except Exception as e:
                        output = f"ERROR calling {tool_name}: {e}"
                elif tool_name in loki_plugins.tool_names():
                    output = loki_plugins.dispatch(tool_name, args)
                else:
                    output = f"ERROR: unknown tool: {tool_name}"
                messages.append({"role": "tool", "content": output})
            continue
        elif (not msg.get('tool_calls')
              and not (msg.get('content') or '').strip()
              and len((msg.get('thinking') or '').split()) > 80
              and msg.get('done_reason') != 'length'):
            # Turn ended normally with ONLY thinking and zero actions → reasoning loop.
            # (done_reason=length + thinking-only goes to the length branch below)
            thinking_words = len((msg.get('thinking') or '').split())

            # Strip thinking from the looping message immediately: it's already
            # displayed, keeping it in context only wastes tokens.
            messages[-1] = {k: v for k, v in messages[-1].items() if k != 'thinking'}

            no_action_strikes += 1
            if no_action_strikes >= 2:
                # Hard break: scrub loop artifacts from history so they don't
                # accumulate. Walk back and remove silent-assistant turns and
                # the injected user reminders that preceded them.
                _clean_idx = len(messages) - 1
                _LOOP_PREFIXES = ('STOP.', 'REASONING LOOP')
                while _clean_idx > 1:
                    m = messages[_clean_idx]
                    is_silent_asst = (m.get('role') == 'assistant'
                                      and not m.get('tool_calls')
                                      and not (m.get('content') or '').strip())
                    is_loop_reminder = (m.get('role') == 'user'
                                        and any((m.get('content') or '').startswith(p)
                                                for p in _LOOP_PREFIXES))
                    if is_silent_asst or is_loop_reminder:
                        _clean_idx -= 1
                    else:
                        break
                messages[:] = messages[:_clean_idx + 1]
                print(f"\n  {c(f'⚠ reasoning loop — break after 2 silent turns ({thinking_words} words)', YELLOW)}\n")
                break

            # Strike 1: minimal reminder, saves context vs long escalation message
            reminder = (
                f"STOP. {thinking_words} words of thinking, zero actions. "
                "Call a tool NOW — run_shell, web_search or fetch_url. "
                "Pick the most likely option and execute it. No more reasoning."
            )
            print(f"\n  {c(f'⚠ reasoning loop [1/2] — {thinking_words} words, no action', YELLOW)}\n")
            messages.append({"role": "user", "content": reminder})
            continue
        elif msg.get('done_reason') == 'length' and stats['auto_continue']:
            length_retries += 1
            # Safety hard-cap TOTALE (non resettato dalla compressione):
            # se dopo 4 tentativi siamo ancora bloccati sul length, meglio
            # fermarsi che loopare a vuoto compress-continue-compress.
            if length_retries > 4:
                print(f"\n  {c('⚠ auto-continue interrotto: 4 tentativi non hanno sbloccato il turno', YELLOW)}")
                print(f"  {c('   prova /compress o /ac per disattivare', DIM)}\n")
                break
            # Il modello ha esaurito lo spazio nel num_ctx. Riprovare con lo
            # STESSO num_ctx (che era gia' saturo) e' inutile: nel migliore
            # dei casi Ollama ritorna subito un altro done_reason=length,
            # nel peggiore si pianta a rimasticare il prompt pieno con la
            # KV cache satura. Prima di appendere "continua" devo liberare
            # spazio: se c'e' headroom sotto MAX_CTX cresco il tier
            # (economico), altrimenti comprimo.
            new_ctx = loki_hw.next_working_ctx(stats['working_ctx'], MAX_CTX)
            if new_ctx > stats['working_ctx']:
                old = stats['working_ctx']
                stats['working_ctx'] = new_ctx
                print(f"\n  {c(f'↗ ctx working esteso: {old} -> {new_ctx} tk (limite raggiunto)', GRAY)}")
            else:
                print(f"\n  {c('⚠ ctx al tetto del modello — comprimo prima di continuare...', YELLOW)}")
                state['messages'] = compress_context(messages)
                messages = state['messages']
            print(f"  {c('↻ limite token raggiunto — continuo...', GRAY)}\n")
            # Thinking-only + done_reason=length: model was looping when ctx ran out.
            # Strip the thinking blob before re-injecting (it's gone anyway — truncated).
            thinking_len = len((msg.get('thinking') or '').split())
            no_action = not msg.get('tool_calls') and not (msg.get('content') or '').strip()
            if thinking_len > 80 and no_action:
                # Strip thinking from the truncated message — it was cut off anyway,
                # keeping it pollutes context with an incomplete blob.
                messages[-1] = {k: v for k, v in messages[-1].items() if k != 'thinking'}
                no_action_strikes += 1
                reminder = (
                    f"REASONING LOOP: thinking truncated after {thinking_len} words — lost forever. "
                    "Call a tool NOW: run_shell, web_search or fetch_url. No more reasoning."
                )
                messages.append({"role": "user", "content": reminder})
            else:
                messages.append({"role": "user", "content": build_continue_message(msg)})
            continue
        else:
            # Politica adattiva: quando il prompt supera l'80% del working_ctx
            # corrente, prima proviamo a estendere il tier (economico rispetto
            # a comprimere). Se working_ctx e' gia' al tetto del modello,
            # comprimiamo. In entrambi i casi Ollama viene richiamato con
            # `num_ctx` aggiornato al turno successivo.
            if stats['ctx_used'] >= int(stats['working_ctx'] * COMPRESS_AT):
                new_ctx = loki_hw.next_working_ctx(stats['working_ctx'], MAX_CTX)
                if new_ctx > stats['working_ctx']:
                    old = stats['working_ctx']
                    stats['working_ctx'] = new_ctx
                    print(f"\n  {c(f'↗ ctx working esteso: {old} -> {new_ctx} tk', GRAY)}\n")
                else:
                    pct  = int(100 * stats['ctx_used'] / MAX_CTX)
                    used = stats['ctx_used']
                    msg_txt = f"⚠ context al {pct}% ({used}/{MAX_CTX} tk) — comprimo automaticamente..."
                    print(f"\n  {c(msg_txt, YELLOW)}")
                    state['messages'] = compress_context(messages)
                    messages = state['messages']
            break

    # Lightweight memory cleanup every turn: strip thinking + truncate old
    # tool outputs. Free, no LLM call. Keeps RAM lean during long sessions.
    state['messages'] = _trim_old_messages(state['messages'])
    messages = state['messages']

    # Message-count hard cap: if history grew too long, force an LLM compress
    # even if context % hasn't hit the threshold yet. Prevents lag after hours.
    non_sys = [m for m in messages if m.get('role') != 'system']
    if len(non_sys) > MAX_MESSAGES_BEFORE_COMPRESS:
        print(f"\n  {c(f'⚠ {len(non_sys)} messaggi in history — comprimo automaticamente...', YELLOW)}")
        state['messages'] = compress_context(messages)
        messages = state['messages']

    # Autosave dopo ogni turno completato (o interrotto): l'utente non deve
    # perdere una sessione lunga per un crash del terminale o di Ollama.
    try:
        loki_persist.autosave_session(state['messages'], MODEL, SESSIONS_DIR)
    except Exception:
        pass


def _handle_slash(text, state):
    """Gestisce i comandi slash in modalita async. Ritorna True se va uscito."""
    action, payload = parse_slash(text, state['messages'])
    if action == 'exit':
        return True
    if action == 'clear':
        state['messages'] = state['messages'][:1]
        stats['pending_images'].clear()
        welcome()
        return False
    if action == 'resume':
        # resume_session ha gia pulito l'output e stampato banner + storia,
        # quindi NON chiamiamo welcome() qui.
        state['messages'] = payload
        stats['pending_images'].clear()
        return False
    if action == 'compress':
        state['messages'] = compress_context(state['messages'])
        return False
    if action == 'trim':
        state['messages'] = payload
        return False
    if action == 'picker':
        _picker_activate(state)
        return False
    return False  # 'handled' e altri: output gia stampato da parse_slash


async def _process_input(text, state):
    """Task async che processa una singola submission dell'utente."""
    if state['processing']:
        return
    _cancel_event.clear()   # nuovo turno: si riparte
    _bashing_event.clear()  # spinner riparte da "cooking..."
    state['processing']   = True
    state['current_task'] = asyncio.current_task()
    try:
        # Echo dell'input nell'output area
        first, *rest = text.splitlines()
        print(f"\n  {ORANGE}❯{R} {first}")
        for line in rest:
            print(f"    {line}")

        if text.startswith('/'):
            should_exit = _handle_slash(text, state)
            if should_exit:
                app = get_app_or_none()
                if app:
                    app.exit()
            return

        stats['messages'] += 1
        message = {"role": "user", "content": text}
        if stats['pending_images']:
            n_imgs = len(stats['pending_images'])
            message["images"] = stats['pending_images'][:]
            print(f"  {c(f'[{n_imgs} immagine/i allegate]', DIM)}")
            stats['pending_images'].clear()
        state['messages'].append(message)

        loop = asyncio.get_running_loop()
        turn_fut = loop.run_in_executor(None, _run_turn, state)
        try:
            await turn_fut
        except asyncio.CancelledError:
            # Ctrl+C ha cancellato il task esterno. Il thread executor pero
            # continua a girare (Python non li puo killare). Diamogli 2s per
            # notare _cancel_event, poi liberiamo il prompt comunque.
            _cancel_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(turn_fut), timeout=2.0)
                print(c("  ✓ turno interrotto pulito", GREEN))
            except asyncio.TimeoutError:
                print(c("  ⚠ turno ancora attivo in background — prompt libero comunque", RED))
            except Exception:
                pass
    finally:
        state['current_task'] = None
        state['processing']   = False


async def async_chat_loop():
    """Loop principale in modalita 'pinned': un solo prompt_async che vive per
    tutta la sessione, con Enter che processa in-place invece di far ritornare
    il prompt. patch_stdout(raw=True) fa apparire lo streaming sopra la riga
    di input, e riquadro+toolbar restano sempre in fondo.
    """
    global _MAIN_LOOP, _PINNED_MODE
    _MAIN_LOOP    = asyncio.get_running_loop()
    _PINNED_MODE  = True

    state = {
        'messages':   [{"role": "system", "content": build_system_prompt()}],
        'processing': False,
        'ctrl_c_ts':  0.0,
    }

    def on_submit(text):
        # Callback sincrono dall'Enter handler di build_session.
        # Schedula il processing sul loop principale.
        if state['processing']:
            return
        _MAIN_LOOP.create_task(_process_input(text, state))

    session = build_session(on_submit=on_submit)

    # Aggiungiamo Ctrl+C / Ctrl+D al set di keybindings gia costruiti dentro build_session.
    kb = session.key_bindings

    @kb.add('c-c')
    def _(event):
        buf = event.current_buffer
        if buf.text:
            buf.reset()
            return
        now = time.time()
        if state['processing']:
            # Interrompiamo il turno corrente. stream_response controlla
            # _cancel_event tra un chunk e l'altro e salva il parziale.
            _cancel_event.set()
            print(c("\n  ⚠ interruzione richiesta...", YELLOW))
            return
        if now - state['ctrl_c_ts'] <= 1.0:
            event.app.exit()
            return
        state['ctrl_c_ts'] = now
        print(c("  premi ancora Ctrl+C per uscire", GRAY))

    @kb.add('c-d')
    def _(event):
        if not event.current_buffer.text and not state['processing']:
            event.app.exit()

    welcome()

    def get_prompt():
        # separatore continuo sopra il riquadro di input, ricalcolato ad ogni
        # render cosi si adatta ai resize del terminale.
        w = max(20, term_width())
        return ANSI(f"{DGRAY}{'─' * w}{R}\n  {ORANGE}❯{R} ")

    with patch_stdout(raw=True):
        try:
            await session.prompt_async(get_prompt)
        except (EOFError, KeyboardInterrupt):
            pass

    print(c("\n  Arrivederci.\n", DIM))


def _make_toolbar_fs():
    """Toolbar per modalita full-screen (analoga a bottom_toolbar in build_session)."""
    elapsed = datetime.now() - stats['start_time']
    mins    = int(elapsed.total_seconds() // 60)
    secs    = int(elapsed.total_seconds() % 60)
    sep     = f"  {DGRAY}│{R}  "
    mode    = c('⏺ AUTO',   ORANGE) if stats['auto_approve']  else c('⏸ MANUAL', GRAY)
    think   = f"{PURPLE}◆ THINK{R}" if stats['show_thinking'] else f"{DGRAY}◇ THINK{R}"
    ac      = f"{GREEN}AC{R}"      if stats['auto_continue'] else f"{DGRAY}AC{R}"
    n_imgs  = len(stats['pending_images'])
    imgs    = f"{sep}{ORANGE}⬡ img:{n_imgs}{R}" if n_imgs else ''
    tk      = f"  {DGRAY}tk:{stats['last_tokens']}{R}" if stats['last_tokens'] else ''
    if stats['ctx_used']:
        pct = int(100 * stats['ctx_used'] / stats['working_ctx'])
        ctx_col = RED if pct >= 80 else (YELLOW if pct >= 60 else DGRAY)
        ctx = f"  {ctx_col}ctx {pct}%{R}"
    else:
        ctx = ''
    mouse_hint  = '' if _mouse_enabled[0] else f"  {YELLOW}✂ selezione{R}"
    model_short = MODEL.split('/')[-1][:28]
    content = (
        f"  {mode}{sep}"
        f"{model_short}{sep}"
        f"{mins:02d}m {secs:02d}s{sep}"
        f"✉ {stats['messages']}  "
        f"{GREEN}✓ {stats['tools_ok']}{R}  "
        f"{RED}✗ {stats['tools_no']}{R}"
        f"{tk}{ctx}{imgs}{mouse_hint}{sep}"
        f"{think}  {ac}  "
    )
    # Pad a fine terminale cosi lo sfondo scuro della toolbar copre tutta la
    # larghezza (senza padding, la parte a destra resta trasparente/incoerente).
    plain_len = len(strip_ansi(content))
    w = term_width()
    if plain_len < w:
        content = content + ' ' * (w - plain_len)
    return ANSI(content)


async def async_chat_loop_fullscreen():
    """Loop in modalita full-screen: Application con Layout HSplit di 4 zone.
    Output area scrolla con la rotella (grazie a _ScrollableOutputControl),
    riquadro input pinnato in basso, toolbar sotto.

    Su errore, restaura stdout e scrive traceback su ~/loki_debug.log.
    """
    global _MAIN_LOOP, _PINNED_MODE, _app_ref
    _MAIN_LOOP        = asyncio.get_running_loop()
    _follow_bottom[0] = True
    _scroll_lines[0]  = 0
    _debug_log("async_chat_loop_fullscreen: start")

    state = {
        'messages':   [{"role": "system", "content": build_system_prompt()}],
        'processing': False,
        'ctrl_c_ts':  0.0,
    }

    _saved_stdout = sys.stdout

    # ----- COSTRUISCI LA LAYOUT PRIMA di redirigere stdout -----
    # Se qualcosa esplode qui, l'errore va sul terminale vero, non nell'oblio.
    try:
        # Output area (con handler rotella). Top-aligned, no padding: il banner
        # sta in alto e i messaggi scorrono sotto man mano che arrivano.
        def _get_output_ft():
            if _picker['active']:
                try:
                    rendered = _picker_render()
                    _output_line_count[0] = rendered.count('\n') + 1
                    return ANSI(rendered)
                except Exception as e:
                    _debug_log(f"_picker_render ERRORE: {e}")
                    _output_line_count[0] = 1
                    return ANSI(f"  {RED}Errore picker: {e}{R}")
            with _output_lock:
                text = ''.join(_output_chunks) or ' '
            _output_line_count[0] = text.count('\n') + 1
            return ANSI(text)

        def _get_output_scroll(window):
            try:
                ri = window.render_info
                if ri is None:
                    return _desired_scroll[0]
                total   = ri.content_height
                visible = ri.window_height
                max_scroll = max(0, total - visible)
                _last_max_scroll[0] = max_scroll
                if _follow_bottom[0]:
                    pos = max_scroll
                else:
                    pos = min(_desired_scroll[0], max_scroll)
                _desired_scroll[0] = pos  # mantieni in sync con il cap reale
                return pos
            except Exception as e:
                import traceback as _tb
                _debug_log(f"_get_output_scroll ERRORE: {e}\n{_tb.format_exc()}")
                return 0

        output_control = _ScrollableOutputControl(
            text=_get_output_ft,
            focusable=False,
            show_cursor=False,
            get_cursor_position=_get_output_cursor_pos,
        )
        output_window = Window(
            content=output_control,
            wrap_lines=True,
            get_vertical_scroll=_get_output_scroll,
            always_hide_cursor=True,
        )
        _output_window_ref[0] = output_window

        # Spinner "cooking..." / "bashing..." — visibile solo mentre
        # state['processing'] e True. I frame ciclano ~6 volte al secondo.
        # Diventa "bashing..." quando _bashing_event e' set, cioe' quando il
        # modello sta emettendo tool_calls o quando run_shell sta girando.
        SPINNER_FRAMES = ['·', '✶', '✽', '✶', '·']
        def _get_spinner_ft():
            idx = int(time.time() * 6) % len(SPINNER_FRAMES)
            label = 'bashing...' if _bashing_event.is_set() else 'cooking...'
            return ANSI(f"  {PURPLE}{SPINNER_FRAMES[idx]}{R} {c(label, DIM)}")

        spinner_container = ConditionalContainer(
            content=Window(
                content=_WheelFTControl(text=_get_spinner_ft, focusable=False),
                height=1,
            ),
            filter=Condition(lambda: state['processing']),
        )

        # Separatore
        def _get_sep_ft():
            # GRAY invece di DGRAY: DGRAY su tema scuro spariva.
            return ANSI(f"{GRAY}{'─' * max(20, term_width())}{R}")

        separator = Window(
            content=_WheelFTControl(text=_get_sep_ft, focusable=False),
            height=1,
        )

        # Input area (Buffer + BufferControl con BeforeInput per il "❯")
        # read_only quando il picker e attivo (non in clone_input): impedisce
        # l'inserimento di caratteri nel buffer senza bisogno di <any> catch-all.
        _buf_readonly = Condition(
            lambda: _picker['active'] and _picker['mode'] != 'clone_input'
        )
        input_buffer = Buffer(
            multiline=True,
            completer=SlashOnlyCompleter(),
            complete_while_typing=True,
            auto_suggest=AutoSuggestFromHistory(),
            history=InMemoryHistory(),
            read_only=_buf_readonly,
        )

        # Enter handler locale al buffer
        kb_input = KeyBindings()

        def on_submit(text):
            if state['processing']:
                return
            _MAIN_LOOP.create_task(_process_input(text, state))

        # eager=True: Enter viene consumato subito, senza dare prima la mano al
        # menu autocomplete (che altrimenti "accetta la completion" e non
        # submitta — bug del /help).
        # filter=not picker: quando il picker e attivo questo binding non deve
        # matchare, altrimenti vince su _p_enter (kb_app viene prima nella lista
        # di match e matches[-1] e sempre il control-level).
        _not_picker_cond = Condition(lambda: not _picker['active'])
        @kb_input.add('enter', eager=True, filter=_not_picker_cond)
        def _(event):
            buf = event.current_buffer
            # Se c'e un menu autocomplete aperto, chiudilo prima di procedere.
            if buf.complete_state is not None:
                buf.complete_state = None
            if buf.text.endswith('\\'):
                buf.delete_before_cursor(1)
                buf.insert_text('\n')
                return
            text = buf.text.strip()
            if not text:
                return
            buf.reset(append_to_history=True)
            _scroll_to_bottom()
            on_submit(text)

        input_control = _WheelBufferControl(
            buffer=input_buffer,
            input_processors=[BeforeInput(ANSI(f"  {ORANGE}❯{R} "))],
            key_bindings=kb_input,
            focusable=True,
        )
        # dont_extend_height=True: il Window si adatta al contenuto reale del
        # buffer. Con buffer vuoto = 1 riga, cresce fino a max=8 se scrivi
        # multiline (con \+Invio).
        input_window = Window(
            content=input_control,
            height=Dimension(min=1, max=8),
            wrap_lines=True,
            dont_extend_height=True,
        )

        # Toolbar
        toolbar_window = Window(
            content=_WheelFTControl(text=_make_toolbar_fs, focusable=False),
            height=1,
            style='class:toolbar',
        )

        # Keybindings app-level (Ctrl+C, Ctrl+D, PageUp/PageDown per scroll da tastiera)
        kb_app = KeyBindings()

        @kb_app.add('c-c')
        def _(event):
            buf = event.current_buffer
            if buf.text:
                buf.reset()
                return
            now = time.time()
            if state['processing']:
                # Segnala il cancel al thread (check tra chunk). In piu, cancella
                # il task async esterno: _process_input catchera CancelledError,
                # aspettera 2s che il thread rilasci, e comunque liberera 'processing'
                # cosi il prompt torna disponibile anche se il thread e appeso.
                _cancel_event.set()
                task = state.get('current_task')
                if task is not None and not task.done():
                    task.cancel()
                print(c("\n  ⚠ interruzione richiesta...", YELLOW))
                return
            if now - state['ctrl_c_ts'] <= 1.0:
                event.app.exit()
                return
            state['ctrl_c_ts'] = now
            print(c("  premi ancora Ctrl+C per uscire", GRAY))

        @kb_app.add('c-d')
        def _(event):
            if not event.current_buffer.text and not state['processing']:
                event.app.exit()

        @kb_app.add('pageup')
        def _(event):
            _scroll_up(10)

        @kb_app.add('pagedown')
        def _(event):
            _scroll_down(10)

        # End = salta al fondo; Home = salta all'inizio del contenuto
        @kb_app.add('end')
        def _(event):
            _scroll_to_bottom()

        @kb_app.add('home')
        def _(event):
            _follow_bottom[0] = False
            _scroll_lines[0]  = 0
            event.app.invalidate()

        # Alt+M: toggle mouse capture (per copiare/incollare col mouse)
        @kb_app.add('escape', 'm')
        def _(event):
            _mouse_enabled[0] = not _mouse_enabled[0]
            new_state = 'ON' if _mouse_enabled[0] else 'OFF'
            hint = 'rotella scroll attiva' if _mouse_enabled[0] else 'ora puoi selezionare col mouse'
            col  = GREEN if _mouse_enabled[0] else GRAY
            print(f"  {c(f'mouse: {new_state}', col)}  {c(hint, DIM)}")
            event.app.invalidate()

        # ── History navigation (↑↓ fuori dal picker) ──────────────────────────
        @kb_input.add('up', filter=_not_picker_cond, eager=True)
        def _hist_up(event):
            buf = event.current_buffer
            if buf.document.cursor_position_row == 0:
                buf.history_backward()
            else:
                buf.cursor_position += buf.document.get_cursor_up_position()

        @kb_input.add('down', filter=_not_picker_cond, eager=True)
        def _hist_down(event):
            buf = event.current_buffer
            doc = buf.document
            if doc.cursor_position_row == doc.line_count - 1:
                buf.history_forward()
            else:
                buf.cursor_position += doc.get_cursor_down_position()

        # ── Picker key bindings ────────────────────────────────────────────────
        _picker_cond      = Condition(lambda: _picker['active'])

        @kb_input.add('up', filter=_picker_cond, eager=True)
        def _p_up(event):
            if _picker['mode'] == 'list':
                _picker['cursor'] = max(0, _picker['cursor'] - 1)
            elif _picker['mode'] in ('delete_confirm',):
                _picker['action'] = 1 - _picker['action']
            event.app.invalidate()

        @kb_input.add('down', filter=_picker_cond, eager=True)
        def _p_down(event):
            if _picker['mode'] == 'list':
                n = len(_picker['sessions'])
                _picker['cursor'] = min(n - 1, _picker['cursor'] + 1) if n else 0
            elif _picker['mode'] in ('delete_confirm',):
                _picker['action'] = 1 - _picker['action']
            event.app.invalidate()

        @kb_input.add('right', filter=_picker_cond, eager=True)
        def _p_right(event):
            mode = _picker['mode']
            if mode == 'list' and _picker['sessions']:
                _picker['mode']   = 'actions'
                _picker['action'] = 0
            elif mode == 'actions':
                _picker['action'] = min(1, _picker['action'] + 1)
            elif mode == 'delete_confirm':
                _picker['action'] = min(1, _picker['action'] + 1)
            event.app.invalidate()

        @kb_input.add('left', filter=_picker_cond, eager=True)
        def _p_left(event):
            mode = _picker['mode']
            if mode == 'actions':
                if _picker['action'] > 0:
                    _picker['action'] -= 1
                else:
                    _picker['mode'] = 'list'
            elif mode == 'delete_confirm':
                if _picker['action'] > 0:
                    _picker['action'] -= 1
                else:
                    _picker['mode'] = 'actions'
                    _picker['action'] = 0
            elif mode == 'clone_input':
                pass  # gestito da backspace
            event.app.invalidate()

        @kb_input.add('escape', filter=_picker_cond, eager=True)
        def _p_esc(event):
            mode = _picker['mode']
            if mode == 'list':
                _picker_deactivate()
            elif mode in ('actions',):
                _picker['mode'] = 'list'
                event.app.invalidate()
            elif mode == 'clone_input':
                # Annulla clone: svuota il buffer e torna alle azioni
                event.current_buffer.reset()
                _picker['mode']   = 'actions'
                _picker['action'] = 1  # clone era action=1
                event.app.invalidate()
            elif mode == 'delete_confirm':
                _picker['mode']   = 'actions'
                _picker['action'] = 0
                event.app.invalidate()

        @kb_input.add('enter', filter=_picker_cond, eager=True)
        def _p_enter(event):
            mode = _picker['mode']
            cur  = _picker['cursor']
            act  = _picker['action']
            ss   = _picker['sessions']
            if not ss:
                _picker_deactivate()
                return
            s = ss[cur]
            if mode == 'list':
                _picker_do_load(s['name'])
            elif mode == 'actions':
                if act == 0:
                    _picker['mode']   = 'delete_confirm'
                    _picker['action'] = 1   # default: Annulla
                    event.app.invalidate()
                else:
                    # Entra in clone_input: setta il mode PRIMA di toccare il buffer
                    # (cosi _buf_readonly diventa False e insert_text funziona).
                    _picker['mode'] = 'clone_input'
                    buf = event.current_buffer
                    buf.reset()
                    buf.insert_text(s['name'] + '_copy')
                    event.app.invalidate()
            elif mode == 'delete_confirm':
                if act == 0:
                    path = s['path']
                    if os.path.isfile(path):
                        os.remove(path)
                    _picker['sessions'] = _picker_load_sessions()
                    _picker['cursor']   = max(0, min(cur, len(_picker['sessions']) - 1))
                    _picker['mode']     = 'list'
                else:
                    _picker['mode'] = 'list'
                event.app.invalidate()
            elif mode == 'clone_input':
                # Legge il nome dal buffer di input (dove l'utente ha scritto)
                new_name = event.current_buffer.text.strip()
                event.current_buffer.reset()
                if new_name:
                    import shutil
                    dst = _session_path(new_name)
                    if not os.path.isfile(dst):
                        shutil.copy2(s['path'], dst)
                    _picker['sessions'] = _picker_load_sessions()
                _picker['mode'] = 'list'
                event.app.invalidate()

        # ── Fine picker key bindings ───────────────────────────────────────────
        # Nota: nessun <any> catch-all — il buffer ha read_only=_buf_readonly
        # che blocca l'inserimento di testo nelle modalita list/actions/delete_confirm.
        # In clone_input il buffer è editabile e i caratteri vanno direttamente
        # nel buffer (che usiamo per leggere il nome finale in _p_enter).

        # FloatContainer per il menu autocomplete: appare sopra l'input come
        # popup ancorato al cursore quando si digita "/..."
        body = HSplit([
            output_window,
            spinner_container,   # visibile solo mentre Loki lavora
            separator,
            input_window,        # 1 riga di default, cresce col multiline
            toolbar_window,
        ])
        root_container = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=10, scroll_offset=1),
                ),
            ],
        )

        layout = Layout(root_container, focused_element=input_window)

        app_style = Style.from_dict({
            'toolbar': 'bg:#0e0e0e fg:#ffffff bold',
        })

        # Condition legata al flag mutabile: al render prompt_toolkit rilegge
        # e attiva/disattiva mouse tracking sul terminale (invia le escape 1000/1006).
        mouse_cond = Condition(lambda: _mouse_enabled[0])

        app = Application(
            layout=layout,
            key_bindings=kb_app,
            full_screen=True,
            style=app_style,
            refresh_interval=0.15,  # cadenza per far animare lo spinner "cooking..."
            mouse_support=mouse_cond,
        )

        # Sovrascrivi _handle_exception per loggare il traceback completo
        _orig_handle_exc = app._handle_exception
        def _logged_handle_exc(loop, context):
            import traceback as _tb
            exc = context.get('exception')
            tb_str = ''.join(_tb.format_exception(type(exc), exc, exc.__traceback__)) if exc else str(context)
            _debug_log(f"RENDER EXCEPTION: {tb_str}")
            _orig_handle_exc(loop, context)
        app._handle_exception = _logged_handle_exc

        _debug_log("layout built OK")

    except Exception as e:
        import traceback
        _debug_log(f"LAYOUT BUILD FAILED: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise

    # ----- REDIRECT stdout + banner + run app -----
    _PINNED_MODE = True
    sys.stdout   = _OutputProxy()
    _app_ref     = app

    try:
        welcome()  # scrive nel _output_chunks via redirect
        _debug_log("about to app.run_async()")
        await app.run_async()
        _debug_log("app.run_async returned normally")
    except Exception as e:
        import traceback
        _debug_log(f"APP RUN FAILED: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        raise
    finally:
        _app_ref = None
        sys.stdout = _saved_stdout
        _PINNED_MODE = False

    print(c("\n  Arrivederci.\n", DIM))


def run_oneshot(query, stdin_data=None, exec_cmd=None):
    """Non-interactive one-shot mode: process a single query and exit.

    Handles three entry points:
      loki "question"              → query from argv
      echo "q" | loki             → query from piped stdin
      loki --exec "cmd" "question" → run cmd first, inject output as context
    """
    # If --exec was given, run the command and prepend its output to the query
    if exec_cmd:
        try:
            import subprocess as _sp
            result = _sp.run(exec_cmd, shell=True, capture_output=True,
                             text=True, timeout=SHELL_TIMEOUT)
            exec_out = (result.stdout + result.stderr).strip()
            if exec_out:
                query = f"Command: {exec_cmd}\n\nOutput:\n{exec_out}\n\n{query or 'Analyze this output.'}"
        except Exception as e:
            query = f"Command failed: {exec_cmd}\nError: {e}\n\n{query or ''}"

    # Prepend piped stdin to the query
    if stdin_data:
        query = f"{stdin_data.strip()}\n\n{query}" if query else stdin_data.strip()

    if not query:
        sys.stderr.write("loki: no query provided\n")
        sys.exit(1)

    messages = [
        {"role": "system",  "content": build_system_prompt()},
        {"role": "user",    "content": query},
    ]

    # Stream response directly to stdout (no fullscreen UI)
    thinking_buf = []
    show_think = os.environ.get('LOKI_THINK', '0') == '1'
    try:
        stream = ollama.chat(
            model=MODEL,
            messages=messages,
            tools=get_active_tools(),
            think=True,
            stream=True,
            options={'num_ctx': stats['working_ctx']},
            keep_alive=KEEP_ALIVE,
        )
        in_thinking = False
        for chunk in stream:
            msg = chunk.get('message', {})
            think_piece = msg.get('thinking') or ''
            content_piece = msg.get('content') or ''
            if think_piece:
                in_thinking = True
                thinking_buf.append(think_piece)
            if in_thinking and not think_piece and content_piece:
                in_thinking = False
                if show_think and thinking_buf:
                    sys.stdout.write(f"<think>\n{''.join(thinking_buf)}\n</think>\n\n")
            if content_piece:
                sys.stdout.write(content_piece)
                sys.stdout.flush()
        # Handle tool calls (one-shot: execute and append, then re-invoke once)
        # Keep it simple: just print tool outputs inline
        final = chunk  # last chunk has done info
        if final.get('message', {}).get('tool_calls'):
            for tc in final['message']['tool_calls']:
                fn = tc.get('function', {}) if isinstance(tc, dict) else {}
                tn = fn.get('name', '')
                args = fn.get('arguments', {}) or {}
                if not isinstance(args, dict):
                    args = {}
                if tn == 'run_shell':
                    out = run_shell(args.get('command', ''))
                    sys.stdout.write(f"\n\n[tool: {tn}]\n{out}\n")
                elif tn == 'read_file':
                    out = run_read_file(args.get('path', ''))
                    sys.stdout.write(f"\n\n[tool: {tn}]\n{out}\n")
                elif tn == 'web_search':
                    out = run_web_search(args.get('query', ''))
                    sys.stdout.write(f"\n\n[tool: {tn}]\n{out}\n")
                elif tn == 'fetch_url':
                    out = run_fetch_url(args.get('url', ''))
                    sys.stdout.write(f"\n\n[tool: {tn}]\n{out}\n")
                else:
                    sys.stdout.write(f"\n\n[tool: {tn} — not supported in one-shot mode]\n")
    except KeyboardInterrupt:
        pass
    sys.stdout.write('\n')
    sys.exit(0)


if __name__ == "__main__":
    # Load user config (~/.loki.conf) before anything else.
    _cfg = load_config()
    if _cfg.get('model') and not os.environ.get('LOKI_MODEL'):
        MODEL = _cfg['model']
    if _cfg.get('persona') and _cfg['persona'] in PERSONAS:
        ACTIVE_PERSONA = _cfg['persona']
    if 'auto_approve' in _cfg:
        stats['auto_approve'] = _cfg['auto_approve']
    if 'auto_continue' in _cfg:
        stats['auto_continue'] = _cfg['auto_continue']
    if 'show_thinking' in _cfg:
        stats['show_thinking'] = _cfg['show_thinking']
    if _cfg.get('shell_timeout'):
        SHELL_TIMEOUT = _cfg['shell_timeout']

    # ── One-shot / pipe mode detection ────────────────────────────────────
    # Parse argv for: loki "query", loki --exec "cmd" "query", piped stdin.
    # Must happen before HW detection to allow fast scripted use.
    _argv = sys.argv[1:]
    _oneshot_query = None
    _exec_cmd = None
    _stdin_data = None

    _i = 0
    while _i < len(_argv):
        if _argv[_i] in ('--exec', '-e') and _i + 1 < len(_argv):
            _exec_cmd = _argv[_i + 1]
            _i += 2
        elif _argv[_i] in ('-y', '--yes', '-h', '--help'):
            _i += 1  # handled by install.sh; skip silently
        elif not _argv[_i].startswith('-'):
            _oneshot_query = _argv[_i]
            _i += 1
        else:
            _i += 1

    if not sys.stdin.isatty():
        _stdin_data = sys.stdin.read()
        # Reopen stdin so prompt_toolkit can read the terminal (if needed)
        try:
            sys.stdin = open('/dev/tty', 'r')
        except Exception:
            pass

    _is_oneshot = bool(_oneshot_query or _stdin_data or _exec_cmd)

    # Load user plugins (~/.loki_plugins.py) — best-effort, never blocks boot.
    _plugin_schema, _plugin_err = loki_plugins.load()
    if _plugin_err:
        sys.stderr.write(f"⚠  Plugin load error: {_plugin_err}\n")
    elif _plugin_schema:
        names = ', '.join(t['function']['name'] for t in _plugin_schema if 'function' in t)
        sys.stderr.write(f"🔌 Plugins loaded: {names}\n")

    detect_max_ctx()
    # Probe HW e scelta del working_ctx iniziale: partiamo piccolo (default 8-16k)
    # invece di allocare KV cache per l'intero MAX_CTX. Se serve, _run_turn
    # bumpa al tier successivo quando il prompt supera l'80%.
    HW = loki_hw.detect_hardware()
    stats['working_ctx'] = loki_hw.initial_working_ctx(
        MAX_CTX, HW['ram_avail_gb'],
        vram_free_gb=HW.get('vram_free_gb', 0.0),
        gpu_kind=HW.get('gpu_kind', 'none'),
    )
    # Pulizia una tantum di sessioni molto vecchie (skip _autosave e altri _*).
    try:
        loki_persist.prune_old_sessions(SESSIONS_DIR)
    except Exception:
        pass
    sys.stderr.write(loki_hw.hw_line(HW, stats['working_ctx'], MAX_CTX) + "\n")

    # ── One-shot mode: skip UI entirely ──────────────────────────────────
    if _is_oneshot:
        run_oneshot(_oneshot_query, stdin_data=_stdin_data, exec_cmd=_exec_cmd)
        # run_oneshot calls sys.exit — this line is never reached

    ui_mode   = os.environ.get('LOKI_UI', 'fullscreen').lower()
    exit_code = 0
    try:
        if ui_mode == 'classic':
            asyncio.run(async_chat_loop())
        else:
            try:
                asyncio.run(async_chat_loop_fullscreen())
            except Exception as e:
                import traceback
                sys.stderr.write(f"\n\n❌ Errore in modalita full-screen: {e}\n")
                sys.stderr.write("Traceback anche su ~/loki_debug.log\n")
                sys.stderr.write("Fallback classico: LOKI_UI=classic ./loki.sh\n\n")
                traceback.print_exc()
                exit_code = 1
    except KeyboardInterrupt:
        # Ctrl+C durante lo shutdown asyncio (aspetta i thread executor per
        # THREAD_JOIN_TIMEOUT). Se un turno modello e' impuntato in Ollama il
        # thread non finira mai — usciamo hard con os._exit e skippiamo il join.
        pass
    # os._exit salta la chiusura pulita asyncio: prompt_toolkit ha gia
    # ripristinato il terminale prima di ritornare, quindi e sicuro.
    os._exit(exit_code)
