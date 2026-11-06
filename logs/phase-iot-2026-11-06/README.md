# Logs — Phase IoT 2026-11-06

Artefak bukti per fase. Direktori `recon/`, `attack/`, dan `evidence/` tidak
di-commit (lihat `.gitignore`); isi `preflight/` di-commit karena merupakan
snapshot kondisi awal.

| Direktori | Isi |
|-----------|-----|
| `preflight/` | `kali-raw.txt` + `summary.md` (20 item pemeriksaan) |
| `recon/` | output nmap, banner/header |
| `attack/` | hydra, cookie jar, request/response injeksi, dump DB, pcap |
| `evidence/` | tangkapan layar, log journald, salinan bukti final |

Nama file skrip serangan memakai timestamp UTC agar tidak saling menimpa.
