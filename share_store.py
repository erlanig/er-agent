import os
import re
import json
import time
import uuid
import secrets

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_SHARES_DIR = os.path.join(_BASE_DIR, "data", "shares")

_PASSCODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def init_store():
    os.makedirs(_SHARES_DIR, exist_ok=True)


def _slugify(text: str) -> str:
    s = (text or "").lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:40] or "web-page"


def generate_passcode(length: int = 6) -> str:
    return "".join(secrets.choice(_PASSCODE_CHARS) for _ in range(length))


def create_share(
    html: str,
    title: str = "",
    chat_id: str = "",
    project_id: str = "",
    base_url: str = ""
) -> dict:
    init_store()
    share_id = str(uuid.uuid4())
    passcode = generate_passcode(6)
    clean_title = (title or "Halaman Web ER Agent").strip()[:100]
    slug = _slugify(clean_title)
    clean_chat = re.sub(r"[^\w\-]", "", str(chat_id or "general"))[:32]
    clean_proj = re.sub(r"[^\w\-]", "", str(project_id or "default"))[:32]

    now = int(time.time())
    data = {
        "id": share_id,
        "passcode": passcode,
        "title": clean_title,
        "slug": slug,
        "chat_id": clean_chat,
        "project_id": clean_proj,
        "html": html or "<!DOCTYPE html><html><body><p>Halaman kosong</p></body></html>",
        "created_at": now,
        "views": 0,
    }

    file_path = os.path.join(_SHARES_DIR, f"{share_id}.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    query_str = f"project={clean_proj}&chat={clean_chat}&title={slug}&ref=er-agent"
    path = f"/share/html/{share_id}?{query_str}"
    full_url = f"{base_url.rstrip('/')}{path}" if base_url else path

    return {
        "id": share_id,
        "passcode": passcode,
        "title": clean_title,
        "slug": slug,
        "chat_id": clean_chat,
        "project_id": clean_proj,
        "path": path,
        "full_url": full_url,
        "created_at": now
    }


def get_share(share_id: str) -> dict | None:
    if not re.fullmatch(r"[\w\-]{8,64}", str(share_id or "")):
        return None
    file_path = os.path.join(_SHARES_DIR, f"{share_id}.json")
    if not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def verify_passcode(share_id: str, candidate: str) -> bool:
    data = get_share(share_id)
    if not data:
        return False
    expected = str(data.get("passcode", "")).strip().upper()
    given = str(candidate or "").strip().upper()
    if not expected or not given:
        return False
    if secrets.compare_digest(expected, given):
        try:
            data["views"] = int(data.get("views", 0)) + 1
            file_path = os.path.join(_SHARES_DIR, f"{share_id}.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return True
    return False
