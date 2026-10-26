"""Ambil GOOGLE_REFRESH_TOKEN untuk ER Agent (jalankan SEKALI di komputer lokal).

Persiapan:
  1. Google Cloud Console -> buat project -> aktifkan API:
     Calendar, Gmail, Drive, Sheets, Tasks, People, DAN Google Docs API.
  2. OAuth consent screen: isi data app, tambahkan email kamu sebagai Test user,
     lalu klik "Publish app" (In production) supaya refresh token tidak kedaluwarsa 7 hari.
  3. Credentials -> Create OAuth client ID -> tipe "Desktop app".
  4. Isi GOOGLE_CLIENT_ID dan GOOGLE_CLIENT_SECRET di .env, lalu jalankan:

         python google_auth.py

  5. Login di browser, izinkan semua akses. Salin GOOGLE_REFRESH_TOKEN yang tercetak
     ke .env dan ke Environment Variables Vercel, lalu redeploy.

Bila scope berubah (seperti file ini), cabut akses lama di myaccount.google.com/permissions
lalu jalankan ulang supaya Google menerbitkan refresh token baru dengan scope lengkap.
"""
import http.server
import os
import secrets
import threading
import urllib.parse
import webbrowser

import requests
from dotenv import load_dotenv

load_dotenv()

PORT = 8765
REDIRECT = f"http://localhost:{PORT}/"
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",       # baca, kirim, draft, arsip
    "https://www.googleapis.com/auth/drive.readonly",     # baca semua file
    "https://www.googleapis.com/auth/drive.file",         # BUAT file/folder/Docs baru (wajib untuk gdocs_create)
    "https://www.googleapis.com/auth/documents",          # baca & ubah Google Docs
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts.readonly",
]

result: dict = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" in q or "error" in q:
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>Selesai. Kamu boleh menutup tab ini dan kembali ke terminal.</h3>".encode())
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def main():
    cid, secret = os.getenv("GOOGLE_CLIENT_ID"), os.getenv("GOOGLE_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("Isi GOOGLE_CLIENT_ID dan GOOGLE_CLIENT_SECRET di .env dulu.")
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": REDIRECT, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state,
    })
    server = http.server.HTTPServer(("localhost", PORT), Handler)
    print("Membuka browser untuk login Google...\nJika tidak terbuka, buka URL ini manual:\n\n" + url + "\n")
    webbrowser.open(url)
    server.serve_forever()

    if result.get("state") != state or "code" not in result:
        raise SystemExit(f"Gagal: {result.get('error', 'state tidak cocok')}")
    r = requests.post("https://oauth2.googleapis.com/token", timeout=20, data={
        "code": result["code"], "client_id": cid, "client_secret": secret,
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
    })
    j = r.json()
    if "refresh_token" not in j:
        raise SystemExit(f"Tidak ada refresh_token. Respons Google: {j}\n"
                         "Cabut akses app di myaccount.google.com/permissions lalu ulangi.")
    print("\nBerhasil. Tambahkan ke .env dan ke Vercel:\n")
    print(f"GOOGLE_REFRESH_TOKEN={j['refresh_token']}\n")


if __name__ == "__main__":
    main()