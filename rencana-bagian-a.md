# Rencana Bagian A — Perangkat IoT Korban

- Status: **disetujui 2026-11-06**
- Proyek: Tugas Perangkat IoT & Pengujian Serangan
- Target: `kali` / kali-raspberrypi / 192.168.100.90
- Keputusan: aktuator **virtual dulu**, deploy **/opt + systemd**

Dokumen ini adalah rencana yang disetujui sebelum pengerjaan. Artefak build,
deploy, script, dan log mengikuti path di bawah (root repo `iiot-monitoring/`).
Dokumen lanjutan (mitigasi & prevention) disimpan di direktori yang sama.

---

## A.0 Tujuan & batasan

**Tujuan:** menghasilkan satu perangkat IoT nyata dengan web server yang berjalan
di `kali` (192.168.100.90), cukup realistis untuk 3 skenario tugas
(serangan -> mitigasi -> prevention), dan **sengaja memiliki celah terkontrol**
untuk didemokan.

**Batasan:**

- Target hanya `192.168.100.90:8080`. Tidak menyentuh host lain.
- Perangkat IoT asli di lab (BARDI bulb `.70`, doorbell `.76`, EZVIZ `.80`,
  Nest `.77`, strip `.82`) **tidak** jadi target tanpa izin pemilik — inventory
  menandainya `monitor_only`.
- Tidak ada akses internet, tidak ada reverse shell, tidak ada perubahan di
  `iiot5g`.
- Semua kode & artefak disimpan di `/home/derry/iiot-monitoring/`.

## A.1 Arsitektur target

```
Laptop attacker (192.168.100.209)          kali Pi (192.168.100.90)
+----------------------------+             +------------------------------+
| hydra, nmap, curl, tcpdump |--HTTP:8080->| Flask app  iot-device        |
+----------------------------+             | user: iotdev (no sudo)       |
                                           | DB: SQLite (users, devices)  |
                                           | Aktuator: virtual LED/relay/ |
                                           | buzzer + sensor suhu         |
                                           +------------------------------+
```

- Bind **specific** ke `192.168.100.90:8080` (bukan `0.0.0.0`) supaya blast
  radius jelas.
- Service berjalan sebagai user sistem baru **`iotdev`** (tanpa sudo, shell
  `/usr/sbin/nologin`), dijalankan `systemd`.
- Aktuator **virtual** dulu (state di SQLite) + adapter GPIO opsional, karena
  `gpiozero/RPi.GPIO` belum terpasang dan Pi sedang undervoltage.

## A.2 Struktur file yang akan dibuat

```
build/iot-device/
+-- app.py                     # entrypoint Flask
+-- requirements.txt           # flask==3.1.x (sudah tersedia)
+-- seed.py                    # inisialisasi DB + user admin/admin + 4 device
+-- iot_device/
|   +-- __init__.py
|   +-- db.py                  # koneksi SQLite, schema, query
|   +-- auth.py                # login/session (cookie lemah = vuln V4)
|   +-- devices.py             # model device + adapter GPIO (stub, opsional)
|   +-- routes.py              # dashboard, API, diagnostics (RCE = vuln V1)
+-- templates/
|   +-- base.html
|   +-- login.html
|   +-- dashboard.html
+-- static/
    +-- style.css
    +-- app.js

deploy/
+-- iot-device.service         # unit systemd baseline (tanpa hardening, disengaja)
+-- install-iot-device.sh      # installer idempotent
+-- uninstall-iot-device.sh    # rollback bersih

scripts/
+-- smoke-iot.sh               # uji fungsi normal
+-- attack-iot.sh              # reproduksi serangan (untuk bukti Bagian A)

logs/phase-iot-2026-11-06/
+-- preflight/                 # snapshot kondisi awal
+-- recon/                     # nmap XML/txt
+-- attack/                    # hydra output, request/response, pcap
+-- evidence/                  # dump DB, log app, screenshot

reports/
+-- bagian-a-*.md              # dokumentasi hasil Bagian A
```

## A.3 Spesifikasi aplikasi

> Desain detail (endpoint, schema, kontrak API, isi tiap celah):
> `plans/iot-attack/rencana-aplikasi-iot.md`.

| Endpoint | Method | Auth | Fungsi |
|----------|--------|------|--------|
| `/` | GET | ya | Dashboard 4 device + status |
| `/login` | GET/POST | tidak | Login form (rentan) |
| `/logout` | GET | ya | Hapus session |
| `/api/status` | GET | ya | JSON semua device + sensor |
| `/api/control` | POST | ya | Ubah state LED/relay/buzzer |
| `/api/diagnostics` | GET | ya | **Celah utama:** `ping` ke host dari parameter |
| `/api/events` | GET | ya | Riwayat aksi (audit) |
| `/health` | GET | tidak | `{"status":"ok"}` untuk monitoring |

**Data model:**

```sql
users(id, username, password, role)              -- plaintext (V2)
devices(id, name, type, state, value, updated_at)
events(id, ts, device_id, action, source_ip)      -- audit trail
```

Device virtual: `led-1` (on/off + brightness), `relay-1`, `buzzer-1`,
`sensor-suhu` (nilai acak 24-32 C, update tiap 10 dtk).

## A.4 Kerentanan yang sengaja ditanam

| ID | Kerentanan | Lokasi | CWE | Peran |
|----|-----------|--------|-----|-------|
| V1 | **Command injection** (`shell=True`, input tak disanitasi) | `/api/diagnostics?host=` | CWE-78 | **Serangan utama** |
| V2 | Password default `admin/admin`, disimpan plaintext | `seed.py` | CWE-798/256 | Initial access, rantai serangan |
| V3 | Tidak ada rate-limit / lockout | `/login` | CWE-307 | Memungkinkan brute force (hydra) |
| V4 | Cookie session tanpa `HttpOnly`/`Secure`, `SECRET_KEY` hardcoded | `config.py` | CWE-1004/798 | Mempermudah takeover session |
| V5 | Tanpa CSRF token | form control | CWE-352 | Aksi device bisa dipicu dari luar |
| V6 | Service tanpa hardening systemd | `.service` | CWE-250 | Kontras untuk fase Prevention |
| V7 | Flask dev server dipakai sebagai server produksi | `app.py` | CWE-1104 | Realisme + Prevention |
| V8 | Information disclosure (versi runtime, hostname, firmware) | `/about` | CWE-200 | Recon |

Catatan: `debug=False` tetap dipakai — RCE-nya terkontrol lewat V1, bukan
debugger.

## A.5 Rencana eksekusi (urutan kerja)

1. **Pre-flight** (read-only, +/-5 mnt)
   - Catat snapshot: `ss -tlnp`, `dpkg -l`, `systemctl list-units`, `df -h`
     -> simpan di `config-snapshot/`.
   - Verifikasi port 8080 kosong, disk >=2G, `python3 -m venv` OK, `sqlite3` ada.
   - Cek ulang `vcgencmd get_throttled` (harus `0x0`; saat ini `0x50005`).

2. **Bangun app** (+/-60-90 mnt)
   - Tulis Flask app + template + seed DB di `build/iot-device/`.
   - Uji lokal di laptop dulu (`python3 app.py`), tanpa menyentuh Pi.

3. **Installer & deploy ke kali** (+/-20 mnt)
   - Script membuat user `iotdev`, direktori `/opt/iot-device` +
     `/var/lib/iot-device`, venv, unit systemd, lalu `enable --now`.
   - Idempotent: dijalankan dua kali tidak error.

4. **Smoke test** (+/-15 mnt)
   - `curl /health`, login `admin/admin`, ubah LED, baca `/api/status`, jalankan
     diagnostics `127.0.0.1`.
   - Pastikan service auto-restart (`systemctl restart`, reboot opsional nanti).

5. **Verifikasi celah (bagian akhir A)** (+/-20 mnt)
   - Buktikan V1 reproducible: `host=127.0.0.1; id` -> output `uid=...`.
   - Buktikan V2/V3: login default + hydra menemukan kredensial.
   - Simpan bukti ke `logs/phase-iot-2026-11-06/`.
   - **Tidak** menjalankan DoS berat di tahap ini (Pi undervoltage).

6. **Dokumentasi A** (+/-20 mnt)
   - `reports/bagian-a-*.md`: arsitektur, daftar celah, cara jalan, cara rollback.

## A.6 Kriteria selesai Bagian A

- [ ] `http://192.168.100.90:8080` dapat diakses dari laptop attacker.
- [ ] Login `admin/admin` berhasil; dashboard menampilkan 4 device.
- [ ] Perubahan state device tersimpan dan muncul di `/api/status`.
- [ ] `/api/diagnostics?host=127.0.0.1` mengembalikan output `ping`.
- [ ] Injeksi `; id` terbukti mengeksekusi perintah (bukti RCE).
- [ ] Hydra berhasil menemukan kredensial default.
- [ ] Service auto-start saat boot dan auto-restart saat crash.
- [ ] `uninstall-iot-device.sh` mengembalikan Pi ke kondisi awal.
- [ ] Semua bukti tersimpan di `logs/phase-iot-2026-11-06/`.

## A.7 Risiko & pengamanan

| Risiko | Kendali |
|--------|---------|
| Pi freeze saat demo (undervoltage 1365x) | Ganti PSU 5V/3A sebelum demo; tunda uji DoS sampai PSU beres |
| RCE disalahgunakan ke host lain | Bind hanya `192.168.100.90:8080`; user `iotdev` tanpa sudo; dokumentasi scope |
| SSH putus-putus (packet loss 50%, rute ganda `ppp0`) | Retry + `BatchMode`; jalankan deploy dalam satu sesi `bash -s` |
| Salah target perangkat lab | Tidak menyentuh `.69/.70/.76/.77/.80/.82`; hanya Pi |
| Perubahan tak bisa dibalik | Snapshot + script uninstall idempotent |

## A.8 Estimasi

| Tahap | Waktu |
|-------|-------|
| Pre-flight + snapshot | 5 mnt |
| Bangun app | 60-90 mnt |
| Deploy + systemd | 20 mnt |
| Smoke + verifikasi celah | 35 mnt |
| Dokumentasi | 20 mnt |
| **Total** | **+/-2,5 jam** |

## A.9 Hasil pre-flight

**Selesai 2026-11-06 03:00 UTC.** Ringkasan: 20 item diperiksa, 16 PASS,
3 WARN (SSH sempat putus, `jq` tidak ada, `ufw` tidak ada), 1 FAIL (daya:
`get_throttled=0x50005`, 1365 event undervoltage). Tidak ada blocker untuk
build/deploy; PSU 5V/3A diperlukan sebelum demo serangan berat.

Detail: `logs/phase-iot-2026-11-06/preflight/summary.md`
Raw: `logs/phase-iot-2026-11-06/preflight/kali-raw.txt`
