# Rencana Aplikasi — IIoT Smart Controller (Baseline Rentan)

- Status: **desain, belum diimplementasikan**
- Bagian: A (perangkat IoT korban)
- Target: `kali` 192.168.100.90:8080
- Stack: Python 3.14 + Flask 3.1 + SQLite (stdlib)
- Prinsip: fitur terlihat wajar seperti perangkat IoT produksi, tetapi punya
  celah yang **sengaja** ditanam dan didokumentasikan.

---

## 1. Cerita perangkat

Perangkat bernama **IIoT Smart Controller 01** — pengendali ruang server kecil.
Fungsinya:

- menyalakan/mematikan **LED indikator**,
- mengendalikan **relay kipas exhaust**,
- membunyikan **buzzer alarm**,
- membaca **sensor suhu** ruang server,
- menyediakan **diagnostik jaringan** (ping) untuk troubleshooting.

Web server-nya yang menjadi permukaan serangan. Dampak serangan jadi masuk akal:
penyerang bisa mematikan kipas, membunyikan alarm, memalsukan suhu, dan membaca
kredensial perangkat.

## 2. Peran pengguna

| Peran | Hak |
|-------|-----|
| `admin` | semua fitur, termasuk halaman perangkat & riwayat |
| `operator` | dashboard, kontrol device, diagnostik |
| anonim | hanya `/health` dan `/login` |

Baseline hanya punya 2 akun (keduanya password lemah, lihat §7).

## 3. Functional requirements

| ID | Fitur | Deskripsi |
|----|-------|-----------|
| FR-1 | Login/logout | Form username+password, session cookie |
| FR-2 | Dashboard | 4 device + suhu + 10 event terakhir |
| FR-3 | Kontrol device | LED on/off + brightness, relay on/off, buzzer on/off |
| FR-4 | Sensor suhu | Nilai simulasi 24–32 °C, update tiap 10 detik |
| FR-5 | Diagnostik | Form ping host, tampilkan output |
| FR-6 | Riwayat | 50 event terakhir (login, kontrol, diagnostik) |
| FR-7 | Identitas | Halaman about: hostname, IP, firmware, versi runtime |
| FR-8 | Health | JSON tanpa auth untuk monitoring |

## 4. Non-functional requirements

| Aspek | Keputusan |
|-------|-----------|
| Bahasa UI | Indonesia (label), kode & komentar Inggris |
| Port | 8080, bind **hanya** `192.168.100.90` |
| Proses | Flask built-in server (`threaded=True`), 1 worker — sengaja bukan WSGI produksi (V7) |
| Database | SQLite di `/var/lib/iot-device/iot.db` |
| Log | stdout -> journald + tabel `events` |
| User OS | `iotdev`, tanpa sudo, shell nologin |
| GPIO | Adapter stub; `GPIO_ENABLED=0` default (virtual) |
| Internet | Tidak dibutuhkan (`venv --system-site-packages`) |
| Waktu | UTC, konsisten dengan Pi |

## 5. Arsitektur internal

```
app.py                  # create_app(), route table, error handler
iot_device/
  __init__.py
  config.py             # env + default tidak aman (SECRET_KEY hardcoded)
  db.py                 # get_db(), schema, seed helpers
  auth.py               # login_required, verify_login, session
  devices.py            # VirtualDevice, GPIO adapter stub, sensor simulator
  routes.py             # halaman HTML + API JSON
templates/              # base, login, dashboard, diagnostics, events, about
static/                 # style.css, app.js (fetch API, tanpa framework)
seed.py                 # buat schema + user + device + settings
```

Alur request:

```
browser/curl -> Flask routes -> auth decorator -> devices.py -> SQLite
                                      |
                                      +-> events (audit)
```

## 6. Model data

```sql
CREATE TABLE users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  username   TEXT NOT NULL UNIQUE,
  password   TEXT NOT NULL,            -- plaintext (V2)
  role       TEXT NOT NULL DEFAULT 'operator',
  created_at TEXT NOT NULL
);

CREATE TABLE devices (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,     -- led-1, relay-1, buzzer-1, sensor-suhu
  type       TEXT NOT NULL,            -- led|relay|buzzer|sensor
  location   TEXT,
  state      TEXT NOT NULL DEFAULT 'off',
  value      REAL NOT NULL DEFAULT 0,  -- brightness 0-100 / suhu °C
  updated_at TEXT NOT NULL
);

CREATE TABLE events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  username   TEXT,
  device     TEXT,
  action     TEXT NOT NULL,            -- login_ok|login_fail|control|diagnostics
  source_ip  TEXT,
  detail     TEXT
);

CREATE TABLE settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

Seed:

| username | password | role |
|----------|----------|------|
| `admin` | `admin` | admin |
| `operator` | `operator123` | operator |

| name | type | location | initial |
|------|------|----------|---------|
| `led-1` | led | Ruang Server | off, 0 |
| `relay-1` | relay | Kipas Exhaust | off |
| `buzzer-1` | buzzer | Pintu Ruang Server | off |
| `sensor-suhu` | sensor | Ruang Server | on, 27.0 °C |

Settings: `firmware=1.0.0`, `device_name=IIoT-SmartController-01`,
`boot_ts=<epoch>`.

## 7. Permukaan serangan & celah yang ditanam

### 7.1 Endpoint

| Endpoint | Method | Auth | Fungsi | Catatan |
|----------|--------|------|--------|---------|
| `/health` | GET | tidak | JSON status | minimal, untuk monitoring |
| `/login` | GET/POST | tidak | form login | V2/V3 |
| `/logout` | GET | ya | hapus session | |
| `/` | GET | ya | dashboard | |
| `/diagnostics` | GET/POST | ya | form ping | memanggil V1 |
| `/events` | GET | ya | tabel riwayat | |
| `/about` | GET | ya | identitas perangkat | V8 info disclosure |
| `/api/status` | GET | ya | state semua device | |
| `/api/control` | POST | ya | ubah state | V5 |
| `/api/diagnostics` | GET | ya | output ping (text/plain) | **V1** |
| `/api/events` | GET | ya | JSON riwayat | |
| `/api/about` | GET | ya | JSON identitas | V8 |

Aturan auth: request ke `/api/*` tanpa session -> `401 JSON`; halaman HTML ->
redirect `/login`.

### 7.2 Daftar celah

| ID | Celah | Implementasi baseline | CWE | Dipakai di |
|----|-------|-----------------------|-----|-----------|
| **V1** | **Command injection** | `subprocess.run(f"ping -c 2 -W 2 {host}", shell=True)` tanpa validasi | CWE-78 | Serangan utama |
| V2 | Kredensial default + plaintext | `admin/admin`, `operator/operator123`, bandingkan string langsung | CWE-798/256 | Initial access |
| V3 | Tanpa rate-limit/lockout | tidak ada delay, counter, atau CAPTCHA | CWE-307 | Brute force hydra |
| V4 | Session lemah | `SECRET_KEY` hardcoded; cookie tanpa `HttpOnly`, `Secure`, `SameSite`; umur 31 hari | CWE-1004/798 | Session takeover |
| V5 | Tanpa CSRF | form/API menerima POST tanpa token | CWE-352 | Aksi device dari situs lain |
| V6 | Service tanpa hardening | unit systemd default, user `iotdev` | CWE-250 | Kontras fase Prevention |
| V7 | Dev server di "produksi" | `app.run()` Flask, bukan gunicorn/waitress | CWE-1104 | Realisme + prevention |
| V8 | Information disclosure | `/about` membocorkan versi Flask/Python, hostname, firmware | CWE-200 | Recon |

**Tidak ditanam (sengaja):** SQL injection, XSS, upload file, path traversal.
Fokus tugas tetap pada satu serangan utama (V1) dengan rantai awal V2/V3.

### 7.3 Detail V1 (paling penting)

```python
# iot_device/routes.py (rencana)
host = request.args.get("host", "127.0.0.1")
cmd  = f"ping -c 2 -W 2 {host}"
proc = subprocess.run(cmd, shell=True, capture_output=True,
                      text=True, timeout=10)
return proc.stdout + proc.stderr, 200, {"Content-Type": "text/plain"}
```

- Tidak ada allowlist karakter, tidak ada `shlex.quote`.
- `timeout=10` hanya membunuh shell; proses anak bisa tertinggal (dicatat
  sebagai temuan tambahan).
- Output dibatasi 4 KB agar response tidak membanjiri.
- Setiap pemanggilan dicatat ke `events` (action=`diagnostics`, detail=cmd) —
  ini yang nanti menjadi bukti serangan **dan** bahan mitigasi.

### 7.4 Detail V4 (session)

```python
app.secret_key = os.environ.get("SECRET_KEY", "iot-device-secret-2026")
app.config.update(
    SESSION_COOKIE_HTTPONLY=False,
    SESSION_COOKIE_SECURE=False,
    SESSION_COOKIE_SAMESITE=None,
    PERMANENT_SESSION_LIFETIME=timedelta(days=31),
)
```

Session hanya berisi `{"user": "...", "role": "..."}`.

### 7.5 Detail V6 (systemd baseline)

```ini
[Service]
Type=simple
User=iotdev
Group=iotdev
WorkingDirectory=/opt/iot-device
EnvironmentFile=-/etc/iot-device/iot-device.env
ExecStart=/opt/iot-device/.venv/bin/python /opt/iot-device/app.py
Restart=always
RestartSec=3
```

Tidak ada `NoNewPrivileges`, `ProtectSystem`, `PrivateTmp`, `MemoryMax`, atau
`CapabilityBoundingSet` — semuanya baru ditambahkan di fase Prevention.

## 8. Kontrak API

### `GET /health`
```json
{"status":"ok","device":"IIoT-SmartController-01","firmware":"1.0.0","uptime_s":1234}
```

### `POST /login` (form)
- Sukses: `302 Location: /`, `Set-Cookie: session=...`
- Gagal: `401` halaman login + pesan generik `Username atau password salah`
- Tidak membedakan user tidak ada vs password salah (tapi tetap tanpa delay).

### `GET /api/status`
```json
{
  "device": "IIoT-SmartController-01",
  "sensor": {"temp_c": 27.4, "updated_at": "2026-11-06T03:10:00Z"},
  "devices": [
    {"name":"led-1","type":"led","state":"off","value":0,"location":"Ruang Server"},
    {"name":"relay-1","type":"relay","state":"off","value":0,"location":"Kipas Exhaust"},
    {"name":"buzzer-1","type":"buzzer","state":"off","value":0,"location":"Pintu Ruang Server"}
  ],
  "uptime_s": 1234
}
```

### `POST /api/control`
Request:
```json
{"device":"led-1","action":"on"}
{"device":"led-1","action":"set","value":75}
{"device":"relay-1","action":"off"}
```
Respons sukses `200 {"ok":true,"device":{...}}`.
Device tidak dikenal -> `404 {"ok":false,"error":"device not found"}`.
`action` tidak dikenal -> `400`.
Setiap perubahan menulis satu baris `events`.

### `GET /api/diagnostics?host=127.0.0.1`
`200 text/plain` berisi stdout+stderr ping.

### `GET /api/events?limit=50`
```json
[{"ts":"...","username":"admin","device":"led-1","action":"control","source_ip":"192.168.100.209","detail":"on"}]
```

### `GET /api/about`
```json
{"device":"IIoT-SmartController-01","hostname":"kali-raspberrypi","ip":"192.168.100.90",
 "firmware":"1.0.0","python":"3.14.7","flask":"3.1.3","boot":"2026-11-06T03:00:00Z"}
```

## 9. Perilaku device virtual

- `led-1`: `off/on`; `value` = brightness 0–100. `on` -> 100.
- `relay-1`: `off/on`.
- `buzzer-1`: `off/on`; `on` otomatis kembali `off` setelah 5 detik
  (thread/timer ringan).
- `sensor-suhu`: nilai = 27.0 + 0.5·sin(t/60) + jitter ±0.3; dihitung saat
  dibaca, disimpan bila sudah >10 detik.
- Semua state persisten di SQLite, selamat dari restart service.

Adapter GPIO (stub, belum aktif):
```python
if os.getenv("GPIO_ENABLED") == "1":
    from gpiozero import LED, Buzzer   # opsional, belum terpasang
    pin_map = {"led-1": 17, "relay-1": 27, "buzzer-1": 22}
```
Jika import gagal -> log warning, fallback virtual. Dengan begitu fase B bisa
menambah hardware tanpa mengubah API.

## 10. Logging & bukti

| Sumber | Isi | Lokasi |
|--------|-----|--------|
| journald | request line, error, startup | `journalctl -u iot-device` |
| tabel `events` | login, kontrol, diagnostik + source IP | SQLite |
| stdout | print startup + warning GPIO | journald |

Yang **tidak** dicatat baseline (sengaja): password, isi cookie, user-agent.
Ini membuat forensik fase Mitigasi lebih menantang, tetapi `source_ip` dan
`detail` tetap cukup untuk merekonstruksi serangan.

## 11. Deployment

### Layout di Pi
```
/opt/iot-device/
  app.py, seed.py, requirements.txt
  iot_device/, templates/, static/
  .venv/                       # venv --system-site-packages
/var/lib/iot-device/iot.db     # owner iotdev:iotdev, 0640
/etc/iot-device/iot-device.env # SECRET_KEY, DB_PATH, BIND_ADDR, PORT, GPIO_ENABLED
/etc/systemd/system/iot-device.service
```

### Installer (`deploy/install-iot-device.sh`)
1. `useradd --system --home /opt/iot-device --shell /usr/sbin/nologin iotdev`
   (skip jika ada).
2. Buat direktori + permission.
3. Copy app ke `/opt/iot-device`.
4. `python3 -m venv --system-site-packages /opt/iot-device/.venv` lalu
   `pip install -r requirements.txt` (Flask sudah tersedia sistem, jadi tanpa
   internet).
5. Seed DB bila belum ada (idempotent).
6. Install unit, `daemon-reload`, `enable` + `restart`.
7. Verifikasi `/health` (JSON `status=ok`) dan status systemd.
8. Aman dijalankan ulang; env file ditulis ulang setiap run.

### Uninstaller
Stop + disable + hapus unit/drop-in, `/opt/iot-device`, dan `/etc/iot-device`,
lalu hapus user + group `iotdev`. **Default menyimpan
`/var/lib/iot-device/iot.db`**; gunakan `--purge` untuk menghapusnya (dengan
konfirmasi tambahan). Verifikasi service berhenti dan user hilang.

### Config baseline
```ini
SECRET_KEY=iot-device-secret-2026
DB_PATH=/var/lib/iot-device/iot.db
BIND_ADDR=192.168.100.90
PORT=8080
GPIO_ENABLED=0
FIRMWARE=1.0.0
```

## 12. Rencana pengujian (smoke test)

| # | Uji | Perintah | Harapan |
|---|-----|----------|---------|
| T1 | Health | `curl -s :8080/health` | 200 JSON |
| T2 | Unauth API | `curl -s :8080/api/status` | 401 JSON |
| T3 | Login gagal | `curl -i -d 'username=admin&password=x' /login` | 401 |
| T4 | Login sukses | `-d 'username=admin&password=admin' -c jar` | 302 + cookie |
| T5 | Status | `curl -b jar /api/status` | 3 device + sensor |
| T6 | Kontrol | POST led-1 on | state `on`, event tercatat |
| T7 | Persistensi | restart service, baca status | state tetap |
| T8 | Diagnostik | `?host=127.0.0.1` | output ping |
| T9 | **Injeksi** | `?host=127.0.0.1;id` | `uid=...` (bukti V1) |
| T10 | Device invalid | POST device `xxx` | 404 JSON |
| T11 | Timeout | `?host=10.255.255.1` | selesai ~10 s, tidak hang |
| T12 | Boot | reboot / `systemctl is-enabled` | enabled + jalan |
| T13 | Log | `journalctl -u iot-device -n 20` | request terlihat |
| T14 | Rollback | jalankan uninstaller | port 8080 tertutup, user hilang |

T9 sengaja menjadi jembatan ke fase serangan: jika `id` muncul, aplikasi
dinyatakan "siap diserang".

## 13. Rantai serangan yang didukung desain ini

1. **Recon:** `nmap -sV -p8080` -> banner Werkzeug/Flask (V7/V8).
2. **Initial access:** buka `/login`, coba `admin/admin`; atau hydra dengan
   wordlist kecil (V2/V3).
3. **Akses terautentikasi:** ambil cookie session (V4).
4. **Eksekusi:** `/api/diagnostics?host=127.0.0.1;id` (V1).
5. **Dampak:**
   - baca DB: `;sqlite3 /var/lib/iot-device/iot.db .dump` (kredensial plaintext,
     riwayat sensor),
   - kontrol device tanpa otorisasi,
   - hentikan service: `;pkill -f app.py` (systemd restart, membuktikan
     availability risk).
6. **Bukti:** request/response, isi `events`, dump DB, tcpdump, log journald.

Batas aman: RCE berjalan sebagai `iotdev` tanpa sudo; tidak ada persistence,
tidak ada serangan ke host lain, tidak ada reverse shell interaktif (cukup
callback HTTP sekali jalan).

## 14. Yang ditunda / di luar scope

- SQLi, XSS, upload, path traversal — tidak ditanam.
- Kamera/streaming — tidak ada hardware; bisa ditambah nanti sebagai device
  virtual jika diperlukan.
- Integrasi Grafana/blackbox di `iiot5g` — opsional, butuh persetujuan karena
  menyentuh stack Phase 1.
- GPIO fisik — ditunda sampai PSU stabil dan hardware tersedia.

## 15. Kriteria desain diterima

- [ ] Semua fitur FR-1..FR-8 punya endpoint/route yang jelas.
- [ ] Setiap celah V1..V8 punya lokasi implementasi dan CWE.
- [ ] Tidak ada celah di luar daftar (tidak ada SQLi/XSS tak sengaja).
- [ ] Rantai serangan di §13 bisa dijalankan hanya dengan tool yang sudah ada
      (hydra, nmap, curl, sqlite3) tanpa instalasi baru.
- [ ] Rollback bersih dan idempotent.
- [ ] Tidak menyentuh host/perangkat lain di lab.

## 16. Estimasi implementasi

| Bagian | Waktu |
|--------|-------|
| config + db + seed | 20 mnt |
| auth + session | 15 mnt |
| devices + sensor | 20 mnt |
| routes + template + static | 45 mnt |
| installer + systemd | 25 mnt |
| smoke test + perbaikan | 30 mnt |
| **Total** | **±2,5 jam** |
