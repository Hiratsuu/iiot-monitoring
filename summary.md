# Pre-flight Bagian A — kali-raspberrypi

- Tanggal: 2026-11-06 03:00 UTC
- Target: 192.168.100.90 (eth0)
- Metode: SSH read-only (`BatchMode=yes`), tanpa perubahan sistem
- Raw output: `kali-raw.txt`

## Ringkasan

| # | Item | Syarat | Hasil | Status |
|---|------|--------|-------|--------|
| 1 | Konektivitas SSH | stabil | tersambung; sempat 1x `No route to host` + 50% packet loss | WARN |
| 2 | OS / kernel | Linux aarch64 | Kali Rolling 2026.2, kernel 6.12.34+rpt | PASS |
| 3 | Failed systemd units | 0 | 0 | PASS |
| 4 | RAM tersedia | >= 256 MiB | 675 MiB available (231 MiB used) | PASS |
| 5 | Disk bebas | >= 2 GB | 9.5 GB (64% terpakai) | PASS |
| 6 | Port 8080 | kosong | free | PASS |
| 7 | Python | >= 3.9 | 3.14.7 | PASS |
| 8 | Flask | tersedia | 3.1.3 | PASS |
| 9 | SQLite (Python) | tersedia | 3.46.1 | PASS |
| 10 | venv | tersedia | OK | PASS |
| 11 | `sqlite3` CLI | tersedia | `/usr/bin/sqlite3` | PASS |
| 12 | Tool serangan | hydra/nmap/tcpdump/curl/nikto | semua ada | PASS |
| 13 | `jq` | opsional | MISSING | WARN |
| 14 | sudo NOPASSWD | ya | `(ALL) NOPASSWD: ALL` | PASS |
| 15 | `/opt` writable | ya | writable via sudo | PASS |
| 16 | User `iotdev` | belum ada | belum ada (sesuai rencana) | PASS |
| 17 | Firewall | teridentifikasi | `ufw` tidak ada; `nft` + `iptables` ada | WARN |
| 18 | Time sync | aktif | NTP active, UTC | PASS |
| 19 | Suhu SoC | < 80 C | 60.1 C | PASS |
| 20 | **Power / throttling** | `get_throttled=0x0` | **`0x50005`**, 1365 event undervoltage | **FAIL** |

## Detail penting

### Power (blocker untuk demo akhir)
- `vcgencmd get_throttled` = `0x50005`
  - bit 0: under-voltage **sedang terjadi**
  - bit 2: throttling **sedang terjadi**
  - bit 16/18: under-voltage & throttling pernah terjadi
- `dmesg`: 1365 baris `Undervoltage detected`.
- Suhu 60.1 C (normal).
- **Dampak:** Pi bisa freeze/reboot saat beban naik (termasuk saat demo serangan
  atau DoS). Build & deploy masih bisa jalan, tetapi demo akhir sebaiknya
  menunggu PSU 5V/3A yang memadai.

### Jaringan
- `eth0` = 192.168.100.90/24, gateway 192.168.100.1.
- `wlan0` tidak muncul (down/tidak dipakai) — tidak diperlukan.
- ARP table memperlihatkan perangkat lab (BARDI `.69/.70/.76/.82`, EZVIZ `.80`,
  Nest `.77`, Pi lain `.55/.73/.88`). **Tidak diprobe**; hanya bukti bahwa
  segmen LAN sama. Target tetap hanya `.90:8080`.

### Tooling
- Tersedia: hydra, nmap, tcpdump, curl, sqlite3, nikto, git.
- Tidak ada: `jq`. Script akan memakai `python3` untuk parsing JSON, jadi tidak
  perlu install apa pun.
- `nft`/`iptables` tersedia; `ufw` tidak. Fase prevention akan memakai nftables.

### Catatan SSH
- Satu percobaan gagal (`No route to host`) lalu berhasil. Penyebab di sisi
  laptop: packet loss 50% di `wlp147s0` dan rute ganda `192.168.100.0/24`
  lewat `ppp0` + `wlp147s0`. Untuk deploy, perintah dijalankan dalam satu sesi
  `ssh 'bash -s'` agar tidak terputus di tengah.

## Kesimpulan

**Siap lanjut ke tahap 2 (bangun aplikasi).** Tidak ada blocker teknis untuk
build/deploy. Satu-satunya blocker adalah **stabilitas daya** untuk demo akhir;
perlu PSU 5V/3A sebelum menjalankan uji serangan berat.

## Tindak lanjut

1. Ganti/cek PSU Pi, lalu target `get_throttled=0x0`.
2. Bangun app Flask di `build/iot-device/` (uji lokal dulu).
3. Deploy ke `/opt` + systemd user `iotdev`.
