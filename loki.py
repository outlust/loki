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
from datetime import datetime
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
VERSION = "0.6.0"
PREVIEW_LINES = 20
SESSIONS_DIR  = os.path.expanduser("~/.loki_sessions")
MEMORY_FILE   = os.path.expanduser("~/.loki_memory.md")
WORKSPACE_DIR  = os.path.expanduser("~/.loki_workspace")
WORKSPACE_FILES = {"plan.md", "failures.md", "ideas.md", "notes.md", "scratch.md"}
COMPRESS_AT      = 0.80   # comprimi all'80% del context
COMPRESS_PREEMPT = 0.65   # soglia pre-emptiva: comprimi/cresci PRIMA di inviare la richiesta
MAX_CTX       = 32768  # tetto reale del modello (rilevato al boot da ollama.show)
WORKING_CTX   = 8192   # ctx effettivamente passato a Ollama (adattivo, live in stats)
KEEP_ALIVE    = '15m'  # tiene il modello caricato in RAM tra i turni
COMPRESS_CTX  = 8192   # num_ctx piu piccolo dedicato alla chiamata di compressione
OUTPUT_MAX_LINES  = 100   # righe oltre le quali tronchiamo l'output mandato al modello
OUTPUT_HEAD_LINES = 40    # prime righe da mantenere
OUTPUT_TAIL_LINES = 30    # ultime righe da mantenere
OUTPUT_MAX_CHARS  = 6000  # hard cap in caratteri (cattura righe-monstre: JSON, base64, log su una riga)
KEEP_RECENT_MSG   = 6    # messaggi recenti da preservare dopo compress (arrotondato a inizio turno)
MEMORY_MAX_SUMMARIES = 5  # quanti riassunti di sessione tenere nel file di memoria
os.makedirs(SESSIONS_DIR, exist_ok=True)

# Moduli helper (logica pura, niente UI):
#   loki_hw      -> probe hardware + policy num_ctx adattiva
#   loki_persist -> autosave per turno + resume-last + prune vecchi
#   loki_mem     -> memoria capata con rotazione
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
    '/help':    'mostra i comandi disponibili',
    '/clear':   'pulisce la conversazione',
    '/reset':   'alias di /clear',
    '/model':   'mostra il modello in uso',
    '/cwd':     'mostra directory corrente',
    '/img':     'allega immagine al prossimo messaggio',
    '/auto':    'attiva approvazione automatica comandi',
    '/manual':  'disattiva approvazione automatica comandi',
    '/ac':        'attiva/disattiva auto-continue quando finiscono i token',
    '/remember':  'salva qualcosa nella memoria persistente',
    '/memory':    'mostra la memoria persistente',
    '/compress':  'riassume la conversazione e salva in memoria',
    '/think':   'mostra/nasconde il ragionamento del modello',
    '/last':    'ristampa ultimo output completo',
    '/history': 'statistiche della sessione',
    '/cost':    'alias di /history',
    '/save':     'salva la sessione corrente  [nome]',
    '/resume':   'elenca/riprendi una sessione salvata (senza arg: elenca; /resume <n|nome>: carica e ristampa la storia)',
    '/resume-last': "riprendi l'autosave dell'ultima sessione (se < 12h)",
    '/delete':   'elimina una sessione salvata [nome|numero]',
    '/clone':    'clona una sessione salvata   [sorgente] [nuovo_nome]',
    '/hw':       'mostra HW rilevato (RAM, thread, GPU, working ctx)',
    '/exit':     'esci dallo shell agent',
    '/quit':     'alias di /exit',
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
                "Esegue un comando bash su Linux e restituisce stdout+stderr. "
                "REGOLA CRITICA: se sai gia' il comando, chiamalo ORA senza altro reasoning. "
                "Hai coordinate per xdotool? `xdotool click X Y` ORA. "
                "Devi verificare DISPLAY, un path, un PID? grep/cat/ls ORA. "
                "Hai considerato 2+ alternative? Scegli la piu' probabile, eseguila ORA. "
                "L'output reale di un comando fallito vale piu' di qualsiasi ragionamento."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Comando bash da eseguire"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Cerca informazioni online (sintassi di tool, documentazione, CVE, workaround). "
                "Usalo PRIMA di provare a indovinare opzioni o flag sconosciuti, e quando un comando "
                "fallisce per motivi non chiari. Restituisce snippet di testo rilevanti."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Stringa di ricerca in inglese o italiano"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_write",
            "description": (
                "Scrivi o aggiorna un file nella workspace persistente. "
                "Usa INVECE di ragionare in loop: se stai considerando piu' di 2 opzioni "
                "o hai fatto un passo fallito, scrivilo su file ORA. "
                "I file sopravvivono tra i turni — il tuo thinking no. "
                "plan.md=piano corrente, failures.md=cosa non ha funzionato, "
                "ideas.md=opzioni considerate, notes.md=note libere, scratch.md=bozze."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Nome file: plan.md | failures.md | ideas.md | notes.md | scratch.md"
                    },
                    "content": {"type": "string", "description": "Contenuto da scrivere"},
                    "mode": {
                        "type": "string",
                        "description": "write=sovrascrivi, append=aggiungi in fondo",
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
                "Leggi un file dalla workspace. "
                "Chiamalo all'inizio di sessioni complesse per ricordare dove eri. "
                "Leggi failures.md prima di riprovare qualcosa che potrebbe gia' aver fallito."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "description": "Nome file: plan.md | failures.md | ideas.md | notes.md | scratch.md"
                    }
                },
                "required": ["file"]
            }
        }
    }
]

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
    auto_state  = c('ON', GREEN) if stats['auto_approve']  else c('OFF', GRAY)
    think_state = c('ON', GREEN) if stats['show_thinking'] else c('OFF', GRAY)
    ac_state    = c('ON', GREEN) if stats['auto_continue'] else c('OFF', GRAY)
    print()
    print(f"  {c('SESSIONE', BOLD)}")
    print(f"    {c('durata', DIM):<20} {mins}m {secs}s")
    print(f"    {c('messaggi', DIM):<20} {stats['messages']}")
    print(f"    {c('comandi ok', DIM):<20} {c(str(stats['tools_ok']), GREEN)}")
    print(f"    {c('comandi no', DIM):<20} {c(str(stats['tools_no']), RED)}")
    print(f"    {c('auto approve', DIM):<20} {auto_state}")
    print(f"    {c('mostra thinking', DIM):<20} {think_state}")
    print(f"    {c('auto continue', DIM):<20} {ac_state}")
    print(f"    {c('working ctx', DIM):<20} {stats['working_ctx']} / {MAX_CTX} tk")
    if stats['ctx_used']:
        pct = int(100 * stats['ctx_used'] / stats['working_ctx'])
        print(f"    {c('ctx usato ultimo', DIM):<20} {stats['ctx_used']} tk ({pct}%)")
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
    """Cerca online via Jina AI reader + DuckDuckGo Lite. Nessuna API key richiesta."""
    import urllib.parse
    query = query.strip()
    if not query:
        return "Errore: query vuota"
    q = urllib.parse.quote_plus(query)
    jina_url = f"https://r.jina.ai/https://lite.duckduckgo.com/lite/?q={q}"
    cmd = f"curl -s --max-time 15 '{jina_url}'"
    print(f"  {c('web_search:', BLUE)} {query}")
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=18)
        text = r.stdout.strip()
        if not text:
            return f"Nessun risultato per: {query}"
        # Filtra righe utili: salta redirect DDG e header Jina
        lines = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if any(skip in line for skip in ('duckduckgo.com/l/?uddg=', 'URL Source:', 'Title:', 'Markdown Content:')):
                continue
            if line in ('---', '===', '\\---'):
                continue
            if len(line) > 15:
                lines.append(line)
        result = '\n'.join(lines)
        if len(result) > 2000:
            result = result[:2000] + '\n[...troncato]'
        return result if result.strip() else f"Nessun risultato utile per: {query}"
    except subprocess.TimeoutExpired:
        return "TIMEOUT: web_search ha superato 18 secondi"
    except Exception as e:
        return f"Errore web_search: {e}"

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
            text=True, timeout=30
        )
        output = (result.stdout + result.stderr).strip() or "(nessun output)"
        stats['last_command'] = command
        stats['last_output'] = output                # completo per /last
        model_output = truncate_for_model(output)    # troncato per il modello
        print_output_block(command, output)          # display intero (truncato dall'UI a PREVIEW_LINES)
        return model_output
    except subprocess.TimeoutExpired:
        print(c("     TIMEOUT (>30s)", RED))
        return "TIMEOUT: comando ha superato 30 secondi"
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
    base = (
        "You are an expert red team operator and offensive security specialist with deep Linux sysadmin skills and vision capabilities. "
        "Your mindset is adversarial by default: always think like an attacker. "
        "When analyzing targets, proactively identify attack surfaces, misconfigurations, exposed secrets, weak auth, and exploitable vulnerabilities. "
        "When executing shell commands, use the run_shell tool. Interpret output and extract security-relevant findings. "
        "Suggest concrete next steps: reconnaissance, exploitation, pivoting, persistence, exfiltration. "
        "Be direct and technical. Skip disclaimers. Never repeat the executed command in the final response. "
        "Never use emoji."
    )
    ops = (
        "\n\n## REGOLE OPERATIVE — efficienza del contesto"
        "\nIl tuo context window e' limitato: ogni output di comando lo consuma. Lavora parsimonioso."
        "\n- Prima di eseguire stima se l'output sara' grande. Se puo' esserlo, filtralo ALLA FONTE:"
        " `head -50 file`, `grep PATTERN file`, `cmd | head -30`, `wc -l`. Mai `cat` di file grandi."
        "\n- Anteponi `timeout N` ai comandi che possono bloccarsi (rete, scansioni, prompt interattivi)."
        " Non lanciare comandi che restano in foreground senza timeout."
        "\n- Per task in piu' passi: scrivi un piano breve in 3-5 punti, poi eseguilo un passo alla volta."
        " Aggiorna il piano dopo ogni passo. Niente raffiche di comandi tutti insieme."
        "\n- Usa il comando MINIMO che produce l'informazione che ti serve adesso."
        " `cmd --help | head -30` invece del man completo. `ls dir | head` invece del ricorsivo."
        "\n- Non ripetere un comando fallito identico: cambia approccio, opzioni o strumento."
        " Se l'output e' stato troncato, restringilo con grep/sed/head invece di rilanciarlo raw."
        "\n- Se un flag, sintassi o strumento ti e' sconosciuto o ha dato errore inspiegabile,"
        " usa SUBITO web_search prima di provare a indovinare. E' piu' veloce di 10 tentativi ciechi."
        "\n- Ragiona conciso. Pianifica, poi agisci. Punta alla risposta col minor numero di comandi."
    )
    think = (
        "\n\n## DISCIPLINA DEL RAGIONAMENTO — vincolo assoluto"
        "\n**NEMICO PRINCIPALE**: il reasoning loop — ragionare su 2+ alternative senza eseguirne"
        " nessuna, o considerare la stessa opzione 2+ volte. Questo consuma context window senza"
        " produrre informazioni reali. L'output di un comando fallito vale piu' di 1000 token di"
        " ragionamento a priori."
        "\n\n**CONTRATTO OBBLIGATORIO — ogni turno di thinking DEVE terminare con UNA di queste due:**"
        "\n  A) una tool call (run_shell o web_search)"
        "\n  B) una risposta finale in testo all'utente"
        "\nNon esiste opzione C (thinking senza azione). Se stai per scegliere C, scegli A."
        "\n\n**TRIPWIRE — queste condizioni scatenano una tool call IMMEDIATA, senza ulteriore reasoning:**"
        "\n- Conosci gia' il comando da eseguire → eseguilo ORA. Non pensarci ancora."
        "\n- Hai coordinate x,y per xdotool → lancia `xdotool click X Y` ORA."
        "\n- Hai un path, un PID, una env var da verificare → usa run_shell ORA."
        "\n- Hai considerato la stessa opzione 2+ volte → prendi la piu' probabile, eseguila ORA."
        "\n- Hai fatto 3+ passi di reasoning senza tool call → lancia `echo 'CP: [stato]'` ORA."
        "\n- Un comando ha fallito con errore non ovvio → diagnostica ORA: `cat /proc/PID/environ`,"
        " `echo $DISPLAY`, `which CMD`, `ls -la PATH`. Non ragionare sul perche'."
        "\n- Non conosci la sintassi esatta di un flag → usa web_search ORA, non indovinare."
        "\n\n**REGOLA DEL PIANO SCRITTO**: all'inizio di ogni task esegui SUBITO"
        " `echo 'PIANO: 1)... 2)... 3)...'`. Il thinking serve a PIANIFICARE il prossimo"
        " singolo passo, non a deliberare tra opzioni gia' identificate."
        "\n\n**IL TUO THINKING NON E' SALVATO NEL CONTESTO.** Se viene troncato e' perso."
        " Ogni pensiero che non culmina in una tool call e' context window bruciata."
    )
    workspace = (
        "\n\n## WORKSPACE — memoria persistente tra i turni"
        "\n- workspace_write/read: file .md in ~/.loki_workspace/ che sopravvivono tra i turni."
        "\n- Il tuo thinking viene scartato dopo ogni turno. I file no."
        "\n- REGOLA: se ti ritrovi a pensare la stessa cosa per la seconda volta → scrivi su workspace invece."
        "\n- plan.md: piano passi corrente (aggiorna dopo ogni passo completato)"
        "\n- failures.md: cosa hai provato che NON ha funzionato (leggi PRIMA di riprovare)"
        "\n- ideas.md: opzioni considerate, pro/contro"
        "\n- notes.md: osservazioni, output importanti da ricordare"
        "\n- scratch.md: bozze libere"
        "\n- TRIPWIRE: stai considerando 3+ opzioni? → workspace_write su ideas.md ORA, poi decidi."
    )
    plan_path = os.path.join(WORKSPACE_DIR, "plan.md")
    plan_section = ""
    try:
        if os.path.exists(plan_path):
            with open(plan_path, 'r', encoding='utf-8') as _f:
                _plan = _f.read().strip()
            if _plan:
                plan_section = f"\n\n## PIANO CORRENTE (da workspace)\n{_plan}"
    except Exception:
        pass
    mem = load_memory()
    mem_section = f"\n\n## MEMORIA PERSISTENTE\n{mem}" if mem else ""
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

def _serialize_msg(msg):
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

def save_session(messages, name=None):
    if not name:
        name = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        'saved_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'model':    MODEL,
        'messages': [_serialize_msg(m) for m in messages[1:]],
    }
    path = _session_path(name)
    with open(path, 'w') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n  {c('sessione salvata:', DIM)} {c(name, ORANGE)}  {c(path, DGRAY)}\n")
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
        print(f"  {c('modello:', DIM)} {MODEL}\n")
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
            save_session(messages, arg.strip() or None)
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
            tools=tools_schema,
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

    # Compressione pre-emptiva: intervieni PRIMA di inviare se il contesto stimato
    # supera COMPRESS_PREEMPT del working_ctx, invece di aspettare done_reason=length.
    est = estimate_context_tokens(messages)
    if est >= int(stats['working_ctx'] * COMPRESS_PREEMPT):
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
                name = fn.get('name')
                args = fn.get('arguments', {}) if isinstance(fn.get('arguments'), dict) else {}
                if name == 'run_shell':
                    output = run_shell(args.get('command', ''))
                elif name == 'web_search':
                    output = run_web_search(args.get('query', ''))
                elif name == 'workspace_write':
                    output = run_workspace_write(
                        args.get('file', ''),
                        args.get('content', ''),
                        args.get('mode', 'append')
                    )
                elif name == 'workspace_read':
                    output = run_workspace_read(args.get('file', ''))
                else:
                    output = f"Tool sconosciuto: {name}"
                messages.append({"role": "tool", "content": output})
            continue
        elif (not msg.get('tool_calls')
              and not (msg.get('content') or '').strip()
              and len((msg.get('thinking') or '').split()) > 150
              and msg.get('done_reason') != 'length'):
            # Turno terminato NORMALMENTE (done_reason='stop') con SOLO thinking
            # e zero azioni: il modello e' in reasoning loop silenzioso.
            # Questo era il buco del detector precedente (scattava solo su 'length').
            # I turni done_reason='length' con thinking-only vanno al branch successivo
            # che gestisce anche la crescita del context prima di reinvocare.
            thinking_words = len((msg.get('thinking') or '').split())
            no_action_strikes += 1
            if no_action_strikes >= 3:
                print(f"\n  {c(f'⚠ reasoning loop [{no_action_strikes}/3] — interruzione forzata', YELLOW)}\n")
                no_action_strikes = 0
                break
            _ESCALATION = [
                (
                    "ATTENZIONE: hai ragionato {w} parole senza eseguire nulla [{s}/3]. "
                    "CONTRATTO OBBLIGATORIO: ogni turno deve produrre una tool call o una risposta finale. "
                    "Esegui SUBITO run_shell o web_search. Quale azione fisica esegui adesso?"
                ),
                (
                    "SECONDO AVVISO — REASONING LOOP [{s}/3]: {w} parole di thinking, zero azioni. "
                    "BLOCCO AUTOMATICO AL PROSSIMO TURNO INATTIVO. "
                    "Smetti di ragionare. Lancia ADESSO la tool call piu' probabile. "
                    "Se hai 2+ opzioni, scegli la prima e basta — l'output ti dira' se era giusta."
                ),
            ]
            template = _ESCALATION[min(no_action_strikes - 1, len(_ESCALATION) - 1)]
            reminder = template.format(w=thinking_words, s=no_action_strikes)
            print(f"\n  {c(f'⚠ reasoning loop [{no_action_strikes}/3] — {thinking_words} parole senza azione', YELLOW)}\n")
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
            # Se il thinking e' lungo ma non ci sono tool call, il modello e'
            # in loop mentale. Inietta un reminder urgente nel contesto.
            thinking_len = len((msg.get('thinking') or '').split())
            no_action = not msg.get('tool_calls') and not (msg.get('content') or '').strip()
            if thinking_len > 150 and no_action:
                no_action_strikes += 1
                reminder = (
                    f"REASONING LOOP RILEVATO [{no_action_strikes}]: il tuo thinking e' stato"
                    f" troncato dopo {thinking_len} parole ed e' andato perso per sempre."
                    " REGOLA ASSOLUTA: smetti di ragionare e lancia SUBITO un tool call."
                    " Anche solo `echo 'CHECKPOINT: [stato attuale in una riga]'`."
                    " L'output di un comando fallito vale piu' di qualsiasi ragionamento."
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


if __name__ == "__main__":
    detect_max_ctx()
    # Probe HW e scelta del working_ctx iniziale: partiamo piccolo (default 8-16k)
    # invece di allocare KV cache per l'intero MAX_CTX. Se serve, _run_turn
    # bumpa al tier successivo quando il prompt supera l'80%.
    HW = loki_hw.detect_hardware()
    stats['working_ctx'] = loki_hw.initial_working_ctx(MAX_CTX, HW['ram_avail_gb'])
    # Pulizia una tantum di sessioni molto vecchie (skip _autosave e altri _*).
    try:
        loki_persist.prune_old_sessions(SESSIONS_DIR)
    except Exception:
        pass
    sys.stderr.write(loki_hw.hw_line(HW, stats['working_ctx'], MAX_CTX) + "\n")
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
