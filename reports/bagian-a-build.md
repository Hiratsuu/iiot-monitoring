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

| Kelompok | Uji | Hasil |
|----------|-----|-------|
| Auth | `/health` 200 JSON; API tanpa auth → 401; halaman → redirect; login salah → 401; login `admin/admin` → 302; cookie tanpa `HttpOnly`/`Secure` (V4); logout | PASS |
| Otorisasi | Admin akses `/events`, `/about`, `/api/events`, `/api/about`; operator hanya dashboard/kontrol/diagnostik (events & about → 403) | PASS |
| Device | Status 3 aktuator + sensor; LED on/set 55; relay on; buzzer on → auto-off 5 detik; device/aksi tidak dikenal → 404/400; sensor read-only → 400 | PASS |
| Validasi input | Body JSON list → 400; field `device` hilang → 400; `value` boolean → 400; method salah di API → 405 JSON | PASS |
| Redirect | `?next=//evil.example` ditolak (redirect `/`); `?next=/diagnostics` dipertahankan | PASS |
| V1 | `/api/diagnostics` text/plain; injeksi `& whoami` → eksekusi; output >4 KB dipotong dengan penanda | PASS |
| Render | Dashboard, diagnostics, events, about | PASS |

**Total: 40/40 PASS.**

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

Dijalankan berulang pada DB yang sama: tetap `2 users`, `4 devices`,
`3 settings`. Tidak ada duplikasi/error. `boot_ts` diperbarui saat service
start sehingga `uptime_s` mencerminkan proses berjalan.

### 2.4 Perbaikan pasca code review

| Temuan | Perbaikan | Status |
|--------|-----------|--------|
| Open redirect `?next=//host` | Validasi `urlsplit` + tolak `//`/backslash; hidden field `next` di form login | selesai |
| Payload `/api/control` bisa 500 | Validasi tipe dict/string/numeric; 400 untuk input salah | selesai |
| Capture diagnostics tak terbatas | Reader thread + batas 4 KB + kill process group saat timeout/oversize | selesai |
| Race timer buzzer | Registry timer + generation; timer lama dibatalkan; event `auto-off` dicatat | selesai |
| `datetime.utcnow()` deprecated | Ganti ke `datetime.now(timezone.utc)` | selesai |
| State + event tidak atomik | Satu transaksi SQLite untuk update device + audit | selesai |
| Group/service user | `groupadd` eksplisit + `useradd --gid` + verifikasi | selesai |
| Env override tidak idempoten | Env file selalu ditulis ulang; permission diperbaiki tiap run | selesai |
| Health check false positive | Cek `systemctl is-active`, validasi JSON, cek listener | selesai |
| `find` menyentuh `.venv` | Dibatasi ke `iot_device/`, `templates/`, `static/` | selesai |
| `EnvironmentFile` opsional, tanpa UMask | Wajib + `UMask=0027` | selesai |
| `pip install` tidak konsisten | `pip install --no-index -r` tiap run (offline) | selesai |
| Uninstaller EOF/leftover | `read` aman, hapus drop-in + group, verifikasi rollback | selesai |
| Smoke/attack script | Retry, JSON parsing, exit code, hydra user list, umask 0600, scope guard | selesai |

## 3. Catatan penting

- Perintah ping memakai sintaks Linux (`ping -c 2 -W 2`). Di Windows output
  ping gagal, tetapi mekanisme injeksi tetap terbukti (`& whoami`).
- Pada target Pi, bukti yang diharapkan: `?host=127.0.0.1;id` → `uid=...`.
- `debug=False`; RCE hanya lewat V1, bukan debugger.
- Denyut daya Pi masih `0x50005`; uji serangan berat ditunda sampai PSU 5V/3A.

## 4. Langkah berikutnya

1. Salin repo ke Pi (`~/iiot-monitoring`), jalankan
   `sudo bash deploy/install-iot-device.sh`.
2. Verifikasi `/health`, login, kontrol device dari laptop attacker.
3. Jalankan `scripts/smoke-iot.sh` (T1–T13) dan `scripts/attack-iot.sh`;
   simpan bukti di `logs/phase-iot-2026-11-06/`.
4. Isi laporan Bagian A final dengan tangkapan layar + output perintah.
5. Lanjut fase Mitigasi (perbaiki V1–V8) dan Prevention (hardening systemd,
   nftables).
