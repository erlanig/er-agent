import os
import re
import json
import time
import uuid
import secrets
import tempfile
import db

_PASSCODE_CHARS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_cached_dir: str | None = None


def _get_shares_dir() -> str:
    global _cached_dir
    if _cached_dir:
        return _cached_dir
    local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "shares")
    try:
        os.makedirs(local_dir, exist_ok=True)
        test_file = os.path.join(local_dir, ".test")
        with open(test_file, "w") as f:
            f.write("1")
        os.remove(test_file)
        _cached_dir = local_dir
        return local_dir
    except Exception:
        tmp_dir = os.path.join(tempfile.gettempdir(), "er_shares")
        os.makedirs(tmp_dir, exist_ok=True)
        _cached_dir = tmp_dir
        return tmp_dir


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

    if db.enabled():
        try:
            db.upsert_share(share_id, passcode, clean_title, data)
        except Exception:
            pass

    # Selalu simpan juga di storage lokal/tmp sebagai fallback
    try:
        folder = _get_shares_dir()
        file_path = os.path.join(folder, f"{share_id}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    query_str = f"project={clean_proj}&chat={clean_chat}&title={slug}&ref=er-agent"
    path = f"/share/html/{share_id}?{query_str}"
    default_base = os.getenv("APP_BASE_URL") or "https://er-agent-inky.vercel.app"
    effective_base = (base_url or default_base).rstrip("/")
    full_url = f"{effective_base}{path}"

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

    # Cek database Postgres lebih dulu bila aktif
    if db.enabled():
        try:
            db_res = db.get_share(share_id)
            if db_res:
                return db_res
        except Exception:
            pass

    # Fallback ke filesystem lokal/tmp
    try:
        folder = _get_shares_dir()
        file_path = os.path.join(folder, f"{share_id}.json")
        if os.path.isfile(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
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
            if db.enabled():
                try:
                    db.upsert_share(share_id, expected, data.get("title", ""), data)
                except Exception:
                    pass
            folder = _get_shares_dir()
            file_path = os.path.join(folder, f"{share_id}.json")
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return True
    return False
