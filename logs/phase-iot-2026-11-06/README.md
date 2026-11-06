# Logs — Phase IoT 2026-11-06

Artefak bukti per fase. Direktori `recon/`, `attack/`, dan `evidence/` tidak
di-commit (lihat `.gitignore`); isi `preflight/` di-commit karena merupakan
snapshot kondisi awal.

| Direktori | Isi |
|-----------|-----|
| `preflight/` | `kali-raw.txt` + `summary.md` (20 item pemeriksaan) |
| `recon/` | output nmap, banner/header, health snapshot |
| `attack/` | hydra, cookie jar, request/response injeksi, ekstraksi DB |
| `evidence/` | tangkapan layar, log journald, salinan bukti final |

`tcpdump`/pcap tidak dijalankan otomatis oleh `attack-iot.sh`; tambahkan
manual bila laporan membutuhkan capture paket. Nama file memakai timestamp UTC
agar tidak saling menimpa.

Salinan asli `summary.md` dan `kali-raw.txt` juga tersimpan di root repo;
versi di `preflight/` adalah salinan kanonik untuk laporan.
