import os
import requests
from dotenv import load_dotenv

load_dotenv()
cid = os.getenv("GOOGLE_CLIENT_ID", "")
sec = os.getenv("GOOGLE_CLIENT_SECRET", "")
tok = os.getenv("GOOGLE_REFRESH_TOKEN", "")

print("client_id berakhiran .apps.googleusercontent.com:", cid.strip().endswith(".apps.googleusercontent.com"))
print("secret diawali GOCSPX-:", sec.strip().startswith("GOCSPX-"))
print("token diawali 1//0:", tok.strip().startswith("1//0"), "| panjang:", len(tok))
print("ada spasi/kutip di token:", tok != tok.strip() or '"' in tok or "'" in tok)

r = requests.post("https://oauth2.googleapis.com/token", timeout=15, data={
    "client_id": cid.strip(), "client_secret": sec.strip(),
    "refresh_token": tok.strip(), "grant_type": "refresh_token",
})
j = r.json()
print("HASIL:", "OK, access_token diterima" if "access_token" in j else j)