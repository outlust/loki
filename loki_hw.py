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
    """Ritorna un dict con RAM/thread/GPU. Chiamata una volta al boot.

    gpu_kind e' uno di: 'nvidia', 'amd_rocm', 'amd_no_rocm', 'none'.
    """
    info = {
        'ram_total_gb': 0.0,
        'ram_avail_gb': 0.0,
        'threads':      os.cpu_count() or 1,
        'gpu_kind':     'none',
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

    # NVIDIA
    try:
        r = subprocess.run(['nvidia-smi', '-L'],
                           capture_output=True, timeout=1)
        if r.returncode == 0:
            info['gpu_kind'] = 'nvidia'
            return info
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # AMD con ROCm attivo
    try:
        r = subprocess.run(['rocm-smi'],
                           capture_output=True, timeout=1)
        if r.returncode == 0:
            info['gpu_kind'] = 'amd_rocm'
            return info
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # AMD presente ma ROCm non attivo -> Ollama girera' su CPU
    try:
        r = subprocess.run(['lspci'],
                           capture_output=True, text=True, timeout=1)
        if r.returncode == 0:
            out = r.stdout
            if 'AMD' in out and ('VGA' in out or 'Display' in out or '3D' in out):
                info['gpu_kind'] = 'amd_no_rocm'
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return info


def initial_working_ctx(model_max_ctx, ram_avail_gb, model_size_gb_hint=8.0):
    """Sceglie il ctx di partenza in modo che il KV cache non mangi la RAM.

    Regola grezza: dopo aver messo da parte la RAM per il modello,
    lascia al KV cache al massimo ~25% del residuo. Con Ollama q8_0 KV
    si stimano ~2 KB per token (varia col modello, ma questo tetto e'
    conservativo).
    """
    residual_gb = max(0.5, ram_avail_gb - model_size_gb_hint)
    budget_gb = residual_gb * 0.25
    budget_tokens = int(budget_gb * 1024 * 1024 * 1024 / (2 * 1024))
    cap = min(model_max_ctx, budget_tokens)
    picked = CTX_TIERS[0]
    for t in CTX_TIERS:
        if t <= cap and t <= model_max_ctx:
            picked = t
    # Default ragionevole: se il cap fosse enorme, parti a 16k, non a 128k.
    return min(picked, 16384, model_max_ctx)


def next_working_ctx(current, model_max_ctx):
    """Tier successivo (usato quando il prompt si avvicina al ceiling)."""
    for t in CTX_TIERS:
        if t > current and t <= model_max_ctx:
            return t
    return current  # gia' al tetto


def hw_line(hw, working_ctx, model_max_ctx):
    """Riga di stato compatta (usata a boot e per /hw)."""
    ram = f"{hw['ram_avail_gb']:.1f}/{hw['ram_total_gb']:.1f} GB"
    gpu_label = {
        'nvidia':      'GPU NVIDIA',
        'amd_rocm':    'GPU AMD (ROCm)',
        'amd_no_rocm': 'GPU AMD (no ROCm -> CPU)',
        'none':        'CPU only',
    }[hw['gpu_kind']]
    return (f"HW: RAM {ram}  {hw['threads']} thr  {gpu_label}  "
            f"ctx {working_ctx}/{model_max_ctx}")
