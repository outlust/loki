"""Hardware detection and adaptive num_ctx policy for Loki.

No UI dependency: pure functions that return data.
Core idea: Ollama's KV cache scales linearly with num_ctx. Keeping it
low is the biggest RAM win. We start small and grow by tiers only when
the prompt actually needs it.
"""
import os
import subprocess


# num_ctx tiers: geometric growth, clean jumps.
CTX_TIERS = [4096, 8192, 16384, 32768, 65536, 131072]


def detect_hardware():
    """Return a dict with RAM/threads/GPU. Called once at boot.

    gpu_kind is one of: 'nvidia', 'amd_rocm', 'amd_no_rocm', 'none'.
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
    # AMD with ROCm active
    try:
        r = subprocess.run(['rocm-smi'],
                           capture_output=True, timeout=1)
        if r.returncode == 0:
            info['gpu_kind'] = 'amd_rocm'
            return info
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # AMD present but ROCm not active -> Ollama will run on CPU
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
    """Pick a starting num_ctx so the KV cache does not eat all the RAM.

    Rough rule: after reserving RAM for the model itself, allow the KV
    cache at most ~25% of what's left. With Ollama's q8_0 KV we estimate
    ~2 KB per token (varies by model, but this ceiling is conservative).
    """
    residual_gb = max(0.5, ram_avail_gb - model_size_gb_hint)
    budget_gb = residual_gb * 0.25
    budget_tokens = int(budget_gb * 1024 * 1024 * 1024 / (2 * 1024))
    cap = min(model_max_ctx, budget_tokens)
    picked = CTX_TIERS[0]
    for t in CTX_TIERS:
        if t <= cap and t <= model_max_ctx:
            picked = t
    # Sensible default: if the cap were huge, start at 16k, not 128k.
    return min(picked, 16384, model_max_ctx)


def next_working_ctx(current, model_max_ctx):
    """Return the next tier (used when the prompt approaches the ceiling)."""
    for t in CTX_TIERS:
        if t > current and t <= model_max_ctx:
            return t
    return current  # already at the ceiling


def hw_line(hw, working_ctx, model_max_ctx):
    """Compact status line (used at boot and by /hw)."""
    ram = f"{hw['ram_avail_gb']:.1f}/{hw['ram_total_gb']:.1f} GB"
    gpu_label = {
        'nvidia':      'NVIDIA GPU',
        'amd_rocm':    'AMD GPU (ROCm)',
        'amd_no_rocm': 'AMD GPU (no ROCm -> CPU)',
        'none':        'CPU only',
    }[hw['gpu_kind']]
    return (f"HW: RAM {ram}  {hw['threads']} thr  {gpu_label}  "
            f"ctx {working_ctx}/{model_max_ctx}")
