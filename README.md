# Loki

A local shell-agent CLI in Python — Claude Code style, but running on [Ollama](https://ollama.com). Full-screen UI built on `prompt_toolkit`: streaming thinking block, slash commands, persistent sessions, and a capped memory file.

Loki is "light" in the sense that it adapts to the hardware it runs on: it detects RAM/CPU/GPU at boot, picks a starting `num_ctx` that will not blow Ollama's KV cache into swap, and grows by tier (4k → 8k → 16k → 32k → 64k → 128k) only when the prompt actually needs it.

## Features

- **Shell agent with per-command confirmation** (`manual` mode) or `auto approve`
- **Automatic context compression** at 80% of the current working ctx
- **Per-turn autosave** to `~/.loki_sessions/_autosave.json` — no session is lost when the terminal dies
- **Persistent memory** capped at 50 KB in `~/.loki_memory.md` (`/remember` for manual notes; end-of-session summaries added automatically)
- **Saved sessions** via `/save`, interactive picker with `/resume`, `/clone`, `/delete`
- **Adaptive `num_ctx`** based on available RAM — the biggest footprint win
- **`keep_alive=15m`** on Ollama so the model is not evicted between turns
- **Contextual spinner**: `cooking...` while thinking, `bashing...` while executing shell
- **Image input** via `/img <path>` (with vision-capable models)

## Requirements

- Linux with `systemd`
- Python 3.9+ (with `python3-venv`)
- [Ollama](https://ollama.com) — the installer will download it if missing
- ~5–20 GB of disk space for the LLM model

## Installation

```bash
git clone https://github.com/<user>/loki.git
cd loki
bash install.sh
```

The installer asks for confirmation on each "heavy" step (installing ollama, pulling the model). Use `-y` to accept everything (with the default model).

At the end it drops a `loki` alias into `~/.bashrc` and `~/.zshrc`. Reload your shell and run:

```bash
loki
```

### Options

```bash
LOKI_HOME=/opt/loki bash install.sh          # custom destination folder
LOKI_MODEL=qwen3:8b bash install.sh -y       # alternative model, no prompts
```

Default model: `orcarouter/Qwen3.8-27B-Uncensored:latest` (~17 GB). Lighter alternatives: `qwen3:8b`, `qwen2.5:7b-instruct`, `llama3.1:8b`.

## Main slash commands

| Command | What it does |
|---|---|
| `/help` | full list |
| `/hw` | show detected hardware + working ctx |
| `/history` | session stats |
| `/compress` | compress now instead of waiting for 80% |
| `/remember <text>` | save a note to persistent memory |
| `/memory` | show the memory file |
| `/save [name]` | save the current session |
| `/resume [name\|number]` | no arg opens the picker, with arg loads |
| `/resume-last` | resume the autosave if < 12h old |
| `/auto` / `/manual` | toggle auto-approve of commands |
| `/think` | show/hide the thinking block |
| `/img <path>` | attach an image to the next message |

## Layout

```
loki-cli/
├── install.sh          # from-zero installer (9 steps, idempotent)
├── uninstall.sh        # clean rollback
├── loki.py             # prompt_toolkit UI + streaming + slash
├── loki_hw.py          # HW probe + adaptive ctx (tiered)
├── loki_persist.py     # autosave/resume-last + session prune
└── loki_mem.py         # capped memory file with rotation
```

After `install.sh`:

```
$LOKI_HOME/              # default: ~/LOKI/
├── loki.py + helpers
├── loki.sh              # launcher (exports LOKI_MODEL, handles systemd)
├── venv/                # Python venv
├── install.sh
└── uninstall.sh

~/.loki_sessions/*.json  # user sessions (shared across installs)
~/.loki_memory.md        # persistent memory
```

## User data

Sessions and memory live under `$HOME`, never inside `$LOKI_HOME`. You can uninstall and reinstall without losing anything.

## License

MIT — see [LICENSE](LICENSE).
