#!/usr/bin/env python3
"""
Manajemen target 9Router untuk Atria Bot.

Masalah yang dipecahkan: lokasi DB 9Router sering berpindah (portable,
re-install, multi-instance, folder migrasi). Modul ini membuat bot bisa
inject ke BANYAK 9Router sekaligus dan lokasinya bisa diatur sendiri.

Urutan prioritas sumber target:

  1. env ATRIA_9ROUTER_DB  -> paling tinggi, override semuanya.
     Pisahkan beberapa path dengan ';' (Windows) atau ',':
         set ATRIA_9ROUTER_DB=C:\\a\\data.sqlite;D:\\b\\data.sqlite
  2. file 9router_targets.json di folder bot (edit manual / lewat menu T)
  3. auto-discovery -> scan lokasi umum tempat 9Router biasa menyimpan DB

Dipakai oleh atria_register.py, tapi bisa dicek sendiri:

    python ninerouter.py            # daftar target aktif
    python ninerouter.py scan       # cari semua DB 9Router di komputer
"""
import json
import os
import sqlite3
from pathlib import Path

WORKDIR = Path(__file__).parent
TARGETS_FILE = WORKDIR / "9router_targets.json"

# Nama file DB milik 9Router.
DB_NAME = "data.sqlite"

# Tabel yang wajib ada supaya DB dianggap 9Router yang sehat.
REQUIRED_TABLES = ("providerConnections", "providerNodes")


def _lazy_ninepanel():
    """Import ninepanel dengan aman (lazy) supaya kalau requests belum
    terpasang, modul lokal tetap bisa dipakai."""
    try:
        import ninepanel as npr
        return npr
    except Exception:
        return None


def _env_paths():
    """Baca ATRIA_9ROUTER_DB. Return list path (boleh kosong)."""
    raw = os.environ.get("ATRIA_9ROUTER_DB", "").strip()
    if not raw:
        return []
    # ';' dulu (path Windows mengandung ':' pada drive letter), lalu ','.
    parts = raw.split(";") if ";" in raw else raw.split(",")
    return [p.strip().strip('"').strip("'") for p in parts if p.strip()]


def _normalize(path):
    """Terima folder maupun file; kembalikan path ke data.sqlite."""
    p = Path(path).expanduser()
    try:
        if p.is_dir():
            # User boleh menunjuk folder 9router atau folder db-nya.
            for cand in (p / DB_NAME, p / "db" / DB_NAME):
                if cand.is_file():
                    return cand.resolve()
            return (p / "db" / DB_NAME).resolve()
        return p.resolve()
    except OSError:
        return p


def db_status(path):
    """Cek kesehatan satu DB. Return (ok: bool, info: str)."""
    p = Path(path)
    if not p.is_file():
        return False, "file tidak ada"
    try:
        # mode=ro supaya pengecekan tidak pernah mengunci DB panel yang jalan.
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except Exception as e:
        return False, str(e)[:45]

    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        return False, f"tabel kurang: {', '.join(missing)}"
    return True, "ok"


def remote_status(t):
    """Cek kesehatan target remote (panel online). Return (ok, info).

    Hanya lakukan ping ringan: coba login (atau Basic Auth). Dipakai
    resolve() untuk menandai target remote OK/BAD.
    """
    npr = _lazy_ninepanel()
    if npr is None:
        return False, "modul ninepanel/requests tidak tersedia"
    url = t.get("url", "")
    pw = t.get("password", "")
    bu = t.get("basic_user")
    bp = t.get("basic_pass", "")
    if not bu and (not url or not pw):
        return False, "url/password (atau basic_user) kosong"
    ok, msg = npr.test_target(url, pw, basic_user=bu, basic_pass=bp)
    if ok:
        return True, "ok"
    return False, msg[:45]


# ─── Auto-discovery ────────────────────────────────────────────────
def _search_roots():
    """Folder-folder yang masuk akal untuk menyimpan data 9Router."""
    roots = []
    home = Path.home()
    for env in ("APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "TEMP"):
        val = os.environ.get(env)
        if val:
            roots.append(Path(val))
    roots += [
        home,
        home / "AppData" / "Roaming",
        home / "AppData" / "Local",
        home / ".config",
        home / "Library" / "Application Support",
        WORKDIR,
        WORKDIR.parent,
    ]
    out, seen = [], set()
    for r in roots:
        try:
            rp = r.resolve()
        except OSError:
            continue
        if rp.is_dir() and rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def _collect(folder, found):
    """Kumpulkan data.sqlite di dalam satu folder 9router."""
    for cand in (folder / DB_NAME, folder / "db" / DB_NAME):
        try:
            if not cand.is_file():
                continue
            key = str(cand.resolve())
        except OSError:
            continue
        if key in found:
            continue
        # Lewati folder backup: itu snapshot lama, bukan DB aktif.
        if "backup" in key.lower():
            continue
        found[key] = db_status(key)


def discover(include_broken=False):
    """Scan komputer untuk DB 9Router. Return list dict {db, ok, info}.

    Hanya menelusuri folder bernama 9router/.9router sampai 2 level di bawah
    root yang umum; scan seluruh disk terlalu lambat untuk dipakai tiap batch.
    """
    found = {}
    for root in _search_roots():
        for name in ("9router", ".9router"):
            try:
                direct = root / name
                if direct.is_dir():
                    _collect(direct, found)
                # Kadang terkubur, mis. Temp\r9-migrate\.9router\db
                for pattern in (f"*/{name}", f"*/*/{name}"):
                    for sub in root.glob(pattern):
                        if sub.is_dir():
                            _collect(sub, found)
            except (OSError, ValueError):
                continue

    out = []
    for db, (ok, info) in sorted(found.items()):
        if ok or include_broken:
            out.append({"db": db, "ok": ok, "info": info})
    return out


# ─── File konfigurasi 9router_targets.json ─────────────────────────
def default_config():
    return {
        "_comment": (
            "Daftar 9Router tujuan inject. 'db' boleh path file data.sqlite "
            "ATAU folder 9router-nya (bot cari db/data.sqlite sendiri). "
            "Set 'enabled': false untuk menonaktifkan sementara tanpa "
            "menghapus. Kalau 'auto_discover' true, DB yang terdeteksi "
            "otomatis ikut dipakai. Env ATRIA_9ROUTER_DB menimpa file ini."
        ),
        "auto_discover": True,
        "targets": [],
    }


def load_config():
    if not TARGETS_FILE.is_file():
        return default_config()
    try:
        cfg = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default_config()
    if not isinstance(cfg, dict):
        return default_config()
    cfg.setdefault("auto_discover", True)
    cfg.setdefault("targets", [])
    if not isinstance(cfg["targets"], list):
        cfg["targets"] = []
    return cfg


def save_config(cfg):
    TARGETS_FILE.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return TARGETS_FILE


def add_target(db, name=None, enabled=True):
    """Tambah target manual (DB lokal). Return (ok, msg)."""
    if not str(db).strip():
        return False, "path kosong"
    path = _normalize(db)
    cfg = load_config()
    for t in cfg["targets"]:
        if _normalize(t.get("db", "")) == path:
            return False, "sudah ada di daftar"
    cfg["targets"].append({
        "type": "local",
        "name": name or path.parent.parent.name or "9router",
        "db": str(path),
        "enabled": bool(enabled),
    })
    save_config(cfg)
    ok, info = db_status(path)
    return True, "ok" if ok else f"ditambahkan, tapi {info}"


def add_remote_target(url, password, name=None, enabled=True,
                      basic_user=None, basic_pass=None):
    """Tambah target remote (panel 9Router online, URL+password).

    Untuk panel yang diproteksi Basic Auth (reverse proxy Caddy/Nginx),
    isi basic_user + basic_pass -> bot pakai Basic Auth untuk semua
    request, skip login 9Router.

    Return (ok, msg). Coba login/test sekali untuk verifikasi.
    """
    npr = _lazy_ninepanel()
    if npr is None:
        return False, "modul ninepanel/requests tidak tersedia"
    url = (url or "").strip()
    pw = (password or "").strip()
    bu = (basic_user or "").strip()
    bp = (basic_pass or "").strip()
    if not url:
        return False, "url kosong"
    if not bu and not pw:
        return False, "password (atau basic_user) kosong"
    # Normalisasi URL biar duplikat terdeteksi.
    norm = npr._norm_base(url)
    cfg = load_config()
    for t in cfg["targets"]:
        if t.get("type") == "remote" and npr._norm_base(t.get("url", "")) == norm:
            return False, "sudah ada di daftar"
    nama = name or (norm.split("//")[-1].split("/")[0] or "remote")
    entry = {
        "type": "remote",
        "name": nama,
        "url": norm,
        "enabled": bool(enabled),
    }
    if bu:
        entry["basic_user"] = bu
        entry["basic_pass"] = bp
        entry["password"] = pw  # opsional, kalau panel juga butuh login 9Router
    else:
        entry["password"] = pw
    cfg["targets"].append(entry)
    save_config(cfg)
    ok, info = remote_status(entry)
    return True, "ok" if ok else f"ditambahkan, tapi {info}"


def remove_target(idx):
    cfg = load_config()
    if not 0 <= idx < len(cfg["targets"]):
        return False, "nomor tidak valid"
    gone = cfg["targets"].pop(idx)
    save_config(cfg)
    return True, gone.get("name") or gone.get("db")


def toggle_target(idx):
    cfg = load_config()
    if not 0 <= idx < len(cfg["targets"]):
        return False, "nomor tidak valid"
    t = cfg["targets"][idx]
    t["enabled"] = not t.get("enabled", True)
    save_config(cfg)
    return True, "ON" if t["enabled"] else "OFF"


def set_auto_discover(value):
    cfg = load_config()
    cfg["auto_discover"] = bool(value)
    save_config(cfg)
    return cfg["auto_discover"]


def adopt_discovered():
    """Salin semua DB hasil scan ke file config supaya jadi permanen."""
    added = 0
    for d in discover():
        ok, _ = add_target(d["db"], Path(d["db"]).parent.parent.name)
        if ok:
            added += 1
    return added


# ─── Resolusi target akhir ─────────────────────────────────────────
def resolve(include_broken=False, skip_remote_status=False):
    """Gabungkan env + config + auto-discovery jadi daftar target final.

    Return list dict: {name, db, ok, info, source, type}.
    Untuk target remote (panel online), field 'db' diisi URL dan
    'type' = 'remote'; field tambahan 'url' & 'password' ikut disertakan
    supaya pemanggil (inject_all_9router) bisa langsung pakai.

    skip_remote_status=True -> lewati ping login ke remote (lebih cepat,
    asumsikan OK). Dipakai untuk inject biar tidak login dua kali.

    Duplikat (path/url sama) otomatis dibuang.
    """
    out, seen = [], set()

    def push_local(db, name, source):
        path = _normalize(db)
        key = str(path).lower()
        if key in seen:
            return
        seen.add(key)
        ok, info = db_status(path)
        if ok or include_broken:
            out.append({"name": name, "db": key, "ok": ok,
                        "info": info, "source": source, "type": "local"})

    def push_remote(url, password, name, source,
                    basic_user=None, basic_pass=None):
        npr = _lazy_ninepanel()
        if npr is None:
            return  # ninepanel/requests tidak ada -> skip remote
        norm = npr._norm_base(url).lower()
        key = "remote:" + norm
        if key in seen:
            return
        seen.add(key)
        if skip_remote_status:
            ok, info = True, "ok (skip cek)"
        else:
            ok, info = remote_status({
                "url": norm, "password": password,
                "basic_user": basic_user, "basic_pass": basic_pass or "",
            })
        if ok or include_broken:
            entry = {"name": name, "db": norm, "ok": ok,
                     "info": info, "source": source, "type": "remote",
                     "url": norm, "password": password}
            if basic_user:
                entry["basic_user"] = basic_user
                entry["basic_pass"] = basic_pass or ""
            out.append(entry)

    # 1. env -> kalau diisi, dia berkuasa penuh (untuk debugging / CI).
    env = _env_paths()
    if env:
        for i, p in enumerate(env, 1):
            push_local(p, f"env-{i}", "env")
        return out

    # 2. file konfigurasi (lokal + remote)
    cfg = load_config()
    for t in cfg["targets"]:
        if not t.get("enabled", True):
            continue
        npr = _lazy_ninepanel()
        if npr and npr.is_remote_target(t):
            push_remote(t.get("url"), t.get("password"),
                        t.get("name") or "remote", "config",
                        basic_user=t.get("basic_user"),
                        basic_pass=t.get("basic_pass", ""))
        elif t.get("db"):
            push_local(t["db"], t.get("name") or "target", "config")

    # 3. auto-discovery (hanya lokal)
    if cfg.get("auto_discover", True):
        for d in discover():
            push_local(d["db"], Path(d["db"]).parent.parent.name or "auto",
                       "auto")

    return out


def describe(t):
    """Baris ringkas untuk ditampilkan di terminal."""
    mark = "OK " if t["ok"] else "BAD"
    tag = t.get("type", "local")
    loc = t.get("url") or t.get("db", "")
    return f"{mark} [{t['source']:<6}|{tag:<6}] {t['name'][:16]:<17} {loc}"


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

    _cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "list"

    if _cmd == "scan":
        print("\n  Scan DB 9Router di komputer ini")
        print("  " + "-" * 68)
        hits = discover(include_broken=True)
        for h in hits:
            print(f"  {'OK ' if h['ok'] else 'BAD'}  {h['db']}")
            if not h["ok"]:
                print(f"         -> {h['info']}")
        if not hits:
            print("  (tidak ada yang ketemu)")
        print(f"\n  Total: {len(hits)} DB, "
              f"{sum(1 for h in hits if h['ok'])} sehat")

    elif _cmd == "add" and len(sys.argv) > 2:
        ok, msg = add_target(sys.argv[2],
                             sys.argv[3] if len(sys.argv) > 3 else None)
        print(f"  {'OK' if ok else 'GAGAL'}: {msg}")

    elif _cmd == "adopt":
        n = adopt_discovered()
        print(f"  {n} target hasil scan disimpan ke {TARGETS_FILE.name}")

    else:
        print(f"\n  Config : {TARGETS_FILE}"
              f"{'' if TARGETS_FILE.is_file() else '   (belum dibuat)'}")
        _env = _env_paths()
        print(f"  Env    : {'; '.join(_env) if _env else '-'}")
        print(f"  Auto   : {load_config().get('auto_discover', True)}")
        print("  " + "-" * 68)
        _targets = resolve(include_broken=True)
        for _t in _targets:
            print("  " + describe(_t))
            if not _t["ok"]:
                print(f"         -> {_t['info']}")
        if not _targets:
            print("  (tidak ada target)")
        print(f"\n  Target siap pakai: {sum(1 for t in _targets if t['ok'])}")
        print("\n  Perintah: python ninerouter.py [list|scan|adopt|"
              "add <path> [nama]]")
