# Bagian A — Hasil Implementasi (Build & Uji Lokal)

- Tanggal: 2026-11-06
- Status: **aplikasi selesai dibangun & lulus uji lokal**, deploy ke Pi belum
- Acuan: `rencana-bagian-a.md`, `rencana-aplikasi-iot.md`
- Target produksi: `192.168.100.90:8080` (belum disentuh dari mesin ini)

## 1. Yang diimplementasikan

| Komponen | Lokasi | Status |
|----------|--------|--------|
| Entrypoint Flask | `build/iot-device/app.py` | selesai |
| Config (V4 secret hardcoded, cookie lemah) | `build/iot-device/iot_device/config.py` | selesai |
| DB + schema + audit | `build/iot-device/iot_device/db.py` | selesai |
| Auth plaintext + tanpa lockout (V2/V3) | `build/iot-device/iot_device/auth.py` | selesai |
| Device virtual + sensor + GPIO stub | `build/iot-device/iot_device/devices.py` | selesai |
| Routes + API + diagnostics (V1) | `build/iot-device/iot_device/routes.py` | selesai |
| Seed idempotent (admin/admin, operator/operator123) | `build/iot-device/seed.py` | selesai |
| Template (login, dashboard, diagnostics, events, about) | `build/iot-device/templates/` | selesai |
| Static CSS + JS polling/kontrol | `build/iot-device/static/` | selesai |
| Unit systemd baseline (V6) | `deploy/iot-device.service` | selesai |
| Installer / uninstaller idempotent | `deploy/*.sh` | selesai |
| Smoke test + skrip serangan | `scripts/*.sh` | selesai |

Celah V1–V8 terpasang sesuai tabel di `rencana-aplikasi-iot.md` §7.2.
Tidak ada SQLi/XSS/upload/path traversal yang ditanam.

## 2. Uji lokal (Windows, Python 3.11.9, Flask 3.1.3)

### 2.1 Uji fungsional (Flask test client)

| # | Uji | Hasil |
|---|-----|-------|
| 1 | `/health` 200 JSON | PASS |
| 2 | `/api/status` tanpa auth → 401 JSON | PASS |
| 3 | Halaman tanpa auth → redirect `/login` | PASS |
| 4 | Login salah → 401 | PASS |
| 5 | Login `admin/admin` → 302 + cookie | PASS |
| 6 | Cookie tanpa `HttpOnly`/`Secure` (V4) | PASS |
| 7 | `/api/status` 3 device + sensor | PASS |
| 8 | Kontrol LED on / brightness 55 | PASS |
| 9 | Buzzer on | PASS |
| 10 | Device tidak dikenal → 404 | PASS |
| 11 | Aksi tidak dikenal → 400 | PASS |
| 12 | Sensor read-only → 400 | PASS |
| 13 | Event audit tercatat | PASS |
| 14 | `/api/about` bocorkan versi (V8) | PASS |
| 15 | `/api/diagnostics` output text/plain | PASS |
| 16 | **V1 injection mengeksekusi perintah** (`& whoami`) | PASS — output `hirats\kimic` |
| 17 | POST tanpa CSRF token diterima (V5) | PASS |
| 18 | Logout menghapus session | PASS |
| 19 | Render dashboard + 4 device | PASS |
| 20 | Render diagnostics + POST ping | PASS |
| 21 | Render events | PASS |
| 22 | Render about | PASS |

**Total: 22/22 PASS.**

### 2.2 Uji server live (`python app.py`, 127.0.0.1:8099)

| Uji | Hasil |
|-----|-------|
| Startup Flask dev server (V7 terlihat di log) | PASS |
| `/health` | `{"status":"ok",...,"uptime_s":3}` |
| Login `admin/admin` | 302 |
| `/api/status` | 3 device + sensor 27.0 °C |
| Kontrol `led-1 on` | `ok:true`, state `on`, value 100 |
| Injeksi `host=127.0.0.1 & whoami` | output `hirats\kimic` (bukti V1) |

### 2.3 Seed idempotent

Dijalankan 3x pada DB yang sama: tetap `2 users`, `4 devices`, `3 settings`.
Tidak ada duplikasi/error.

## 3. Catatan penting

- Perintah ping memakai sintaks Linux (`ping -c 2 -W 2`). Di Windows output
  ping gagal, tetapi mekanisme injeksi tetap terbukti (`& whoami`).
- Pada target Pi, bukti yang diharapkan: `?host=127.0.0.1;id` → `uid=...`.
- `debug=False`; RCE hanya lewat V1, bukan debugger.
- Denyut daya Pi masih `0x50005`; uji serangan berat ditunda sampai PSU 5V/3A.

## 4. Langkah berikutnya

1. Salin repo ke Pi (`~/iiot-monitoring`), jalankan
   `sudo ./deploy/install-iot-device.sh`.
2. Verifikasi `/health`, login, kontrol device dari laptop attacker.
3. Jalankan `scripts/smoke-iot.sh` (T1–T11) dan `scripts/attack-iot.sh`;
   simpan bukti di `logs/phase-iot-2026-11-06/attack/`.
4. Isi laporan Bagian A final dengan tangkapan layar + output perintah.
5. Lanjut fase Mitigasi (perbaiki V1–V8) dan Prevention (hardening systemd,
   nftables).
