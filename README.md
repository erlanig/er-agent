# Bedrock Chat

Chatbot ala ChatGPT berbasis Flask, memakai Amazon Bedrock (Mantle), mendukung tool calling,
dan siap dideploy ke Vercel.

- **GPT-OSS 120B / 20B** lewat endpoint OpenAI-compatible (`/v1/chat/completions`)
- **Claude Sonnet 4.6 / Haiku 4.5** lewat endpoint Anthropic Messages (`/anthropic/v1/messages`)
- Tool bawaan: waktu, kalkulator, fetch URL, dan GitHub Actions (lihat run dan trigger workflow)
- Streaming jawaban, riwayat obrolan tersimpan di browser, login password sederhana

```
bedrock-chat/
├── app.py               # Flask: route, auth, endpoint streaming
├── providers.py         # loop model + tool untuk GPT-OSS dan Claude
├── tools.py             # registry tool (tambah tool baru di sini)
├── templates/index.html # seluruh UI
├── requirements.txt
├── vercel.json
├── .python-version
├── .env.example
└── .github/workflows/demo.yml   # workflow contoh untuk uji tool CI/CD
```

---

## Langkah 0 — Amankan API key (penting)

API key yang pernah ditempel ke chat atau tempat umum harus dianggap bocor.

1. Buka AWS Console, region **ap-southeast-3**, menu **Amazon Bedrock → API keys**.
2. Hapus/revoke key lama, lalu buat key baru. Pilih masa berlaku pendek jika memungkinkan.
3. Jangan pernah menaruh key di kode atau di Git. File `.env` sudah ada di `.gitignore`.

## Langkah 1 — Siapkan alat

- Python 3.12 (`python --version`)
- Git dan akun GitHub
- Akun Vercel (login dengan GitHub paling mudah)
- Opsional: Node.js agar bisa memakai Vercel CLI (`npm i -g vercel`)

## Langkah 2 — Jalankan lokal

```bash
cd bedrock-chat
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # Windows: copy .env.example .env
# edit .env: isi BEDROCK_API_KEY (key baru), BEDROCK_BASE_URL, APP_PASSWORD

python app.py
```

Buka http://localhost:5000, masukkan `APP_PASSWORD`, lalu coba salah satu saran di layar awal.

## Langkah 3 — Uji akses Bedrock dengan curl (sebelum deploy)

Ini cara tercepat membedakan masalah key/region/model dari masalah kode.

```bash
export BEDROCK_API_KEY="key-baru-kamu"
export BASE="https://bedrock-mantle.ap-southeast-3.api.aws"

# 1) Daftar model yang tersedia di region ini (pakai ID persis dari sini)
curl -s "$BASE/v1/models" -H "Authorization: Bearer $BEDROCK_API_KEY"

# 2) GPT-OSS
curl -s "$BASE/v1/chat/completions" \
  -H "Authorization: Bearer $BEDROCK_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"openai.gpt-oss-120b","messages":[{"role":"user","content":"Halo"}]}'

# 3) Claude
curl -s "$BASE/anthropic/v1/messages" \
  -H "Authorization: Bearer $BEDROCK_API_KEY" \
  -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
  -d '{"model":"anthropic.claude-sonnet-4-6","max_tokens":100,"messages":[{"role":"user","content":"Halo"}]}'
```

Jika ID model di region kamu berbeda, isi `MODEL_GPT_OSS_120B`, `MODEL_CLAUDE_SONNET`, dan
sejenisnya di `.env` (dan di Vercel nanti). Tidak perlu mengubah kode.

Catatan: `BEDROCK_RUNTIME_BASE_URL` (`bedrock-runtime.../openai/v1`) tidak dipakai di proyek ini.
Endpoint Mantle melayani GPT-OSS dan Claude sekaligus, jadi satu `BEDROCK_BASE_URL` cukup.

## Langkah 4 — Push ke GitHub

```bash
git init
git add .
git commit -m "Bedrock Chat"
git branch -M main
# buat repo kosong (private disarankan) di github.com, lalu:
git remote add origin https://github.com/USERNAME/bedrock-chat.git
git push -u origin main
```

Pastikan `.env` tidak ikut ter-commit (`git status` tidak boleh menampilkannya).

## Langkah 5 — Deploy ke Vercel

### Lewat dashboard (paling mudah)

1. vercel.com/new → **Import** repo `bedrock-chat`.
2. Framework Preset biarkan **Other**/terdeteksi Flask otomatis. Root Directory: `./`.
3. Buka **Environment Variables**, tambahkan (untuk Production, Preview, Development):

   | Nama | Nilai |
   |---|---|
   | `BEDROCK_API_KEY` | key baru |
   | `BEDROCK_BASE_URL` | `https://bedrock-mantle.ap-southeast-3.api.aws/v1` |
   | `APP_PASSWORD` | password kuat |
   | `GITHUB_TOKEN` | (opsional, untuk tool CI/CD) |
   | `GITHUB_DEFAULT_REPO` | (opsional) `username/nama-repo` |
   | `GITHUB_ALLOWED_REPOS` | (opsional) daftar repo yang boleh disentuh, pisah koma |

4. Klik **Deploy**. Setiap `git push` ke `main` setelah ini otomatis redeploy.

### Lewat CLI

```bash
vercel login
vercel link
vercel env add BEDROCK_API_KEY      # ulangi untuk variabel lain
vercel --prod
```

## Langkah 6 — Cek hasil deploy

1. Buka `https://NAMA-PROJECT.vercel.app/api/health` → harus `{"ok":true}`.
2. Buka halaman utama, login, kirim "Jam berapa sekarang di Tokyo?" → harus muncul chip tool `get_current_time`.
3. Ganti model lewat dropdown dan ulangi.

## Aktifkan tool CI/CD

1. GitHub → Settings → Developer settings → **Fine-grained personal access token**.
   Pilih hanya repo target, permission **Actions: Read and write** (cukup, jangan beri yang lain).
2. Isi `GITHUB_TOKEN`, `GITHUB_DEFAULT_REPO`, dan `GITHUB_ALLOWED_REPOS` di Vercel, lalu redeploy.
3. Di UI, nyalakan toggle **Izinkan aksi CI/CD**. Tanpa toggle ini model hanya melihat tool baca saja.
4. Uji dengan workflow contoh: "Jalankan workflow demo.yml di branch main dengan pesan 'tes'", lalu
   "Cek statusnya".

Pengaman yang sudah ada: aksi tulis butuh toggle, repo dibatasi allowlist, nama repo/workflow divalidasi,
dan seluruh aplikasi dikunci password. Jangan menonaktifkan semuanya sekaligus.

## Menambah tool baru

Tambahkan di `tools.py`:

```python
@tool(
    "nama_tool",
    "Deskripsi singkat kapan tool ini dipakai.",
    {"param": {"type": "string", "description": "..."}},
    ["param"],
    dangerous=False,   # True bila mengubah sesuatu di luar
)
def nama_tool(param: str):
    return {"hasil": param.upper()}
```

Tidak ada kode lain yang perlu diubah; kedua model otomatis melihatnya.

## Troubleshooting

| Gejala | Penyebab umum | Solusi |
|---|---|---|
| `401` dari Bedrock | Key salah/kedaluwarsa/beda region | Buat key baru di region yang sama dengan `BEDROCK_BASE_URL` |
| `404`/model not found | ID model tidak ada di region itu | Cek `GET /v1/models`, set `MODEL_*` |
| `AccessDenied` | Model belum diaktifkan untuk akun, atau IAM tanpa `bedrock-mantle:CallWithBearerToken` | Aktifkan akses model di konsol, cek izin |
| UI muncul "APP_PASSWORD belum diset" | Env var belum ada di Vercel | Tambahkan lalu **Redeploy** (env baru hanya berlaku di deployment baru) |
| Jawaban muncul sekaligus, bukan mengalir | Platform mem-buffer respons | Tetap berfungsi; hanya efek visual |
| `FUNCTION_INVOCATION_TIMEOUT` | Proses lebih lama dari `maxDuration` | Naikkan di `vercel.json` (batas tergantung plan) |
| Halaman 401 dari Vercel | Deployment Protection aktif | Project Settings → Deployment Protection, sesuaikan |

## Catatan keamanan

- Password aplikasi disimpan di `localStorage` browser dan dikirim lewat header; cukup untuk penggunaan
  pribadi/tim kecil. Untuk publik, ganti ke login sungguhan (mis. Vercel Authentication atau OAuth).
- Riwayat obrolan hanya ada di browser masing-masing, tidak di server.
- Tool `fetch_url` hanya mengizinkan host publik dan tidak mengikuti redirect, untuk mencegah SSRF dasar.
