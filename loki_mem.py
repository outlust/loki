"""Persistent memory file for Loki: capped size with rotation.

The file `~/.loki_memory.md` mixes:
- user notes at the top (added via `/remember`);
- `### Compressed session <date>` blocks produced by /compress.

We rotate the blocks keeping the last N; if the file still exceeds the
hard cap (many /remember entries, oversized summaries), we trim from the
top and insert an explicit truncation marker.
"""
import os
import re


MEMORY_HARD_CAP_BYTES = 50 * 1024  # 50 KB hard cap


def load_memory(path):
    if os.path.isfile(path):
        with open(path) as f:
            return f.read().strip()
    return ""


def append_memory(path, text):
    with open(path, 'a') as f:
        f.write(f"\n\n{text.strip()}")


def rotate_summaries(path, max_summaries=5):
    """Keep at most `max_summaries` `### Compressed session` blocks.
    User notes at the top are never touched.
    """
    if not os.path.exists(path):
        return
    with open(path) as f:
        content = f.read()
    parts = re.split(r'(?m)^### Compressed session ', content)
    if len(parts) - 1 <= max_summaries:
        return
    header = parts[0].rstrip()
    kept = parts[-max_summaries:]
    body = "\n\n".join(f"### Compressed session {p.rstrip()}" for p in kept)
    new = (header + "\n\n" + body).strip() + "\n"
    with open(path, 'w') as f:
        f.write(new)


def enforce_hard_cap(path, cap_bytes=MEMORY_HARD_CAP_BYTES):
    """If the file exceeds cap_bytes even after rotate_summaries, trim from
    the top at the first useful paragraph boundary and insert a marker.
    """
    if not os.path.isfile(path):
        return
    size = os.path.getsize(path)
    if size <= cap_bytes:
        return
    with open(path) as f:
        content = f.read()
    target = int(cap_bytes * 0.9)  # go under the cap to avoid retriggering
    excess = len(content) - target
    if excess <= 0:
        return
    cut_from = excess
    nl = content.find('\n\n', cut_from)
    if nl >= 0:
        cut_from = nl + 2
    trimmed = content[cut_from:].lstrip()
    marker = (f"[memory auto-truncated: {excess} oldest bytes "
              f"removed to fit within the cap]\n\n")
    with open(path, 'w') as f:
        f.write(marker + trimmed)
