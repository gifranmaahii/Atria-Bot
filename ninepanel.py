#!/usr/bin/env python3
"""
Inject key Atria ke panel 9Router ONLINE lewat HTTP API.

9Router (https://github.com/decolua/9router) adalah Next.js app dengan
dashboard ber-password. Modul ini bikin bot bisa inject ke banyak panel
9Router online sekaligus, cukup pakai URL + password.

Alur per target:
  1. POST /api/auth/login  {password}        -> cookie auth_token (JWT)
  2. GET  /api/provider-nodes               -> cari node openai-compatible-chat-atria
  3. POST /api/provider-nodes (kalau belum) -> buat node Atria AI
  4. GET  /api/providers                    -> daftar koneksi (cek duplikat)
  5. POST /api/providers  (kalau baru)      -> inject key
     PUT  /api/providers/[id] (kalau sudah) -> update key

Cookie auth_token disimpan per-target supaya login cukup sekali (valid 24h).
"""
import json
import os
import time
from pathlib import Path

try:
    import requests as req
except ImportError:  # pragma: no cover
    req = None

WORKDIR = Path(__file__).parent
TARGETS_FILE = WORKDIR / "9router_targets.json"

# Konstanta node Atria di panel 9Router (sama dengan inject SQLite lokal).
NINEROUTER_PREFIX = "atria"
NINEROUTER_NODE_ID = f"openai-compatible-chat-{NINEROUTER_PREFIX}"
NINEROUTER_NODE_NAME = "Atria AI"
NINEROUTER_BASE_URL = "https://api.atria-asi.ai/v1"
NINEROUTER_DEFAULT_MODEL = "Atria-Dawn-Preview"

_TIMEOUT = 20
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Cookie per-target di memory; kalau proses mati, login ulang otomatis.
_AUTH_CACHE = {}  # key: base_url, value: {"token":..., "ts":...}


# ─── Util ──────────────────────────────────────────────────────────
def _norm_base(url):
    """Normalisasi base URL panel: pastikan ada skema, tanpa trailing /."""
    u = (url or "").strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "http://" + u
    return u.rstrip("/")


def is_remote_target(t):
    """True kalau target dict itu tipe remote (panel online)."""
    if not isinstance(t, dict):
        return False
    if t.get("type") == "remote":
        return True
    if t.get("url") and t.get("password"):
        return True
    return False


def remote_summary(t):
    """Ringkas target remote (password disembunyikan)."""
    url = _norm_base(t.get("url", ""))
    pw = str(t.get("password", ""))
    return f"{url} (pass: {'*' * max(3, len(pw))})"


def _api(base, path):
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _headers(token=None):
    h = {"User-Agent": _UA, "Content-Type": "application/json"}
    if token:
        h["Cookie"] = f"auth_token={token}"
    return h


def _auth_tuple(t=None, basic_user=None, basic_pass=None):
    """Return requests `auth=` tuple kalau mode Basic Auth dipakai.

    Prioritas: arg eksplisit > field di target dict (basic_user/basic_pass).
    None berarti tidak pakai Basic Auth (mode login 9Router biasa).
    """
    if basic_user is not None:
        return (basic_user, basic_pass or "")
    if t and t.get("basic_user"):
        return (t.get("basic_user"), t.get("basic_pass") or "")
    return None


def _has_basic(t=None, basic_user=None):
    """True kalau target pakai proteksi Basic Auth (reverse proxy)."""
    return bool(basic_user is not None or (t and t.get("basic_user")))


def _clear_cache(base):
    _AUTH_CACHE.pop(_norm_base(base), None)


# ─── Login & status ─────────────────────────────────────────────────
def login(base, password, force=False, basic_user=None, basic_pass=None):
    """Login ke panel 9Router. Return (ok, msg). Cookie disimpan di cache.

    Kalau basic_user diisi, panel diproteksi Basic Auth (reverse proxy
    seperti Caddy/Nginx) -> skip login 9Router, pakai Basic Auth untuk
    semua request. Token diset dummy ("BASIC") sebagai penanda.
    """
    if req is None:
        return False, "requests belum terpasang"
    base = _norm_base(base)
    if not base:
        return False, "url kosong"

    # Mode Basic Auth: tidak perlu login 9Router, cookie diset dummy.
    if _has_basic(None, basic_user):
        if not basic_user or basic_user.strip() == "":
            return False, "basic_user kosong"
        _AUTH_CACHE[base] = {"token": "BASIC", "ts": time.time(),
                             "basic": True,
                             "basic_user": basic_user,
                             "basic_pass": basic_pass or ""}
        return True, "BASIC"

    if not password:
        return False, "password kosong"

    key = base
    cached = _AUTH_CACHE.get(key)
    # JWT 9Router valid 24h; kita anggap aman < 23h.
    if cached and not force and (time.time() - cached["ts"] < 23 * 3600):
        return True, cached["token"]

    try:
        r = req.post(_api(base, "/api/auth/login"),
                     headers=_headers(), json={"password": password},
                     timeout=_TIMEOUT, allow_redirects=False)
    except req.exceptions.ConnectionError as e:
        return False, f"tidak konek ({str(e)[:40]})"
    except Exception as e:
        return False, str(e)[:60]

    if r.status_code == 429:
        return False, "terkunci (terlalu banyak gagal, tunggu)"
    if r.status_code == 403:
        try:
            d = r.json()
            if d.get("mustChangePassword"):
                return False, "password default belum diganti di panel"
        except Exception:
            pass
        return False, "login ditolak (403)"
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"

    # Ambil cookie auth_token dari Set-Cookie.
    token = None
    cookies = r.headers.get("set-cookie", "")
    for part in cookies.split(","):
        if "auth_token=" in part:
            seg = part.split("auth_token=", 1)[1]
            token = seg.split(";", 1)[0].strip()
            break

    if not token:
        try:
            token = r.cookies.get("auth_token")
        except Exception:
            token = None

    if not token:
        return False, "login OK tapi cookie auth_token tidak ketemu"

    _AUTH_CACHE[key] = {"token": token, "ts": time.time()}
    return True, token


def is_authenticated(base):
    """Cek status login via GET /api/auth/status (tanpa password)."""
    if req is None:
        return False
    base = _norm_base(base)
    cached = _AUTH_CACHE.get(base)
    if not cached:
        return False
    auth = (cached.get("basic_user"), cached.get("basic_pass", "")) \
        if cached.get("basic") and cached.get("basic_user") else None
    try:
        r = req.get(_api(base, "/api/auth/status"),
                    headers=_headers(cached["token"]), auth=auth,
                    timeout=_TIMEOUT)
        if r.status_code == 200:
            return bool(r.json().get("authenticated"))
    except Exception:
        pass
    return False


# ─── Node provider (openai-compatible-chat-atria) ────────────────────
def ensure_node(base, token, auth=None):
    """Pastikan node Atria AI ada. Return (ok, msg)."""
    if req is None:
        return False, "requests belum terpasang"
    try:
        r = req.get(_api(base, "/api/provider-nodes"),
                    headers=_headers(token), auth=auth, timeout=_TIMEOUT)
    except Exception as e:
        return False, f"node list gagal: {str(e)[:40]}"
    if r.status_code != 200:
        return False, f"node list HTTP {r.status_code}"
    try:
        nodes = r.json().get("nodes", []) or []
    except Exception:
        return False, "node list JSON rusak"

    for n in nodes:
        if n.get("id") == NINEROUTER_NODE_ID:
            return True, "exists"
        if n.get("prefix") == NINEROUTER_PREFIX:
            return True, "exists"

    body = {
        "name": NINEROUTER_NODE_NAME,
        "prefix": NINEROUTER_PREFIX,
        "apiType": "chat",
        "baseUrl": NINEROUTER_BASE_URL,
        "type": "openai-compatible",
    }
    try:
        r = req.post(_api(base, "/api/provider-nodes"),
                     headers=_headers(token), json=body, auth=auth,
                     timeout=_TIMEOUT)
    except Exception as e:
        return False, f"node create gagal: {str(e)[:40]}"
    if r.status_code == 201:
        return True, "created"
    if r.status_code == 400:
        return False, "node create 400 (prefix bentrok?)"
    return False, f"node create HTTP {r.status_code}"


def list_nodes(base, token, auth=None):
    """Return list node dict (kosong kalau gagal)."""
    if req is None:
        return []
    try:
        r = req.get(_api(base, "/api/provider-nodes"),
                    headers=_headers(token), auth=auth, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json().get("nodes", []) or []
    except Exception:
        pass
    return []


# ─── Provider connections (inject key) ─────────────────────────────
def list_connections(base, token, auth=None):
    """Return list koneksi (apiKey disembunyikan server, deteksi via
    name/email + prefix node)."""
    if req is None:
        return []
    try:
        r = req.get(_api(base, "/api/providers"),
                    headers=_headers(token), auth=auth, timeout=_TIMEOUT)
        if r.status_code == 200:
            return r.json().get("connections", []) or []
    except Exception:
        pass
    return []


def _conn_matches_atria(c):
    """True kalau koneksi ini milik node Atria."""
    psd = c.get("providerSpecificData") or {}
    return (psd.get("prefix") == NINEROUTER_PREFIX
            or psd.get("nodeName") == NINEROUTER_NODE_NAME
            or c.get("provider") == NINEROUTER_NODE_ID)


def _find_atria_node_id(nodes):
    """Cari id node Atria dari daftar node (id bisa random lewat API)."""
    for n in nodes:
        if n.get("prefix") == NINEROUTER_PREFIX:
            return n.get("id") or NINEROUTER_NODE_ID
        if n.get("name") == NINEROUTER_NODE_NAME:
            return n.get("id") or NINEROUTER_NODE_ID
    return NINEROUTER_NODE_ID


def _psd(email):
    """Susun providerSpecificData konsisten dengan inject lokal."""
    return {
        "prefix": NINEROUTER_PREFIX,
        "apiType": "chat",
        "baseUrl": NINEROUTER_BASE_URL,
        "nodeName": NINEROUTER_NODE_NAME,
        "authMethod": "apikey",
        "source": "atria-tempmail-farmer",
        "apiBase": "https://api.atria-asi.ai",
        "chatUrl": "https://api.atria-asi.ai/v1/chat/completions",
        "email": email,
        "connectionProxyEnabled": False,
        "connectionProxyUrl": "",
        "connectionNoProxy": "",
    }


def inject_key(base, password, api_key, email=None, name=None,
               skip_if_exists=False, basic_user=None, basic_pass=None):
    """Inject/update satu key Atria ke panel 9Router online.

    Return (ok, msg):
      - (False, "NO_KEY")
      - (False, "LOGIN_FAIL: ...")
      - (True,  "ALREADY_EXISTS")  -> key sudah ada & skip_if_exists=True
      - (True,  "SUCCESS")         -> koneksi baru dibuat
      - (True,  "UPDATED")         -> koneksi lama di-update
      - (False, "<error>")

    basic_user/basic_pass: kalau panel diproteksi Basic Auth (reverse proxy),
    isi keduanya -> skip login 9Router, pakai Basic Auth untuk semua request.
    """
    if not api_key:
        return False, "NO_KEY"

    base = _norm_base(base)
    ok, tok = login(base, password, basic_user=basic_user,
                    basic_pass=basic_pass)
    if not ok:
        return False, f"LOGIN_FAIL: {tok}"
    token = tok

    # Ambil auth tuple (None kalau bukan mode Basic Auth).
    cached = _AUTH_CACHE.get(base) or {}
    auth = _auth_tuple(cached if cached.get("basic") else None,
                       basic_user, basic_pass)

    # 1. Pastikan node Atria ada.
    nok, nmsg = ensure_node(base, token, auth=auth)
    if not nok:
        return False, f"NODE_FAIL: {nmsg}"

    nodes = list_nodes(base, token, auth=auth)
    node_id = _find_atria_node_id(nodes) or NINEROUTER_NODE_ID

    label = name or email or api_key[:18]

    # 2. Cari key yang sudah ada (deteksi duplikat via name/email).
    conns = list_connections(base, token, auth=auth)
    existing = None
    for c in conns:
        if not _conn_matches_atria(c):
            continue
        c_email = (c.get("providerSpecificData") or {}).get("email")
        if (c.get("name") == label) or (email and c_email == email):
            existing = c
            break

    if existing:
        if skip_if_exists:
            return True, "ALREADY_EXISTS"
        # Update: kirim apiKey baru + defaultModel terbaru.
        try:
            r = req.put(_api(base, f"/api/providers/{existing['id']}"),
                        headers=_headers(token), auth=auth,
                        json={
                            "apiKey": api_key,
                            "name": label,
                            "defaultModel": NINEROUTER_DEFAULT_MODEL,
                            "isActive": True,
                            "testStatus": "active",
                            "providerSpecificData": _psd(email),
                        }, timeout=_TIMEOUT)
        except Exception as e:
            return False, f"update gagal: {str(e)[:40]}"
        if r.status_code == 200:
            return True, "UPDATED"
        return False, f"update HTTP {r.status_code}"

    # 3. Buat koneksi baru.
    body = {
        "provider": node_id,
        "apiKey": api_key,
        "name": label,
        "defaultModel": NINEROUTER_DEFAULT_MODEL,
        "isActive": True,
        "testStatus": "active",
        "providerSpecificData": _psd(email),
    }
    try:
        r = req.post(_api(base, "/api/providers"),
                     headers=_headers(token), json=body, auth=auth,
                     timeout=_TIMEOUT)
    except Exception as e:
        return False, f"create gagal: {str(e)[:40]}"
    if r.status_code == 201:
        return True, "SUCCESS"
    if r.status_code == 404:
        return False, "node Atria tidak ketemu (buat node manual dulu)"
    return False, f"create HTTP {r.status_code}"



# ─── Test koneksi (dipakai menu T untuk cek target remote) ──────────
def test_target(base, password, basic_user=None, basic_pass=None):
    """Cek target remote: login + ambil status. Return (ok, msg).

    Kalau basic_user diisi, mode Basic Auth -> skip login 9Router,
    tes langsung GET /api/auth/status dengan Basic Auth.
    """
    if req is None:
        return False, "requests belum terpasang"
    base = _norm_base(base)
    ok, tok = login(base, password, basic_user=basic_user,
                    basic_pass=basic_pass)
    if not ok:
        return False, f"login gagal: {tok}"
    auth = _auth_tuple(None, basic_user, basic_pass)
    try:
        r = req.get(_api(base, "/api/auth/status"),
                    headers=_headers(tok), auth=auth, timeout=_TIMEOUT)
    except Exception as e:
        return False, f"status gagal: {str(e)[:40]}"
    # Mode Basic Auth: anggap OK kalau status 200 (endpoint reachable +
    # auth diterima), walau field authenticated bisa false (panel 9Router
    # tidak wajib login session kalau Basic Auth sudah cover-nya).
    if r.status_code == 401:
        return False, "Basic Auth ditolak (user/pass salah)"
    if r.status_code == 200:
        try:
            d = r.json()
            if auth is not None:
                return True, "ok (Basic Auth valid)"
            if d.get("authenticated"):
                return True, "ok (login valid)"
            return False, "login OK tapi tidak authenticated"
        except Exception:
            return False, "status JSON rusak"
    return False, f"status HTTP {r.status_code}"


if __name__ == "__main__":
    import sys
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    # Format:
    #   test <url> <password> [--basic user:pass]
    #   inject <url> <password> <api_key> [email] [--basic user:pass]
    args = sys.argv[2:]
    bu = bp = None
    if "--basic" in args:
        i = args.index("--basic")
        if i + 1 < len(args) and ":" in args[i + 1]:
            bu, bp = args[i + 1].split(":", 1)
        args = args[:i] + args[i + 2:]

    if sys.argv[1] == "test" and len(args) >= 2:
        ok, msg = test_target(args[0], args[1], basic_user=bu,
                               basic_pass=bp)
        print(f"  {'OK ' if ok else 'BAD'} {args[0]} -> {msg}")
    elif sys.argv[1] == "inject" and len(args) >= 3:
        email = args[3] if len(args) > 3 else None
        ok, msg = inject_key(args[0], args[1], args[2], email=email,
                             basic_user=bu, basic_pass=bp)
        print(f"  {'OK ' if ok else 'BAD'} {msg}")
    else:
        print("  Usage:")
        print("    python ninepanel.py test  <url> <password>")
        print("    python ninepanel.py inject <url> <password> <api_key> [email]")



