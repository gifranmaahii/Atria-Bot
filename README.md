# Atria API Key Farmer

Automated API key farming for [Atria AI](https://api.atria-asi.ai/) — **100% gratis, tanpa GSuite, tanpa modal**.

## Features

- **Temp Email multi-provider** — 4 provider gratis dengan auto fallback, tanpa API key berbayar
- **Fully automated** — bikin inbox → register → ambil kode 6 digit → verify → create API key
- **Interactive menu** — list, test, scan models, export, re-verify, delete, test provider mail
- **9Router export** — ready-to-inject JSON untuk panel 9Router

## Temp Mail Providers

Dicoba berurutan, kalau satu mati otomatis lanjut ke berikutnya:

| # | Provider | Bikin akun? | Rate limit | Catatan |
|---|----------|-------------|------------|---------|
| 1 | `worker` (Cloudflare inbox) | tidak | tidak ada | tercepat, default |
| 2 | `tempmailio` | tidak | tidak ada | fallback utama |
| 3 | `mailtm` | ya | 1 akun / 60 detik | cadangan |
| 4 | `guerrilla` | tidak | ringan | cadangan terakhir |

Cek provider mana yang hidup:

```bash
python atria_register.py mail
```

## Model

| Model | Context | Type |
|-------|---------|------|
| `Atria-Dawn-Preview` | 256K | Reasoning (OpenAI-compatible) |

Free tier: **100,000,000 tokens** per account.

## Requirements

```bash
pip install playwright requests
pip install "camoufox[geoip]"    # sangat disarankan: buat lolos Turnstile
playwright install chromium
python -m camoufox fetch         # unduh browser camoufox
```

Tanpa Camoufox bot tetap jalan (pakai Chromium), tapi peluang lolos Turnstile
jauh lebih kecil.

## Files

| File | Description |
|------|-------------|
| `atria_register.py` | Bot utama — temp email, interactive menu + CLI |
| `turnstile_solver.py` | Solver Cloudflare Turnstile (4 strategi) |
| `ninerouter.py` | Manajemen target 9Router (multi-instance, auto-discovery) |
| `ninepanel.py` | Injek key ke panel 9Router online via HTTP API (target remote) |
| `api_keys.json` | Semua key tersimpan di sini |
| `9router_targets.json` | Daftar 9Router tujuan inject (dibuat otomatis) |
| `models.json` | Hasil scan model |
| `9router_export.json` | Export siap pakai untuk 9Router |

## Usage

```bash
# Interactive menu
python atria_register.py

# CLI shortcuts
python atria_register.py 1                  # buat 1 akun
python atria_register.py batch 50           # batch 50 akun
python atria_register.py batch 10 worker    # batch pakai 1 provider tertentu
python atria_register.py test               # test semua key
python atria_register.py list               # list semua key
python atria_register.py scan               # scan model tersedia
python atria_register.py cek                # cek akses token
python atria_register.py export             # export untuk 9Router
python atria_register.py sync               # inject semua key ke semua 9Router
python atria_register.py targets            # atur target 9Router (multi-instance)
python atria_register.py add                # tambah key manual + auto sync
python atria_register.py mail               # test provider temp mail
python atria_register.py captcha            # test solver Cloudflare Turnstile
```

## Menu

```
╔══════════════════════════════════════════════╗
║   ATRIA Temp-Email Key Farmer v3             ║
║   free temp mail → register → verify → key   ║
╚══════════════════════════════════════════════╝

  1. Buat 1 akun (temp email)
  2. Batch N akun (temp email)
  3. Test semua key
  4. Cek sisa token
  5. Scan semua model
  6. List semua key
  7. Export 9Router JSON
  8. Hapus key
  9. Re-verify semua key
  S. Sync semua key ke 9Router
  T. Atur target 9Router (multi-instance)
  A. Tambah key manual (register sendiri)
  M. Test provider temp mail
  C. Test solver Cloudflare Turnstile
  0. Keluar
```

## Cloudflare Turnstile Solver

Form register Atria (`auth.atria-asi.ai/register`, sitekey
`0x4AAAAAAE1sToF2hSKk4DHo`) dilindungi **Cloudflare Turnstile**. Bot sekarang
punya solver bawaan di `turnstile_solver.py` dengan **4 strategi berlapis**
yang dicoba berurutan sampai ada yang menghasilkan token:

| # | Strategi | Cara kerja | Butuh apa |
|---|----------|------------|-----------|
| 1 | `inline` | Biarkan widget di halaman auto-solve sendiri | – |
| 2 | `click` | Klik checkbox widget lewat koordinat mouse | – |
| 3 | `standalone` | Render ulang widget di tab same-origin, tokennya disuntik balik ke form | – |
| 4 | `service` | Lempar ke 2Captcha / CapSolver / CapMonster | API key |

Kunci keberhasilannya ada di **browser**: bot memakai
[Camoufox](https://github.com/daijro/camoufox) (Firefox anti-fingerprint) kalau
terpasang, karena Chromium headless polos hampir selalu ditolak Turnstile.
Kalau Camoufox tidak ada, otomatis balik ke Chromium Playwright.

Token juga di-solve **sebelum** tombol submit ditekan — form register menolak
terkirim selama `cf-turnstile-response` masih kosong. Kalau satu percobaan
gagal, bot mengulang dari awal (inbox + fingerprint baru) sebanyak
`ATRIA_CAPTCHA_RETRY` kali.

### Test solver

```bash
python atria_register.py captcha    # atau menu C
python turnstile_solver.py          # self-test langsung
python turnstile_solver.py <sitekey> <url>
```

### Kalau "No code" (email verifikasi tidak masuk)

Turnstile sudah lulus tapi bot cetak `FAIL: No code` — artinya Atria tidak
mengirim email kode ke domain temp-mail yang dipakai. Banyak layanan
blacklist domain temp-mail populer. Bot otomatis **berganti provider**
setiap retry (worker → tempmailio → mailtm → guerrilla), jadi sering kali
cukup biarkan retry berjalan.

Kalau semua provider gagal:
1. **Perpanjang tunggu** — email kadang lambat:
   ```bash
   set ATRIA_MAIL_WAIT=180        # Windows (default 120s)
   ```
2. **Tambah retry** supaya semua provider sempat dicoba:
   ```bash
   set ATRIA_CAPTCHA_RETRY=4      # default 2 (3 percobaan)
   ```
3. **Pakai provider tertentu** lewat argumen:
   ```bash
   python atria_register.py batch 5 mailtm
   # provider: worker | tempmailio | mailtm | guerrilla | auto
   ```

### Captcha: reCAPTCHA Enterprise + mode semi-manual

Sejak 2026 Atria **pindah dari Cloudflare Turnstile ke reCAPTCHA Enterprise**
(`captchaConfig = {type: "RecaptchaEnterprise", mode: "checkbox"}`, terbaca di
`window.logtoSsr.signInExperience.data.captchaConfig`). Konsekuensinya:

- Token reCAPTCHA **tidak bisa dipalsukan**. Logto memanggil `executeCaptcha()`
  dan menunggu callback dari Google; kalau callback tidak pernah datang, form
  tidak pernah terkirim (gejala lamanya: "Register submitted" lalu 307 ke
  `/sign-in` karena sesi interaction kedaluwarsa).
- Widget captcha **baru dirender setelah tombol submit diklik** — sebelum itu
  `<div class="...captchaBox">` masih kosong. Karena itu urutan bot sekarang:
  isi email → **klik submit** → selesaikan captcha → Logto mengirim form sendiri.

Karena itu mode default adalah **semi-manual dan gratis** (`ATRIA_MANUAL=1`):

1. Browser dibuka **berjendela** otomatis (headless dipaksa mati).
2. Bot mengklik checkbox "I'm not a robot" **sendiri**.
3. Kalau reputasi IP bagus → langsung lolos tanpa campur tangan.
   Kalau Google memunculkan challenge gambar → **kamu** yang menyelesaikan,
   bot menunggu dan lanjut otomatis begitu token muncul.

```bash
python atria_register.py 1              # 1 akun, kamu bantu captcha sekali
set ATRIA_MANUAL_WAIT=300              # perpanjang waktu menyelesaikan captcha
set ATRIA_MANUAL_AUTOCLICK=0           # matikan auto-klik checkbox
set ATRIA_MANUAL=0                     # matikan mode manual (butuh solver berbayar)
```

Mau **100% otomatis**? Butuh solver berbayar untuk reCAPTCHA v2/Enterprise
(NopeCHA Token API `type=recaptcha2` + `enterprise:true` — 20 credit/solve,
Starter $4.99/bln; atau CapSolver/2Captcha). Set key-nya lalu
`ATRIA_MANUAL=0`.

### Email: hanya grr.la / sharklasers.com yang lolos

Atria memasang `emailBlocklistPolicy` (170+ domain temp-mail,
`blockSubaddressing: true`). Hasil scan:

| Provider | Domain | Status |
|----------|--------|--------|
| **guerrilla** | `grr.la`, `sharklasers.com` | **LOLOS** — dipakai default |
| worker | `inbox.octdev.biz.id` | diblokir (`*.biz.id`) |
| tempmailio | `yzcalo.com`, `ozsaip.com`, dll | diblokir |
| mailtm | `uberip.com` | diblokir |

GuerrillaMail API selalu membalas `@guerrillamailblock.com` (yang diblokir)
apa pun parameter `domain` yang dikirim, jadi bot **mengganti domainnya di
sisi klien** ke `grr.la`/`sharklasers.com` — inbox tetap terbaca lewat
`sid_token` yang sama karena ketiganya alias mailbox yang sama.

Provider yang diblokir dilewati otomatis (dengan pesan alasan) supaya tidak
membuang `ATRIA_MAIL_WAIT` detik per akun. Paksa pakai dengan
`ATRIA_ALLOW_BLOCKED=1`.

### Kalau captcha masih gagal

1. **Pasang Camoufox** kalau belum (fingerprint lebih meyakinkan → lebih sering
   lolos tanpa challenge gambar):
   ```bash
   pip install "camoufox[geoip]"
   ```
2. **Naikkan batas waktu manual**:
   ```bash
   set ATRIA_MANUAL_WAIT=300
   ```
3. **Pakai captcha service** (paling andal, berbayar):
   ```bash
   set CAPSOLVER_API_KEY=CAP-xxxxx
   set ATRIA_MANUAL=0
   # atau TWOCAPTCHA_API_KEY / CAPMONSTER_API_KEY
   ```

Kalau semua mentok, jalur manual tetap tersedia: register sendiri di browser,
buat key di `/console/keys`, lalu `python atria_register.py add` (menu A).

### Env tuning

| Env | Default | Fungsi |
|-----|---------|--------|
| `ATRIA_MANUAL` | `1` | Mode semi-manual (headful + kamu bantu captcha). `0` = butuh solver berbayar |
| `ATRIA_MANUAL_WAIT` | `180` | Detik menunggu kamu menyelesaikan captcha |
| `ATRIA_MANUAL_AUTOCLICK` | `1` | Bot mengklik checkbox reCAPTCHA sendiri lebih dulu |
| `ATRIA_ALLOW_BLOCKED` | - | `1` untuk tetap memakai provider email yang diblokir Atria |
| `ATRIA_CAPTCHA_MODE` | `manual,service` (saat `ATRIA_MANUAL=1`) | Strategi yang dipakai (koma): `manual,inline,click,standalone,service` |
| `ATRIA_CAPTCHA_WAIT` | `40` | Detik menunggu token per strategi (non-manual) |
| `ATRIA_CAPTCHA_RETRY` | `2` | Berapa kali register diulang kalau captcha gagal |
| `ATRIA_CAPTCHA_SERVICE` | auto | `2captcha` / `capsolver` / `capmonster` / `none` |
| `ATRIA_CAPTCHA_SERVICE_WAIT` | `180` | Detik menunggu jawaban captcha service |
| `CAPSOLVER_API_KEY` | - | API key CapSolver |
| `TWOCAPTCHA_API_KEY` | - | API key 2Captcha |
| `CAPMONSTER_API_KEY` | - | API key CapMonster |
| `ATRIA_RECAPTCHA_SITEKEY` | sitekey Atria | Cadangan kalau `captchaConfig` tidak terbaca |
| `ATRIA_TURNSTILE_SITEKEY` | sitekey lama | Hanya untuk form Turnstile (tidak dipakai Atria lagi) |
| `ATRIA_BROWSER` | `auto` | `camoufox` atau `chromium` |
| `ATRIA_HEADLESS` | `1` | Dipaksa `0` saat `ATRIA_MANUAL=1` |
| `ATRIA_NAV_TIMEOUT` | `90000` | Timeout navigasi Playwright (ms) |
| `ATRIA_MAIL_WAIT` | `120` | Detik menunggu email kode verifikasi (naikkan kalau email lambat) |
| `ATRIA_NO_9ROUTER` | - | Set `1` untuk matikan auto-inject ke 9Router |
| `ATRIA_9ROUTER_DB` | auto | Path DB 9Router; pisah `;` untuk banyak target sekaligus |

## Inject ke 9Router (multi-target)

Key bisa langsung masuk ke panel 9Router tanpa import manual — dan sejak
sekarang bisa ke **beberapa 9Router sekaligus**, karena lokasi DB-nya sering
berpindah (portable, re-install, folder migrasi, dua instance paralel).

```bash
python atria_register.py sync      # inject semua key ke SEMUA target (menu S)
python atria_register.py targets   # atur daftar target (menu T)
python ninerouter.py               # lihat target aktif
python ninerouter.py scan          # cari semua DB 9Router di komputer
python ninerouter.py adopt         # simpan hasil scan jadi permanen
python ninerouter.py add "D:\9router\db\data.sqlite" kantor
```

### Dari mana target diambil

Urutan prioritas — yang di atas menimpa yang di bawah:

| # | Sumber | Keterangan |
|---|--------|------------|
| 1 | Env `ATRIA_9ROUTER_DB` | Override total. Pisahkan banyak path dengan `;` |
| 2 | `9router_targets.json` | Daftar manual, bisa diedit lewat menu T atau langsung |
| 3 | Auto-discovery | Scan lokasi umum (`%APPDATA%\9router`, `~/.9router`, `~/.config/9router`, macOS Application Support, `%TEMP%`, folder bot) |

```bash
# Windows — dua DB sekaligus
set ATRIA_9ROUTER_DB=C:\Users\me\AppData\Roaming\9router;D:\9router-portable

# Linux/macOS
export ATRIA_9ROUTER_DB="$HOME/.9router,$HOME/backup/9router"
```

Path boleh menunjuk file `data.sqlite` **atau** folder `9router`-nya — bot
mencari `db/data.sqlite` di dalamnya sendiri.

### Contoh `9router_targets.json`

```json
{
  "auto_discover": true,
  "targets": [
    { "type": "local",  "name": "utama",    "db": "C:\\Users\\me\\AppData\\Roaming\\9router\\db\\data.sqlite", "enabled": true },
    { "type": "local",  "name": "portable", "db": "D:\\9router-portable",                                     "enabled": true },
    { "type": "remote", "name": "vps-jauh", "url": "https://9router.example.com", "password": "rahasia123",   "enabled": true },
    { "type": "remote", "name": "proxy",    "url": "https://panel.dibalik-proxy.com:20443",                    "enabled": true,
      "basic_user": "admin", "basic_pass": "123456" }
  ]
}
```

- `type` bersifat opsional — kalau tidak diisi, diasumsikan `"local"` (kompatibel dengan config lama).
- `enabled: false` → target dilewati tanpa perlu dihapus.
- `auto_discover: false` → hanya pakai daftar manual, tidak scan otomatis.

#### Target remote (panel 9Router online)

Selain DB lokal (`type: "local"` + `db`), bot bisa injek langsung ke panel
9Router yang **berjalan di server lain** via HTTP API. Cukup isi `type:
"remote"`, `url` (base URL panel, mis. `https://9router.example.com`), dan
`password` panel. Bot akan: login → pastikan node Atria ada → buat/update
koneksi (deteksi duplikat via `name`/`email` karena API menyembunyikan
`apiKey` saat GET).

##### Panel di balik reverse proxy (Basic Auth)

Kalau panel diproteksi **popup username/password** di browser (HTTP Basic
Auth — biasanya dari reverse proxy Caddy/Nginx), isi `basic_user` +
`basic_pass`. Bot akan kirim Basic Auth header untuk **semua request** dan
**skip login 9Router** (Basic Auth sudah dianggap authenticated oleh API).

Tanda-tanda panel pakai Basic Auth: browser muncul popup "Authentication
Required" / "Sign in" (bukan form login 9Router), atau `python ninepanel.py
test <url> <pass>` balas `HTTP 401` / "tidak konek".

Cara paling mudah menambah remote target: menu **T → R** (URL + password).
Wizard akan tanya apakah panel pakai Basic Auth — isi username (mis. `admin`)
kalau YA, kosongkan kalau TIDAK.

Verifikasi cepat lewat CLI:

```bash
# Panel tanpa Basic Auth (login 9Router biasa)
python ninepanel.py test https://9router.example.com password-panel

# Panel dengan Basic Auth (admin:123456)
python ninepanel.py test https://panel.proxy.com:20443 ignored --basic admin:123456

# Inject langsung (Basic Auth)
python ninepanel.py inject https://panel.proxy.com:20443 ignored atr_xxx email@x.com --basic admin:123456
```

Catatan: arg `<password>` bisa diisi apa saja (mis. `ignored`) kalau pakai
`--basic`, karena login 9Router di-skip.

### Menu T

```
  [T] Target 9Router
  Auto-discover : ON
  Target manual:
   1. [ON |local ]  utama      ok
   2. [OFF|remote] vps-jauh    ok

   A. Tambah target lokal   R. Tambah target remote (URL+password)
   H. Hapus target          E. Enable/disable
   D. Auto-discover ON/OFF  S. Scan komputer
   P. Simpan hasil scan jadi permanen
```

### Yang terjadi saat inject

- Tiap target dicek dulu kesehatannya (dibuka read-only, tabel
  `providerConnections` + `providerNodes` harus ada). DB rusak otomatis dilewati.
- Node provider `Atria AI` (`openai-compatible-chat-atria`, tipe
  `openai-compatible`, baseUrl `https://api.atria-asi.ai/v1`) dibuat kalau belum ada.
- Tiap key jadi 1 row di `providerConnections` dengan `defaultModel`
  `Atria-Dawn-Preview`, `authType` `apikey`, `isActive = 1`.
- Idempotent: key yang sudah ada di-update (bukan duplikat), field runtime
  9Router (usage/backoff/modelLock) tetap dipertahankan.
- Hasil per key ditandai ringkas, satu huruf per target:
  `+` baru | `u` update | `.` skip | `x` gagal. Jadi `u+` berarti target-1
  ter-update dan target-2 dapat row baru.

Inject otomatis juga jalan tiap key baru dibuat, ke semua target sekaligus.
Matikan dengan env `ATRIA_NO_9ROUTER=1`. Refresh panel 9Router setelah sync.

## API Key Format

```
atr_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Base URL: `https://api.atria-asi.ai/v1`

### Example API Call

```bash
curl -X POST https://api.atria-asi.ai/v1/chat/completions \
  -H "Authorization: Bearer atr_YOUR_KEY_HERE" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Atria-Dawn-Preview",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 100
  }'
```

## Auth Flow

```
temp mail provider (bikin inbox, gratis)
    ↓
Atria /register (isi email)
    ↓
polling inbox → ambil kode 6 digit
    ↓
Atria /register/verification-code (isi kode)
    ↓
Auto-login → /console/keys → create API key
```

Login pakai **email + kode OTP** (Logto), jadi tidak perlu Google OAuth / akun GSuite sama sekali.

## Rate Limits

- **worker / tempmailio / guerrilla**: tanpa rate limit berarti
- **mail.tm**: 1 akun per 60 detik (makanya ditaruh urutan ke-3)
- **Atria API**: tidak ada limit terdokumentasi (usage-based, cap 100M token)

## Export Format (9Router)

```json
[
  {
    "apiKey": "atr_xxx",
    "name": "key-1",
    "provider": "openai-compatible-chat",
    "baseUrl": "https://api.atria-asi.ai/v1",
    "defaultModel": "Atria-Dawn-Preview"
  }
]
```

## Notes

- Setiap key menyimpan field `provider` biar ketahuan asal temp mail-nya
- Tiap akun temp email dapat 100M token gratis
- Model reasoning mengembalikan field `reasoning_content` — ini normal
- Balasan model bisa lambat, jadi timeout curl di-set 90 detik
- Hapus `api_keys.json` untuk mulai dari nol

## License

MIT

