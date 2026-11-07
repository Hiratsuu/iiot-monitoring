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
  seed.py                  # schema + user + device (one-shot, --force re-adds)
  iot_device/              # config, db, auth, devices, routes, seed_data
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
rencana-*.md               # dokumen rencana (root repo)
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
| `/events` | GET | admin | riwayat 50 event |
| `/about` | GET | admin | identitas perangkat (V8) |
| `/api/status` | GET | ya | state semua device |
| `/api/control` | POST | ya | ubah state device (V5) |
| `/api/diagnostics` | GET | ya | output ping (**V1**) |
| `/api/events` | GET | admin | JSON riwayat |
| `/api/about` | GET | admin | JSON identitas (V8) |

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
# rsync is preferred: it keeps LF/modes and skips local Python artefacts
rsync -av --exclude '.venv' --exclude 'data' \
  build deploy scripts README.md kali@192.168.100.90:~/iiot-monitoring/
ssh kali@192.168.100.90 \
  'cd ~/iiot-monitoring && chmod +x deploy/*.sh scripts/*.sh && sudo bash deploy/install-iot-device.sh'
```

Tanpa `rsync`, gunakan `scp -p -r` lalu `chmod +x` di Pi. Jangan mengirim
`build/iot-device/.venv` (artefak Windows, ~27 MB) atau folder `data/`.

Installer: preflight memeriksa `BIND_ADDR` ada di interface, port belum
dipakai proses lain, dan tidak ada symlink pada path kunci (`APP_DIR`,
`DATA_DIR`, `CONF_DIR`, unit, env, `iot.db`/`-wal`/`-shm`, `.venv`). Lalu
service distop (gagal stop = abort), group/user dibuat sebelum chown, DB
dibackup WAL-aware + integrity check ke `/var/backups/iot-device/`, file
di-stage dan dipindah, venv/Flask diverifikasi, env ditulis hanya jika belum
ada atau dioverride (env lama dibackup), seed hanya untuk DB baru, unit
dipasang, dan health check memvalidasi JSON `/health`. Jika gagal, trap
menghentikan/menonaktifkan unit dan menyebut lokasi backup.

Aman dijalankan ulang: env kustom **tidak** tertimpa dan DB yang sudah ada
tidak di-seed ulang. Override eksplisit menulis ulang env; `FORCE_ENV=1`
memaksa tulis ulang tetapi tetap memakai nilai yang sudah ada untuk key yang
tidak dioverride (bukan reset ke default). Key yang hilang/kosong di env lama
ditulis ulang dengan default. Nilai env ditulis apa adanya: hindari spasi di
awal/akhir atau tanda kutip pada `SECRET_KEY`/`FIRMWARE`. Backup installer
tidak dirotasi otomatis; bersihkan `/var/backups/iot-device/` secara berkala.

Override opsional:
`BIND_ADDR=... PORT=... GPIO_ENABLED=1 sudo -E bash deploy/install-iot-device.sh`.

Cek read-only tanpa root (mis. dari laptop; default `BIND_ADDR` Pi tidak ada
di laptop):
`BIND_ADDR=127.0.0.1 PORT=8099 bash deploy/install-iot-device.sh --preflight-only`.

## Uji

```bash
# dari laptop attacker
./scripts/smoke-iot.sh http://192.168.100.90:8080

# reproduksi rantai serangan (butuh konfirmasi)
./scripts/attack-iot.sh
```

Smoke test mencakup **T1–T13** (T7 persistence lintas restart aktif dengan
`WITH_RESTART=1`, T12/T13 butuh systemd/journal di host target), termasuk
injeksi `?host=127.0.0.1;id`. Skrip serangan menjalankan nmap, hydra
(kredensial default), lalu bukti injeksi dan ekstraksi DB. Hasil recon
tersimpan di `logs/phase-iot-2026-11-06/recon/`, bukti serangan di
`logs/phase-iot-2026-11-06/attack/` (permission `0600`/`0700`).

## Rollback

```bash
sudo bash deploy/uninstall-iot-device.sh            # simpan database
sudo bash deploy/uninstall-iot-device.sh --purge    # backup lalu hapus data
```

`--purge` membackup `iot.db` (+ `-wal`/`-shm` bila ada) dan env ke
`/root/iot-device-backup-<timestamp>.tar.gz` sebelum `rm -rf`; backup gagal =
purge dibatalkan. Uninstaller menolak berjalan jika service masih aktif atau
path kunci berupa symlink, membackup env juga pada uninstall biasa (DB
disimpan), lalu memverifikasi service berhenti, user/group hilang,
unit/drop-in/direktori terhapus, dan tidak ada listener tersisa di port
aplikasi (listener tak terduga = ERROR, bukan WARNING). `--purge` menghapus
data aplikasi tetapi **tidak** menghapus backup installer di
`/var/backups/iot-device/`; hapus manual jika perlu.

Restore dari backup `--purge` (arsip berisi `iot.db` dan `iot-device.env` di
root arsip):

```bash
sudo mkdir -p /tmp/iot-restore
sudo tar -xzf /root/iot-device-backup-<timestamp>.tar.gz -C /tmp/iot-restore
sudo install -d -m 0750 /var/lib/iot-device /etc/iot-device
sudo install -m 0640 -o iotdev -g iotdev /tmp/iot-restore/iot.db /var/lib/iot-device/iot.db
sudo install -m 0640 -o root -g iotdev /tmp/iot-restore/iot-device.env /etc/iot-device/iot-device.env
```

Jika arsip juga berisi `iot.db-wal`/`iot.db-shm`, salin keduanya ke
`/var/lib/iot-device/` dengan owner `iotdev`. Setelah restore, jalankan
installer lagi untuk membuat user/group/unit.

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
- [x] Perbaikan P0/P1 hasil code review
- [ ] Deploy ke Pi (butuh akses SSH dari mesin ini)
- [ ] Bukti serangan tersimpan di `logs/`
