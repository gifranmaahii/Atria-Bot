#!/usr/bin/env python3
"""
Atria Temp-Email Register Bot v3 — Interactive Menu (GSuite + temp email)
temp email (multi provider) → register → verify → create API key
CLI: python3 atria_register.py [1|batch N|test|list|export|cek|scan|mail]
"""
import asyncio, email as eml, imaplib, json, os, random, re, sqlite3, string
import subprocess, sys, time
import urllib.parse, uuid
import requests as req
from datetime import datetime, timezone
from pathlib import Path

import turnstile_solver as ts
import ninerouter as nr
import gsuite_provider as gs

# Terminal Windows default (cp437/cp1252) tidak bisa mencetak karakter
# box-drawing di BANNER: tanpa ini muncul UnicodeEncodeError atau mojibake.
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = "https://api.atria-asi.ai"
AUTH = "https://auth.atria-asi.ai"
WORKDIR = Path(__file__).parent
KEYS_FILE = WORKDIR / "api_keys.json"
MODELS_FILE = WORKDIR / "models.json"
EXPORT_FILE = WORKDIR / "9router_export.json"
GSUITE_FILE = WORKDIR / "gsuite.txt"

# ─── 9Router direct inject ─────────────────────────────────────────
# Node custom di panel 9Router (tipe openai-compatible) tempat semua key
# Atria nempel. Pola id-nya sama dengan node lain yang sudah ada di DB
# (openai-compatible-chat-bai, openai-compatible-chat-unikey, dst),
# jadi panel langsung mengenalinya tanpa perlu import manual.
ATRIA_DEFAULT_MODEL = "Atria-Dawn-Preview"
NINEROUTER_PREFIX = "atria"
NINEROUTER_NODE_ID = f"openai-compatible-chat-{NINEROUTER_PREFIX}"
NINEROUTER_NODE_NAME = "Atria AI"
# Auto-inject tiap habis bikin key. Matikan dengan ATRIA_NO_9ROUTER=1.
NINEROUTER_AUTO = os.environ.get("ATRIA_NO_9ROUTER", "").strip().lower() not in (
    "1", "true", "yes", "on")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# ─── Temp mail endpoints (semua gratis, tanpa API key / tanpa modal) ──
WORKER_API = "https://private-inbox-worker.abogoboga.workers.dev"
WORKER_DOMAIN = "inbox.octdev.biz.id"
WORKER_TS_TOKEN = "1x00000000000000000000AA"
TMIO_API = "https://api.internal.temp-mail.io/api/v3"
MAILTM_API = "https://api.mail.tm"
GUERRILLA_API = "https://api.guerrillamail.com/ajax.php"
MAILTM_PASS = "Xk9#mP2!qZ"

# Domain GuerrillaMail yang LOLOS emailBlocklistPolicy Atria (hasil scan:
# dari semua domain temp-mail yang diuji, hanya dua ini yang tidak ditolak).
# Keduanya alias dari mailbox yang sama, jadi inbox tetap terbaca lewat
# sid_token yang sama meski domainnya diganti di sisi klien.
GUERRILLA_DOMAINS = ["grr.la", "sharklasers.com"]

# Provider yang domainnya sudah masuk blocklist Atria (email verifikasi tidak
# akan pernah dikirim). Disimpan supaya bisa dilewati otomatis dan alasannya
# tercetak jelas alih-alih berakhir sebagai "No code" yang membingungkan.
BLOCKED_PROVIDERS = {
    "worker": "inbox.octdev.biz.id (*.biz.id diblokir Atria)",
    "tempmailio": "yzcalo.com / ozsaip.com dll (temp-mail.io diblokir Atria)",
    "mailtm": "uberip.com (mail.tm diblokir Atria)",
}

# Urutan provider yang dicoba. Guerrilla (grr.la) paling depan karena satu-
# satunya yang domainnya masih lolos blocklist Atria. Sisanya hanya dicoba
# kalau user memaksa lewat ATRIA_ALLOW_BLOCKED=1.
PROVIDER_ORDER = ["guerrilla", "worker", "tempmailio", "mailtm"]

# Izinkan provider yang domainnya sudah diblokir (untuk eksperimen/debug).
ALLOW_BLOCKED = (os.environ.get("ATRIA_ALLOW_BLOCKED", "").strip().lower()
                 in ("1", "true", "yes", "on"))

# Jeda antar akun saat batch (detik). mail.tm butuh 60s, sisanya tidak.
DELAY_FAST = 8
DELAY_MAILTM = 62

# Berapa detik menunggu token Cloudflare Turnstile per strategi solver.
# Naikkan lewat env ATRIA_CAPTCHA_WAIT kalau koneksi lambat.
try:
    CAPTCHA_WAIT = int(os.environ.get("ATRIA_CAPTCHA_WAIT", "40"))
except ValueError:
    CAPTCHA_WAIT = 40

# Berapa kali register diulang dari awal kalau Turnstile gagal ditembus.
try:
    CAPTCHA_RETRY = int(os.environ.get("ATRIA_CAPTCHA_RETRY", "2"))
except ValueError:
    CAPTCHA_RETRY = 2

# Camoufox (Firefox anti-fingerprint) jauh lebih sering lolos Turnstile
# dibanding Chromium polos. Kalau tidak terpasang, otomatis balik ke
# Chromium Playwright. Paksa Chromium dengan ATRIA_BROWSER=chromium.
BROWSER_PREF = (os.environ.get("ATRIA_BROWSER") or "auto").strip().lower()
try:
    from camoufox.async_api import AsyncCamoufox
    HAS_CAMOUFOX = True
except ImportError:
    AsyncCamoufox = None
    HAS_CAMOUFOX = False

# Headless bisa dimatikan (ATRIA_HEADLESS=0) untuk captcha yang keras kepala;
# mode berjendela punya tingkat kelulusan lebih tinggi.
HEADLESS = (os.environ.get("ATRIA_HEADLESS", "1").strip().lower()
            not in ("0", "false", "no", "off"))

# Mode semi-manual: Atria memakai reCAPTCHA Enterprise yang tidak bisa
# ditembus gratis, jadi user yang mencentang "I'm not a robot" sekali per
# akun. Aktif secara default; matikan dengan ATRIA_MANUAL=0 kalau memang
# punya solver berbayar (CAPSOLVER_API_KEY / TWOCAPTCHA_API_KEY).
MANUAL_CAPTCHA = (os.environ.get("ATRIA_MANUAL", "1").strip().lower()
                  not in ("0", "false", "no", "off"))

if MANUAL_CAPTCHA:
    # Percuma menunggu klik user di browser yang tidak kelihatan.
    HEADLESS = False
    # Pastikan solver benar-benar memakai strategi manual, bukan strategi
    # Turnstile lama yang hanya membuang waktu di form reCAPTCHA.
    os.environ.setdefault("ATRIA_CAPTCHA_MODE", "manual,service")

# Timeout navigasi Playwright (ms). auth.atria-asi.ai kadang lambat.
try:
    NAV_TIMEOUT = int(os.environ.get("ATRIA_NAV_TIMEOUT", "90000"))
except ValueError:
    NAV_TIMEOUT = 90000

CODE_RE = re.compile(r"(?:code(?:\s+is)?|kode)[^\d]{0,20}(\d{6})", re.I)
ANY_CODE_RE = re.compile(r"\b(\d{6})\b")

# ─── Banner ────────────────────────────────────────────────────────
BANNER = r"""
  ╔══════════════════════════════════════════════╗
  ║   ATRIA Temp-Email Key Farmer v3             ║
  ║   free temp mail → register → verify → key   ║
  ╚══════════════════════════════════════════════╝
"""


def _rand(n=10):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


# ─── Provider base ─────────────────────────────────────────────────
class Inbox:
    """Kontrak minimal: punya .address, .provider, dan .fetch() -> list[str]."""
    provider = "?"

    def __init__(self):
        self.address = None

    def create(self):
        raise NotImplementedError

    def fetch(self):
        """Return list of text blob (subject + preview + body) per pesan."""
        raise NotImplementedError

    @property
    def delay(self):
        return DELAY_FAST


# ─── 1. Cloudflare worker inbox (dipakai juga di main.py) ───────────
class WorkerInbox(Inbox):
    provider = "worker"

    def create(self):
        self.address = f"atria{_rand(9)}@{WORKER_DOMAIN}"
        return self.address

    def fetch(self):
        url = f"{WORKER_API}/api/inbox/{urllib.parse.quote(self.address)}"
        r = req.get(url, headers={
            "Accept": "application/json",
            "User-Agent": UA,
            "X-Turnstile-Token": WORKER_TS_TOKEN,
        }, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        msgs = data.get("messages") or data.get("emails") or []
        return [f"{m.get('subject','')} {m.get('preview','')} {m.get('body','')}"
                for m in msgs]


# ─── 2. temp-mail.io ───────────────────────────────────────────────
class TempMailIoInbox(Inbox):
    provider = "tempmailio"

    def create(self):
        r = req.post(f"{TMIO_API}/email/new",
                     json={"min_name_length": 10, "max_name_length": 10},
                     headers={"User-Agent": UA, "Accept": "application/json"},
                     timeout=20)
        if r.status_code != 200:
            return None
        self.address = r.json().get("email")
        return self.address

    def fetch(self):
        # NOTE: alamat TIDAK boleh di-url-encode di sini, server balas 400.
        r = req.get(f"{TMIO_API}/email/{self.address}/messages",
                    headers={"User-Agent": UA, "Accept": "application/json"},
                    timeout=20)
        if r.status_code != 200:
            return []
        msgs = r.json()
        if not isinstance(msgs, list):
            return []
        return [f"{m.get('subject','')} {m.get('body_text','')} {m.get('body_html','')}"
                for m in msgs]


# ─── 3. mail.tm (butuh bikin akun, rate limit 1/menit) ─────────────
class MailTmInbox(Inbox):
    provider = "mailtm"

    def __init__(self):
        super().__init__()
        self.token = None

    @property
    def delay(self):
        return DELAY_MAILTM

    def create(self):
        r = req.get(f"{MAILTM_API}/domains", timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
        # API pernah balas {"hydra:member": [...]} dan sekarang list biasa,
        # jadi dua-duanya di-handle.
        items = data if isinstance(data, list) else data.get("hydra:member", [])
        if not items:
            return None
        domain = items[0]["domain"]
        self.address = f"atria{_rand(8)}@{domain}"
        r = req.post(f"{MAILTM_API}/accounts",
                     json={"address": self.address, "password": MAILTM_PASS},
                     timeout=20)
        if r.status_code != 201:
            return None
        r2 = req.post(f"{MAILTM_API}/token",
                      json={"address": self.address, "password": MAILTM_PASS},
                      timeout=20)
        self.token = r2.json().get("token") if r2.status_code in (200, 201) else None
        return self.address if self.token else None

    def fetch(self):
        r = req.get(f"{MAILTM_API}/messages",
                    headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        msgs = data if isinstance(data, list) else data.get("hydra:member", [])
        return [f"{m.get('subject','')} {m.get('intro','')}" for m in msgs]


# ─── 4. guerrillamail ──────────────────────────────────────────────
class GuerrillaInbox(Inbox):
    """GuerrillaMail dengan domain alternatif.

    Atria memblokir `guerrillamailblock.com` (dan 170+ domain temp-mail lain)
    lewat emailBlocklistPolicy. Tapi `grr.la` dan `sharklasers.com` — dua
    alias milik GuerrillaMail yang berbagi mailbox yang sama — LOLOS dari
    blocklist itu.

    API-nya sendiri selalu membalas @guerrillamailblock.com apa pun nilai
    parameter `domain` yang dikirim (sudah diverifikasi: set_email_user&
    domain=grr.la tetap balas guerrillamailblock.com), jadi substitusi
    domainnya dilakukan di sisi klien. Inbox tetap dibaca dengan sid_token
    yang sama, sehingga email yang dikirim ke @grr.la tetap terbaca.
    """
    provider = "guerrilla"

    def __init__(self):
        super().__init__()
        self.sid = None
        self.user = None
        self.api_address = None

    def create(self):
        r = req.get(GUERRILLA_API, params={"f": "get_email_address"},
                    headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200:
            return None
        d = r.json()
        self.sid = d.get("sid_token")
        self.api_address = d.get("email_addr") or ""
        if not self.sid or "@" not in self.api_address:
            return None

        # Pakai username sendiri supaya alamatnya tidak tabrakan dengan
        # user GuerrillaMail lain yang sedang memantau inbox acak.
        self.user = f"atria{_rand(8)}"
        r2 = req.get(GUERRILLA_API,
                     params={"f": "set_email_user", "email_user": self.user,
                             "domain": GUERRILLA_DOMAINS[0],
                             "sid_token": self.sid},
                     headers={"User-Agent": UA}, timeout=20)
        if r2.status_code == 200:
            got = (r2.json() or {}).get("email_addr") or ""
            if "@" in got:
                self.api_address = got
                self.user = got.split("@")[0]

        # Ganti domain ke alias yang tidak diblokir Atria.
        domain = random.choice(GUERRILLA_DOMAINS)
        self.address = f"{self.user}@{domain}"
        return self.address

    def fetch(self):
        r = req.get(GUERRILLA_API,
                    params={"f": "check_email", "seq": 0, "sid_token": self.sid},
                    headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200:
            return []
        msgs = r.json().get("list", []) or []
        return [f"{m.get('mail_subject','')} {m.get('mail_excerpt','')}" for m in msgs]


PROVIDERS = {
    "worker": WorkerInbox,
    "tempmailio": TempMailIoInbox,
    "mailtm": MailTmInbox,
    "guerrilla": GuerrillaInbox,
}


def new_inbox(preferred=None):
    """Coba provider satu per satu sampai ada yang berhasil bikin alamat.

    Provider yang domainnya sudah masuk blocklist Atria dilewati (kecuali
    ATRIA_ALLOW_BLOCKED=1) karena email verifikasinya tidak akan pernah
    sampai — hanya membuang MAIL_WAIT detik per akun.
    """
    if preferred and preferred != "auto":
        order = [preferred]
    else:
        order = list(PROVIDER_ORDER)

    for name in order:
        cls = PROVIDERS.get(name)
        if not cls:
            continue
        if name in BLOCKED_PROVIDERS and not ALLOW_BLOCKED:
            print(f"  skip {name}: {BLOCKED_PROVIDERS[name]}", flush=True)
            continue
        try:
            box = cls()
            if box.create():
                return box
        except Exception:
            continue
    return None


# Timeout tunggu kode verifikasi (detik). Beberapa layanan lambat kirim email.
try:
    MAIL_WAIT = int(os.environ.get("ATRIA_MAIL_WAIT", "120"))
except ValueError:
    MAIL_WAIT = 120


def wait_for_code(box, timeout=MAIL_WAIT, interval=4):
    """Polling inbox sampai ketemu kode 6 digit."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            for blob in box.fetch():
                m = CODE_RE.search(blob) or ANY_CODE_RE.search(blob)
                if m:
                    return m.group(1)
        except Exception:
            pass
        time.sleep(interval)
    return None


async def _captcha_state(page):
    """Deteksi Cloudflare Turnstile di form register.

    Return "none" (tidak ada widget), "pending" (widget ada tapi token kosong),
    atau "solved" (token sudah terisi). Delegasi ke turnstile_solver supaya
    logika deteksi hanya ada di satu tempat.
    """
    return await ts.widget_state(page)


async def solve_captcha(page, label="", url=None):
    """Tembus Turnstile di halaman aktif.

    Return True kalau aman untuk lanjut (token dapat, atau memang tidak ada
    captcha), False kalau semua strategi solver gagal.
    """
    def log(msg):
        print(f"{label} {msg}", flush=True)

    token = await ts.solve(page, sitekey=None, url=url,
                           timeout=CAPTCHA_WAIT, log=log)
    if token is None:
        return False
    if token == "":
        return True     # halaman ini memang tidak pasang Turnstile
    return True


class _BrowserSession:
    """Context manager yang menyerahkan (context, cleanup) siap pakai.

    Camoufox dipakai duluan karena fingerprint-nya jauh lebih meyakinkan di
    mata Cloudflare; Chromium Playwright jadi cadangan kalau camoufox tidak
    terpasang atau dipaksa lewat ATRIA_BROWSER=chromium.
    """

    def __init__(self, proxy=None, headless=None):
        self._pw = None
        self._browser = None
        self._ctx = None
        self.kind = "chromium"
        self._proxy = proxy
        # None = pakai HEADLESS global; True/False memaksa mode untuk sesi ini.
        self._headless = headless

    async def __aenter__(self):
        headless = HEADLESS if self._headless is None else self._headless
        use_camoufox = HAS_CAMOUFOX and BROWSER_PREF in ("auto", "camoufox", "firefox")
        if use_camoufox:
            try:
                self._ctx = await AsyncCamoufox(
                    headless=headless,
                    disable_coop=True,
                    i_know_what_im_doing=True,
                    humanize=True,
                    os="windows",
                    config={"forceScopeAccess": True},
                ).start()
                self.kind = "camoufox"
                return self._ctx
            except Exception as e:
                print(f"  camoufox gagal start ({str(e)[:50]}), pakai chromium",
                      flush=True)
                self._ctx = None

        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"])
        ctx_kwargs = {"user_agent": UA, "viewport": {"width": 1280, "height": 800}, "locale": "en-US"}
        if self._proxy:
            ctx_kwargs["proxy"] = {"server": self._proxy}
        self._ctx = await self._browser.new_context(**ctx_kwargs)
        self.kind = "chromium"
        return self._ctx

    async def __aexit__(self, *_exc):
        for obj in (self._ctx, self._browser, self._pw):
            if obj is None:
                continue
            try:
                await (obj.stop() if hasattr(obj, "stop") else obj.close())
            except Exception:
                pass
        return False


# ─── Core: register + create key ───────────────────────────────────
async def register_and_create_key(idx, total, provider=None):
    """Register 1 akun. Kalau Turnstile gagal ditembus atau email kode
    tidak masuk, ulang dari awal dengan inbox (provider BERBEDA kalau
    "No code") + fingerprint browser baru sebanyak CAPTCHA_RETRY kali.

    Strategi provider:
    - auto: tiap retry maju ke provider berikutnya (worker->tempmailio->...).
    - forced (mis. "tempmailio"): mulai dari provider itu; kalau gagal
      karena "No code" (domain ditolak Atria), otomatis fallback ke
      provider berikutnya. Turnstile failure tetap retry provider sama.
    """
    L = f"[{idx}/{total}]"
    attempts = max(1, CAPTCHA_RETRY + 1)
    order = list(PROVIDER_ORDER)

    # Indeks awal provider dalam urutan.
    if provider and provider != "auto":
        try:
            start = order.index(provider)
        except ValueError:
            start = 0
    else:
        start = 0

    tried_providers = []
    for attempt in range(1, attempts + 1):
        # Provider untuk attempt ini: maju satu tiap retry (wrap-around).
        cur = order[(start + attempt - 1) % len(order)]
        tried_providers.append(cur)
        if attempt > 1:
            print(f"{L} Retry {attempt}/{attempts} (provider: {cur}, "
                  f"fingerprint baru)...", flush=True)
            await asyncio.sleep(5)
        result, retryable, reason = await _register_attempt(idx, total, cur, L)
        if result or not retryable:
            return result
        # Kalau gagal Turnstile (bukan "No code"), jangan ganti provider —
        # masalahnya di solver, bukan di domain email.
        if reason == "captcha":
            # Retry berikutnya tetap maju provider (tidak ada salahnya),
            # tapi kasih tahu user penyebabnya Turnstile.
            continue
        # reason == "no_code" atau lainnya -> provider sudah otomatis maju.
    print(f"{L} FAIL: tidak berhasil setelah {attempts} percobaan "
          f"(provider: {' -> '.join(tried_providers)})", flush=True)
    return None


async def _register_attempt(idx, total, provider, L):
    """Satu percobaan register.

    Return (entry_or_None, boleh_diulang, alasan).
    alasan: "captcha" (Turnstile gagal), "no_code" (email tidak masuk),
            "" (sukses / tidak retryable).
    """
    box = new_inbox(provider)
    if not box:
        print(f"{L} FAIL: semua provider temp mail down", flush=True)
        return None, False, ""
    email = box.address
    print(f"{L} {email} ({box.provider})", flush=True)

    session = _BrowserSession()
    async with session as ctx:
        print(f"{L} Browser: {session.kind}"
              f"{'' if HEADLESS else ' (headful)'}", flush=True)
        page = await ts._new_page(ctx)

        captured = []
        register_responses = []  # [(url, status, body_snippet)]

        async def on_resp(resp):
            try:
                if resp.status in (200, 201):
                    body = await resp.text()
                    found = re.findall(r"atr_[A-Za-z0-9_-]{20,}", body)
                    if found:
                        captured.extend(found)
                # Capture semua response ke auth.atria-asi.ai (register,
                # verification-code, sign-in) untuk diagnostik "No code".
                url = resp.url
                if "atria-asi.ai" in url and \
                        ("/register" in url or "/sign-in" in url or
                         "/verification" in url):
                    try:
                        body = await resp.text()
                    except Exception:
                        body = ""
                    register_responses.append(
                        (url, resp.status, body[:300]))
            except Exception:
                pass
        page.on("response", on_resp)

        try:
            await page.goto(f"{BASE}/sign-in", wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT)
            await asyncio.sleep(3)
            await page.locator('a:has-text("Create account")').click()
            await asyncio.sleep(3)
            await page.locator('input[name="identifier"]').fill(email)

            register_url = page.url or f"{AUTH}/register"
            cfg = await ts.recaptcha_config(page)

            if ts.is_recaptcha(cfg):
                # PENTING: di alur reCAPTCHA Logto, widget "I'm not a robot"
                # baru DIRENDER setelah tombol submit diklik — sebelum itu
                # <div class="...captchaBox"> masih kosong (sudah diverifikasi:
                # iframe recaptcha = 0 sebelum klik, 2 sesudah klik). Jadi
                # urutannya harus submit dulu, baru centang captcha; Logto
                # sendiri yang mengirim form begitu callback-nya dipanggil.
                print(f"{L} Captcha: {cfg.get('type')} (mode "
                      f"{cfg.get('mode')}) - submit dulu supaya widget muncul",
                      flush=True)
                await page.locator('button[type="submit"]').click()
                await asyncio.sleep(3)
                print(f"{L} Register submitted", flush=True)

                if not await solve_captcha(page, L, register_url):
                    print(f"{L} FAIL: captcha tidak lolos", flush=True)
                    return None, True, "captcha"
                # Beri waktu Logto meneruskan form setelah callback captcha.
                await asyncio.sleep(5)
            else:
                # Turnstile lama: token harus ada SEBELUM submit.
                if not await solve_captcha(page, L, register_url):
                    print(f"{L} FAIL: captcha tidak lolos", flush=True)
                    return None, True, "captcha"

                await page.locator('button[type="submit"]').click()
                await asyncio.sleep(4)
                print(f"{L} Register submitted", flush=True)

            # Cek URL + response setelah submit. Kalau Atria menerima
            # email, biasanya redirect ke /register/verification-code.
            cur_url = page.url or ""
            if "verification" in cur_url:
                print(f"{L} Atria menerima email (redirect ke verifikasi)",
                      flush=True)
            elif "/register" in cur_url:
                # Masih di /register — kemungkinan ditolak. Log response
                # terakhir untuk lihat alasan (invalid email, rate limit, dll).
                for u, st, body in register_responses[-3:]:
                    print(f"{L}   resp {st}: {u.split('atria-asi.ai')[-1]} "
                          f"-> {body[:120]}", flush=True)

            # Sebagian alur memunculkan challenge kedua setelah submit.
            # Untuk reCAPTCHA hal ini sudah ditangani di blok di atas, jadi
            # jangan memanggil ulang (akan memunculkan prompt manual palsu).
            if not ts.is_recaptcha(cfg):
                await solve_captcha(page, L, register_url)

            # Kalau setelah semua itu masih nyangkut di /register tanpa token
            # captcha yang sah, submit-nya memang tidak pernah sampai server.
            if "/register" in (page.url or "") and \
                    "verification" not in (page.url or ""):
                if ts.is_recaptcha(cfg):
                    stuck = not await ts.recaptcha_token(page)
                else:
                    stuck = await _captcha_state(page) == "pending"
                if stuck:
                    print(f"{L} FAIL: form tidak terkirim (captcha menolak)",
                          flush=True)
                    return None, True, "captcha"


            code = await asyncio.to_thread(wait_for_code, box)
            if not code:
                # Diagnostik: cek apakah email masuk tapi kode tidak match,
                # atau benar-benar kosong. Ini bantu debug "No code".
                try:
                    msgs = box.fetch()
                    n = len(msgs) if msgs else 0
                except Exception:
                    n = -1
                domain = email.split("@")[-1] if "@" in email else "?"
                if n > 0:
                    sample = (msgs[0][:80] if msgs else "")
                    print(f"{L} FAIL: No code (email masuk {n} pesan, "
                          f"tapi kode tak match) — coba provider lain. "
                          f"preview: {sample}", flush=True)
                else:
                    print(f"{L} FAIL: No code (inbox kosong — domain "
                          f"{domain} kemungkinan ditolak Atria, "
                          f"coba provider lain)", flush=True)
                return None, True, "no_code"

            print(f"{L} Code: {code}", flush=True)

            for i, digit in enumerate(code):
                await page.locator(f'input[name="passcode_{i}"]').fill(digit)
                await asyncio.sleep(0.3)
            await asyncio.sleep(8)

            await page.goto(f"{BASE}/console/keys", wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT)
            await asyncio.sleep(4)

            clicked = False
            for sel in ['button:has-text("创建密钥")', 'button:has-text("Create key")']:
                btns = page.locator(sel)
                for i in range(await btns.count()):
                    if await btns.nth(i).is_visible():
                        await btns.nth(i).click()
                        clicked = True
                        break
                if clicked:
                    break

            if not clicked:
                print(f"{L} FAIL: No create btn", flush=True)
                return None, False, ""


            await asyncio.sleep(2)
            dialog = page.locator(".key-create-dialog")
            if await dialog.count() > 0:
                await dialog.evaluate("el => { if (!el.open) el.showModal(); }")
                await asyncio.sleep(1)
                await dialog.locator("input").first.fill(f"key-{idx}")
                await asyncio.sleep(0.3)
                await dialog.locator('button[type="submit"]').first.click(force=True)
                await asyncio.sleep(5)

            key = None
            if captured:
                key = captured[-1]
            if not key:
                body = await page.inner_text("body")
                found = re.findall(r"atr_[A-Za-z0-9_-]{20,}", body)
                if found:
                    key = found[0]
            if not key:
                html = await page.content()
                found = re.findall(r"atr_[A-Za-z0-9_-]{20,}", html)
                if found:
                    key = found[0]

            if key:
                print(f"{L} OK: {key[:30]}...", flush=True)
                return {"email": email, "key": key, "name": f"key-{idx}",
                        "source": "tempmail", "provider": box.provider,
                        "status": "active",
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}, False, ""
            print(f"{L} FAIL: No key", flush=True)
            return None, False, ""
        except Exception as e:
            print(f"{L} ERROR: {e}", flush=True)
            return None, False, ""


# ─── GSuite: register via Google OAuth ─────────────────────────────
# Atria masih punya tombol "Continue with Google" di /sign-in. Login akun
# Workspace lewat OAuth TIDAK memicu reCAPTCHA Atria, jadi tidak butuh
# solver captcha dan tidak perlu membaca kode dari inbox (IMAP basic-auth
# memang sudah ditolak Google untuk semua akun Workspace).
GSUITE_HEADLESS = (os.environ.get("ATRIA_GSUITE_HEADLESS", "1").strip().lower()
                   not in ("0", "false", "no", "off"))
# Alur gsuite: pakai proxy dulu, gagal -> rotasi proxy baru, baru direct.
GSUITE_PROXY = (os.environ.get("ATRIA_GSUITE_PROXY", "1").strip().lower()
                not in ("0", "false", "no", "off"))
GSUITE_PROXY_MAX = int(os.environ.get("ATRIA_PROXY_MAX", "4") or "4")

_GSUITE_USED = set()


def next_gsuite_account():
    """Akun gsuite berikutnya yang belum menghasilkan key."""
    used = {e.get("email") for e in load_keys()} | _GSUITE_USED
    for a in gs.load_gsuite_accounts(str(GSUITE_FILE)):
        if a["email"] not in used:
            _GSUITE_USED.add(a["email"])
            return a
    return None


async def _google_login_state(gpage):
    """Ringkas kondisi halaman login Google: 'challenge:<...>' /
    'error:<...>' / 'consent' / ''. Dipakai membedakan kegagalan
    kredensial dari layar consent yang tinggal diklik.

    Bahasa halaman mengikuti setelan akun (Workspace ID sering
    Indonesia), jadi frasa EN dan ID dicek berdua.
    """
    try:
        txt = (await gpage.inner_text("body")).lower()
    except Exception:
        return "closed"
    if ("verify it" in txt or "2-step" in txt or "second step" in txt
            or "verifikasi 2 langkah" in txt or "kode verifikasi" in txt):
        return "challenge: Google minta verifikasi 2FA"
    if ("wrong password" in txt or "that password is incorrect" in txt
            or "kata sandi salah" in txt):
        return "error: password salah"
    if ("couldn't find your google account" in txt
            or "tidak dapat menemukan akun google" in txt):
        return "error: email tidak dikenal Google"
    if ("unusual activity" in txt or "couldn't verify" in txt
            or "aktivitas tidak biasa" in txt):
        return "error: Google blokir (bot detection)"
    if (("allow" in txt or "i agree" in txt or "access to your" in txt
         or "i accept" in txt or "welcome to your new" in txt
         or "terms of service" in txt)
            or ("mengizinkan" in txt or "akses info tentang" in txt
                or "selamat datang" in txt or "setuju" in txt)):
        return "consent"
    return ""


async def _gsuite_register(account, idx=1, total=1, proxy=None):
    """Register 1 akun Atria via Google OAuth (email+password gsuite)."""
    L = f"[{idx}/{total}]"
    email, password = account["email"], account["password"]
    print(f"{L} gsuite: {email}"
          + (f" via {proxy}" if GSUITE_PROXY and proxy else " (direct)")
          + "", flush=True)

    session = _BrowserSession(proxy=proxy if GSUITE_PROXY else None,
                              headless=GSUITE_HEADLESS)
    async with session as ctx:
        print(f"{L} Browser: {session.kind}"
              f"{'' if GSUITE_HEADLESS else ' (headful)'}", flush=True)
        page = await ts._new_page(ctx)
        captured = []

        async def on_resp(resp):
            try:
                if resp.status in (200, 201):
                    body = await resp.text()
                    found = re.findall(r"atr_[A-Za-z0-9_-]{20,}", body)
                    if found:
                        captured.extend(found)
            except Exception:
                pass
        page.on("response", on_resp)

        try:
            await page.goto(f"{BASE}/sign-in", wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT)
            await asyncio.sleep(3)
            await page.locator(
                'a:has-text("Continue with Google"),'
                ' button:has-text("Continue with Google")').first.click()
            print(f"{L} -> menunggu halaman Google OAuth...", flush=True)

            # OAuth bisa membuka popup atau navigasi di tab yang sama.
            gpage = None
            deadline = time.time() + 30
            while time.time() < deadline:
                for pg in page.context.pages:
                    if "accounts.google.com" in (pg.url or ""):
                        gpage = pg
                        break
                if gpage:
                    break
                await asyncio.sleep(1)
            if gpage is None:
                gpage = page  # kemungkinan navigasi terjadi di tab sama

            ok = await _gsuite_google_login(gpage, email, password, L)
            if not ok:
                return None
            cpage = await _gsuite_wait_console(page, L)
            print(f"{L} Login OK, buat key...", flush=True)
            return await _gsuite_create_key(cpage, captured, idx, L, email)
        except Exception as e:
            print(f"{L} ERROR: {e}", flush=True)
            return None


def run_gsuite_with_rotation(acct, idx=1, total=1):
    """Pakai proxy dulu; gagal -> ganti proxy baru; habis -> direct.

    Proxy diambil dari pool PetaniProxy (gsuite_provider.next_proxy) dan
    dicep dulu bisa-tidaknya CONNECT ke HTTPS, supaya tidak membuang waktu
    menyalakan browser demi proxy yang sudah mati.
    """
    if not GSUITE_PROXY:
        return asyncio.run(_gsuite_register(acct, idx, total))
    tried = set()
    for attempt in range(1, GSUITE_PROXY_MAX + 1):
        proxy = gs.next_proxy()
        if proxy is None:
            break  # pool kosong
        if proxy in tried:
            continue  # sudah pernah dipakai di akun ini
        tried.add(proxy)
        ip = gs.test_proxy(proxy)
        if not ip:
            print(f"[{idx}/{total}] proxy mati, rotasi... {proxy}", flush=True)
            continue
        print(f"[{idx}/{total}] attempt {attempt}/{GSUITE_PROXY_MAX} "
              f"via {proxy} [{ip}]", flush=True)
        result = asyncio.run(_gsuite_register(acct, idx, total, proxy=proxy))
        if result:
            return result
        print(f"[{idx}/{total}] gagal, rotasi proxy baru...", flush=True)
    print(f"[{idx}/{total}] usaha terakhir: direct (tanpa proxy)", flush=True)
    return asyncio.run(_gsuite_register(acct, idx, total, proxy=None))


# __GSUITE_HELPERS__


async def _gsuite_google_login(gpage, email, password, L):
    """Isi email -> password -> consent di accounts.google.com."""
    deadline = time.time() + 150
    while time.time() < deadline:
        try:
            cur = gpage.url or ""
        except Exception:
            cur = "closed"
        if "atria" in cur and "accounts.google.com" not in cur:
            return True
        state = await _google_login_state(gpage)
        if state.startswith(("error", "challenge", "closed")):
            print(f"{L} FAIL: {state}", flush=True)
            return False
        # ── Consent / account chooser / Workspace ToS ("speedbump") ──
        # Bahasa halaman ikut setelan akun (Workspace ID: "Lanjutkan",
        # "Setuju", "Izinkan"). Deteksi via teks state maupun URL chooser
        # Google (/signin/oauth/id), lalu klik tombol utamanya —
        # get_by_role("button") juga menangkap div[role=button].
        if state == "consent" or "/signin/oauth/id" in cur:
            cb = gpage.locator('input[type="checkbox"]')
            if await cb.count():
                try:
                    await cb.first.click(timeout=3000)
                    await asyncio.sleep(1)
                except Exception:
                    pass
            btn = gpage.get_by_role(
                "button",
                name=re.compile(r"(lanjutkan|setuju|izinkan|mulai|allow|"
                                r"i agree|i accept|accept|i understand|"
                                r"got it|continue)", re.I))
            if await btn.count():
                await btn.first.click()
                await asyncio.sleep(3)
                continue
        # Cek password SEBELUM email: di halaman password masih ada
        # #identifierId versi hidden yang tidak bisa diisi (memakan timeout).
        if await gpage.locator('input[name="Passwd"]').count():
            try:
                await gpage.fill('input[name="Passwd"]', password, timeout=10000)
                await gpage.click('#passwordNext')
            except Exception:
                pass
            await asyncio.sleep(5)
            continue
        if await gpage.locator('#identifierId').count():
            try:
                await gpage.fill('#identifierId', email, timeout=10000)
                await gpage.click('#identifierNext')
            except Exception:
                pass
            await asyncio.sleep(3)
            continue
        # Account chooser: pilih "Use another account" sebelum isi email.
        another = gpage.locator('text=/another account/i')
        if await another.count():
            await another.first.click()
            await asyncio.sleep(3)
            continue
        await asyncio.sleep(3)
    print(f"{L} FAIL: Google login tidak selesai", flush=True)
    return False


async def _gsuite_wait_console(page, L):
    """Salah satu tab context berakhir di console Atria setelah OAuth."""
    deadline = time.time() + 90
    while time.time() < deadline:
        for pg in page.context.pages:
            try:
                u = pg.url or ""
            except Exception:
                continue
            if "atria" in u and "console" in u:
                return pg
        await asyncio.sleep(2)
    print(f"{L} WARN: tab console belum muncul, paksa navigasi", flush=True)
    return page


async def _gsuite_create_key(cpage, captured, idx, L, email):
    """Klik create key di /console/keys, kembalikan entry hasil."""
    await cpage.goto(f"{BASE}/console/keys", wait_until="domcontentloaded",
                     timeout=NAV_TIMEOUT)
    await asyncio.sleep(4)

    clicked = False
    for sel in ['button:has-text("创建密钥")', 'button:has-text("Create key")']:
        btns = cpage.locator(sel)
        for i in range(await btns.count()):
            if await btns.nth(i).is_visible():
                await btns.nth(i).click()
                clicked = True
                break
        if clicked:
            break
    if not clicked:
        print(f"{L} FAIL: No create btn (login OAuth mungkin gagal)", flush=True)
        return None

    await asyncio.sleep(2)
    dialog = cpage.locator(".key-create-dialog")
    if await dialog.count() > 0:
        await dialog.evaluate("el => { if (!el.open) el.showModal(); }")
        await asyncio.sleep(1)
        await dialog.locator("input").first.fill(f"key-{idx}")
        await asyncio.sleep(0.3)
        await dialog.locator('button[type="submit"]').first.click(force=True)
        await asyncio.sleep(5)

    key = None
    if captured:
        key = captured[-1]
    if not key:
        body = await cpage.inner_text("body")
        found = re.findall(r"atr_[A-Za-z0-9_-]{20,}", body)
        if found:
            key = found[0]
    if not key:
        html = await cpage.content()
        found = re.findall(r"atr_[A-Za-z0-9_-]{20,}", html)
        if found:
            key = found[0]

    if key:
        print(f"{L} OK: {key[:30]}...", flush=True)
        return {"email": email, "key": key, "name": f"key-{idx}",
                "source": "gsuite", "provider": "gsuite",
                "status": "active",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    print(f"{L} FAIL: No key", flush=True)
    return None

# ─── Load / Save ───────────────────────────────────────────────────
def load_keys():
    if KEYS_FILE.exists():
        return json.loads(KEYS_FILE.read_text())
    return []


def save_keys(data):
    KEYS_FILE.write_text(json.dumps(data, indent=2))


def get_valid_keys():
    return [e for e in load_keys() if e.get("key")]


def provider_stats():
    """Hitung key per provider temp mail."""
    stats = {}
    for e in get_valid_keys():
        p = e.get("provider") or e.get("source") or "?"
        stats[p] = stats.get(p, 0) + 1
    return stats


# ─── 9Router: inject langsung ke SQLite ────────────────────────────
def _now_iso():
    """Timestamp ISO-8601 UTC, format sama dengan yang dipakai 9Router."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def find_9router_db():
    """DB 9Router utama (target pertama yang sehat).

    Dipertahankan demi kompatibilitas kode lama. Untuk inject ke SEMUA
    9Router sekaligus, pakai list_9router_targets() / sync_all_to_9router().
    """
    targets = nr.resolve()
    if targets:
        return Path(targets[0]["db"])

    # Cadangan kalau ninerouter tidak menemukan apa pun.
    cands = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        cands.append(Path(appdata) / "9router" / "db" / "data.sqlite")
    home = Path.home()
    cands += [
        home / "AppData" / "Roaming" / "9router" / "db" / "data.sqlite",
        home / ".9router" / "db" / "data.sqlite",
        home / ".config" / "9router" / "db" / "data.sqlite",
        home / "Library" / "Application Support" / "9router" / "db" / "data.sqlite",
    ]
    for p in cands:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def list_9router_targets(include_broken=False):
    """Semua 9Router tujuan inject (env > config > auto-discovery)."""
    return nr.resolve(include_broken=include_broken)


def _ensure_9router_node(conn):
    """Pastikan node provider 'Atria AI' ada di tabel providerNodes."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM providerNodes WHERE id = ?", (NINEROUTER_NODE_ID,))
    if cur.fetchone():
        return False

    now = _now_iso()
    node_data = {
        "prefix": NINEROUTER_PREFIX,
        "apiType": "chat",
        "baseUrl": f"{BASE}/v1",
    }
    cur.execute(
        "INSERT INTO providerNodes (id, type, name, data, createdAt, updatedAt) "
        "VALUES (?, 'openai-compatible', ?, ?, ?, ?)",
        (NINEROUTER_NODE_ID, NINEROUTER_NODE_NAME, json.dumps(node_data), now, now),
    )
    return True


def _9router_conn_data(api_key, email):
    """Susun kolom `data` (JSON) untuk satu koneksi provider."""
    return {
        "apiKey": api_key,
        "defaultModel": ATRIA_DEFAULT_MODEL,
        "testStatus": "active",
        "errorCode": None,
        "lastRefreshAt": _now_iso(),
        "providerSpecificData": {
            "prefix": NINEROUTER_PREFIX,
            "apiType": "chat",
            "baseUrl": f"{BASE}/v1",
            "nodeName": NINEROUTER_NODE_NAME,
            "authMethod": "apikey",
            "source": "atria-tempmail-farmer",
            "apiBase": BASE,
            "chatUrl": f"{BASE}/v1/chat/completions",
            "email": email,
            "connectionProxyEnabled": False,
            "connectionProxyUrl": "",
            "connectionNoProxy": "",
        },
    }


def inject_to_9router(api_key, email=None, name=None, skip_if_exists=False, db=None):
    """Inject/update satu key Atria ke database 9Router (SQLite).

    Return (ok: bool, msg: str):
      - (False, "DB_NOT_FOUND")  -> 9Router belum terpasang / DB tidak ketemu
      - (True, "ALREADY_EXISTS") -> key sudah ada & skip_if_exists=True
      - (True, "UPDATED")        -> key lama di-upsert dengan data terbaru
      - (True, "SUCCESS")        -> koneksi baru dibuat
      - (False, "<error>")       -> gagal, pesan errornya
    """
    if not api_key:
        return False, "NO_KEY"

    db_path = Path(db) if db else find_9router_db()
    if not db_path or not db_path.is_file():
        return False, "DB_NOT_FOUND"

    label = name or email or api_key[:18]
    conn = None
    try:
        # timeout supaya tidak langsung 'database is locked' kalau panel
        # 9Router sedang jalan dan menulis ke DB yang sama.
        conn = sqlite3.connect(str(db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        _ensure_9router_node(conn)

        # apiKey adalah identitas unik; email bisa kosong di data lama.
        cur.execute(
            "SELECT id, data FROM providerConnections WHERE provider = ?",
            (NINEROUTER_NODE_ID,),
        )
        found = None
        for row in cur.fetchall():
            try:
                if json.loads(row["data"] or "{}").get("apiKey") == api_key:
                    found = row
                    break
            except (ValueError, TypeError):
                continue

        now = _now_iso()
        if found:
            if skip_if_exists:
                conn.commit()
                return True, "ALREADY_EXISTS"
            # Pertahankan field runtime 9Router (modelLock_*, usage, backoff...)
            try:
                merged = json.loads(found["data"] or "{}")
            except (ValueError, TypeError):
                merged = {}
            merged.update(_9router_conn_data(api_key, email))
            cur.execute(
                "UPDATE providerConnections SET name = ?, email = ?, data = ?, "
                "isActive = 1, updatedAt = ? WHERE id = ?",
                (label, email, json.dumps(merged), now, found["id"]),
            )
            conn.commit()
            return True, "UPDATED"

        cur.execute(
            "INSERT INTO providerConnections "
            "(id, provider, authType, name, email, isActive, data, createdAt, updatedAt) "
            "VALUES (?, ?, 'apikey', ?, ?, 1, ?, ?, ?)",
            (
                str(uuid.uuid4()),
                NINEROUTER_NODE_ID,
                label,
                email,
                json.dumps(_9router_conn_data(api_key, email)),
                now,
                now,
            ),
        )
        conn.commit()
        return True, "SUCCESS"

    except Exception as e:
        return False, str(e)[:80]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def inject_all_9router(api_key, email=None, name=None, skip_if_exists=False,
                       targets=None):
    """Inject satu key ke SEMUA 9Router yang terdaftar (lokal + remote).

    Target remote (panel online) dikirim via HTTP API ninepanel.inject_key,
    target lokal via SQLite inject_to_9router.

    Return (ok_count, total, detail) — detail = list (nama_target, msg).
    ok_count > 0 berarti minimal satu 9Router berhasil menerima key.
    """
    # Ambil daftar target (skip_remote_status biar tidak login dua kali).
    if targets is None:
        targets = nr.resolve(include_broken=False,
                              skip_remote_status=True)
    if not targets:
        return 0, 0, [("-", "DB_NOT_FOUND")]

    ok_count = 0
    detail = []
    for t in targets:
        ttype = t.get("type", "local")
        if ttype == "remote":
            # Inject via HTTP API ninepanel.
            npr = nr._lazy_ninepanel()
            if npr is None:
                detail.append((t.get("name", "?"), "NO_NINEPANEL"))
                continue
            ok, msg = npr.inject_key(
                t.get("url"), t.get("password", ""),
                api_key, email=email, name=name,
                skip_if_exists=skip_if_exists,
                basic_user=t.get("basic_user"),
                basic_pass=t.get("basic_pass", ""))
        else:
            # Inject lokal via SQLite.
            ok, msg = inject_to_9router(
                api_key, email, name, skip_if_exists=skip_if_exists,
                db=t["db"])
        if ok:
            ok_count += 1
        detail.append((t.get("name", "?"), msg))
    return ok_count, len(targets), detail


def auto_inject_9router(entry):
    """Dipanggil otomatis tiap 1 key berhasil dibuat (menu 1 / batch / CLI).

    Sekarang menyebar ke semua 9Router yang terdaftar, bukan cuma satu.
    """
    if not NINEROUTER_AUTO or not entry or not entry.get("key"):
        return False

    ok_count, total, detail = inject_all_9router(
        entry["key"], entry.get("email"), entry.get("email"),
        skip_if_exists=True)

    if total == 0 or (total == 1 and detail[0][1] == "DB_NOT_FOUND"):
        print("      -> 9Router: skip (DB tidak ditemukan)", flush=True)
        return False

    if total == 1:
        msg = detail[0][1]
        entry["9router"] = msg
        if ok_count:
            print(f"      -> 9Router: {msg}", flush=True)
        else:
            print(f"      -> 9Router: FAIL ({msg})", flush=True)
        return bool(ok_count)

    # Lebih dari satu target: ringkas supaya log batch tidak membanjir.
    entry["9router"] = f"{ok_count}/{total}"
    ringkas = ", ".join(f"{n}:{m}" for n, m in detail)
    status = "OK" if ok_count == total else ("PARTIAL" if ok_count else "FAIL")
    print(f"      -> 9Router x{total}: {status} ({ringkas})", flush=True)
    return bool(ok_count)


def sync_all_to_9router(entries=None, force=True, quiet=False):
    """Inject/update semua key valid ke SEMUA 9Router yang terdaftar.

    force=True  -> key yang sudah ada tetap di-update (data terbaru).
    force=False -> key yang sudah ada dilewati (ALREADY_EXISTS).

    Return dict: {"new":n, "updated":n, "skipped":n, "fail":n,
                  "targets":[...], "db":path_pertama}
    """
    keys = load_keys() if entries is None else entries
    valid = [e for e in keys if e.get("key")]
    res = {"new": 0, "updated": 0, "skipped": 0, "fail": 0,
           "targets": [], "db": None}

    if not valid:
        if not quiet:
            print("\n  No valid keys.")
        return res

    targets = nr.resolve(include_broken=False, skip_remote_status=True)
    if not targets:
        if not quiet:
            print("\n  9Router target tidak ditemukan.")
            print("  Tambahkan manual lewat menu T, atau:")
            print("    python ninerouter.py add <path\\data.sqlite>")
            print("    (remote) menu T -> R, isi URL + password")
        return res

    res["targets"] = [t.get("url") or t["db"] for t in targets]
    res["db"] = targets[0].get("url") or targets[0]["db"]

    if not quiet:
        print(f"\n  [S] Sync {len(valid)} key -> {len(targets)} 9Router")
        for i, t in enumerate(targets, 1):
            ttype = t.get("type", "local")
            loc = t.get("url") or t["db"]
            print(f"   {i}. [{t['source']}|{ttype}] {loc}")
        print(f"  Node: {NINEROUTER_NODE_NAME} ({NINEROUTER_NODE_ID})")
        print("  " + "-" * 62)

    npr = nr._lazy_ninepanel()
    for i, e in enumerate(valid, 1):
        email = e.get("email") or ""
        if not quiet:
            print(f"  {i:>2}. {email[:34]:<35}", end="", flush=True)

        marks = []
        for t in targets:
            ttype = t.get("type", "local")
            if ttype == "remote":
                if npr is None:
                    res["fail"] += 1
                    marks.append("x")
                    continue
                ok, msg = npr.inject_key(
                    t.get("url"), t.get("password", ""),
                    e["key"], email=email,
                    name=email or e.get("name"),
                    skip_if_exists=not force,
                    basic_user=t.get("basic_user"),
                    basic_pass=t.get("basic_pass", ""))
            else:
                ok, msg = inject_to_9router(
                    e["key"], email, email or e.get("name"),
                    skip_if_exists=not force, db=t["db"])
            if ok:
                if msg == "SUCCESS":
                    res["new"] += 1
                elif msg == "UPDATED":
                    res["updated"] += 1
                else:
                    res["skipped"] += 1
                marks.append("+" if msg == "SUCCESS"
                             else ("u" if msg == "UPDATED" else "."))
            else:
                res["fail"] += 1
                marks.append("x")
        e["9router"] = "".join(marks)
        if not quiet:
            print("".join(marks))

    if entries is None:
        save_keys(keys)

    if not quiet:
        print("  " + "-" * 62)
        print("  Legenda: + baru | u update | . skip | x gagal")
        print(f"  Result: {res['new']} baru | {res['updated']} update | "
              f"{res['skipped']} skip | {res['fail']} gagal "
              f"(x{len(targets)} target)")
        print("  Restart / refresh panel 9Router untuk lihat hasilnya "
              "(lokal); remote langsung update.")
    return res


def menu_a_add():
    """Tambah key manual (hasil register sendiri di browser) + sync ke 9Router."""
    print("\n  [A] Tambah Key Manual")
    print("  " + "-" * 55)
    print("  Register sendiri di https://api.atria-asi.ai/sign-in,")
    print("  buat key di /console/keys, lalu tempel di sini.")
    print("  Kosongkan input untuk selesai.\n")

    keys = load_keys()
    known = {e.get("key") for e in keys if e.get("key")}
    added = []

    while True:
        raw = input("  API key (atr_...): ").strip()
        if not raw:
            break
        if not raw.startswith("atr_") or len(raw) < 24:
            print("  Format salah, key Atria diawali 'atr_'. Ulangi.")
            continue
        if raw in known:
            print("  Key ini sudah ada di api_keys.json, dilewati.")
            continue

        email = input("  Email akun (opsional): ").strip()
        entry = {
            "email": email or f"manual-{len(keys) + 1}",
            "key": raw,
            "name": f"key-{len(keys) + 1}",
            "source": "manual",
            "provider": "manual",
            "status": "active",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        keys.append(entry)
        known.add(raw)
        added.append(entry)
        save_keys(keys)
        print(f"  Tersimpan: {entry['email']} ({raw[:20]}...)\n")

    if not added:
        print("\n  Tidak ada key baru.")
        return

    print(f"\n  {len(added)} key baru disimpan -> {KEYS_FILE.name}")
    res = sync_all_to_9router(entries=added, force=True)
    if res["new"] or res["updated"]:
        print(f"  9Router: {res['new']} baru | {res['updated']} update")


def menu_s_sync():
    """Sync semua key ke 9Router (inject langsung ke SQLite)."""
    sync_all_to_9router(force=True)


# ─── Menu Commands ─────────────────────────────────────────────────
def menu_1_create():
    """Buat 1 akun"""
    print("\n  [1] Buat 1 Akun Temp Email")
    print("  " + "-" * 40)
    result = asyncio.run(register_and_create_key(1, 1))
    if result:
        keys = load_keys()
        keys.append(result)
        save_keys(keys)
        # Auto-inject key baru ke semua 9Router (lokal + remote).
        auto_inject_9router(result)
        print(f"\n  OK: {result['email']}")
        print(f"  Key: {result['key']}")
    else:
        print("\n  FAIL")


def menu_2_batch():
    """Batch N akun"""
    n = input("\n  Jumlah akun: ").strip()
    if not n.isdigit() or int(n) < 1:
        print("  Invalid number")
        return
    n = int(n)
    print(f"\n  [2] Batch {n} Akun Temp Email")
    print(f"  Provider: {' -> '.join(PROVIDER_ORDER)} (auto fallback)")
    print(f"  Estimasi: ~{max(1, n * (DELAY_FAST + 45) // 60)} menit")
    print("  " + "-" * 40)

    existing = load_keys()
    done = {e.get("email") for e in existing}

    for idx in range(1, n + 1):
        if idx > 1:
            print(f"  Wait {DELAY_FAST}s...", flush=True)
            time.sleep(DELAY_FAST)
        try:
            result = asyncio.run(register_and_create_key(idx, n))
        except KeyboardInterrupt:
            save_keys(existing)
            ok = sum(1 for e in existing if e.get("key"))
            print(f"\n\n  ⏹  Dihentikan di akun {idx}/{n}.")
            print(f"     Tersimpan {ok} key. (lihat menu 6 / 7 untuk export)")
            return
        if result and result.get("email") not in done:
            existing.append(result)
            done.add(result["email"])
            save_keys(existing)
            # Auto-inject key baru ke semua 9Router (lokal + remote).
            auto_inject_9router(result)

    ok = sum(1 for e in existing if e.get("key"))
    fail = sum(1 for e in existing if not e.get("key"))
    print(f"\n  DONE: {ok} keys | {fail} failed")
    print(f"  Total keys in file: {ok}")


def run_gbatch(n):
    """Batch GSuite (Google OAuth) — dipakai menu maupun CLI."""
    existing = load_keys()
    done = {e.get("email") for e in existing}
    ok = 0
    for idx in range(1, n + 1):
        acct = next_gsuite_account()
        if not acct:
            print("  Akun gsuite habis (tambahkan di gsuite.txt).")
            break
        try:
            result = run_gsuite_with_rotation(acct, idx, n)
        except KeyboardInterrupt:
            save_keys(existing)
            print(f"\n\n  ⏹  Dihentikan. Tersimpan "
                  f"{sum(1 for e in existing if e.get('key'))} key.")
            return
        if result and result.get("email") not in done:
            existing.append(result)
            done.add(result["email"])
            save_keys(existing)
            auto_inject_9router(result)
            ok += 1
        if idx < n:
            time.sleep(3)
    print(f"\n  DONE: +{ok} key gsuite")


def menu_g_create():
    """Buat 1 akun via Google OAuth (GSuite)"""
    print("\n  [G] Buat 1 Akun GSuite (Google OAuth)")
    print("  " + "-" * 40)
    acct = next_gsuite_account()
    if not acct:
        print("  Tidak ada akun gsuite tersisa di gsuite.txt.")
        return
    result = run_gsuite_with_rotation(acct, 1, 1)
    if result:
        keys = load_keys()
        keys.append(result)
        save_keys(keys)
        auto_inject_9router(result)
        print(f"\n  OK: {result['email']}")
        print(f"  Key: {result['key']}")
    else:
        print("\n  FAIL")


def menu_g_batch():
    """Batch N akun via Google OAuth (GSuite)"""
    n = input("\n  Jumlah akun: ").strip()
    if not n.isdigit() or int(n) < 1:
        print("  Invalid number")
        return
    n = int(n)
    print(f"\n  [B] Batch {n} Akun GSuite (Google OAuth)")
    print("  " + "-" * 40)
    run_gbatch(n)


def menu_3_test():
    """Test semua key"""
    keys = load_keys()
    if not keys:
        print("\n  No keys. Run menu 1 or 2 first.")
        return

    print(f"\n  [3] Test {len(keys)} Keys")
    print("  " + "-" * 40)

    for i, e in enumerate(keys, 1):
        k = e.get("key")
        if not k:
            print(f"  {i:>2}. {e.get('email','?')[:35]:<36} NO KEY")
            continue
        print(f"  {i:>2}. {e.get('email','?')[:35]:<36} {k[:22]}...", end=" ", flush=True)
        # Model reasoning -> balasan bisa lama, timeout dibikin longgar.
        r = subprocess.run(["curl", "-s", "-X", "POST", f"{BASE}/v1/chat/completions",
            "-H", f"Authorization: Bearer {k}", "-H", "Content-Type: application/json",
            "-d", json.dumps({"model": "Atria-Dawn-Preview",
                "messages": [{"role": "user", "content": "say ok"}], "max_tokens": 50}),
            "--max-time", "90"], capture_output=True, text=True)
        try:
            d = json.loads(r.stdout)
            if "choices" in d:
                print("OK")
                e["status"] = "active"
            else:
                err = d.get("error", {}).get("message", "?")[:40]
                print(f"ERR: {err}")
                e["status"] = "error"
        except Exception:
            print("FAIL")
            e["status"] = "error"
    save_keys(keys)


def menu_4_check():
    """Cek sisa token per akun"""
    keys = load_keys()
    if not keys:
        print("\n  No keys.")
        return

    print(f"\n  [4] Cek Token {len(keys)} Akun")
    print("  " + "-" * 40)

    for i, e in enumerate(keys, 1):
        k = e.get("key")
        if not k:
            print(f"  {i:>2}. {e.get('email','?')[:35]:<36} NO KEY")
            continue
        print(f"  {i:>2}. {e.get('email','?')[:35]:<36}", end="", flush=True)
        r = subprocess.run(["curl", "-s", "-X", "GET", f"{BASE}/v1/models",
            "-H", f"Authorization: Bearer {k}",
            "--max-time", "15"], capture_output=True, text=True)
        try:
            d = json.loads(r.stdout)
            if "data" in d:
                print(f" OK ({len(d['data'])} models)")
            else:
                print(f" ERR: {d.get('error',{}).get('message','?')[:30]}")
        except Exception:
            print(" FAIL")


def menu_5_scan():
    """Scan semua free models"""
    keys = get_valid_keys()
    if not keys:
        print("\n  No keys.")
        return

    k = keys[0]["key"]
    print(f"\n  [5] Scan Models (key: {k[:22]}...)")
    print("  " + "-" * 40)

    r = subprocess.run(["curl", "-s", "-X", "GET", f"{BASE}/v1/models",
        "-H", f"Authorization: Bearer {k}",
        "--max-time", "15"], capture_output=True, text=True)
    try:
        d = json.loads(r.stdout)
        models = d.get("data", [])
        print(f"\n  Total models: {len(models)}")
        print(f"  {'Model ID':<45} {'Owned':<8}")
        print(f"  {'-'*53}")
        for m in sorted(models, key=lambda x: x["id"]):
            owned = "yes" if m.get("owned_by") else ""
            print(f"  {m['id']:<45} {owned}")
        MODELS_FILE.write_text(json.dumps([m["id"] for m in models], indent=2))
        print(f"\n  Saved -> {MODELS_FILE.name}")
    except Exception:
        print(f"  FAIL: {r.stdout[:100]}")


def menu_6_list():
    """List semua key"""
    keys = load_keys()
    if not keys:
        print("\n  No keys.")
        return

    print("\n  [6] List Keys")
    print("  " + "-" * 55)
    print(f"  {'#':<4} {'Email':<40} {'Key':<26} {'Prov':<11} {'St':<5}")
    print(f"  {'-'*88}")

    for i, e in enumerate(keys, 1):
        k = e.get("key") or "-"
        prov = (e.get("provider") or e.get("source") or "?")[:10]
        st = "OK" if e.get("key") else "FAIL"
        print(f"  {i:<4} {e.get('email','?'):<40} {k[:25]:<26} {prov:<11} {st}")

    stats = provider_stats()
    detail = ", ".join(f"{v} {p}" for p, v in sorted(stats.items())) or "-"
    print(f"\n  Total: {len(keys)} ({detail})")


def menu_7_export():
    """Export 9Router JSON"""
    valid = get_valid_keys()
    if not valid:
        print("\n  No valid keys.")
        return

    exp = [{"apiKey": e["key"], "name": e.get("name", f"a{i}"),
            "provider": "openai-compatible-chat",
            "baseUrl": f"{BASE}/v1", "defaultModel": "Atria-Dawn-Preview"}
           for i, e in enumerate(valid, 1)]
    out = WORKDIR / "9router_export.json"
    out.write_text(json.dumps(exp, indent=2))
    print(f"\n  Exported {len(exp)} providers -> {out}")


def menu_8_delete():
    """Delete key by number"""
    keys = load_keys()
    if not keys:
        print("\n  No keys.")
        return

    menu_6_list()
    n = input("\n  Nomor key yang mau dihapus: ").strip()
    if not n.isdigit() or int(n) < 1 or int(n) > len(keys):
        print("  Invalid")
        return
    removed = keys.pop(int(n) - 1)
    save_keys(keys)
    print(f"  Deleted: {removed.get('email','?')} ({removed.get('key','?')[:20]}...)")


def menu_9_reverify():
    """Re-verify all keys (test one by one with fresh call)"""
    keys = load_keys()
    if not keys:
        print("\n  No keys.")
        return

    print(f"\n  [9] Re-verify {len(keys)} Keys")
    print("  " + "-" * 40)

    active, dead = 0, 0
    for i, e in enumerate(keys, 1):
        k = e.get("key")
        if not k:
            dead += 1
            continue
        print(f"  {i:>2}. {e.get('email','?')[:30]:<31}", end="", flush=True)
        r = subprocess.run(["curl", "-s", "-X", "POST", f"{BASE}/v1/chat/completions",
            "-H", f"Authorization: Bearer {k}", "-H", "Content-Type: application/json",
            "-d", json.dumps({"model": "Atria-Dawn-Preview",
                "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}),
            "--max-time", "90"], capture_output=True, text=True)
        try:
            d = json.loads(r.stdout)
            if "choices" in d:
                print("ACTIVE")
                e["status"] = "active"
                active += 1
            elif "limit" in str(d).lower() or "quota" in str(d).lower():
                print("LIMITED (but valid)")
                e["status"] = "limited"
                active += 1
            else:
                err = d.get("error", {}).get("type", "")
                print(f"DEAD: {err[:25]}")
                e["status"] = "dead"
                dead += 1
        except Exception:
            print("DEAD")
            e["status"] = "dead"
            dead += 1

    save_keys(keys)
    print(f"\n  Result: {active} active | {dead} dead")


def menu_m_mailtest():
    """Cek provider temp mail mana yang lagi hidup"""
    print("\n  [M] Test Provider Temp Mail")
    print("  " + "-" * 40)
    for name in PROVIDER_ORDER:
        print(f"  {name:<12}", end="", flush=True)
        try:
            box = PROVIDERS[name]()
            addr = box.create()
            if not addr:
                print("DOWN")
                continue
            box.fetch()
            print(f"OK   -> {addr}")
        except Exception as ex:
            print(f"DOWN ({str(ex)[:35]})")


def _prompt(text=""):
    """input() yang membuang BOM di awal baris.

    Kalau input dialirkan lewat pipe (mis. PowerShell), byte BOM UTF-8 ikut
    terbawa. Tergantung encoding stdin, BOM terbaca sebagai '\ufeff' atau
    'ï»¿', dan tanpa dibersihkan pilihan menu yang valid akan ditolak.
    """
    s = input(text)
    for bom in ("\ufeff", "ï»¿"):
        if s.startswith(bom):
            s = s[len(bom):]
    return s.strip()


def menu_t_targets():
    """Atur daftar 9Router tujuan inject (bisa banyak sekaligus)."""
    while True:
        cfg = nr.load_config()
        manual = cfg.get("targets", [])
        env = nr._env_paths()

        print("\n  [T] Target 9Router")
        print("  " + "-" * 62)
        print(f"  Config : {nr.TARGETS_FILE.name}"
              f"{'' if nr.TARGETS_FILE.is_file() else '  (belum dibuat)'}")
        print(f"  Auto-discover : {'ON' if cfg.get('auto_discover', True) else 'OFF'}")
        if env:
            print(f"  ENV OVERRIDE  : {len(env)} path "
                  "(ATRIA_9ROUTER_DB aktif, daftar manual diabaikan)")
        print("  " + "-" * 62)

        if manual:
            print("  Target manual:")
            npr = nr._lazy_ninepanel()
            for i, t in enumerate(manual, 1):
                ttype = t.get("type", "local")
                flag = "ON " if t.get("enabled", True) else "OFF"
                if ttype == "remote":
                    if npr is not None and npr.is_remote_target(t):
                        ok, info = nr.remote_status(t)
                    else:
                        ok, info = False, "ninepanel n/a"
                    health = "ok" if ok else info
                    loc = npr._norm_base(t.get("url", "")) if npr else t.get("url", "")
                    btag = "|basic" if t.get("basic_user") else ""
                    print(f"   {i}. [{flag}|remote{btag}] {t.get('name','?')[:16]:<17} {health}")
                    print(f"        {loc}")
                else:
                    ok, info = nr.db_status(t.get("db", ""))
                    health = "ok" if ok else info
                    print(f"   {i}. [{flag}|local]  {t.get('name','?')[:16]:<17} {health}")
                    print(f"        {t.get('db','')}")
        else:
            print("  Target manual: (kosong)")

        aktif = nr.resolve(include_broken=True)
        print("  " + "-" * 62)
        print(f"  Dipakai saat inject ({sum(1 for t in aktif if t['ok'])} sehat):")
        for t in aktif:
            print("   " + nr.describe(t))
        if not aktif:
            print("   (tidak ada!)")

        print("\n   A. Tambah target lokal   R. Tambah target remote (URL+password)")
        print("   H. Hapus target          E. Enable/disable")
        print("   D. Auto-discover ON/OFF  S. Scan komputer")
        print("   P. Simpan hasil scan jadi permanen")
        print("   0. Kembali")
        pilih = _prompt("\n  Pilih: ").lower()

        if pilih in ("0", ""):
            return

        if pilih == "a":
            path = _prompt("  Path data.sqlite / folder 9router: ")
            if not path:
                print("  Dibatalkan.")
                continue
            nama = _prompt("  Nama (opsional): ") or None
            ok, msg = nr.add_target(path, nama)
            print(f"  {'OK' if ok else 'GAGAL'}: {msg}")

        elif pilih == "r":
            url = _prompt("  URL panel 9Router (https://...): ").strip()
            if not url:
                print("  Dibatalkan.")
                continue
            print("  Panel pakai popup username/password (Basic Auth)?")
            print("  - isi username (mis. 'admin') kalau YA")
            print("  - kosongkan kalau TIDAK (login 9Router biasa via password)")
            bu = _prompt("  Basic Auth username (opsional): ").strip() or None
            bp = ""
            pw = ""
            if bu:
                bp = _prompt("  Basic Auth password: ").strip()
                if not bp:
                    print("  Password Basic Auth kosong, dibatalkan.")
                    continue
                pw = _prompt("  Password panel 9Router (opsional, biarkan kosong"
                             " kalau Basic Auth saja): ").strip()
            else:
                pw = _prompt("  Password panel: ").strip()
                if not pw:
                    print("  Password kosong, dibatalkan.")
                    continue
            nama = _prompt("  Nama (opsional): ") or None
            ok, msg = nr.add_remote_target(url, pw, nama,
                                            basic_user=bu, basic_pass=bp)
            print(f"  {'OK' if ok else 'GAGAL'}: {msg}")

        elif pilih == "h":
            if not manual:
                print("  Daftar manual kosong.")
                continue
            n = _prompt("  Nomor yang dihapus: ")
            if n.isdigit():
                ok, msg = nr.remove_target(int(n) - 1)
                print(f"  {'Dihapus: ' + str(msg) if ok else 'GAGAL: ' + str(msg)}")

        elif pilih == "e":
            if not manual:
                print("  Daftar manual kosong.")
                continue
            n = _prompt("  Nomor yang di-toggle: ")
            if n.isdigit():
                ok, msg = nr.toggle_target(int(n) - 1)
                print(f"  {'Sekarang: ' + str(msg) if ok else 'GAGAL: ' + str(msg)}")

        elif pilih == "d":
            baru = nr.set_auto_discover(not cfg.get("auto_discover", True))
            print(f"  Auto-discover sekarang: {'ON' if baru else 'OFF'}")

        elif pilih == "s":
            print("\n  Mencari DB 9Router...")
            hits = nr.discover(include_broken=True)
            for h in hits:
                print(f"   {'OK ' if h['ok'] else 'BAD'}  {h['db']}")
                if not h["ok"]:
                    print(f"         -> {h['info']}")
            if not hits:
                print("   (tidak ada yang ketemu)")

        elif pilih == "p":
            n = nr.adopt_discovered()
            print(f"  {n} target hasil scan disimpan permanen.")

        else:
            print("  Pilihan tidak dikenal.")


def menu_c_captcha():
    """Test solver captcha langsung di form register Atria."""
    use_cf = HAS_CAMOUFOX and BROWSER_PREF in ("auto", "camoufox", "firefox")
    browser = ("camoufox" if use_cf else "chromium") + ("" if HEADLESS else " (headful)")
    service = ts.service_name() or "-  (set CAPSOLVER_API_KEY / TWOCAPTCHA_API_KEY)"

    print("\n  [C] Test Solver Captcha (reCAPTCHA Enterprise)")
    print("  " + "-" * 40)
    print(f"  Browser  : {browser}")
    print(f"  Strategi : {', '.join(ts._env_modes())}")
    print(f"  Service  : {service}")
    print(f"  Wait     : {CAPTCHA_WAIT}s per strategi"
          f"{f', manual {ts.MANUAL_WAIT}s' if MANUAL_CAPTCHA else ''}")
    print("  " + "-" * 40)
    if MANUAL_CAPTCHA:
        print("  Mode manual aktif: siapkan diri untuk mencentang captcha")
        print("  di jendela browser yang akan terbuka.")
    ok = asyncio.run(ts._self_test(ts.ATRIA_RECAPTCHA_SITEKEY,
                                   f"{AUTH}/register", HEADLESS))
    if ok:
        print("\n  Solver siap -> menu 1 / 2 / batch bisa dipakai lagi.")
    else:
        print("\n  Tips kalau gagal:")
        print("    - pastikan ATRIA_MANUAL=1 (default) dan kamu mencentang")
        print("      captcha sebelum batas waktu habis")
        print("    - naikkan batas: set ATRIA_MANUAL_WAIT=300")
        print("    - pasang camoufox: pip install camoufox[geoip]")


# ─── Main Menu ─────────────────────────────────────────────────────
def main():
    print(BANNER)
    stats = provider_stats()
    total = sum(stats.values())
    detail = ", ".join(f"{v} {p}" for p, v in sorted(stats.items())) or "kosong"
    print(f"  Keys: {total} total ({detail})")
    _mail = [p for p in PROVIDER_ORDER
             if p not in BLOCKED_PROVIDERS or ALLOW_BLOCKED]
    print(f"  Mail: {' -> '.join(_mail)} "
          f"({'/'.join(GUERRILLA_DOMAINS)}, lolos blocklist Atria)")
    _cf = HAS_CAMOUFOX and BROWSER_PREF in ("auto", "camoufox", "firefox")
    if MANUAL_CAPTCHA:
        print(f"  Captcha: reCAPTCHA Enterprise - MODE MANUAL "
              f"({'camoufox' if _cf else 'chromium'} headful, "
              f"kamu centang sendiri)")
    else:
        print(f"  Captcha: solver aktif ({'camoufox' if _cf else 'chromium'}"
              f"{'' if HEADLESS else ' headful'}"
              f"{', ' + ts.service_name() if ts.service_available() else ''})")
    _tg = list_9router_targets()
    if _tg:
        print(f"  9Router: {len(_tg)} target aktif"
              f" ({', '.join(t['name'][:14] for t in _tg[:3])}"
              f"{', ...' if len(_tg) > 3 else ''})")
    else:
        print("  9Router: belum ada target (menu T untuk atur)")
    print()
    print("  1. Buat 1 akun (temp email)")
    print("  2. Batch N akun (temp email)")
    print("  3. Test semua key")
    print("  4. Cek sisa token")
    print("  5. Scan semua model")
    print("  6. List semua key")
    print("  7. Export 9Router JSON")
    print("  8. Hapus key")
    print("  9. Re-verify semua key")
    print("  S. Sync semua key ke 9Router")
    print("  T. Atur target 9Router (multi-instance)")
    print("  A. Tambah key manual (register sendiri)")
    print("  G. Buat 1 akun (GSuite / Google OAuth)")
    print("  B. Batch N akun (GSuite / Google OAuth)")
    print("  M. Test provider temp mail")
    print("  C. Test solver Cloudflare Turnstile")
    print("  0. Keluar")
    print()

    choice = _prompt("  Pilih [0-9/S/T/A/G/B/M/C]: ").lower()

    actions = {
        "1": menu_1_create, "2": menu_2_batch, "3": menu_3_test,
        "4": menu_4_check, "5": menu_5_scan, "6": menu_6_list,
        "7": menu_7_export, "8": menu_8_delete, "9": menu_9_reverify,
        "s": menu_s_sync, "a": menu_a_add, "m": menu_m_mailtest,
        "c": menu_c_captcha, "t": menu_t_targets,
        "g": menu_g_create, "b": menu_g_batch,
    }
    if choice == "0":
        print("  Bye!")
    elif choice in actions:
        actions[choice]()
    else:
        print("  Invalid choice")


def run_batch(n, provider=None):
    """Batch non-interaktif (dipakai CLI)."""
    existing = load_keys()
    done = {e.get("email") for e in existing}
    for idx in range(1, n + 1):
        if idx > 1:
            time.sleep(DELAY_FAST)
        try:
            result = asyncio.run(register_and_create_key(idx, n, provider))
        except KeyboardInterrupt:
            save_keys(existing)
            ok = sum(1 for e in existing if e.get("key"))
            print(f"\n\n  Dihentikan di akun {idx}/{n}. Tersimpan {ok} key.")
            return
        if result and result.get("email") not in done:
            existing.append(result)
            done.add(result["email"])
            save_keys(existing)
            # Auto-inject key baru ke semua 9Router (lokal + remote).
            auto_inject_9router(result)
    ok = sum(1 for e in existing if e.get("key"))
    print(f"\nDONE: {ok} keys")


def usage():
    print(BANNER)
    print("  Usage:")
    print("    python atria_register.py                # interactive menu")
    print("    python atria_register.py 1              # buat 1 akun")
    print("    python atria_register.py batch N        # batch N akun")
    print("    python atria_register.py batch N worker # paksa 1 provider")
    print("    python atria_register.py gsuite         # 1 akun via Google OAuth")
    print("    python atria_register.py gbatch N       # batch N via Google OAuth")
    print("    python atria_register.py test           # test all keys")
    print("    python atria_register.py list           # list all keys")
    print("    python atria_register.py cek            # cek token")
    print("    python atria_register.py scan           # scan models")
    print("    python atria_register.py export         # export 9Router")
    print("    python atria_register.py sync           # inject semua key ke 9Router")
    print("    python atria_register.py targets        # atur target 9Router")
    print("    python atria_register.py add            # tambah key manual + sync")
    print("    python atria_register.py mail           # test provider temp mail")
    print("    python atria_register.py captcha        # test solver Turnstile")
    print()
    print(f"  Provider temp mail: {', '.join(PROVIDERS)}")
    print(f"  Captcha solver    : {', '.join(ts._env_modes())}")
    print("  Env captcha       : ATRIA_CAPTCHA_MODE, ATRIA_CAPTCHA_WAIT,")
    print("                      ATRIA_CAPTCHA_RETRY, ATRIA_BROWSER,")
    print("                      ATRIA_HEADLESS, CAPSOLVER_API_KEY,")
    print("                      TWOCAPTCHA_API_KEY, CAPMONSTER_API_KEY")
    print("  Env 9Router       : ATRIA_9ROUTER_DB (pisah ';' untuk multi-DB),")
    print("                      ATRIA_NO_9ROUTER=1 untuk matikan auto-inject")
    _t = list_9router_targets()
    print(f"  Target 9Router    : {len(_t)} aktif")
    for _x in _t:
        print(f"     [{_x['source']}] {_x['db']}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        cli = {"1": menu_1_create, "test": menu_3_test, "list": menu_6_list,
               "export": menu_7_export, "cek": menu_4_check,
               "scan": menu_5_scan, "mail": menu_m_mailtest,
               "sync": menu_s_sync, "add": menu_a_add,
               "captcha": menu_c_captcha, "turnstile": menu_c_captcha,
               "targets": menu_t_targets, "target": menu_t_targets,
               "gsuite": menu_g_create}
        if arg == "batch" and len(sys.argv) > 2 and sys.argv[2].isdigit():
            run_batch(int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None)
        elif arg == "gbatch" and len(sys.argv) > 2 and sys.argv[2].isdigit():
            run_gbatch(int(sys.argv[2]))
        elif arg in cli:
            cli[arg]()
        else:
            usage()
    else:
        main()



