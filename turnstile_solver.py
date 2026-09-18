#!/usr/bin/env python3
"""
Cloudflare Turnstile solver untuk Atria Bot.

Empat strategi, dicoba berurutan sampai ada yang menghasilkan token:

  1. inline      - widget yang sudah ada di halaman dibiarkan auto-solve
                   (browser anti-fingerprint seperti Camoufox biasanya lolos)
  2. click       - klik checkbox widget (mode "managed"/"non-interactive")
  3. standalone  - render ulang widget di tab same-origin lalu tokennya
                   disuntik balik ke form asli
  4. service     - lempar ke 2Captcha / CapSolver / CapMonster (butuh API key)

Dipakai oleh atria_register.py, tapi bisa dites sendiri:

    python turnstile_solver.py                 # self-test sitekey Atria
    python turnstile_solver.py <sitekey> <url> # sitekey lain
"""
import asyncio
import json as _json
import os
import time

import requests as req


# ─── Peredam noise asyncio Windows ─────────────────────────────────
def _silence_proactor_noise():
    """Redam "ValueError: I/O operation on closed pipe" saat program selesai.

    Di Windows + Python 3.12+, transport subprocess milik browser kadang baru
    di-garbage-collect SETELAH event loop ditutup. __del__-nya lalu mencoba
    membangun pesan ResourceWarning lewat repr(), repr() memanggil
    socket.fileno() pada pipe yang sudah mati, dan ValueError-nya tercetak
    sebagai "Exception ignored in: ...".

    Murni kosmetik: terjadi setelah semua pekerjaan selesai dan tidak
    memengaruhi hasil. Tapi bikin panik kalau dibaca, jadi kita bungkam.
    Filter warnings biasa tidak mempan karena repr() dievaluasi lebih dulu,
    sebelum warning sempat difilter.
    """
    if os.name != "nt":
        return
    try:
        from asyncio.base_subprocess import BaseSubprocessTransport
        from asyncio.proactor_events import _ProactorBasePipeTransport
    except ImportError:
        return

    for cls in (_ProactorBasePipeTransport, BaseSubprocessTransport):
        original = getattr(cls, "__del__", None)
        if original is None or getattr(original, "_atria_patched", False):
            continue

        def _safe_del(self, _orig=original):
            try:
                _orig(self)
            except (ValueError, AttributeError, OSError, RuntimeError):
                pass

        _safe_del._atria_patched = True
        cls.__del__ = _safe_del


_silence_proactor_noise()

# Sitekey Turnstile di form register Atria (auth.atria-asi.ai/register).
ATRIA_SITEKEY = os.environ.get(
    "ATRIA_TURNSTILE_SITEKEY", "0x4AAAAAAE1sToF2hSKk4DHo")
ATRIA_REGISTER_URL = "https://auth.atria-asi.ai/register"

# Sejak 2026 Atria pindah dari Cloudflare Turnstile ke reCAPTCHA Enterprise
# (CSP halaman register memuat www.google.com/recaptcha/enterprise.js dan
# window.logtoSsr.captchaConfig melaporkan type="RecaptchaEnterprise").
# Sitekey ini hanya cadangan; nilai sebenarnya dibaca dari captchaConfig.
ATRIA_RECAPTCHA_SITEKEY = os.environ.get(
    "ATRIA_RECAPTCHA_SITEKEY", "6LcR9rwtAAAAAP6JAuM8TRkRbcQ5jpQ_IyK9qRNd")

# Strategi yang diaktifkan. Bisa dipersempit lewat env, contoh:
#   ATRIA_CAPTCHA_MODE=inline,click
#
# "manual" = mode semi-otomatis gratis: browser dibuka berjendela, user
# mencentang "I'm not a robot" sendiri, bot lanjut begitu token muncul.
# Ditaruh paling depan untuk reCAPTCHA karena tanpa solver berbayar tidak
# ada cara lain memicu callback Logto.
DEFAULT_MODES = ("inline", "click", "standalone", "service")
ALL_MODES = ("manual", "inline", "click", "standalone", "service")


def _env_modes():
    raw = (os.environ.get("ATRIA_CAPTCHA_MODE") or "").strip().lower()
    if not raw or raw == "auto":
        return DEFAULT_MODES
    picked = tuple(m.strip() for m in raw.split(",") if m.strip() in ALL_MODES)
    return picked or DEFAULT_MODES


def _env_flag(name, default=False):
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


CAPTCHA_WAIT = _env_int("ATRIA_CAPTCHA_WAIT", 40)           # detik per strategi
SERVICE_WAIT = _env_int("ATRIA_CAPTCHA_SERVICE_WAIT", 180)  # detik untuk service

# Panjang minimum token yang dianggap sah. Token produksi ratusan karakter,
# tapi sitekey testing Cloudflare mengeluarkan "XXXX.DUMMY.TOKEN.XXXX"
# (21 char) — ambangnya dibuat 20 supaya self-test tetap bisa jalan.
MIN_TOKEN_LEN = 20


# ─── Pembacaan token dari DOM ──────────────────────────────────────
# Turnstile menaruh token di hidden input; kalau halaman pakai render
# eksplisit, token hanya bisa diambil lewat turnstile.getResponse().
_TOKEN_SELECTORS = (
    'input[name="cf-turnstile-response"]',
    'input[name="g-recaptcha-response"]',
    'input[id^="cf-chl-widget"]',
)

_JS_READ_TOKEN = """(minLen) => {
    const pick = (v) => (typeof v === 'string' && v.length >= minLen) ? v : null;
    const sels = ['input[name="cf-turnstile-response"]',
                  'input[name="g-recaptcha-response"]',
                  'input[id^="cf-chl-widget"]'];
    for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
            const t = pick(el.value);
            if (t) return t;
        }
    }
    try {
        if (window.turnstile && window.turnstile.getResponse) {
            const t = pick(window.turnstile.getResponse());
            if (t) return t;
        }
    } catch (e) { /* widget belum dirender */ }
    return null;
}"""

# "none"    -> form ini memang tidak pakai captcha
# "pending" -> widget ada, token belum terisi
# "solved"  -> token sudah ada
#
# CATATAN: selector widget di sini sengaja TIDAK memakai [class*=captchaBox]
# atau input[name="cf-turnstile-response"] sebagai bukti "solved". Dulu
# inject_token() membuat hidden input palsu bernama cf-turnstile-response,
# lalu state ini membacanya balik dan melapor "sudah ter-solve otomatis"
# padahal form belum punya token reCAPTCHA yang sah (false positive yang
# membuat register mandek tanpa pesan error).
_JS_STATE = """(minLen) => {
    const pick = (v) => (typeof v === 'string' && v.length >= minLen) ? v : null;
    let token = null;
    for (const el of document.querySelectorAll(
            'input[name="cf-turnstile-response"], input[id^="cf-chl-widget"]')) {
        token = token || pick(el.value);
    }
    try {
        if (!token && window.turnstile && window.turnstile.getResponse) {
            token = pick(window.turnstile.getResponse());
        }
    } catch (e) { /* ignore */ }
    if (token) return 'solved';
    const widget = document.querySelector(
        '.cf-turnstile, [data-sitekey], ' +
        'iframe[src*="challenges.cloudflare.com"], ' +
        'iframe[src*="recaptcha"], .g-recaptcha, [class*=captchaBox], ' +
        'input[name="cf-turnstile-response"]');
    return widget ? 'pending' : 'none';
}"""


async def read_token(page):
    """Ambil token Turnstile dari halaman, atau None kalau belum ada."""
    try:
        return await page.evaluate(_JS_READ_TOKEN, MIN_TOKEN_LEN)
    except Exception:
        return None


async def widget_state(page):
    """Return "none" | "pending" | "solved"."""
    try:
        return await page.evaluate(_JS_STATE, MIN_TOKEN_LEN)
    except Exception:
        return "none"


async def find_sitekey(page, fallback=ATRIA_SITEKEY):
    """Baca sitekey dari DOM supaya tetap jalan kalau Atria menggantinya."""
    try:
        found = await page.evaluate("""() => {
            const el = document.querySelector('[data-sitekey]');
            if (el) return el.getAttribute('data-sitekey');
            const fr = document.querySelector(
                'iframe[src*="challenges.cloudflare.com"]');
            if (fr) {
                const m = fr.getAttribute('src').match(/[?&]k=([^&]+)/);
                if (m) return decodeURIComponent(m[1]);
            }
            const m2 = document.documentElement.innerHTML.match(
                /0x4[A-Za-z0-9_-]{20,}/);
            return m2 ? m2[0] : null;
        }""")
        if found:
            return found
    except Exception:
        pass
    return fallback


# ─── reCAPTCHA Enterprise (captcha Atria yang aktif sekarang) ──────
# Camoufox menjalankan page.evaluate() di isolated world sehingga window.*
# milik halaman (logtoSsr, grecaptcha, ___grecaptcha_cfg) tidak terlihat.
# Solusinya: eksekusi script di main world lewat add_script_tag, lalu titipkan
# hasilnya di window.name — satu-satunya kanal yang ikut terbaca dari
# isolated world.
_JS_BRIDGE_CONFIG = """(() => {
    const out = { type: null, siteKey: null, mode: null, token: null };
    // Logto menaruh konfigurasinya di logtoSsr.signInExperience.data.
    // captchaConfig (sudah diverifikasi di auth.atria-asi.ai), tapi jalurnya
    // pernah berubah antar versi — jadi ditelusuri rekursif saja.
    const findCfg = (obj, depth, seen) => {
        if (!obj || typeof obj !== 'object' || depth > 6) return null;
        if (seen.has(obj)) return null;
        seen.add(obj);
        if (obj.captchaConfig && typeof obj.captchaConfig === 'object') {
            return obj.captchaConfig;
        }
        for (const k of Object.keys(obj)) {
            const hit = findCfg(obj[k], depth + 1, seen);
            if (hit) return hit;
        }
        return null;
    };
    try {
        const cfg = findCfg(window.logtoSsr, 0, new Set());
        if (cfg) {
            out.type = cfg.type || null;
            out.siteKey = cfg.siteKey || cfg.sitekey || null;
            out.mode = cfg.mode || null;
        }
    } catch (e) { /* SSR payload belum ada */ }
    if (!out.siteKey) {
        try {
            const el = document.querySelector('.g-recaptcha[data-sitekey], [data-sitekey]');
            if (el) out.siteKey = el.getAttribute('data-sitekey');
        } catch (e) { /* ignore */ }
    }
    if (!out.siteKey) {
        try {
            const fr = document.querySelector('iframe[src*="recaptcha"]');
            const m = fr && fr.getAttribute('src').match(/[?&]k=([^&]+)/);
            if (m) { out.siteKey = decodeURIComponent(m[1]); out.type = out.type || 'Recaptcha'; }
        } catch (e) { /* ignore */ }
    }
    // Skrip enterprise.js sudah dimuat = halaman ini memakai reCAPTCHA,
    // meski payload SSR tidak terbaca (mis. setelah client-side navigation).
    if (!out.type) {
        try {
            const s = document.querySelector('script[src*="recaptcha/enterprise.js"]');
            if (s) out.type = 'RecaptchaEnterprise';
            else if (document.querySelector('script[src*="recaptcha"]')) out.type = 'Recaptcha';
        } catch (e) { /* ignore */ }
    }
    try {
        const ta = document.querySelector('textarea[name="g-recaptcha-response"], #g-recaptcha-response');
        if (ta && ta.value) out.token = ta.value;
    } catch (e) { /* ignore */ }
    if (!out.token) {
        try {
            const g = window.grecaptcha;
            const api = (g && g.enterprise) ? g.enterprise : g;
            if (api && api.getResponse) {
                const t = api.getResponse();
                if (t) out.token = t;
            }
        } catch (e) { /* widget belum dirender */ }
    }
    try { window.name = 'ATRIA_CAPTCHA:' + JSON.stringify(out); } catch (e) { /* ignore */ }
})();"""


async def _bridge_read(page):
    """Jalankan probe di main world, ambil hasilnya lewat window.name."""
    try:
        prev = await page.evaluate("() => window.name") or ""
    except Exception:
        prev = ""
    try:
        await page.add_script_tag(content=_JS_BRIDGE_CONFIG)
    except Exception:
        return {}
    try:
        raw = await page.evaluate("() => window.name") or ""
    except Exception:
        return {}
    # Pulihkan window.name supaya tidak mengganggu skrip halaman.
    try:
        await page.add_script_tag(
            content="window.name = %s;" % _json.dumps(
                prev if not str(prev).startswith("ATRIA_CAPTCHA:") else ""))
    except Exception:
        pass
    if not str(raw).startswith("ATRIA_CAPTCHA:"):
        return {}
    try:
        return _json.loads(str(raw)[len("ATRIA_CAPTCHA:"):]) or {}
    except ValueError:
        return {}


async def recaptcha_config(page):
    """Baca konfigurasi captcha Logto: {"type","siteKey","mode"}.

    type contoh: "RecaptchaEnterprise" (Atria sekarang), "Turnstile" (dulu).
    Dict kosong kalau halaman tidak memasang captcha sama sekali.
    """
    data = await _bridge_read(page)
    return {k: data.get(k) for k in ("type", "siteKey", "mode")} if data else {}


async def recaptcha_token(page):
    """Token reCAPTCHA yang sah, atau None.

    Hanya percaya textarea g-recaptcha-response milik Google dan
    grecaptcha.enterprise.getResponse() — bukan hidden input buatan sendiri.
    """
    data = await _bridge_read(page)
    token = (data or {}).get("token")
    if isinstance(token, str) and len(token) >= MIN_TOKEN_LEN:
        return token
    return None


def is_recaptcha(config):
    """True kalau captchaConfig menunjuk ke reCAPTCHA (v2 / Enterprise)."""
    return "recaptcha" in str((config or {}).get("type") or "").lower()


_JS_INJECT = """(token) => {
    let hit = false;
    const desc = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value');
    const setter = desc && desc.set;
    const fire = (el) => {
        if (setter) { setter.call(el, token); } else { el.value = token; }
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        hit = true;
    };
    const sels = ['input[name="cf-turnstile-response"]',
                  'input[name="g-recaptcha-response"]',
                  'input[id^="cf-chl-widget"]'];
    for (const sel of sels) {
        document.querySelectorAll(sel).forEach(fire);
    }
    if (!hit) {
        // Form belum punya hidden input: bikin sendiri di <form> pertama,
        // atau di <body> kalau form pun belum dirender.
        const host = document.querySelector('form') || document.body;
        if (host) {
            const inp = document.createElement('input');
            inp.type = 'hidden';
            inp.name = 'cf-turnstile-response';
            inp.value = token;
            host.appendChild(inp);
            hit = true;
        }
    }
    // Sebagian front-end membaca dari callback global, bukan dari input.
    window.turnstileToken = token;
    window.__cf_turnstile_token = token;
    try {
        if (window.turnstile && window.turnstile.getResponse) {
            window.turnstile.getResponse = () => token;
        }
    } catch (e) { /* ignore */ }
    try {
        if (typeof window.onTurnstileSuccess === 'function') {
            window.onTurnstileSuccess(token);
        }
    } catch (e) { /* ignore */ }
    return hit;
}"""


async def inject_token(page, token):
    """Suntik token ke form asli + picu event supaya framework (React/Vue) sadar.

    Camoufox menjalankan page.evaluate() di isolated world, jadi kalau hasilnya
    nol kita ulangi lewat <script> tag yang dieksekusi di main world halaman.
    """
    if not token:
        return False

    hit = False
    try:
        hit = bool(await page.evaluate(_JS_INJECT, token))
    except Exception:
        hit = False

    # Verifikasi: kalau token belum benar-benar terbaca balik, pakai script tag.
    if (await read_token(page)) == token:
        return True

    try:
        safe = token.replace("\\", "\\\\").replace('"', '\\"')
        await page.add_script_tag(content=f'({_JS_INJECT})("{safe}");')
        return (await read_token(page)) == token or hit
    except Exception:
        return hit


# ─── Strategi 0: manual (gratis, user yang mencentang) ─────────────
MANUAL_WAIT = _env_int("ATRIA_MANUAL_WAIT", 180)   # detik menunggu user


async def solve_manual(page, timeout=MANUAL_WAIT, log=None, config=None):
    """Tunggu USER mencentang "I'm not a robot" di jendela browser.

    Ini jalur gratis untuk reCAPTCHA Enterprise: tidak ada token yang bisa
    dipalsukan, dan callback Logto (executeCaptcha) hanya berjalan kalau
    Google sendiri yang memanggilnya. Jadi bot berhenti di sini, user
    mencentang sekali, lalu bot melanjutkan otomatis begitu
    grecaptcha.getResponse() mengembalikan token.

    Wajib headful (ATRIA_HEADLESS=0) — di mode headless tidak ada yang bisa
    diklik dan fungsi ini pasti timeout.
    """
    log = log or (lambda _m: None)
    cfg = config if config is not None else await recaptcha_config(page)
    kind = (cfg or {}).get("type") or "captcha"

    # Widget baru dirender setelah tombol submit diklik (Logto memanggil
    # executeCaptcha() di situ), jadi tunggu iframe-nya benar-benar ada.
    for _ in range(20):
        try:
            if await page.locator('iframe[src*="recaptcha"]').count():
                break
            # Halaman sudah lanjut sendiri -> tidak ada captcha untuk diklik.
            if "verification" in (page.url or ""):
                log("captcha: halaman sudah lanjut tanpa challenge")
                return ""
        except Exception:
            pass
        await asyncio.sleep(1)

    try:
        await page.bring_to_front()
    except Exception:
        pass

    # Gulirkan widget ke tengah layar supaya user langsung melihatnya.
    try:
        loc = page.locator('iframe[src*="recaptcha"], .g-recaptcha, '
                           '[class*=captchaBox]').first
        if await loc.count():
            await loc.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass

    # Centang checkbox-nya otomatis lebih dulu. Kalau reputasi IP bagus,
    # Google langsung memberi token tanpa image challenge dan user tidak
    # perlu melakukan apa pun. Kalau muncul challenge gambar, setidaknya
    # user tinggal menyelesaikan gambarnya saja.
    if _env_flag("ATRIA_MANUAL_AUTOCLICK", True):
        try:
            anchor = page.frame_locator(
                'iframe[src*="recaptcha/enterprise/anchor"], '
                'iframe[src*="recaptcha/api2/anchor"], '
                'iframe[title="reCAPTCHA"]').locator("#recaptcha-anchor")
            await anchor.click(timeout=8000)
            log("captcha: checkbox diklik otomatis")
            # Beri kesempatan token muncul tanpa campur tangan user.
            for _ in range(6):
                await asyncio.sleep(1.5)
                tok = await recaptcha_token(page)
                if tok:
                    log(f"captcha: lolos tanpa challenge ({len(tok)} char)")
                    return tok
                if "verification" in (page.url or ""):
                    log("captcha: lolos tanpa challenge (halaman lanjut)")
                    return "verified"
        except Exception:
            pass

    print("\n" + "=" * 62, flush=True)
    print("  GILIRAN KAMU: selesaikan captcha di jendela browser", flush=True)
    print("  (checkbox sudah diklik otomatis; kalau muncul challenge", flush=True)
    print("   gambar, pilih gambarnya lalu tekan Verify)", flush=True)
    print(f"  captcha : {kind}", flush=True)
    print(f"  batas   : {timeout} detik (bot lanjut OTOMATIS setelah selesai)", flush=True)
    print("=" * 62 + "\n", flush=True)

    deadline = time.time() + timeout
    last_note = 0.0
    while time.time() < deadline:
        token = await recaptcha_token(page)
        if token:
            log(f"captcha: manual OK ({len(token)} char)")
            return token
        # Logto kadang langsung meneruskan form begitu callback jalan,
        # sehingga token sudah tidak terbaca lagi di halaman baru.
        try:
            if "verification" in (page.url or ""):
                log("captcha: manual OK (halaman lanjut ke verifikasi)")
                return "verified"
        except Exception:
            pass
        # Turnstile lama tetap didukung lewat jalur yang sama.
        token = await read_token(page)
        if token:
            log(f"captcha: manual OK ({len(token)} char, turnstile)")
            return token

        left = int(deadline - time.time())
        if time.time() - last_note > 15:
            last_note = time.time()
            print(f"  menunggu centang... sisa {left}s", flush=True)
        await asyncio.sleep(1.5)

    log("captcha: manual timeout (tidak ada yang mencentang)")
    return None


# ─── Strategi 1: inline (tunggu widget auto-solve) ─────────────────
async def solve_inline(page, timeout=CAPTCHA_WAIT, log=None):
    """Widget sudah ada di halaman; tunggu Cloudflare mengisi tokennya sendiri."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = await read_token(page)
        if token:
            if log:
                log(f"turnstile: inline OK ({len(token)} char)")
            return token
        if await widget_state(page) == "none":
            return None
        await asyncio.sleep(1)
    return None


# ─── Strategi 2: klik checkbox widget ──────────────────────────────
async def solve_click(page, timeout=CAPTCHA_WAIT, log=None):
    """Klik checkbox di dalam iframe Turnstile.

    Isi iframe cross-origin + shadow DOM, jadi elemennya tidak bisa di-locate
    langsung. Yang dilakukan: cari bounding box widget lalu klik kira-kira di
    posisi checkbox (kiri, tengah vertikal), kemudian tunggu token muncul.
    """
    deadline = time.time() + timeout
    clicked_at = 0.0
    while time.time() < deadline:
        token = await read_token(page)
        if token:
            if log:
                log(f"turnstile: click OK ({len(token)} char)")
            return token

        # Jangan spam klik; beri jeda 5 detik antar percobaan.
        if time.time() - clicked_at > 5:
            clicked_at = time.time()
            for sel in ('iframe[src*="challenges.cloudflare.com"]',
                        ".cf-turnstile", "[class*=captchaBox]", "[data-sitekey]"):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() == 0:
                        continue
                    box = await loc.bounding_box()
                    if not box or box["width"] < 10:
                        continue
                    await page.mouse.click(
                        box["x"] + 30, box["y"] + box["height"] / 2, delay=60)
                    break
                except Exception:
                    continue
        await asyncio.sleep(1)
    return None


# ─── Strategi 3: render ulang widget di tab same-origin ────────────
# Token dibaca dari hidden input yang dibuat Turnstile sendiri, bukan dari
# variabel global: Camoufox menjalankan page.evaluate() di isolated world
# sehingga window.* halaman tidak terlihat dari Python.
_SOLVER_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
<style>body{{margin:0;height:100vh;display:flex;justify-content:center;\
align-items:center;background:#111}}</style>
</head><body>
<div class="cf-turnstile" data-sitekey="{sitekey}" data-callback="cb"{extra}></div>
<input type="hidden" id="atria-token" value="">
<script>function cb(t){{document.getElementById('atria-token').value=t;}}</script>
</body></html>"""


async def _new_page(target):
    """Buka tab baru dari Browser maupun BrowserContext.

    Camoufox.start() mengembalikan Browser, sedangkan Playwright biasa
    memberi BrowserContext. Default-context milik Browser sendiri menolak
    .new_page(), jadi di situ kita naik satu level ke Browser-nya.
    """
    try:
        return await target.new_page()
    except Exception:
        browser = getattr(target, "browser", None)
        if browser is not None:
            return await browser.new_page()
        raise


async def solve_standalone(context, sitekey, url, timeout=CAPTCHA_WAIT,
                           action=None, cdata=None, log=None):
    """Buka tab baru di origin yang sama, render widget sendiri, ambil token.

    Cloudflare memvalidasi hostname pemanggil terhadap sitekey, makanya
    halaman buatan ini di-serve lewat route interception pada URL yang
    origin-nya sama dengan form asli.
    """
    parts = url.rstrip("/").split("/")
    origin = "/".join(parts[:3]) if len(parts) >= 3 else url.rstrip("/")
    route_url = f"{origin}/__atria_turnstile__"

    extra = ""
    if action:
        extra += f' data-action="{action}"'
    if cdata:
        extra += f' data-cdata="{cdata}"'
    html = _SOLVER_HTML.format(sitekey=sitekey, extra=extra)

    page = await _new_page(context)
    try:
        async def handle(route):
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route(route_url, handle)
        await page.goto(route_url, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_selector(".cf-turnstile", timeout=10000)
        except Exception:
            return None

        deadline = time.time() + timeout
        clicked_at = 0.0
        while time.time() < deadline:
            token = await read_token(page)
            if not token:
                try:
                    token = await page.evaluate(
                        "(minLen) => { const el = document.getElementById("
                        "'atria-token'); return (el && el.value.length >= minLen) "
                        "? el.value : null; }", MIN_TOKEN_LEN)
                except Exception:
                    token = None
            if token:
                if log:
                    log(f"turnstile: standalone OK ({len(token)} char)")
                return token
            if time.time() - clicked_at > 5:
                clicked_at = time.time()
                try:
                    box = await page.locator(".cf-turnstile").first.bounding_box()
                    if box and box["width"] > 10:
                        await page.mouse.click(
                            box["x"] + 30, box["y"] + box["height"] / 2, delay=60)
                except Exception:
                    pass
            await asyncio.sleep(1)
        return None
    except Exception as e:
        if log:
            log(f"turnstile: standalone error ({str(e)[:60]})")
        return None
    finally:
        try:
            await page.unroute(route_url)
        except Exception:
            pass
        try:
            await page.close()
        except Exception:
            pass


# ─── Strategi 4: captcha service berbayar ──────────────────────────
def _service_config():
    """Tentukan service + API key dari env. Return (nama, key) atau (None, None).

    Prioritas: ATRIA_CAPTCHA_SERVICE kalau di-set, selain itu service
    pertama yang API key-nya terisi.
    """
    keys = {
        "2captcha": (os.environ.get("TWOCAPTCHA_API_KEY")
                     or os.environ.get("2CAPTCHA_API_KEY") or ""),
        "capsolver": os.environ.get("CAPSOLVER_API_KEY", ""),
        "capmonster": os.environ.get("CAPMONSTER_API_KEY", ""),
    }
    generic = (os.environ.get("ATRIA_CAPTCHA_KEY") or "").strip()
    want = (os.environ.get("ATRIA_CAPTCHA_SERVICE") or "").strip().lower()

    if want in ("none", "off", "0"):
        return None, None
    if want in keys:
        key = keys[want].strip() or generic
        return (want, key) if key else (None, None)

    for name, key in keys.items():
        if key.strip():
            return name, key.strip()
    if generic:
        # Key generik tanpa nama service -> anggap 2captcha (format paling umum).
        return "2captcha", generic
    return None, None


def _solve_2captcha(key, sitekey, url, action, cdata, timeout):
    payload = {"key": key, "method": "turnstile", "sitekey": sitekey,
               "pageurl": url, "json": 1}
    if action:
        payload["action"] = action
    if cdata:
        payload["data"] = cdata
    r = req.post("https://2captcha.com/in.php", data=payload, timeout=30)
    data = r.json()
    if data.get("status") != 1:
        raise RuntimeError(data.get("request", "submit failed"))
    task = data["request"]

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        r2 = req.get("https://2captcha.com/res.php",
                     params={"key": key, "action": "get", "id": task, "json": 1},
                     timeout=30)
        d2 = r2.json()
        if d2.get("status") == 1:
            return d2["request"]
        if d2.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(d2.get("request", "poll failed"))
    raise RuntimeError("timeout")


def _solve_task_api(base, key, sitekey, url, action, cdata, timeout, task_type):
    """CapSolver & CapMonster memakai skema createTask/getTaskResult yang sama."""
    task = {"type": task_type, "websiteURL": url, "websiteKey": sitekey}
    meta = {}
    if action:
        meta["action"] = action
    if cdata:
        meta["cdata"] = cdata
    if meta:
        task["metadata" if "capsolver" in base else "data"] = meta

    r = req.post(f"{base}/createTask",
                 json={"clientKey": key, "task": task}, timeout=30)
    data = r.json()
    if data.get("errorId"):
        raise RuntimeError(data.get("errorDescription", "createTask failed"))
    task_id = data.get("taskId")

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        r2 = req.post(f"{base}/getTaskResult",
                      json={"clientKey": key, "taskId": task_id}, timeout=30)
        d2 = r2.json()
        if d2.get("errorId"):
            raise RuntimeError(d2.get("errorDescription", "getTaskResult failed"))
        if d2.get("status") == "ready":
            sol = d2.get("solution", {}) or {}
            return sol.get("token") or sol.get("gRecaptchaResponse")
    raise RuntimeError("timeout")


def solve_service_sync(sitekey, url, action=None, cdata=None,
                       timeout=SERVICE_WAIT):
    """Versi blocking; dipanggil lewat asyncio.to_thread dari solve()."""
    name, key = _service_config()
    if not name or not key:
        return None, "no-api-key"
    try:
        if name == "2captcha":
            return _solve_2captcha(key, sitekey, url, action, cdata, timeout), name
        if name == "capsolver":
            return _solve_task_api("https://api.capsolver.com", key, sitekey, url,
                                   action, cdata, timeout,
                                   "AntiTurnstileTaskProxyLess"), name
        if name == "capmonster":
            return _solve_task_api("https://api.capmonster.cloud", key, sitekey,
                                   url, action, cdata, timeout,
                                   "TurnstileTaskProxyless"), name
    except Exception as e:
        return None, f"{name}: {str(e)[:60]}"
    return None, "unknown-service"


async def solve_service(sitekey, url, action=None, cdata=None,
                        timeout=SERVICE_WAIT, log=None):
    token, info = await asyncio.to_thread(
        solve_service_sync, sitekey, url, action, cdata, timeout)
    if token:
        if log:
            log(f"turnstile: service {info} OK ({len(token)} char)")
        return token
    if log and info != "no-api-key":
        log(f"turnstile: service gagal ({info})")
    return None


def service_available():
    """True kalau ada API key captcha service di env."""
    name, key = _service_config()
    return bool(name and key)


def service_name():
    return _service_config()[0]


# ─── Orkestrator ───────────────────────────────────────────────────
async def solve(page, context=None, sitekey=None, url=None, timeout=CAPTCHA_WAIT,
                action=None, cdata=None, modes=None, log=None, inject=True):
    """Selesaikan captcha di `page`, lalu (opsional) suntik tokennya ke form.

    Mendeteksi sendiri jenis captcha lewat window.logtoSsr.captchaConfig:

      - RecaptchaEnterprise (Atria sekarang) -> hanya mode "manual" yang
        masuk akal tanpa solver berbayar. Strategi Turnstile (inline/click/
        standalone) dilewati karena menghasilkan token untuk captcha yang
        tidak dipakai, dan itu membuang ~2 menit per akun sampai sesi
        interaction Logto kedaluwarsa (gejalanya: 307 ke /sign-in).
      - Turnstile / lainnya -> perilaku lama.

    Return:
      str   -> token berhasil didapat
      ""    -> halaman ini memang tidak memuat captcha (lanjut saja)
      None  -> semua strategi gagal
    """
    modes = modes or _env_modes()
    log = log or (lambda _m: None)

    config = await recaptcha_config(page)

    if is_recaptcha(config):
        # Token yang sah mungkin sudah ada (user mencentang lebih dulu).
        token = await recaptcha_token(page)
        if token:
            log(f"captcha: {config.get('type')} sudah ter-solve "
                f"({len(token)} char)")
            return token

        log(f"captcha: {config.get('type')} terdeteksi "
            f"(sitekey {str(config.get('siteKey'))[:20]}..., "
            f"mode {config.get('mode')})")

        # Strategi Turnstile tidak berlaku di sini; sisakan manual/service.
        usable = [m for m in modes if m in ("manual", "service")]
        if not usable:
            usable = ["manual"]

        for mode in usable:
            token = None
            if mode == "manual":
                token = await solve_manual(page, log=log, config=config)
                # "" = halaman sudah melewati captcha sendiri (bukan gagal).
                if token == "":
                    return ""
            elif mode == "service":
                if not service_available():
                    continue
                token = await solve_service(
                    config.get("siteKey") or ATRIA_RECAPTCHA_SITEKEY,
                    url or page.url or ATRIA_REGISTER_URL,
                    action=action, cdata=cdata, log=log)
            if token:
                # JANGAN inject untuk reCAPTCHA: token dari solve_manual
                # memang sudah berada di textarea milik Google dan callback
                # Logto sudah jalan. Menimpanya justru merusak state widget.
                return token
            log(f"captcha: strategi '{mode}' gagal")
        return None

    state = await widget_state(page)
    if state == "none":
        return ""
    if state == "solved":
        token = await read_token(page)
        if token:
            log("turnstile: sudah ter-solve otomatis")
            return token

    sitekey = sitekey or await find_sitekey(page)
    url = url or page.url or ATRIA_REGISTER_URL
    # page.context milik Camoufox adalah default-context yang tidak bisa
    # .new_page(); pakai browser-nya kalau ada supaya solve_standalone jalan.
    if context is None:
        context = page.context
        context = getattr(context, "browser", None) or context

    for mode in modes:
        token = None
        if mode == "manual":
            token = await solve_manual(page, log=log, config=config)
        elif mode == "inline":
            token = await solve_inline(page, timeout=timeout, log=log)
        elif mode == "click":
            token = await solve_click(page, timeout=timeout, log=log)
        elif mode == "standalone":
            token = await solve_standalone(context, sitekey, url, timeout=timeout,
                                           action=action, cdata=cdata, log=log)
        elif mode == "service":
            if not service_available():
                continue
            token = await solve_service(sitekey, url, action=action,
                                        cdata=cdata, log=log)
        if token:
            if inject:
                await inject_token(page, token)
            return token
        log(f"turnstile: strategi '{mode}' gagal")

    return None


# ─── Self-test ─────────────────────────────────────────────────────
async def _self_test(sitekey, url, headless=True):
    def log(m):
        print(f"  {m}", flush=True)

    name, key = _service_config()
    print("\n  Turnstile self-test")
    print("  " + "-" * 52)
    print(f"  sitekey : {sitekey}")
    print(f"  url     : {url}")
    print(f"  service : {name or '-'}"
          f"{' (key ' + key[:6] + '...)' if key else ' (tanpa API key)'}")
    print(f"  modes   : {', '.join(_env_modes())}")
    print("  " + "-" * 52)

    browser = None
    ctx = None
    pw = None
    started = time.time()
    try:
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            AsyncCamoufox = None

        if AsyncCamoufox is not None:
            print("  browser : camoufox", flush=True)
            ctx = await AsyncCamoufox(
                headless=headless, disable_coop=True, i_know_what_im_doing=True,
                humanize=True, os="windows",
                config={"forceScopeAccess": True}).start()
        else:
            from playwright.async_api import async_playwright
            print("  browser : chromium (camoufox tidak terpasang)", flush=True)
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=headless)
            ctx = await browser.new_context()

        page = await _new_page(ctx)
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await asyncio.sleep(3)

        # Halaman Atria baru merender widget setelah form disentuh, jadi isi
        # dulu field email kalau ada supaya captcha benar-benar muncul.
        try:
            ident = page.locator('input[name="identifier"]').first
            if await ident.count():
                await ident.fill(f"selftest{int(time.time())}@grr.la")
                await asyncio.sleep(2)
        except Exception:
            pass

        cfg = await recaptcha_config(page)
        if cfg.get("type"):
            print(f"  captcha : {cfg.get('type')} "
                  f"(mode {cfg.get('mode')}, sitekey "
                  f"{str(cfg.get('siteKey'))[:18]}...)", flush=True)

        token = await solve(page, context=ctx, sitekey=sitekey, url=url, log=log)

        # Halaman tanpa widget sama sekali: uji sitekey lewat tab standalone
        # supaya self-test tetap bermakna. Hanya berlaku untuk Turnstile —
        # reCAPTCHA tidak punya jalur standalone yang bisa diverifikasi.
        if token == "" and not is_recaptcha(cfg):
            print("  INFO widget belum dirender di halaman; "
                  "uji sitekey lewat standalone", flush=True)
            token = await solve_standalone(ctx, sitekey, url, log=log)

        elapsed = time.time() - started

        if token:
            print(f"\n  OK   token ({len(token)} char) dalam {elapsed:.1f}s")
            print(f"  {token[:60]}...")
        else:
            print(f"\n  FAIL semua strategi gagal ({elapsed:.1f}s)")
            if not service_available():
                print("  Tip: set CAPSOLVER_API_KEY / TWOCAPTCHA_API_KEY "
                      "untuk mengaktifkan strategi 'service'.")
            print("  Tip: coba ATRIA_HEADLESS=0 (mode berjendela lebih "
                  "sering lolos).")
        return bool(token)
    finally:
        for obj in (ctx, browser, pw):
            if obj is None:
                continue
            try:
                await (obj.stop() if hasattr(obj, "stop") else obj.close())
            except Exception:
                pass
        # Beri waktu transport subprocess menutup diri supaya asyncio tidak
        # memuntahkan ResourceWarning "I/O operation on closed pipe" di Windows.
        await asyncio.sleep(0.3)


if __name__ == "__main__":
    import sys
    import warnings

    warnings.filterwarnings("ignore", category=ResourceWarning)

    _sitekey = sys.argv[1] if len(sys.argv) > 1 else ATRIA_SITEKEY
    _url = sys.argv[2] if len(sys.argv) > 2 else ATRIA_REGISTER_URL
    _headless = (os.environ.get("ATRIA_HEADLESS", "1").strip().lower()
                 not in ("0", "false", "no", "off"))
    sys.exit(0 if asyncio.run(_self_test(_sitekey, _url, _headless)) else 1)
