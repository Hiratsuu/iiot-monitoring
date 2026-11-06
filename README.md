# IIoT Smart Controller 01 — Baseline Rentan (Bagian A)

Perangkat IoT virtual untuk tugas **Perangkat IoT & Pengujian Serangan**.
Aplikasi Flask berjalan di Raspberry Pi (`kali`, `192.168.100.90:8080`) dan
**sengaja memiliki celah terkontrol** untuk didemokan pada tiga fase:
serangan → mitigasi → prevention.

> **PERINGATAN:** kode ini rentan secara sengaja (command injection, kredensial
> default, cookie lemah, tanpa CSRF). Jalankan **hanya** di lab terisolasi dan
> jangan pernah diekspos ke internet.

## Ringkasan perangkat

| Item | Nilai |
|------|-------|
| Nama | IIoT Smart Controller 01 |
| Firmware | 1.0.0 |
| Stack | Python 3.14 (target) / Flask 3.1 / SQLite |
| Port | 8080, bind ke `192.168.100.90` (via env file) |
| User OS | `iotdev` (tanpa sudo, nologin) |
| DB | `/var/lib/iot-device/iot.db` |
| Kredensial awal | `admin/admin`, `operator/operator123` |

Fitur: dashboard 4 device (LED, relay kipas, buzzer, sensor suhu), kontrol
aktuator, diagnostik ping, riwayat event, halaman identitas, dan `/health`.

## Struktur repo

```
build/iot-device/          # aplikasi Flask
  app.py                   # entrypoint
  seed.py                  # schema + user + device (idempotent)
  iot_device/              # config, db, auth, devices, routes
  templates/  static/      # UI
deploy/
  iot-device.service       # unit systemd baseline (tanpa hardening, V6)
  install-iot-device.sh    # installer idempotent
  uninstall-iot-device.sh  # rollback
scripts/
  smoke-iot.sh             # uji fungsi + bukti V1
  attack-iot.sh            # reproduksi rantai serangan
logs/phase-iot-2026-11-06/ # preflight, recon, attack, evidence
reports/                   # dokumentasi hasil
plans / rencana-*.md       # dokumen rencana (root repo)
```

## Menjalankan lokal (laptop, tanpa Pi)

```bash
cd build/iot-device
python3 -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export DB_PATH="$PWD/data/iot.db"
export BIND_ADDR=127.0.0.1
export PORT=8080
python seed.py
python app.py
```

Buka `http://127.0.0.1:8080`, login `admin/admin`.

Catatan: perintah ping memakai sintaks Linux (`ping -c 2 -W 2`). Di Windows
diagnostik akan gagal tetapi mekanisme injeksi tetap bisa diuji dengan
pemisah shell Windows, misalnya `127.0.0.1 & whoami`. Deployment target tetap
Linux.

## Endpoint

| Endpoint | Method | Auth | Fungsi |
|----------|--------|------|--------|
| `/health` | GET | tidak | JSON status untuk monitoring |
| `/login` | GET/POST | tidak | form login (V2/V3) |
| `/logout` | GET | ya | hapus session |
| `/` | GET | ya | dashboard |
| `/diagnostics` | GET/POST | ya | form ping |
| `/events` | GET | ya | riwayat 50 event |
| `/about` | GET | ya | identitas perangkat (V8) |
| `/api/status` | GET | ya | state semua device |
| `/api/control` | POST | ya | ubah state device (V5) |
| `/api/diagnostics` | GET | ya | output ping (**V1**) |
| `/api/events` | GET | ya | JSON riwayat |
| `/api/about` | GET | ya | JSON identitas (V8) |

## Celah yang ditanam

| ID | Celah | Lokasi | CWE |
|----|-------|--------|-----|
| V1 | Command injection (`shell=True`) | `iot_device/routes.py` | CWE-78 |
| V2 | Kredensial default + plaintext | `seed.py`, `auth.py` | CWE-798/256 |
| V3 | Tanpa rate-limit/lockout | `/login` | CWE-307 |
| V4 | Cookie lemah + secret hardcoded | `config.py` | CWE-1004/798 |
| V5 | Tanpa CSRF | form/API | CWE-352 |
| V6 | systemd tanpa hardening | `deploy/iot-device.service` | CWE-250 |
| V7 | Flask dev server | `app.py` | CWE-1104 |
| V8 | Information disclosure | `/about` | CWE-200 |

Tidak ditanam: SQLi, XSS, upload, path traversal.

## Deploy ke Pi

Prasyarat: SSH ke `192.168.100.90` (user dengan sudo), port 8080 kosong,
Python + Flask tersedia. PSU 5V/3A sangat disarankan (Pi sedang undervoltage).

```bash
scp -r build deploy scripts README.md kali@192.168.100.90:~/iiot-monitoring/
ssh kali@192.168.100.90 'cd ~/iiot-monitoring && sudo ./deploy/install-iot-device.sh'
```

Installer: membuat user `iotdev`, direktori, venv, env file, seed DB, unit
systemd, lalu `enable --now` dan verifikasi `/health`. Aman dijalankan ulang.

Override opsional: `BIND_ADDR=... PORT=... GPIO_ENABLED=1 sudo -E ./deploy/install-iot-device.sh`.

## Uji

```bash
# dari laptop attacker
./scripts/smoke-iot.sh http://192.168.100.90:8080

# reproduksi rantai serangan (butuh konfirmasi)
./scripts/attack-iot.sh
```

Smoke test membuktikan T1–T11 termasuk injeksi `?host=127.0.0.1;id`.
Skrip serangan menjalankan nmap, hydra (kredensial default), lalu bukti
injeksi dan dump DB. Hasil tersimpan di `logs/phase-iot-2026-11-06/attack/`.

## Rollback

```bash
sudo ./deploy/uninstall-iot-device.sh            # simpan database
sudo ./deploy/uninstall-iot-device.sh --purge    # hapus database juga
```

## Batas aman

- Target hanya `192.168.100.90:8080`; perangkat lab lain tidak disentuh.
- RCE berjalan sebagai `iotdev` tanpa sudo; tanpa persistence/reverse shell.
- Tidak ada perubahan pada stack `iiot5g`.
- Demo serangan berat ditunda sampai PSU stabil (`get_throttled=0x0`).

## Status

- [x] Aplikasi Flask + template + seed
- [x] Celah V1–V8 terpasang
- [x] Installer/uninstaller + unit systemd
- [x] Skrip smoke & serangan
- [ ] Deploy ke Pi (butuh akses SSH dari mesin ini)
- [ ] Bukti serangan tersimpan di `logs/`
