#!/usr/bin/env python3
"""GSuite IMAP provider + proxy rotation untuk atria_register.py"""

import imaplib, email, re, time, random, os
import urllib.request
import json as _json
from pathlib import Path
from email.header import decode_header

# ─── GSuite accounts (from gsuite.txt) ──────────────────────────────
def load_gsuite_accounts(path="gsuite.txt"):
    """Parse gsuite.txt: email|password per baris"""
    from pathlib import Path
    p = Path(path)
    if not p.is_file():
        p = Path(__file__).parent / path
    if not p.is_file():
        return []
    accounts = []
    for line in p.read_text(encoding="utf-8").strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) >= 2:
            accounts.append({"email": parts[0].strip(), "password": parts[1].strip()})
    return accounts

# ─── Proxy pool: PetaniProxy (gateway API > file panen > statik) ────
PETANI_DIR = Path(__file__).parent / "petani-proxy"
PROXY_FILE = PETANI_DIR / "output" / "live_all.txt"
GATEWAY_BASE = os.environ.get("PETANI_API", "http://127.0.0.1:8888").rstrip("/")
GATEWAY_ALL = GATEWAY_BASE + "/api/all"
PROXY_TEST_URL = "https://api.ipify.org?format=text"

# Cadangan terakhir kalau gateway & file panen sama-sekali kosong.
FALLBACK_PROXY_LIST = [
    "http://124.156.194.52:8080",   "http://128.199.202.122:8080",
    "http://103.164.55.81:3128",     "http://107.150.41.226:18080",
    "http://103.237.102.191:11111",  "http://139.59.1.14:8080",
    "http://103.255.243.57:8080",    "http://101.32.65.42:8888",
    "http://112.216.54.226:12121",   "http://1.231.81.166:3128",
    "http://116.204.182.120:80",     "http://103.130.61.61:8081",
    "http://113.11.120.105:30226",   "http://117.236.124.166:3128",
    "http://121.199.43.200:7890",
]

_proxy_pool = None
_proxy_idx = 0


def _load_proxies():
    """Ambil proxy hidup: gateway /api/all > output/live_all.txt > statik."""
    # 1) Gateway PetaniProxy (pool divalidasi terus oleh auto-healer).
    try:
        with urllib.request.urlopen(GATEWAY_ALL, timeout=4) as r:
            data = _json.loads(r.read().decode("utf-8"))
        urls = [f"{p.get('protocol', 'http')}://{p['proxy']}"
                for p in data.get("proxies", [])]
        if urls:
            return urls
    except Exception:
        pass
    # 2) File panen lokal (python main.py --fast-harvest / --serve).
    try:
        if PROXY_FILE.is_file():
            lines = [ln.strip() for ln in PROXY_FILE.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.startswith("#")]
            if lines:
                return [f"http://{ln}" for ln in lines]
    except Exception:
        pass
    # 3) Daftar statik.
    return list(FALLBACK_PROXY_LIST)


def next_proxy():
    """Proxy berikutnya (round-robin). None kalau pool kosong -> direct."""
    global _proxy_pool, _proxy_idx
    if _proxy_pool is None:
        _proxy_pool = _load_proxies()
    if not _proxy_pool:
        return None
    p = _proxy_pool[_proxy_idx % len(_proxy_pool)]
    _proxy_idx += 1
    return p


def proxy_pool_size():
    """Jumlah proxy di pool (0 kalau belum ada sama sekali)."""
    global _proxy_pool
    if _proxy_pool is None:
        _proxy_pool = _load_proxies()
    return len(_proxy_pool or [])


def reset_proxy_pool():
    """Buang cache pool agar membaca daftar panen terbaru."""
    global _proxy_pool, _proxy_idx
    _proxy_pool = None
    _proxy_idx = 0


def test_proxy(proxy, timeout=8):
    """Cek cepat proxy mampu CONNECT ke HTTPS (Google & Atria pakai HTTPS).

    Mengembalikan egress IP kalau hidup, None kalau mati / tidak bisa tunnel.
    """
    if not proxy:
        return None
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    try:
        with opener.open(PROXY_TEST_URL, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return None

# ─── Gmail IMAP helper ──────────────────────────────────────────────
# XOAUTH2 tidak dipakai; login user/password biasa (harus aktifkan
# "Less secure app access" atau App Password di Google account).

CODE_PATTERN = re.compile(r"(?:code(?:\s+is)?|kode)[^\d]{0,30}(\d{6})", re.I)
ANY_CODE = re.compile(r"\b(\d{6})\b")
ATRIA_FROM = re.compile(r"atria", re.I)

_IMAP_CACHE = {}  # {email: (conn, password, last_used)}

def _decode_header_val(val):
    """Decode email header value (bisa encoded)."""
    if val is None:
        return ""
    parts = decode_header(val)
    result = []
    for text, charset in parts:
        if isinstance(text, bytes):
            try:
                result.append(text.decode(charset or "utf-8", errors="replace"))
            except Exception:
                result.append(text.decode("utf-8", errors="replace"))
        else:
            result.append(text)
    return " ".join(result)

def get_imap_conn(email, password):
    """Dapatkan koneksi IMAP (cached, reconnect kalau timeout)."""
    conn, _, _ = _IMAP_CACHE.get(email, (None, None, 0))
    if conn is not None:
        try:
            conn.noop()
            return conn
        except Exception:
            try:
                conn.logout()
            except Exception:
                pass
            _IMAP_CACHE.pop(email, None)

    # Connect baru
    conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    conn.login(email, password)
    conn.select("INBOX")
    _IMAP_CACHE[email] = (conn, password, time.time())
    return conn

def fetch_code_from_gmail(email, password, timeout=60):
    """Poll Gmail inbox via IMAP, cari verification code dari Atria."""
    deadline = time.time() + timeout
    conn = get_imap_conn(email, password)

    while time.time() < deadline:
        try:
            # Search unseen + recent messages from last 5 min
            conn.noop()
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                time.sleep(5)
                continue

            msg_ids = data[0].split()
            if not msg_ids:
                time.sleep(5)
                continue

            # Cek dari yang terbaru
            for mid in reversed(msg_ids[-10:]):
                status, msg_data = conn.fetch(mid, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                subject = _decode_header_val(msg.get("Subject", ""))
                sender = _decode_header_val(msg.get("From", ""))

                # Cek dari Atria
                body_text = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct in ("text/plain", "text/html"):
                            try:
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body_text += payload.decode("utf-8", errors="replace")
                            except Exception:
                                pass
                else:
                    try:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body_text = payload.decode("utf-8", errors="replace")
                    except Exception:
                        pass

                combined = f"{subject} {body_text}"

                # Try exact pattern first
                m = CODE_PATTERN.search(combined) or ANY_CODE.search(combined)
                if m:
                    return m.group(1)

                # Mark as seen so we don't re-read
                conn.store(mid, "+FLAGS", "\\Seen")

        except Exception as e:
            # Reconnect on error
            try:
                conn.logout()
            except Exception:
                pass
            _IMAP_CACHE.pop(email, None)
            time.sleep(3)
            conn = get_imap_conn(email, password)
            continue

        time.sleep(5)

    return None

class GSuiteInbox:
    """Kompatibel dengan interface Inbox di atria_register.py"""
    provider = "gsuite"

    def __init__(self, email, password):
        self.address = email
        self.email = email
        self.password = password

    def create(self):
        return self.address  # already exists

    def fetch(self):
        """Return list of text blob per message (untuk kompatibilitas)."""
        try:
            conn = get_imap_conn(self.email, self.password)
            conn.noop()
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                return []
            msg_ids = data[0].split()
            if not msg_ids:
                return []
            results = []
            for mid in reversed(msg_ids[-5:]):
                status, msg_data = conn.fetch(mid, "(BODY.PEEK[])")
                if status != "OK":
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                subject = _decode_header_val(msg.get("Subject", ""))
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        ct = part.get_content_type()
                        if ct in ("text/plain", "text/html"):
                            try:
                                p = part.get_payload(decode=True)
                                if p:
                                    body += p.decode("utf-8", errors="replace")
                            except Exception:
                                pass
                else:
                    try:
                        p = msg.get_payload(decode=True)
                        if p:
                            body = p.decode("utf-8", errors="replace")
                    except Exception:
                        pass
                results.append(f"{subject} {body}")
            return results
        except Exception:
            return []

    @property
    def delay(self):
        return 5  # GSuite IMAP lebih cepat
