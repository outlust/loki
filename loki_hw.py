"""Rilevamento hardware e politica di num_ctx adattiva per Loki.

Nessuna dipendenza dall'UI: funzioni pure che ritornano dati.
L'idea centrale: il KV cache di Ollama scala con num_ctx. Tenerlo basso
e' la vittoria piu' grossa in termini di RAM. Partiamo piccolo e
cresciamo per tier solo quando il prompt lo richiede davvero.
"""
import os
import subprocess


# Tier di num_ctx: crescita geometrica, salti franchi.
CTX_TIERS = [4096, 8192, 16384, 32768, 65536, 131072]


def detect_hardware():
    """Return a dict with RAM/thread/GPU/VRAM. Called once at boot.

    gpu_kind: 'nvidia' | 'amd_rocm' | 'amd_no_rocm' | 'none'.
    vram_total_gb: float (0.0 if unknown or CPU-only).
    vram_free_gb:  float (0.0 if unknown).
    """
    info = {
        'ram_total_gb':  0.0,
        'ram_avail_gb':  0.0,
        'threads':       os.cpu_count() or 1,
        'gpu_kind':      'none',
        'vram_total_gb': 0.0,
        'vram_free_gb':  0.0,
    }
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    info['ram_total_gb'] = int(line.split()[1]) / (1024 * 1024)
                elif line.startswith('MemAvailable:'):
                    info['ram_avail_gb'] = int(line.split()[1]) / (1024 * 1024)
    except Exception:
        pass

    # NVIDIA — detect VRAM via nvidia-smi query
    try:
        r = subprocess.run(['nvidia-smi', '-L'], capture_output=True, timeout=2)
        if r.returncode == 0:
            info['gpu_kind'] = 'nvidia'
            try:
                q = subprocess.run(
                    ['nvidia-smi', '--query-gpu=memory.total,memory.free',
                     '--format=csv,noheader,nounits'],
                    capture_output=True, text=True, timeout=2,
                )
                if q.returncode == 0:
                    parts = q.stdout.strip().split(',')
                    if len(parts) >= 2:
                        info['vram_total_gb'] = int(parts[0].strip()) / 1024
                        info['vram_free_gb']  = int(parts[1].strip()) / 1024
            except Exception:
                pass
            return info
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # AMD with ROCm active — detect VRAM via rocm-smi
    try:
        r = subprocess.run(['rocm-smi'], capture_output=True, timeout=2)
        if r.returncode == 0:
            info['gpu_kind'] = 'amd_rocm'
            try:
                q = subprocess.run(
                    ['rocm-smi', '--showmeminfo', 'vram', '--csv'],
                    capture_output=True, text=True, timeout=2,
                )
                if q.returncode == 0:
                    for line in q.stdout.splitlines():
                        parts = line.split(',')
                        if len(parts) >= 3 and parts[0].strip().startswith('card'):
                            total = int(parts[1].strip()) / (1024 ** 3)
                            used  = int(parts[2].strip()) / (1024 ** 3)
                            info['vram_total_gb'] = round(total, 2)
                            info['vram_free_gb']  = round(total - used, 2)
                            break
            except Exception:
                pass
            return info
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # AMD present but ROCm not active -> Ollama will run on CPU
    try:
        r = subprocess.run(['lspci'], capture_output=True, text=True, timeout=1)
        if r.returncode == 0:
            out = r.stdout
            if 'AMD' in out and ('VGA' in out or 'Display' in out or '3D' in out):
                info['gpu_kind'] = 'amd_no_rocm'
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return info


def initial_working_ctx(model_max_ctx, ram_avail_gb, model_size_gb_hint=8.0,
                         vram_free_gb=0.0, gpu_kind='none'):
    """Choose starting ctx so KV cache doesn't eat RAM/VRAM.

    When a GPU is available, use free VRAM as the budget (Ollama offloads
    KV cache to VRAM first). On CPU-only, use 25% of residual RAM.
    KV cache estimate: ~0.5 KB/token for q4, ~2 KB/token for q8_0.
    Conservative estimate uses 1 KB/token (~1M tokens/GB).
    """
    if gpu_kind in ('nvidia', 'amd_rocm') and vram_free_gb > 0:
        # Reserve half free VRAM for model weights already loaded; give 40% to KV
        budget_gb = vram_free_gb * 0.40
        budget_tokens = int(budget_gb * 1024 * 1024 * 1024 / 1024)  # 1KB/token
    else:
        residual_gb = max(0.5, ram_avail_gb - model_size_gb_hint)
        budget_gb = residual_gb * 0.25
        budget_tokens = int(budget_gb * 1024 * 1024 * 1024 / (2 * 1024))
    cap = min(model_max_ctx, budget_tokens)
    picked = CTX_TIERS[0]
    for t in CTX_TIERS:
        if t <= cap and t <= model_max_ctx:
            picked = t
    # Sensible default: if budget is huge, start at 16k not 128k.
    return min(picked, 16384, model_max_ctx)


def next_working_ctx(current, model_max_ctx):
    """Tier successivo (usato quando il prompt si avvicina al ceiling)."""
    for t in CTX_TIERS:
        if t > current and t <= model_max_ctx:
            return t
    return current  # gia' al tetto


def hw_line(hw, working_ctx, model_max_ctx):
    """Compact status line (used at boot and for /hw)."""
    ram = f"{hw['ram_avail_gb']:.1f}/{hw['ram_total_gb']:.1f} GB"
    gpu_label = {
        'nvidia':      'GPU NVIDIA',
        'amd_rocm':    'GPU AMD (ROCm)',
        'amd_no_rocm': 'GPU AMD (no ROCm → CPU)',
        'none':        'CPU only',
    }[hw['gpu_kind']]
    vram_part = ''
    if hw.get('vram_total_gb', 0) > 0:
        vram_part = f"  VRAM {hw['vram_free_gb']:.1f}/{hw['vram_total_gb']:.1f} GB"
    return (f"HW: RAM {ram}{vram_part}  {hw['threads']} thr  {gpu_label}  "
            f"ctx {working_ctx}/{model_max_ctx}")
