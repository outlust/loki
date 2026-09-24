"""Security-focused tools for Loki.

Loaded automatically when the 'security' persona is active.
No external pip dependencies — pure Python stdlib + optional system tools.
All functions return a plain string (usually JSON) for the model to parse.
"""
import os
import re
import json
import base64
import hashlib
import math
import struct
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import ssl
from datetime import datetime


# ── http_probe ────────────────────────────────────────────────────────────────

def http_probe(url, method='GET', headers=None, data=None,
               follow_redirects=True, timeout=15, verify_ssl=False, **_):
    """Make an HTTP/HTTPS request and return a structured response."""
    import time as _time

    method = (method or 'GET').upper()
    hdrs = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'close',
    }
    if headers:
        if isinstance(headers, dict):
            hdrs.update(headers)
        elif isinstance(headers, str):
            for line in headers.splitlines():
                if ':' in line:
                    k, _, v = line.partition(':')
                    hdrs[k.strip()] = v.strip()

    body_bytes = None
    if data:
        if isinstance(data, dict):
            body_bytes = urllib.parse.urlencode(data).encode()
            hdrs.setdefault('Content-Type', 'application/x-www-form-urlencoded')
        elif isinstance(data, str):
            body_bytes = data.encode()

    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    redirect_chain = []
    current_url = url
    final_resp = None
    start = _time.time()
    max_redirects = 10 if follow_redirects else 0

    for _ in range(max_redirects + 1):
        req = urllib.request.Request(current_url, data=body_bytes, headers=hdrs, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=int(timeout), context=ctx)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as exc:
            return json.dumps({'error': str(exc), 'url': current_url}, indent=2)

        status = resp.status if hasattr(resp, 'status') else resp.code
        redirect_chain.append({'url': current_url, 'status': status})

        location = resp.headers.get('Location') if hasattr(resp, 'headers') else None
        if follow_redirects and location and status in (301, 302, 303, 307, 308):
            current_url = urllib.parse.urljoin(current_url, location)
            if status in (301, 302, 303):
                method = 'GET'
                body_bytes = None
            continue

        final_resp = resp
        break

    elapsed_ms = int((_time.time() - start) * 1000)

    if final_resp is None:
        return json.dumps({'error': 'too many redirects', 'chain': redirect_chain}, indent=2)

    resp_headers = dict(final_resp.headers) if hasattr(final_resp, 'headers') else {}
    try:
        body_raw = final_resp.read(8192)
    except Exception:
        body_raw = b''
    try:
        body_preview = body_raw.decode('utf-8', errors='replace')
    except Exception:
        body_preview = repr(body_raw[:200])

    result = {
        'status': final_resp.status if hasattr(final_resp, 'status') else final_resp.code,
        'url': current_url,
        'elapsed_ms': elapsed_ms,
        'redirect_chain': redirect_chain[:-1],
        'headers': resp_headers,
        'content_type': resp_headers.get('Content-Type', ''),
        'content_length': resp_headers.get('Content-Length', 'unknown'),
        'body_preview': body_preview[:3000],
    }

    if current_url.startswith('https://'):
        try:
            import socket
            parsed = urllib.parse.urlparse(current_url)
            host = parsed.hostname or ''
            port = parsed.port or 443
            with socket.create_connection((host, port), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert() or {}
                    result['tls'] = {
                        'version': ssock.version(),
                        'cipher': ssock.cipher()[0] if ssock.cipher() else None,
                        'subject': dict(x[0] for x in cert.get('subject', [])),
                        'issuer': dict(x[0] for x in cert.get('issuer', [])),
                        'not_after': cert.get('notAfter', ''),
                        'san': [v for _, v in cert.get('subjectAltName', [])],
                    }
        except Exception as e:
            result['tls'] = {'error': str(e)}

    return json.dumps(result, indent=2)


# ── encode_decode ─────────────────────────────────────────────────────────────

def encode_decode(data, operation, encoding, **_):
    """Encode or decode data using common formats."""
    import html as _html
    import codecs

    op = (operation or '').lower().strip()
    enc = (encoding or '').lower().strip().replace('-', '').replace('_', '').replace(' ', '')
    is_encode = op in ('encode', 'enc', 'e')

    try:
        if enc == 'base64':
            if is_encode:
                result = base64.b64encode(data.encode()).decode()
            else:
                padded = data + '=' * (-len(data) % 4)
                result = base64.b64decode(padded).decode('utf-8', errors='replace')

        elif enc in ('base64url', 'b64url', 'urlsafe'):
            if is_encode:
                result = base64.urlsafe_b64encode(data.encode()).decode()
            else:
                padded = data + '=' * (-len(data) % 4)
                result = base64.urlsafe_b64decode(padded).decode('utf-8', errors='replace')

        elif enc == 'hex':
            if is_encode:
                result = data.encode().hex()
            else:
                clean = data.replace(' ', '').replace('0x', '').replace('\\x', '')
                result = bytes.fromhex(clean).decode('utf-8', errors='replace')

        elif enc in ('url', 'percent', 'urlencode'):
            if is_encode:
                result = urllib.parse.quote(data, safe='')
            else:
                result = urllib.parse.unquote(data)

        elif enc in ('html', 'htmlentity', 'htmlentities'):
            if is_encode:
                result = _html.escape(data, quote=True)
            else:
                result = _html.unescape(data)

        elif enc in ('rot13', 'caesar13'):
            result = codecs.encode(data, 'rot_13')

        elif enc in ('binary', 'bin'):
            if is_encode:
                result = ' '.join(f'{b:08b}' for b in data.encode())
            else:
                bits = data.replace(' ', '').replace('\n', '')
                result = bytes(
                    int(bits[i:i+8], 2) for i in range(0, len(bits), 8)
                ).decode('utf-8', errors='replace')

        elif enc in ('utf8', 'utf-8', 'unicode'):
            if is_encode:
                result = data.encode('utf-8').hex()
            else:
                result = bytes.fromhex(data.replace(' ', '')).decode('utf-8', errors='replace')

        elif enc in ('htmldec', 'decimal', 'htmldecimal'):
            if is_encode:
                result = ''.join(f'&#{ord(c)};' for c in data)
            else:
                result = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), data)

        else:
            available = 'base64, base64url, hex, url, html, htmldec, rot13, binary, utf8'
            return f"ERROR: unknown encoding '{encoding}'. Available: {available}"

        return json.dumps({
            'operation': op,
            'encoding': encoding,
            'input_length': len(data),
            'result': result,
        }, indent=2)

    except Exception as e:
        return f"ERROR: {e}"


# ── hash_data ─────────────────────────────────────────────────────────────────

def hash_data(data, algorithms=None, **_):
    """Compute one or more hashes of a string."""
    ALL = ['md5', 'sha1', 'sha224', 'sha256', 'sha384', 'sha512']
    if not algorithms:
        algs = ALL
    elif isinstance(algorithms, (list, tuple)):
        algs = [a.strip().lower() for a in algorithms]
    else:
        algs = [a.strip().lower() for a in str(algorithms).replace(',', ' ').split()]

    raw = data.encode() if isinstance(data, str) else data
    hashes = {}
    for alg in algs:
        try:
            hashes[alg] = hashlib.new(alg, raw).hexdigest()
        except ValueError:
            hashes[alg] = f'unsupported algorithm: {alg}'

    return json.dumps({'input_length': len(raw), 'hashes': hashes}, indent=2)


# ── identify_hash ─────────────────────────────────────────────────────────────

def identify_hash(hash_string, **_):
    """Identify likely hash algorithm(s) from length and charset."""
    h = hash_string.strip().lower()
    length = len(h)
    is_hex = bool(re.fullmatch(r'[0-9a-f]+', h))
    is_b64 = bool(re.fullmatch(r'[a-zA-Z0-9+/=]+', hash_string.strip()))

    candidates = []
    if is_hex:
        hex_map = {
            8:   [('CRC32', 'high'), ('Adler-32', 'medium')],
            16:  [('MD5 (half)', 'low'), ('LANMAN (half)', 'low')],
            32:  [('MD5', 'high'), ('MD4', 'medium'), ('NTLM', 'medium'), ('LM', 'low')],
            40:  [('SHA-1', 'high'), ('RIPEMD-160', 'medium')],
            48:  [('SHA-224', 'high')],
            64:  [('SHA-256', 'high'), ('BLAKE2s-256', 'medium'), ('Keccak-256', 'low')],
            96:  [('SHA-384', 'high')],
            128: [('SHA-512', 'high'), ('BLAKE2b-512', 'medium'), ('Whirlpool', 'low'), ('Keccak-512', 'low')],
        }
        if length in hex_map:
            candidates.extend(hex_map[length])
        else:
            candidates.append((f'Unknown hex hash ({length} chars / {length*4} bits)', 'low'))

    if is_b64 and not is_hex:
        try:
            decoded_len = len(base64.b64decode(hash_string.strip() + '=='))
            b64_map = {16: 'MD5', 20: 'SHA-1', 28: 'SHA-224', 32: 'SHA-256',
                       48: 'SHA-384', 64: 'SHA-512'}
            if decoded_len in b64_map:
                candidates.append((f'{b64_map[decoded_len]} (base64)', 'high'))
        except Exception:
            pass

    if '$' in hash_string:
        if hash_string.startswith('$2'):
            candidates.insert(0, ('bcrypt', 'high'))
        elif hash_string.startswith('$6$'):
            candidates.insert(0, ('SHA-512crypt (Linux shadow)', 'high'))
        elif hash_string.startswith('$5$'):
            candidates.insert(0, ('SHA-256crypt (Linux shadow)', 'high'))
        elif hash_string.startswith('$1$'):
            candidates.insert(0, ('MD5crypt', 'high'))
        elif hash_string.startswith('$apr1$'):
            candidates.insert(0, ('APR1-MD5 (Apache)', 'high'))

    if not candidates:
        candidates.append(('Unknown — does not match common formats', 'low'))

    return json.dumps({
        'input': hash_string.strip(),
        'length': length,
        'charset': 'hex' if is_hex else ('base64' if is_b64 else 'other'),
        'candidates': [{'algorithm': a, 'confidence': c} for a, c in candidates],
        'hashcat_hint': _hashcat_mode(candidates[0][0]) if candidates else None,
    }, indent=2)


def _hashcat_mode(algo_name):
    modes = {
        'MD5': '-m 0', 'MD4': '-m 900', 'NTLM': '-m 1000', 'LM': '-m 3000',
        'SHA-1': '-m 100', 'SHA-224': '-m 1300', 'SHA-256': '-m 1400',
        'SHA-384': '-m 10800', 'SHA-512': '-m 1700',
        'SHA-512crypt (Linux shadow)': '-m 1800',
        'SHA-256crypt (Linux shadow)': '-m 7400',
        'MD5crypt': '-m 500', 'bcrypt': '-m 3200',
        'APR1-MD5 (Apache)': '-m 1600',
    }
    return modes.get(algo_name, 'unknown — check hashcat --help')


# ── file_entropy ──────────────────────────────────────────────────────────────

def file_entropy(path, **_):
    """Calculate Shannon entropy of a file and detect its type."""
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return f"ERROR: file not found: {path}"

    file_size = os.path.getsize(path)
    freq = [0] * 256
    magic = b''

    try:
        with open(path, 'rb') as f:
            magic = f.read(16)
            f.seek(0)
            total = 0
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                for byte in chunk:
                    freq[byte] += 1
                total += len(chunk)
    except PermissionError:
        return f"ERROR: permission denied: {path}"
    except Exception as e:
        return f"ERROR: {e}"

    entropy = 0.0
    for count in freq:
        if count > 0:
            p = count / total
            entropy -= p * math.log2(p)

    magic_sigs = [
        (b'\x7fELF',       'ELF executable (Linux)'),
        (b'MZ',            'PE executable (Windows)'),
        (b'\xca\xfe\xba\xbe', 'Mach-O binary (macOS, fat)'),
        (b'\xce\xfa\xed\xfe', 'Mach-O binary (macOS, 32-bit LE)'),
        (b'\xcf\xfa\xed\xfe', 'Mach-O binary (macOS, 64-bit LE)'),
        (b'\x89PNG',       'PNG image'),
        (b'\xff\xd8\xff',  'JPEG image'),
        (b'GIF8',          'GIF image'),
        (b'BM',            'BMP image'),
        (b'PK\x03\x04',   'ZIP / JAR / DOCX / XLSX / APKG archive'),
        (b'\x1f\x8b',     'GZIP compressed'),
        (b'BZh',           'BZIP2 compressed'),
        (b'\xfd7zXZ',     'XZ compressed'),
        (b'7z\xbc\xaf',   '7-Zip archive'),
        (b'\x52\x61\x72\x21', 'RAR archive'),
        (b'%PDF',          'PDF document'),
        (b'%!PS',          'PostScript document'),
        (b'\x25\x50\x44\x46', 'PDF document'),
        (b'\xd0\xcf\x11\xe0', 'MS Office (OLE2) document'),
        (b'SQLite',        'SQLite database'),
        (b'\x7f\x45\x4c\x46', 'ELF (alt)'),
    ]
    file_type = 'unknown binary'
    for sig, label in magic_sigs:
        if magic.startswith(sig):
            file_type = label
            break
    if file_type == 'unknown binary':
        if all(32 <= b < 127 or b in (9, 10, 13) for b in magic[:min(16, len(magic))]):
            file_type = 'text / ASCII'

    if entropy >= 7.8:
        interpretation = 'ENCRYPTED or COMPRESSED — essentially random'
    elif entropy >= 7.0:
        interpretation = 'likely packed/obfuscated or compressed binary'
    elif entropy >= 6.0:
        interpretation = 'binary executable (normal range for ELF/PE)'
    elif entropy >= 4.0:
        interpretation = 'mixed binary/text'
    else:
        interpretation = 'plain text or source code'

    return json.dumps({
        'path': path,
        'size_bytes': file_size,
        'size_human': (f'{file_size / 1024:.1f} KB' if file_size < 1_048_576
                       else f'{file_size / 1_048_576:.2f} MB'),
        'entropy_bits_per_byte': round(entropy, 4),
        'interpretation': interpretation,
        'magic_type': file_type,
        'byte_stats': {
            'null_bytes': freq[0],
            'printable_ascii': sum(freq[i] for i in range(32, 127)),
            'high_bytes_128_255': sum(freq[i] for i in range(128, 256)),
            'unique_byte_values': sum(1 for c in freq if c > 0),
        },
    }, indent=2)


# ── check_linux_privesc ───────────────────────────────────────────────────────

GTFOBINS_SUID = {
    'nmap', 'vim', 'vi', 'nano', 'find', 'bash', 'dash', 'sh', 'zsh',
    'python', 'python2', 'python3', 'python3.6', 'python3.8', 'python3.10',
    'perl', 'perl5', 'ruby', 'lua', 'php', 'php7', 'php8',
    'awk', 'gawk', 'nawk', 'mawk', 'tclsh', 'expect',
    'git', 'ftp', 'scp', 'rsync', 'wget', 'curl',
    'tar', 'zip', 'unzip', '7z', 'gzip', 'bzip2',
    'less', 'more', 'man', 'env', 'tee', 'cp', 'mv',
    'chmod', 'chown', 'dd', 'cat', 'head', 'tail',
    'xxd', 'od', 'hexdump', 'base64', 'openssl',
    'node', 'nodejs', 'npm', 'docker', 'podman',
    'screen', 'tmux', 'strace', 'ltrace', 'gdb',
    'as', 'ld', 'gcc', 'g++', 'make', 'ar',
    'objcopy', 'readelf', 'objdump', 'nm',
    'scp', 'sftp', 'ssh', 'nc', 'netcat', 'ncat',
    'socat', 'tcpdump', 'tshark', 'wireshark',
    'mysql', 'sqlite3', 'psql',
    'journalctl', 'systemctl', 'apt', 'apt-get', 'dpkg', 'pip', 'pip3',
    'ansible', 'puppet', 'chef', 'salt',
    'ruby1', 'ruby2', 'irb', 'jruby',
    'java', 'javac', 'jar',
}


def check_linux_privesc(**_):
    """Run a battery of Linux privilege escalation checks."""

    def run(cmd, timeout=8):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=timeout)
            return r.stdout.strip()
        except Exception:
            return ''

    findings = []
    info = {
        'user': run('id'),
        'hostname': run('hostname -f 2>/dev/null || hostname'),
        'kernel': run('uname -r'),
        'os': run("grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '\"'"),
        'shell': os.environ.get('SHELL', 'unknown'),
        'cwd': os.getcwd(),
        'env_PATH': os.environ.get('PATH', ''),
    }

    # ── sudo ──────────────────────────────────────────────────────────────────
    sudo_out = run('sudo -ln 2>/dev/null')
    if sudo_out:
        sev = 'CRITICAL' if 'NOPASSWD' in sudo_out else 'HIGH'
        detail = ('NOPASSWD sudo rights — no password needed for listed commands'
                  if 'NOPASSWD' in sudo_out else
                  'sudo rights (password required — check with sudo -l after auth)')
        findings.append({'severity': sev, 'category': 'sudo',
                         'detail': detail, 'output': sudo_out[:600]})

    # ── SUID binaries ─────────────────────────────────────────────────────────
    suid_raw = run('find / -perm -4000 -type f 2>/dev/null', timeout=20)
    interesting_suid = []
    all_suid = suid_raw.splitlines()
    for path in all_suid:
        if os.path.basename(path).split('.')[0] in GTFOBINS_SUID:
            interesting_suid.append(path)
    if interesting_suid:
        findings.append({
            'severity': 'HIGH',
            'category': 'suid_gtfobins',
            'detail': f'{len(interesting_suid)} SUID binary/binaries exploitable via GTFOBins',
            'binaries': interesting_suid,
            'gtfobins_url': 'https://gtfobins.github.io/',
        })
    elif all_suid:
        findings.append({
            'severity': 'INFO',
            'category': 'suid_other',
            'detail': f'{len(all_suid)} SUID binaries found (none in GTFOBins list)',
            'binaries': all_suid[:20],
        })

    # ── SGID binaries ─────────────────────────────────────────────────────────
    sgid_raw = run('find / -perm -2000 -type f 2>/dev/null', timeout=20)
    if sgid_raw:
        sgid_interesting = [p for p in sgid_raw.splitlines()
                            if os.path.basename(p).split('.')[0] in GTFOBINS_SUID]
        if sgid_interesting:
            findings.append({
                'severity': 'HIGH',
                'category': 'sgid_gtfobins',
                'detail': f'{len(sgid_interesting)} SGID binary/binaries exploitable via GTFOBins',
                'binaries': sgid_interesting,
            })

    # ── Writable /etc ─────────────────────────────────────────────────────────
    writable_etc = run('find /etc -writable -type f 2>/dev/null')
    if writable_etc:
        files = writable_etc.splitlines()
        sev = 'CRITICAL' if any('/etc/passwd' in f or '/etc/shadow' in f
                                 or '/etc/cron' in f or '/etc/sudoers' in f
                                 for f in files) else 'HIGH'
        findings.append({
            'severity': sev,
            'category': 'writable_etc',
            'detail': f'{len(files)} writable file(s) under /etc',
            'files': files[:30],
        })

    # ── Writable PATH directories ─────────────────────────────────────────────
    ww_path = []
    for d in info['env_PATH'].split(':'):
        if d and os.path.isdir(d):
            try:
                if os.stat(d).st_mode & 0o002:
                    ww_path.append(d)
            except OSError:
                pass
    if ww_path:
        findings.append({
            'severity': 'HIGH',
            'category': 'path_hijack',
            'detail': 'World-writable directories in PATH — PATH hijacking possible',
            'dirs': ww_path,
        })

    # ── Cron ──────────────────────────────────────────────────────────────────
    cron_out = run(
        'cat /etc/crontab 2>/dev/null; '
        'ls -la /etc/cron.d/ 2>/dev/null; '
        'crontab -l 2>/dev/null; '
        'cat /var/spool/cron/crontabs/* 2>/dev/null | head -40'
    )
    if cron_out:
        findings.append({
            'severity': 'INFO',
            'category': 'cron',
            'detail': 'Cron jobs found — check if any script is writable by current user',
            'output': cron_out[:1000],
        })

    # ── Linux capabilities ────────────────────────────────────────────────────
    caps = run('getcap -r / 2>/dev/null', timeout=20)
    if caps:
        dangerous_caps = ['cap_setuid', 'cap_setgid', 'cap_net_raw',
                          'cap_sys_admin', 'cap_dac_override', 'cap_chown']
        cap_lines = caps.splitlines()
        dangerous = [l for l in cap_lines
                     if any(dc in l.lower() for dc in dangerous_caps)]
        findings.append({
            'severity': 'HIGH' if dangerous else 'INFO',
            'category': 'capabilities',
            'detail': f'{len(cap_lines)} binary/binaries with capabilities'
                      + (f'; {len(dangerous)} with dangerous caps' if dangerous else ''),
            'dangerous': dangerous,
            'all': cap_lines[:30],
        })

    # ── Docker socket ─────────────────────────────────────────────────────────
    docker_sock = '/var/run/docker.sock'
    if os.path.exists(docker_sock):
        if os.access(docker_sock, os.W_OK):
            findings.append({
                'severity': 'CRITICAL',
                'category': 'docker_socket',
                'detail': 'Docker socket is writable — trivial root escape',
                'exploit': 'docker run --rm -v /:/mnt alpine chroot /mnt sh',
            })
        else:
            findings.append({
                'severity': 'INFO',
                'category': 'docker_socket',
                'detail': 'Docker socket exists but not writable by current user',
            })

    # ── LXD / LXC group ───────────────────────────────────────────────────────
    groups_out = run('groups')
    if 'lxd' in groups_out or 'lxc' in groups_out:
        findings.append({
            'severity': 'CRITICAL',
            'category': 'lxd_group',
            'detail': 'User is in lxd/lxc group — container escape to root is straightforward',
            'reference': 'https://book.hacktricks.xyz/linux-hardening/privilege-escalation/interesting-groups-linux-pe/lxd-privilege-escalation',
        })

    # ── /etc/passwd writable ──────────────────────────────────────────────────
    if os.access('/etc/passwd', os.W_OK):
        findings.append({
            'severity': 'CRITICAL',
            'category': 'passwd_writable',
            'detail': '/etc/passwd is writable — add a root user: '
                      "echo 'pwned::0:0:root:/root:/bin/bash' >> /etc/passwd",
        })

    # ── /etc/shadow readable ──────────────────────────────────────────────────
    if os.access('/etc/shadow', os.R_OK):
        shadow_content = run('cat /etc/shadow 2>/dev/null | grep -v "^$\\|^#" | head -20')
        findings.append({
            'severity': 'CRITICAL',
            'category': 'shadow_readable',
            'detail': '/etc/shadow is readable — password hashes exposed for offline cracking',
            'hashes_preview': shadow_content[:500],
        })

    # ── NFS no_root_squash ───────────────────────────────────────────────────
    nfs_exports = run('cat /etc/exports 2>/dev/null')
    if 'no_root_squash' in nfs_exports:
        findings.append({
            'severity': 'HIGH',
            'category': 'nfs_no_root_squash',
            'detail': 'NFS export with no_root_squash — mount from attacker machine and write SUID binary',
            'exports': nfs_exports[:400],
        })

    # ── Readable SSH private keys ─────────────────────────────────────────────
    ssh_keys = run(
        'find / \\( -name "id_rsa" -o -name "id_ed25519" -o -name "id_ecdsa" '
        '-o -name "id_dsa" -o -name "*.pem" \\) -type f 2>/dev/null | head -30',
        timeout=15
    )
    if ssh_keys:
        readable = [k for k in ssh_keys.splitlines() if os.access(k, os.R_OK)]
        if readable:
            findings.append({
                'severity': 'HIGH',
                'category': 'ssh_private_keys',
                'detail': f'{len(readable)} readable SSH private key(s) found',
                'keys': readable,
            })

    # ── Secrets in environment variables ─────────────────────────────────────
    secret_patterns = ['password', 'passwd', 'secret', 'token', 'api_key',
                       'apikey', 'credential', 'private_key', 'access_key',
                       'auth', 'bearer', 'jwt', 'database_url']
    env_out = run('env 2>/dev/null')
    secret_vars = []
    for line in env_out.splitlines():
        key = line.split('=')[0].lower()
        if any(p in key for p in secret_patterns):
            secret_vars.append(line[:120])
    if secret_vars:
        findings.append({
            'severity': 'HIGH',
            'category': 'env_secrets',
            'detail': f'{len(secret_vars)} potential secret(s) in environment variables',
            'vars': secret_vars,
        })

    # ── World-writable files with sticky bit off ──────────────────────────────
    ww_files = run('find /tmp /var/tmp /dev/shm -type f -perm -002 2>/dev/null | head -20', timeout=10)
    if ww_files:
        findings.append({
            'severity': 'INFO',
            'category': 'world_writable_files',
            'detail': 'World-writable files in temp directories',
            'files': ww_files.splitlines(),
        })

    # ── Passwords in common config files ─────────────────────────────────────
    config_grep = run(
        'grep -rn --include="*.conf" --include="*.cfg" --include="*.ini" '
        '--include="*.env" --include=".env" -iE "password|passwd|secret|token" '
        '/etc /opt /srv /var/www 2>/dev/null | grep -v "^Binary" | head -20',
        timeout=15
    )
    if config_grep:
        findings.append({
            'severity': 'HIGH',
            'category': 'config_passwords',
            'detail': 'Potential hardcoded credentials in config files',
            'matches': config_grep.splitlines()[:20],
        })

    # ── Interesting running processes ─────────────────────────────────────────
    procs = run('ps aux --no-header 2>/dev/null | grep -v "^root.*\\[" | head -30')
    if procs:
        findings.append({
            'severity': 'INFO',
            'category': 'running_processes',
            'detail': 'Snapshot of running processes (look for DB servers, internal apps, root processes)',
            'output': procs[:1000],
        })

    # ── Sort by severity ──────────────────────────────────────────────────────
    sev_order = {'CRITICAL': 0, 'HIGH': 1, 'MEDIUM': 2, 'INFO': 3}
    findings.sort(key=lambda x: sev_order.get(x.get('severity', 'INFO'), 9))

    return json.dumps({
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'context': info,
        'summary': {
            'total_findings': len(findings),
            'critical': sum(1 for f in findings if f['severity'] == 'CRITICAL'),
            'high':     sum(1 for f in findings if f['severity'] == 'HIGH'),
            'info':     sum(1 for f in findings if f['severity'] == 'INFO'),
        },
        'findings': findings,
    }, indent=2)


# ── Schema definitions ─────────────────────────────────────────────────────────

SECURITY_TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "http_probe",
            "description": (
                "Make an HTTP/HTTPS request and return structured response info: "
                "status code, all headers, body preview (first 3 KB), redirect chain, "
                "and TLS certificate details. Better than curl for recon because output "
                "is structured JSON the model can parse directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to probe (http:// or https://)"},
                    "method": {"type": "string", "description": "HTTP method: GET (default), POST, PUT, HEAD, OPTIONS, DELETE, PATCH"},
                    "headers": {"type": "string", "description": "Extra request headers, one per line: 'Header-Name: value'"},
                    "data": {"type": "string", "description": "Request body for POST/PUT; raw string or key=value&key2=value2"},
                    "follow_redirects": {"type": "boolean", "description": "Follow HTTP redirects (default true)"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 15)"},
                    "verify_ssl": {"type": "boolean", "description": "Verify TLS certificate (default false — allows self-signed certs)"},
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "encode_decode",
            "description": (
                "Encode or decode a string using common formats. "
                "Supports: base64, base64url, hex, url (percent), html, htmldec, rot13, binary, utf8. "
                "Essential for CTF payload crafting, cookie/token analysis, and obfuscation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "The string to encode or decode"},
                    "operation": {"type": "string", "description": "'encode' or 'decode'"},
                    "encoding": {"type": "string", "description": "Format: base64 | base64url | hex | url | html | htmldec | rot13 | binary | utf8"},
                },
                "required": ["data", "operation", "encoding"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "hash_data",
            "description": (
                "Compute MD5, SHA-1, SHA-256, SHA-512 (and more) hashes of a string in one call. "
                "Useful for password cracking prep, file integrity, and CTF challenge solving."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "data": {"type": "string", "description": "The string to hash"},
                    "algorithms": {"type": "string", "description": "Comma-separated algorithm list (default: all). Options: md5, sha1, sha224, sha256, sha384, sha512"},
                },
                "required": ["data"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "identify_hash",
            "description": (
                "Identify the likely hash algorithm(s) from a hash string by length and character set. "
                "Returns candidates with confidence scores and the matching hashcat -m mode. "
                "Use before attempting to crack a hash when the algorithm is unknown."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash_string": {"type": "string", "description": "The hash to identify (hex, base64, or $format)"},
                },
                "required": ["hash_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_entropy",
            "description": (
                "Calculate Shannon entropy of a file (0–8 bits/byte) and detect its type from magic bytes. "
                "~8.0 = encrypted or compressed; 6–7 = packed binary; <4 = plain text. "
                "Use before reversing or strings to decide if unpacking is needed first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or ~-relative path to the file"},
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_linux_privesc",
            "description": (
                "Run a comprehensive Linux privilege escalation check. "
                "Covers: NOPASSWD sudo, SUID/SGID GTFOBins, writable /etc, cron jobs, "
                "Linux capabilities, Docker/LXD socket, /etc/shadow readable, NFS no_root_squash, "
                "readable SSH keys, hardcoded config credentials, env secrets, world-writable PATH dirs. "
                "Returns a severity-ranked JSON report (CRITICAL / HIGH / INFO). "
                "Run this immediately after gaining a shell."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
]
