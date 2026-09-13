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
COMPRESS_AT   = 0.80   # compress at 80% of the context
MAX_CTX       = 32768  # model's real ceiling (detected at boot via ollama.show)
WORKING_CTX   = 8192   # ctx actually passed to Ollama (adaptive, live in stats)
KEEP_ALIVE    = '15m'  # keeps the model loaded in RAM between turns
COMPRESS_CTX  = 8192   # smaller num_ctx dedicated to the compression call
OUTPUT_MAX_LINES  = 100  # rows above which we truncate the output sent to the model
OUTPUT_HEAD_LINES = 40   # first rows to keep
OUTPUT_TAIL_LINES = 30   # last rows to keep
KEEP_RECENT_MSG   = 6    # recent messages preserved after compress (rounded at turn start)
MEMORY_MAX_SUMMARIES = 5  # how many session summaries to keep in the memory file
os.makedirs(SESSIONS_DIR, exist_ok=True)

# Helper modules (pure logic, no UI):
#   loki_hw      -> hardware probe + adaptive num_ctx policy
#   loki_persist -> per-turn autosave + resume-last + prune old
#   loki_mem     -> capped memory file with rotation
import loki_hw
import loki_persist
import loki_mem

HW = None  # dict populated at boot by loki_hw.detect_hardware()

R      = "\033[0m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
ORANGE = "\033[38;5;135m"   # medium purple (main accent)
GREEN  = "\033[38;5;93m"    # dark purple  (ok/secondary states)
RED    = "\033[38;5;196m"
BLUE   = "\033[38;5;39m"
GRAY   = "\033[38;5;244m"
DGRAY  = "\033[38;5;238m"
CYAN   = "\033[38;5;51m"
YELLOW = "\033[38;5;220m"
PURPLE = "\033[38;5;141m"   # lavender (thinking block)

SLASH_COMMANDS = {
    '/help':    'show available commands',
    '/clear':   'clear the conversation',
    '/reset':   'alias of /clear',
    '/model':   'show the model in use',
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
    '/delete':   'delete a saved session [name|number]',
    '/clone':    'clone a saved session   [source] [new_name]',
    '/hw':       'show detected HW (RAM, threads, GPU, working ctx)',
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
    'last_tokens':    0,   # tokens GENERATED in the last response (eval_count)
    'ctx_used':       0,   # tokens in the PROMPT of the last call (prompt_eval_count) — the context-fill signal
    'working_ctx':    WORKING_CTX,  # ctx actually allocated by Ollama (adaptive)
    'pending_images': [],
    'last_command':   None,
    'last_output':    None,
}

tools_schema = [{
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "Run a bash command on Linux and return stdout+stderr",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Bash command to run"}
            },
            "required": ["command"]
        }
    }
}]

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

# Global flag set by async_chat_loop: True when we are inside the
# prompt_toolkit Application. get_app_or_none() does not work from an
# executor thread (ContextVar does not propagate), so we use an explicit flag.
_PINNED_MODE = False

# Event set by the Ctrl+C handler to interrupt the current streaming/turn.
# stream_response checks it between chunks and raises KeyboardInterrupt
# when set, so it reuses the existing interruption handler.
_cancel_event = threading.Event()

# Event True when the model is emitting tool_calls (bash) or when
# run_shell is executing a command: the spinner shows "bashing..." instead
# of "cooking...". Set/cleared in stream_response and run_shell.
_bashing_event = threading.Event()

# ==================== FULLSCREEN v2 =====================
# Shared accumulator for all text going into the output area.
_output_chunks   = []
_output_lock     = threading.Lock()
_app_ref         = None
_follow_bottom   = [True]   # True = stick to bottom (default); False = user scrolled up
_scroll_lines    = [0]      # when NOT following: N lines from the top of content
_last_max_scroll = [0]      # last scrollable amount seen at render (for capping/sync)
_desired_scroll  = [0]      # desired position (updated immediately at each scroll, for virtual cursor)
_output_line_count  = [1]   # number of lines from the last _get_output_ft() — for _get_output_cursor_pos
_output_window_ref  = [None]  # reference to the output Window — to read render_info
_mouse_enabled   = [True]
# Session picker state (activated by /resume with no arguments)
_picker = dict(active=False, sessions=[], cursor=0,
               mode='list', action=0, clone_buf='')
_picker_state_ref = [None]   # reference to state dict for loading the chosen session


def _debug_log(msg):
    """Log to ~/loki_debug.log. Useful for errors otherwise invisible in
    fullscreen (where stdout goes to void or into the output area)."""
    try:
        with open(os.path.expanduser("~/loki_debug.log"), "a") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def _output_append(text):
    """Accumulate text into the output area. Handles '\\r' (return to start
    of current line and overwrite) so progress bars and the like work."""
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
    """File-like that intercepts sys.stdout and feeds the output area."""
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
    """Scroll up by N lines. The first scroll-up exits 'follow-bottom' mode
    and pins the manual position to the current value (known max_scroll)."""
    if _follow_bottom[0]:
        _follow_bottom[0] = False
        # If _last_max_scroll is not yet updated (first render did not
        # happen yet), fall back to logical line count as a reasonable value.
        _scroll_lines[0] = _last_max_scroll[0] if _last_max_scroll[0] > 0 else max(0, _output_line_count[0] - 1)
    _scroll_lines[0] = max(0, _scroll_lines[0] - step)
    _desired_scroll[0] = _scroll_lines[0]
    if _app_ref is not None:
        _app_ref.invalidate()


def _scroll_down(step):
    """Scroll down by N lines. If you return to the bottom (or past it)
    'follow-bottom' mode is re-entered so new content shows up on its own."""
    if _follow_bottom[0]:
        return  # already at the bottom, nothing to do
    _scroll_lines[0] += step
    if _scroll_lines[0] >= _last_max_scroll[0]:
        _follow_bottom[0] = True
        _scroll_lines[0]  = 0
        _desired_scroll[0] = _last_max_scroll[0]
    else:
        _desired_scroll[0] = _scroll_lines[0]   # update immediately
    if _app_ref is not None:
        _app_ref.invalidate()


def _scroll_to_bottom():
    _follow_bottom[0] = True
    _scroll_lines[0]  = 0
    _desired_scroll[0] = _last_max_scroll[0]
    if _app_ref is not None:
        _app_ref.invalidate()


def _get_output_cursor_pos() -> Point:
    """Return the virtual cursor position of the FormattedTextControl.
    Uses _output_line_count (updated by _get_output_ft() in the same frame)
    to stay inside the bounds of the rendered content.
    Also refreshes _last_max_scroll from the previous frame's render_info,
    so _scroll_up/_scroll_down have a real cap (with wrap_lines=True
    get_vertical_scroll is never called)."""
    try:
        lc = _output_line_count[0]
        # Refresh max scroll by reading the previous frame's render_info.
        # visible_line_numbers is the list of visible logical lines; its
        # unique length = how many logical lines fit in the window right now.
        win = _output_window_ref[0]
        if win is not None:
            ri = win.render_info
            if ri is not None and ri.displayed_lines:
                vis = len(set(ri.displayed_lines))   # unique visible logical lines
                _last_max_scroll[0] = max(0, lc - vis)
        if _follow_bottom[0]:
            return Point(x=0, y=max(0, lc - 1))
        return Point(x=0, y=min(_desired_scroll[0], max(0, lc - 1)))
    except Exception as e:
        import traceback as _tb
        _debug_log(f"_get_output_cursor_pos ERROR: {e}\n{_tb.format_exc()}")
        return Point(x=0, y=0)


# ── Session Picker ────────────────────────────────────────────────────────────

def _picker_load_sessions():
    """Load the session list ordered from most recent."""
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
        if secs < 60:    return f"{secs}s ago"
        if secs < 3600:  return f"{secs//60}m ago"
        if secs < 86400: return f"{secs//3600}h ago"
        return f"{secs//86400}d ago"
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
    """Generate the ANSI text for the picker to show in the output area."""
    ss  = _picker['sessions']
    cur = _picker['cursor']
    mode= _picker['mode']
    act = _picker['action']
    W   = max(50, term_width() - 6)

    out = []
    out.append(f"\n  {BOLD}{ORANGE}SAVED SESSIONS{R}  {DIM}{len(ss)} sessions{R}")
    out.append(f"  {DGRAY}{'─' * (W - 2)}{R}")

    if not ss:
        out.append(f"\n  {DIM}no saved sessions{R}")
        out.append(f"\n  {GRAY}Esc{R} = close")
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

        # Action labels: shown inline only on the selected row
        if sel and mode in ('list', 'actions', 'delete_confirm', 'clone_input'):
            del_lbl = f"{BOLD}{RED}[✕ Delete]{R}" if (mode == 'actions' and act == 0) else f"{DGRAY}[✕ Delete]{R}"
            cln_lbl = f"{BOLD}{GREEN}[⎘ Clone]{R}" if (mode == 'actions' and act == 1) else f"{DGRAY}[⎘ Clone]{R}"
            emoji_str = f"  {del_lbl}  {cln_lbl}"
        else:
            emoji_str = ""

        if sel and mode == 'delete_confirm':
            out.append(f"  {arrow} {name_str:<28}  {elapsed_str}  {msgs_str}{emoji_str}")
            d = f"{RED if act==0 else DGRAY}[ Yes, delete ]{R}"
            a = f"{GREEN if act==1 else DGRAY}[ Cancel ]{R}"
            out.append(f"       {d}  {a}  {DGRAY}(← → choose, Enter to confirm){R}")
        elif sel and mode == 'clone_input':
            out.append(f"  {arrow} {name_str:<28}  {elapsed_str}  {msgs_str}{emoji_str}")
            out.append(f"  {DIM}Type the new name in the field below and press Enter{R}  {DGRAY}(Esc = cancel){R}")
        else:
            out.append(f"  {arrow} {name_str:<28}  {elapsed_str}  {msgs_str}{emoji_str}")

    out.append(f"\n  {DGRAY}{'─' * (W - 2)}{R}")
    if mode in ('list', 'actions'):
        out.append(f"  {GRAY}↑↓{R} navigate  {GRAY}Enter{R} open  {GRAY}→{R} pick action  {GRAY}Esc{R} exit")
    return '\n'.join(out)


def _picker_do_load(name):
    """Load the selected session: closes the picker, calls resume_session."""
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


# ── End Session Picker ────────────────────────────────────────────────────────

def _wheel_scroll_output(mouse_event):
    """Shared handler: wheel up/down → scroll output."""
    et = mouse_event.event_type
    if et == MouseEventType.SCROLL_UP:
        _scroll_up(3)
        return None
    if et == MouseEventType.SCROLL_DOWN:
        _scroll_down(3)
        return None
    return NotImplemented


class _ScrollableOutputControl(FormattedTextControl):
    """FormattedTextControl + wheel handler for the output area itself."""
    def mouse_handler(self, mouse_event):
        res = _wheel_scroll_output(mouse_event)
        if res is NotImplemented:
            return NotImplemented
        return res


class _WheelFTControl(FormattedTextControl):
    """'Passive' FormattedTextControl that intercepts wheel events and forwards
    them to the output area — used for toolbar/separator/spinner so the wheel
    works even when the cursor is over them."""
    def mouse_handler(self, mouse_event):
        res = _wheel_scroll_output(mouse_event)
        if res is NotImplemented:
            return super().mouse_handler(mouse_event)
        return res


class _WheelBufferControl(BufferControl):
    """BufferControl that intercepts the wheel and forwards it to the output area,
    instead of trying to scroll inside the (small) input buffer."""
    def mouse_handler(self, mouse_event):
        res = _wheel_scroll_output(mouse_event)
        if res is NotImplemented:
            return super().mouse_handler(mouse_event)
        return res


def write(text):
    sys.stdout.write(text)
    # In pinned mode patch_stdout's StdoutProxy accumulates until '\n'
    # and redraws the prompt on each emit: flushing char-by-char yields
    # one char per line. Outside patch_stdout we need the explicit flush.
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
    # In fullscreen: clear the output buffer instead of writing escape "clear" to the tty.
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
        f"  {c('/help', ORANGE)} for help  {c('·', DGRAY)}  {c('/exit', ORANGE)} to quit",
        "",
        f"  {c('cwd', DIM)}    {os.getcwd()}",
        f"  {c('model', DIM)}  {MODEL}",
    ]
    box(lines)
    tips = [
        "Type '/' to see the available commands",
        "Use \\ + Enter to add a newline",
        "Up/down arrows to navigate history",
        "Ctrl+R to search history",
        "/last to review the last full output",
        "/think toggles visible reasoning",
        "/auto to auto-approve commands",
        "Ctrl+C during streaming stops the current turn",
        "Alt+M to re-enable mouse selection",
    ]
    print(f"\n  {c('Tip', GRAY)} {c(random.choice(tips), DIM)}\n")

def show_help():
    print()
    print(f"  {c('COMMANDS', BOLD)}")
    for cmd, desc in SLASH_COMMANDS.items():
        print(f"    {c(cmd.ljust(12), ORANGE)} {c(desc, GRAY)}")
    print()
    print(f"  {c('KEYBOARD', BOLD)}")
    keys = [
        ("Enter",           "send message"),
        ("\\ + Enter",      "new line"),
        ("up/down arrows",  "command history"),
        ("Ctrl+R",          "search history"),
        ("Ctrl+C / Ctrl+D", "exit"),
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
    print(f"  {c('SESSION', BOLD)}")
    print(f"    {c('elapsed', DIM):<20} {mins}m {secs}s")
    print(f"    {c('messages', DIM):<20} {stats['messages']}")
    print(f"    {c('commands ok', DIM):<20} {c(str(stats['tools_ok']), GREEN)}")
    print(f"    {c('commands no', DIM):<20} {c(str(stats['tools_no']), RED)}")
    print(f"    {c('auto approve', DIM):<20} {auto_state}")
    print(f"    {c('show thinking', DIM):<20} {think_state}")
    print(f"    {c('auto continue', DIM):<20} {ac_state}")
    print(f"    {c('working ctx', DIM):<20} {stats['working_ctx']} / {MAX_CTX} tk")
    if stats['ctx_used']:
        pct = int(100 * stats['ctx_used'] / stats['working_ctx'])
        print(f"    {c('last ctx used', DIM):<20} {stats['ctx_used']} tk ({pct}%)")
    print()

def _confirm_sync(command):
    """Confirmation modal via input() on stdin. Used in legacy mode or
    inside run_in_terminal (which detaches stdin from the pinned app)."""
    print()
    print(f"  {c('⏺', ORANGE)} {c('Bash', BOLD)}  {c(command, CYAN)}")
    print(f"     {c('1', BOLD)} {c('·', DGRAY)} Yes, run")
    print(f"     {c('2', BOLD)} {c('·', DGRAY)} Yes, and don't ask again for this session")
    print(f"     {c('3', BOLD)} {c('·', DGRAY)} No, stop here")
    while True:
        try:
            choice = input(f"     {c('❯', ORANGE)} ").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        if choice in ('1', 'y', 'Y', 's', 'S', ''):
            return True
        if choice == '2':
            stats['auto_approve'] = True
            print(c("     auto approve enabled for this session", YELLOW))
            return True
        if choice in ('3', 'n', 'N'):
            return False
        print(c("     invalid choice", RED))


# Reference to the main event loop, populated when async_chat_loop starts.
_MAIN_LOOP = None


def confirm_command(command):
    if stats['auto_approve']:
        print()
        print(f"  {c('⏺', GREEN)} {c('Bash', BOLD)}  {c(command, CYAN)}  {c('[auto]', DIM)}")
        return True

    app = get_app_or_none()
    if app is None or _MAIN_LOOP is None:
        return _confirm_sync(command)

    # Pinned async mode: suspend the app with run_in_terminal and ask for confirmation
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
        print(c("     confirm timeout — command rejected", RED))
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
        # No blocking input(): just show a hint.
        # The user can always reread the full output with /last.
        print(f"     {c(f'{hidden} lines hidden — use /last to see the full output', DIM)}")

def truncate_for_model(output):
    lines = output.splitlines()
    total = len(lines)
    if total <= OUTPUT_MAX_LINES:
        return output
    head    = lines[:OUTPUT_HEAD_LINES]
    tail    = lines[-OUTPUT_TAIL_LINES:]
    omitted = total - OUTPUT_HEAD_LINES - OUTPUT_TAIL_LINES
    sep     = f"\n[... {omitted} lines omitted — use grep/head/tail if you need the middle ...]\n"
    return "\n".join(head) + sep + "\n".join(tail)

def _maybe_sudo_apt(command):
    stripped = command.lstrip()
    if re.match(r'apt(?:-get)?\s', stripped) and not command.startswith('sudo'):
        return 'sudo ' + command
    return command

def run_shell(command):
    command = _maybe_sudo_apt(command)
    if not confirm_command(command):
        stats['tools_no'] += 1
        _bashing_event.clear()
        return "COMMAND REJECTED BY THE USER"
    stats['tools_ok'] += 1
    _bashing_event.set()  # spinner -> "bashing..." for the whole subprocess duration
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=30
        )
        output = (result.stdout + result.stderr).strip() or "(no output)"
        stats['last_command'] = command
        stats['last_output'] = output                # full text for /last
        model_output = truncate_for_model(output)    # truncated for the model
        print_output_block(command, output)          # full display (UI truncates at PREVIEW_LINES)
        return model_output
    except subprocess.TimeoutExpired:
        print(c("     TIMEOUT (>30s)", RED))
        return "TIMEOUT: command exceeded 30 seconds"
    finally:
        # Command finished: revert to "cooking..." for the possible
        # model continuation that reflects on the output.
        _bashing_event.clear()

def show_last():
    if not stats['last_output']:
        print(c("  no previous output\n", DIM))
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
    print(f"  {c('memory updated:', DIM)} {MEMORY_FILE}\n")

def show_memory():
    mem = load_memory()
    if not mem:
        print(c("  memory empty\n", DIM))
        return
    print(f"\n  {c('MEMORY', BOLD)}\n")
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
    mem = load_memory()
    if mem:
        return base + f"\n\n## PERSISTENT MEMORY\n{mem}"
    return base

def _find_turn_start(messages, idx):
    """Move idx backwards until it lands on a 'user' message.

    Prevents 'recent' from starting with an orphan 'tool' (whose assistant/tool_call
    ended up in to_compress) or an assistant reply to a now-lost tool_call.
    A 'user' message is a clean turn boundary.
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
            body = f"{body[:half]}\n[... {omitted} chars omitted ...]\n{body[-half:]}"
        return f"[tool_output]: {body}"
    return f"[{role}]: {content[:txt_lim]}"


def _rotate_memory_summaries():
    """Keep at most MEMORY_MAX_SUMMARIES session summaries in the file.

    User manual notes (`/remember`) at the top stay intact. Also applies a
    byte hard cap via loki_mem.enforce_hard_cap: if after rotation the file
    is still too large (many manual notes, giant summaries), it trims from
    the top with an explicit marker.
    """
    loki_mem.rotate_summaries(MEMORY_FILE, max_summaries=MEMORY_MAX_SUMMARIES)
    loki_mem.enforce_hard_cap(MEMORY_FILE)


def detect_max_ctx():
    """Query Ollama for the model's real context length.

    If the call fails (Ollama down, model not found) it leaves the default.
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


def compress_context(messages):
    if len(messages) <= 3:
        print(c("  conversation too short to compress\n", DIM))
        return messages

    keep_start = max(1, len(messages) - KEEP_RECENT_MSG)
    keep_start = _find_turn_start(messages, keep_start)
    to_compress = messages[1:keep_start]
    if not to_compress:
        print(c("  nothing to compress\n", DIM))
        return messages

    MAX_TRANSCRIPT = 12000  # more room: we now include tool_output too
    raw_transcript = "\n\n".join(_fmt_msg_for_summary(m) for m in to_compress)
    if len(raw_transcript) > MAX_TRANSCRIPT:
        half = MAX_TRANSCRIPT // 2
        transcript = (
            raw_transcript[:half]
            + f"\n\n[... {len(raw_transcript) - MAX_TRANSCRIPT} chars omitted ...]\n\n"
            + raw_transcript[-half:]
        )
    else:
        transcript = raw_transcript

    BAR_W   = 24
    EST_MAX = 1500   # estimated tokens for the summary (bar ceiling)
    SPIN    = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

    def _render_bar(chars, done=False):
        approx = max(chars // 4, 1)
        filled = min(BAR_W, int(BAR_W * approx / EST_MAX)) if not done else BAR_W
        bar    = '█' * filled + '░' * (BAR_W - filled)
        color  = GREEN if done else ORANGE
        return f"\r  {c('◎ compressing', PURPLE)}  {DGRAY}[{R}{color}{bar}{R}{DGRAY}]{R}  {c(f'~{approx}tk', DGRAY)}  "

    # show spinner while the model processes the prompt (before the first token)
    spin_i     = [0]
    first_seen = [False]

    def _spin():
        ch = SPIN[spin_i[0] % len(SPIN)]
        write(f"\r  {c('◎ compressing', PURPLE)}  {DGRAY}{ch} processing...{R}        ")
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
                "content": "You are an assistant that summarizes conversations between a user and a shell agent in a dense, precise way in English."
            }, {
                "role": "user",
                "content": (
                    "Summarize this conversation into a concise markdown block. "
                    "[tool_call] blocks are executed commands, [tool_output] blocks are their output. "
                    "Preserve: technical facts discovered, files/paths/hosts/ports encountered, "
                    "decisions taken, user goals not yet completed, and any relevant errors. "
                    f"Ignore small talk and trivial confirmations:\n\n{transcript}"
                )
            }],
            stream=True,
            # compress is a one-shot on a transcript already trimmed to 12000 chars:
            # we don't need all of MAX_CTX, a small ceiling avoids reallocating
            # a huge KV cache for a short task.
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
        write(f"\r  {c('⚠ compression interrupted — context unchanged', YELLOW)}        \n\n")
        return messages
    except Exception as e:
        stop_spin.set()
        write(f"\r  {c(f'compression error: {e}', RED)}        \n\n")
        return messages

    stop_spin.set()

    write(_render_bar(chars, done=True))
    write(f"\r  {c('◎ compressing', PURPLE)}  {c('[' + '█' * BAR_W + ']', GREEN)}  {c('✓ done', GREEN)}        \n")

    summary = "".join(summary_parts)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    append_memory(f"### Compressed session {ts}\n{summary}")
    _rotate_memory_summaries()

    recent = messages[keep_start:]
    recent_clean = []
    for m in recent:
        if m.get('role') == 'assistant' and m.get('thinking'):
            m = {k: v for k, v in m.items() if k != 'thinking'}
        recent_clean.append(m)
    new_messages = [{"role": "system", "content": build_system_prompt()}] + recent_clean
    print(f"  {c('✓ saved to memory', GREEN)}  {c(f'{len(to_compress)} messages compressed', GRAY)}\n")
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
    print(f"\n  {c('session saved:', DIM)} {c(name, ORANGE)}  {c(path, DGRAY)}\n")
    return name

def _replay_message(m):
    """Reprint a single saved message in the chat's compact format."""
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
            print(f"     {c(f'     ... {len(lines) - 15} lines omitted', DIM)}")
        print(f"     {c('└' + '─' * w, DGRAY)}")


def resume_session(arg, messages):
    """/resume — if `arg` is empty: list. Otherwise: load + reprint history.
    Returns the new messages list to put in state, or None."""
    files = sorted(
        [f for f in os.listdir(SESSIONS_DIR) if f.endswith('.json')],
        reverse=True,
    )
    entries = [f[:-5] for f in files]

    # -- no argument: list --
    if not arg:
        if not entries:
            print(c("\n  no saved sessions\n", DIM))
            return None
        print()
        print(f"  {c('SAVED SESSIONS', BOLD)}")
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
        print(f"\n  {c('to resume one:', DIM)} {c('/resume <number|name>', ORANGE)}\n")
        return None

    # -- with argument: load --
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(entries):
            arg = entries[idx]
        else:
            print(c(f"\n  invalid number: {arg}\n", RED))
            return None
    path = _session_path(arg)
    if not os.path.isfile(path):
        print(c(f"\n  session not found: {arg}\n", RED))
        return None
    with open(path) as f:
        data = json.load(f)
    loaded = data.get('messages', [])
    n_user = len([m for m in loaded if m['role'] == 'user'])

    # Clear the output area and reprint banner + history.
    if _PINNED_MODE:
        with _output_lock:
            _output_chunks.clear()
    fill = max(1, term_width() - 4)
    print(f"\n  {c('◈', GREEN)} {c('Session resumed:', DIM)} {c(arg, ORANGE)}  "
          f"{c(f'{n_user} user messages', GRAY)}")
    print(f"  {c('─' * fill, GRAY)}")
    for m in loaded:
        _replay_message(m)
    print(f"\n  {c('─' * fill, GRAY)}")
    print(f"  {c('▸ keep typing below', DIM)}\n")

    return messages[:1] + loaded  # system prompt + resumed messages

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
        print(f"  {c('model:', DIM)} {MODEL}\n")
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
            print(c("  usage: /remember <text>\n", GRAY))
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
        print(f"  {c('show thinking:', DIM)} {state}\n")
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
                # In fullscreen: activate the interactive picker
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
                print(c("  no autosave present\n", DIM))
            else:
                hrs = age // 3600
                print(c(f"  autosave too old ({hrs}h ago) — ignored\n", DIM))
            return 'handled', None
        age = loki_persist.autosave_age_seconds(SESSIONS_DIR) or 0
        mins_ago = age // 60
        raw = payload.get('messages', [])
        new_msgs = [{"role": "system", "content": build_system_prompt()}] + raw
        n_user = sum(1 for m in raw if m.get('role') == 'user')
        print(f"\n  {c('◈ autosave restored', GREEN)}  "
              f"{c(f'{n_user} user messages · {mins_ago}m ago', GRAY)}\n")
        return 'resume', new_msgs
    if cmd == '/hw':
        if HW is None:
            print(c("  HW not detected yet\n", DIM))
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
            print(c("  usage: /delete <name|number>\n", GRAY))
            return 'handled', None
        entries = [f[:-5] for f in sorted(os.listdir(SESSIONS_DIR), reverse=True) if f.endswith('.json')]
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(entries):
                arg = entries[idx]
            else:
                print(c(f"  invalid number: {arg}\n", RED))
                return 'handled', None
        path = _session_path(arg)
        if not os.path.isfile(path):
            print(c(f"  session not found: {arg}\n", RED))
        else:
            os.remove(path)
            print(f"  {c('deleted:', DIM)} {c(arg, ORANGE)}\n")
        return 'handled', None
    if cmd == '/save':
        if messages:
            save_session(messages, arg.strip() or None)
        else:
            print(c("  no messages to save\n", DIM))
        return 'handled', None
    if cmd == '/clone':
        cparts = arg.strip().split(maxsplit=1)
        if len(cparts) < 2:
            print(c("  usage: /clone <source> <new_name>\n", GRAY))
            return 'handled', None
        src_arg, dst_name = cparts[0], cparts[1].strip()
        entries = [f[:-5] for f in sorted(os.listdir(SESSIONS_DIR), reverse=True) if f.endswith('.json')]
        if src_arg.isdigit():
            idx = int(src_arg) - 1
            if 0 <= idx < len(entries):
                src_arg = entries[idx]
            else:
                print(c(f"  invalid number: {src_arg}\n", RED))
                return 'handled', None
        src_path = _session_path(src_arg)
        dst_path = _session_path(dst_name)
        if not os.path.isfile(src_path):
            print(c(f"  session not found: {src_arg}\n", RED))
        elif os.path.isfile(dst_path):
            print(c(f"  a session with this name already exists: {dst_name}\n", RED))
        else:
            import shutil
            shutil.copy2(src_path, dst_path)
            print(f"  {c('cloned:', DIM)} {c(src_arg, GRAY)} {c('→', DGRAY)} {c(dst_name, ORANGE)}\n")
        return 'handled', None
    if cmd == '/img':
        path = os.path.expanduser(arg.strip().strip('"\''))
        if os.path.isfile(path):
            stats['pending_images'].append(path)
            print(f"  {c('image attached:', DIM)} {path}")
            print(f"  {c('it will be sent with the next message', GRAY)}\n")
        else:
            print(c(f"  file not found: {path}\n", RED))
        return 'handled', None
    print(c(f"  unknown command: {cmd}\n", RED))
    return 'handled', None

def build_session(on_submit=None):
    """Build the PromptSession.

    If `on_submit` is passed (async pinned mode), Enter calls the callback
    and resets the buffer without returning from prompt_async(): this way
    the prompt stays visible with its toolbar. If None, legacy synchronous
    behavior.
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
        refresh_interval=1.0,   # refresh the toolbar (timer, ctx%) every second
    )

class WordFlow:
    """Word-aware wrapper for streaming output.

    Buffers characters until a word boundary (space/newline), then decides
    whether the word fits in the current line: if not, it wraps BEFORE writing
    it. Words longer than a full line are force-split.
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
        print(c(f"  Ollama error: {e}\n", RED))
        return None

    try:
        for chunk in stream:
            # Ctrl+C from the app: interrupt the stream as if it were a
            # real KeyboardInterrupt, so we reuse the except branch below
            # that closes pending blocks and saves partial content.
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
                # The model is emitting a run_shell call: the spinner flips
                # to "bashing..." to signal we are about to execute (or are
                # already executing) shell code.
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
        write(f"\n  {GRAY}⚠ interrupted{R}\n\n")
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
    """Builds a smarter 'continue' message telling the model where it left off."""
    thinking = (last_msg.get('thinking') or '').strip()
    content  = (last_msg.get('content')  or '').strip()
    if content:
        tail = content[-500:]
        return (
            "continue the response cut off by the token limit. "
            f"You were writing: «…{tail}» — "
            "go on without repeating what you already wrote."
        )
    if thinking:
        thinking_tail = "\n".join(thinking.splitlines()[-12:])
        return (
            "your reasoning was cut off by the token limit.\n"
            f"Last thoughts:\n```\n{thinking_tail}\n```\n"
            "Complete the reasoning and produce the final answer."
        )
    return "continue"

def _run_turn(state):
    """Run the full turn (streaming + tool loop) in the executor thread.

    Calls stream_response and, if the model produced tool_calls, runs the
    tools and re-invokes stream_response, like the old synchronous chat_loop.
    All output goes through print()/write() and patch_stdout shows it above
    the pinned prompt.
    """
    messages = state['messages']
    consecutive_continues = 0
    while True:
        msg = stream_response(messages)
        if msg is None:
            messages.pop()
            break
        messages.append(msg)

        # If the user pressed Ctrl+C during the stream, stream_response
        # already returned saving the partial: leave the tool loop without
        # re-invoking the model.
        if _cancel_event.is_set():
            break

        if msg['tool_calls']:
            consecutive_continues = 0
            for tc in msg['tool_calls']:
                fn = tc.get('function', {})
                if fn.get('name') == 'run_shell':
                    args = fn.get('arguments', {})
                    cmd_arg = args.get('command', '') if isinstance(args, dict) else ''
                    output = run_shell(cmd_arg)
                    messages.append({"role": "tool", "content": output})
            continue
        elif msg.get('done_reason') == 'length' and stats['auto_continue']:
            consecutive_continues += 1
            if consecutive_continues >= 2:
                print(f"\n  {c('⚠ too many consecutive continues — compressing before moving on...', YELLOW)}")
                state['messages'] = compress_context(messages)
                messages = state['messages']
                consecutive_continues = 0
            print(f"  {c('↻ token limit reached — continuing...', GRAY)}\n")
            messages.append({"role": "user", "content": build_continue_message(msg)})
            continue
        else:
            consecutive_continues = 0
            # Adaptive policy: when the prompt exceeds 80% of the current
            # working_ctx, first try to extend the tier (cheaper than
            # compressing). If working_ctx is already at the model ceiling,
            # we compress. Either way Ollama is called with the updated
            # `num_ctx` on the next turn.
            if stats['ctx_used'] >= int(stats['working_ctx'] * COMPRESS_AT):
                new_ctx = loki_hw.next_working_ctx(stats['working_ctx'], MAX_CTX)
                if new_ctx > stats['working_ctx']:
                    old = stats['working_ctx']
                    stats['working_ctx'] = new_ctx
                    print(f"\n  {c(f'↗ working ctx extended: {old} -> {new_ctx} tk', GRAY)}\n")
                else:
                    pct  = int(100 * stats['ctx_used'] / MAX_CTX)
                    used = stats['ctx_used']
                    msg_txt = f"⚠ context at {pct}% ({used}/{MAX_CTX} tk) — compressing automatically..."
                    print(f"\n  {c(msg_txt, YELLOW)}")
                    state['messages'] = compress_context(messages)
                    messages = state['messages']
            break

    # Autosave after every completed (or interrupted) turn: the user must
    # not lose a long session because of a terminal or Ollama crash.
    try:
        loki_persist.autosave_session(state['messages'], MODEL, SESSIONS_DIR)
    except Exception:
        pass


def _handle_slash(text, state):
    """Handle slash commands in async mode. Returns True to signal exit."""
    action, payload = parse_slash(text, state['messages'])
    if action == 'exit':
        return True
    if action == 'clear':
        state['messages'] = state['messages'][:1]
        stats['pending_images'].clear()
        welcome()
        return False
    if action == 'resume':
        # resume_session already cleared the output and reprinted banner + history,
        # so we do NOT call welcome() here.
        state['messages'] = payload
        stats['pending_images'].clear()
        return False
    if action == 'compress':
        state['messages'] = compress_context(state['messages'])
        return False
    if action == 'picker':
        _picker_activate(state)
        return False
    return False  # 'handled' and others: output already printed by parse_slash


async def _process_input(text, state):
    """Async task that processes a single user submission."""
    if state['processing']:
        return
    _cancel_event.clear()   # new turn: restart
    _bashing_event.clear()  # spinner restarts as "cooking..."
    state['processing']   = True
    state['current_task'] = asyncio.current_task()
    try:
        # Echo the input into the output area
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
            print(f"  {c(f'[{n_imgs} image(s) attached]', DIM)}")
            stats['pending_images'].clear()
        state['messages'].append(message)

        loop = asyncio.get_running_loop()
        turn_fut = loop.run_in_executor(None, _run_turn, state)
        try:
            await turn_fut
        except asyncio.CancelledError:
            # Ctrl+C canceled the outer task. The executor thread however
            # keeps running (Python cannot kill threads). Give it 2s to
            # notice _cancel_event, then free the prompt regardless.
            _cancel_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(turn_fut), timeout=2.0)
                print(c("  ✓ turn cleanly interrupted", GREEN))
            except asyncio.TimeoutError:
                print(c("  ⚠ turn still running in background — prompt freed anyway", RED))
            except Exception:
                pass
    finally:
        state['current_task'] = None
        state['processing']   = False


async def async_chat_loop():
    """Main loop in 'pinned' mode: a single prompt_async living for the whole
    session, where Enter processes in-place instead of returning from the
    prompt. patch_stdout(raw=True) makes the streaming appear above the input
    line, with the input box and toolbar always at the bottom.
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
        # Sync callback from the Enter handler in build_session.
        # Schedules processing on the main loop.
        if state['processing']:
            return
        _MAIN_LOOP.create_task(_process_input(text, state))

    session = build_session(on_submit=on_submit)

    # Add Ctrl+C / Ctrl+D to the keybindings already built inside build_session.
    kb = session.key_bindings

    @kb.add('c-c')
    def _(event):
        buf = event.current_buffer
        if buf.text:
            buf.reset()
            return
        now = time.time()
        if state['processing']:
            # Interrupt the current turn. stream_response checks
            # _cancel_event between chunks and saves the partial.
            _cancel_event.set()
            print(c("\n  ⚠ interruption requested...", YELLOW))
            return
        if now - state['ctrl_c_ts'] <= 1.0:
            event.app.exit()
            return
        state['ctrl_c_ts'] = now
        print(c("  press Ctrl+C again to exit", GRAY))

    @kb.add('c-d')
    def _(event):
        if not event.current_buffer.text and not state['processing']:
            event.app.exit()

    welcome()

    def get_prompt():
        # Continuous separator above the input box, recomputed on each
        # render so it adapts to terminal resizes.
        w = max(20, term_width())
        return ANSI(f"{DGRAY}{'─' * w}{R}\n  {ORANGE}❯{R} ")

    with patch_stdout(raw=True):
        try:
            await session.prompt_async(get_prompt)
        except (EOFError, KeyboardInterrupt):
            pass

    print(c("\n  Goodbye.\n", DIM))


def _make_toolbar_fs():
    """Toolbar for fullscreen mode (analog of bottom_toolbar in build_session)."""
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
    mouse_hint  = '' if _mouse_enabled[0] else f"  {YELLOW}✂ selection{R}"
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
    # Right-pad to the end of the terminal so the toolbar's dark background
    # covers the full width (without padding the right side stays transparent/inconsistent).
    plain_len = len(strip_ansi(content))
    w = term_width()
    if plain_len < w:
        content = content + ' ' * (w - plain_len)
    return ANSI(content)


async def async_chat_loop_fullscreen():
    """Fullscreen loop: Application with an HSplit Layout of 4 zones.
    The output area scrolls with the mouse wheel (via _ScrollableOutputControl),
    the input box is pinned at the bottom, and the toolbar sits below.

    On error, restores stdout and writes a traceback to ~/loki_debug.log.
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

    # ----- BUILD THE LAYOUT BEFORE redirecting stdout -----
    # If something blows up here, the error goes to the real terminal, not into the void.
    try:
        # Output area (with wheel handler). Top-aligned, no padding: the banner
        # sits at the top and messages flow underneath as they arrive.
        def _get_output_ft():
            if _picker['active']:
                try:
                    rendered = _picker_render()
                    _output_line_count[0] = rendered.count('\n') + 1
                    return ANSI(rendered)
                except Exception as e:
                    _debug_log(f"_picker_render ERROR: {e}")
                    _output_line_count[0] = 1
                    return ANSI(f"  {RED}Picker error: {e}{R}")
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

        # Spinner "cooking..." / "bashing..." — visible only while
        # state['processing'] is True. Frames cycle ~6 times per second.
        # Becomes "bashing..." when _bashing_event is set, i.e. when the
        # model is emitting tool_calls or when run_shell is running.
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

        # Separator
        def _get_sep_ft():
            # GRAY instead of DGRAY: DGRAY was invisible on dark themes.
            return ANSI(f"{GRAY}{'─' * max(20, term_width())}{R}")

        separator = Window(
            content=_WheelFTControl(text=_get_sep_ft, focusable=False),
            height=1,
        )

        # Input area (Buffer + BufferControl with BeforeInput for the "❯")
        # read_only when the picker is active (except in clone_input): prevents
        # character insertion in the buffer without needing an <any> catch-all.
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

        # Enter handler local to the buffer
        kb_input = KeyBindings()

        def on_submit(text):
            if state['processing']:
                return
            _MAIN_LOOP.create_task(_process_input(text, state))

        # eager=True: Enter is consumed immediately, without first handing
        # off to the autocomplete menu (which would otherwise "accept the
        # completion" instead of submitting — the /help bug).
        # filter=not picker: when the picker is active this binding must not
        # match, otherwise it wins over _p_enter (kb_app comes first in the
        # match list and matches[-1] is always the control-level).
        _not_picker_cond = Condition(lambda: not _picker['active'])
        @kb_input.add('enter', eager=True, filter=_not_picker_cond)
        def _(event):
            buf = event.current_buffer
            # If an autocomplete menu is open, close it before continuing.
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
        # dont_extend_height=True: the Window adapts to the real content of
        # the buffer. Empty buffer = 1 line, grows up to max=8 for multiline
        # input (with \+Enter).
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

        # App-level keybindings (Ctrl+C, Ctrl+D, PageUp/PageDown for keyboard scroll)
        kb_app = KeyBindings()

        @kb_app.add('c-c')
        def _(event):
            buf = event.current_buffer
            if buf.text:
                buf.reset()
                return
            now = time.time()
            if state['processing']:
                # Signal cancel to the thread (checked between chunks). Also
                # cancel the outer async task: _process_input will catch
                # CancelledError, wait 2s for the thread to release, and free
                # 'processing' anyway so the prompt is available even if the
                # thread is stuck.
                _cancel_event.set()
                task = state.get('current_task')
                if task is not None and not task.done():
                    task.cancel()
                print(c("\n  ⚠ interruption requested...", YELLOW))
                return
            if now - state['ctrl_c_ts'] <= 1.0:
                event.app.exit()
                return
            state['ctrl_c_ts'] = now
            print(c("  press Ctrl+C again to exit", GRAY))

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

        # End = jump to bottom; Home = jump to top of content
        @kb_app.add('end')
        def _(event):
            _scroll_to_bottom()

        @kb_app.add('home')
        def _(event):
            _follow_bottom[0] = False
            _scroll_lines[0]  = 0
            event.app.invalidate()

        # Alt+M: toggle mouse capture (for copy/paste with the mouse)
        @kb_app.add('escape', 'm')
        def _(event):
            _mouse_enabled[0] = not _mouse_enabled[0]
            new_state = 'ON' if _mouse_enabled[0] else 'OFF'
            hint = 'wheel scroll active' if _mouse_enabled[0] else 'you can now select with the mouse'
            col  = GREEN if _mouse_enabled[0] else GRAY
            print(f"  {c(f'mouse: {new_state}', col)}  {c(hint, DIM)}")
            event.app.invalidate()

        # ── History navigation (↑↓ outside the picker) ──────────────────────────
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
                pass  # handled by backspace
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
                # Cancel clone: empty the buffer and go back to actions
                event.current_buffer.reset()
                _picker['mode']   = 'actions'
                _picker['action'] = 1  # clone was action=1
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
                    # Enter clone_input: set the mode BEFORE touching the buffer
                    # (so _buf_readonly becomes False and insert_text works).
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
                # Read the name from the input buffer (where the user typed)
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

        # ── End picker key bindings ────────────────────────────────────────────
        # Note: no <any> catch-all — the buffer has read_only=_buf_readonly
        # which blocks text insertion in list/actions/delete_confirm modes.
        # In clone_input the buffer is editable and characters flow directly
        # into the buffer (which we read to get the final name in _p_enter).

        # FloatContainer for the autocomplete menu: appears above the input as
        # a popup anchored to the cursor while typing "/..."
        body = HSplit([
            output_window,
            spinner_container,   # visible only while Loki is working
            separator,
            input_window,        # 1 line by default, grows for multiline
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

        # Condition tied to the mutable flag: at render time prompt_toolkit
        # re-reads it and enables/disables terminal mouse tracking (emits
        # the 1000/1006 escape sequences).
        mouse_cond = Condition(lambda: _mouse_enabled[0])

        app = Application(
            layout=layout,
            key_bindings=kb_app,
            full_screen=True,
            style=app_style,
            refresh_interval=0.15,  # cadence for animating the "cooking..." spinner
            mouse_support=mouse_cond,
        )

        # Override _handle_exception to log the full traceback
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
        welcome()  # writes into _output_chunks via redirect
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

    print(c("\n  Goodbye.\n", DIM))


if __name__ == "__main__":
    detect_max_ctx()
    # HW probe + choice of the initial working_ctx: we start small (default 8-16k)
    # instead of allocating KV cache for the whole MAX_CTX. If needed, _run_turn
    # bumps to the next tier when the prompt exceeds 80%.
    HW = loki_hw.detect_hardware()
    stats['working_ctx'] = loki_hw.initial_working_ctx(MAX_CTX, HW['ram_avail_gb'])
    # One-off cleanup of very old sessions (skip _autosave and other _*).
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
                sys.stderr.write(f"\n\n❌ Fullscreen mode error: {e}\n")
                sys.stderr.write("Traceback also at ~/loki_debug.log\n")
                sys.stderr.write("Classic fallback: LOKI_UI=classic ./loki.sh\n\n")
                traceback.print_exc()
                exit_code = 1
    except KeyboardInterrupt:
        # Ctrl+C during asyncio shutdown (waits for executor threads up to
        # THREAD_JOIN_TIMEOUT). If a model turn is stuck inside Ollama the
        # thread will never finish — we exit hard with os._exit and skip the join.
        pass
    # os._exit skips the clean asyncio shutdown: prompt_toolkit has already
    # restored the terminal before returning, so this is safe.
    os._exit(exit_code)
