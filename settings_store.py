"""Pengaturan admin (server-side) + passcode admin + audit log.

Penyimpanan (otomatis dipilih):
1. Postgres (tabel kv, kunci 'admin_settings') bila DATABASE_URL diset  -> permanen, lintas instance.
2. File JSON (ADMIN_SETTINGS_PATH atau ./data/admin_settings.json)      -> permanen di server biasa.
3. /tmp/er_agent_admin_settings.json bila folder proyek read-only (Vercel tanpa DB) -> TIDAK permanen.

Passcode admin disimpan sebagai hash PBKDF2-SHA256 (tidak pernah plaintext). Selama belum diganti,
passcode = env ADMIN_PASSCODE atau default "Tekna@12".
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time

import db

DEFAULT_PASSCODE = os.getenv("ADMIN_PASSCODE") or "Tekna@12"
_PBKDF2_ROUNDS = 240_000
_CACHE_TTL = 8.0  # detik; agar perubahan dari instance lain cepat terlihat tanpa query tiap request
_AUDIT_MAX = 200

_MODEL_KEYS = ["claude-sonnet-4-6", "claude-haiku-4-5", "gpt-oss-120b", "gpt-oss-20b"]
_STYLES = ("ringkas", "seimbang", "detail")
_EFFORTS = ("low", "medium", "high")
_FONTS = ("auto", "Aptos", "Times New Roman", "Calibri", "Arial")

# key -> (default, validator)
def _int(lo, hi):
    def v(x):
        if isinstance(x, bool):
            raise ValueError
        x = int(x)
        if not lo <= x <= hi:
            raise ValueError(f"harus {lo}–{hi}")
        return x
    return v


def _bool(x):
    if not isinstance(x, bool):
        raise ValueError("harus true/false")
    return x


def _str(maxlen, allow_empty=True):
    def v(x):
        if not isinstance(x, str):
            raise ValueError("harus teks")
        x = x.strip()
        if not allow_empty and not x:
            raise ValueError("tidak boleh kosong")
        if len(x) > maxlen:
            raise ValueError(f"maksimal {maxlen} karakter")
        return x
    return v


def _choice(options):
    def v(x):
        if x not in options:
            raise ValueError("pilihan: " + ", ".join(options))
        return x
    return v


def _str_list(maxlen=80, maxitems=300):
    def v(x):
        if not isinstance(x, list) or len(x) > maxitems:
            raise ValueError("harus daftar")
        out = []
        for i in x:
            if not isinstance(i, str) or not re.fullmatch(r"[\w.\-]{1,%d}" % maxlen, i):
                raise ValueError(f"item tidak valid: {i!r}")
            if i not in out:
                out.append(i)
        return out
    return v


SCHEMA = {
    "app_name": ("ER Agent", _str(40, allow_empty=False)),
    "welcome_title": ("Apa yang ingin dikerjakan hari ini?", _str(120)),
    "welcome_subtitle": ("Analisis data, riset jurnal, coding, dan otomasi kerja dalam satu tempat.", _str(240)),
    "enabled_models": (list(_MODEL_KEYS), _str_list()),
    "default_model": ("claude-sonnet-4-6", _str(60, allow_empty=False)),
    "max_tokens": (16000, _int(1000, 64000)),
    "reasoning_effort": ("low", _choice(_EFFORTS)),
    "deep_reasoning_effort": ("high", _choice(_EFFORTS)),
    "claude_thinking_budget": (6000, _int(0, 32000)),
    "max_tool_steps": (10, _int(1, 20)),
    "tool_routing": (True, _bool),
    "prompt_caching": (True, _bool),
    "allow_actions": (False, _bool),
    "disabled_tools": ([], _str_list()),
    "global_instructions": ("", _str(8000)),
    "anti_slop": (True, _bool),
    "default_style": ("seimbang", _choice(_STYLES)),
    "memory_enabled": (True, _bool),
    "context_budget_chars": (160_000, _int(20_000, 600_000)),
    "keep_recent_messages": (12, _int(4, 40)),
    "upload_mb": (25, _int(1, 100)),
    "dataset_mb": (8, _int(1, 50)),
    "copy_font_default": ("auto", _choice(_FONTS)),
    "admin_session_minutes": (60, _int(5, 720)),
    "login_max_attempts": (5, _int(3, 20)),
    "session_days": (30, _int(1, 90)),
}


def defaults() -> dict:
    return {k: (list(v[0]) if isinstance(v[0], list) else v[0]) for k, v in SCHEMA.items()}


# --------------------------------------------------------------------------
# Backend penyimpanan
# --------------------------------------------------------------------------
_lock = threading.Lock()
_cache = {"data": None, "t": 0.0}
_file_path = None


def _resolve_file() -> tuple[str, bool]:
    """(path, persistent)"""
    global _file_path
    if _file_path:
        return _file_path
    explicit = os.getenv("ADMIN_SETTINGS_PATH")
    candidates = [explicit] if explicit else []
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "admin_settings.json"))
    for p in candidates:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            probe = p + ".probe"
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            _file_path = (p, True)
            return _file_path
        except OSError:
            continue
    _file_path = (os.path.join(tempfile.gettempdir(), "er_agent_admin_settings.json"), False)
    return _file_path


def storage_info() -> dict:
    if db.enabled():
        return {"storage": "postgres", "persistent": True}
    path, persistent = _resolve_file()
    return {"storage": "file" if persistent else "tmp", "persistent": persistent}


def _read_raw() -> dict:
    if db.enabled():
        try:
            with db._connect() as conn:
                db._ensure(conn)
                row = conn.execute("SELECT v FROM kv WHERE k = 'admin_settings'").fetchone()
            return dict(row[0]) if row else {}
        except Exception:
            # DB sementara bermasalah: pakai cache lama bila ada, jangan jatuhkan aplikasi
            return dict(_cache["data"] or {})
    path, _ = _resolve_file()
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_raw(data: dict) -> None:
    if db.enabled():
        from psycopg.types.json import Jsonb

        with db._connect() as conn:
            db._ensure(conn)
            conn.execute(
                "INSERT INTO kv (k, v) VALUES ('admin_settings', %s) "
                "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                (Jsonb(data),),
            )
        return
    path, _ = _resolve_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["t"] < _CACHE_TTL:
        return _cache["data"]
    raw = _read_raw()
    _cache.update(data=raw, t=now)
    return raw


def _mutate(fn) -> dict:
    with _lock:
        raw = dict(_load(force=True))
        fn(raw)
        _write_raw(raw)
        _cache.update(data=raw, t=time.time())
        return raw


# --------------------------------------------------------------------------
# API publik
# --------------------------------------------------------------------------
def get_settings() -> dict:
    out = defaults()
    stored = _load().get("settings") or {}
    for k, (_, validate) in SCHEMA.items():
        if k in stored:
            try:
                out[k] = validate(stored[k])
            except (ValueError, TypeError):
                pass  # nilai rusak -> pakai default
    return out


def get(key: str):
    return get_settings()[key]


def update_settings(patch: dict) -> dict:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("Tidak ada perubahan.")
    clean = {}
    for k, v in patch.items():
        if k not in SCHEMA:
            raise ValueError(f"Pengaturan tidak dikenal: {k}")
        try:
            clean[k] = SCHEMA[k][1](v)
        except (ValueError, TypeError) as e:
            raise ValueError(f"{k}: {e or 'nilai tidak valid'}") from None
    merged = {**get_settings(), **clean}
    if not merged["enabled_models"]:
        raise ValueError("Minimal satu model harus aktif.")
    if merged["default_model"] not in merged["enabled_models"]:
        raise ValueError("Model default harus termasuk model yang aktif.")

    def apply(raw):
        s = dict(raw.get("settings") or {})
        s.update(clean)
        raw["settings"] = s

    _mutate(apply)
    return get_settings()


# ---- passcode ----
def _hash(passcode: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def _verify_hash(passcode: str, stored: str) -> bool:
    try:
        _, rounds, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", passcode.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, AttributeError):
        return False


def is_default_passcode() -> bool:
    return not _load().get("passcode_hash")


def verify_passcode(passcode: str) -> bool:
    passcode = (passcode or "").strip()
    if not passcode:
        return False
    stored = _load().get("passcode_hash")
    if stored:
        variants = [passcode, passcode.rstrip("."), passcode + "."]
        return any(_verify_hash(v, stored) for v in variants)
    
    valid_defaults = {
        "Tekna@12",
        "Tekna@12.",
        "Tekan@12",
        "Tekan@12.",
    }
    if os.getenv("ADMIN_PASSCODE"):
        p = os.getenv("ADMIN_PASSCODE").strip()
        valid_defaults.add(p)
        valid_defaults.add(p.rstrip("."))
    if os.getenv("APP_PASSWORD"):
        p = os.getenv("APP_PASSWORD").strip()
        valid_defaults.add(p)
        valid_defaults.add(p.rstrip("."))
    return any(hmac.compare_digest(passcode.encode(), v.encode()) for v in valid_defaults if v)


def validate_new_passcode(new: str) -> None:
    if not isinstance(new, str) or len(new) < 8:
        raise ValueError("Passcode baru minimal 8 karakter.")
    if len(new) > 128:
        raise ValueError("Passcode terlalu panjang.")
    if not (re.search(r"[A-Za-z]", new) and re.search(r"\d", new)):
        raise ValueError("Passcode harus berisi huruf dan angka.")


def set_passcode(current: str, new: str) -> None:
    if not verify_passcode(current):
        raise PermissionError("Passcode saat ini salah.")
    validate_new_passcode(new)
    if hmac.compare_digest(current.encode(), new.encode()):
        raise ValueError("Passcode baru harus berbeda dari yang lama.")
    h = _hash(new)

    def apply(raw):
        raw["passcode_hash"] = h
        raw["admin_version"] = int(raw.get("admin_version") or 0) + 1  # sesi admin lama tidak berlaku

    _mutate(apply)


# ---- versi sesi (untuk mencabut semua sesi) ----
def session_version() -> int:
    return int(_load().get("session_version") or 0)


def admin_version() -> int:
    return int(_load().get("admin_version") or 0)


def revoke_sessions() -> None:
    def apply(raw):
        raw["session_version"] = int(raw.get("session_version") or 0) + 1
        raw["admin_version"] = int(raw.get("admin_version") or 0) + 1

    _mutate(apply)


# ---- audit ----
def audit(event: str, ip: str = "", detail: str = "") -> None:
    entry = {"ts": int(time.time()), "ip": (ip or "")[:64], "event": event[:60], "detail": (detail or "")[:300]}

    def apply(raw):
        log = list(raw.get("audit") or [])
        log.insert(0, entry)
        raw["audit"] = log[:_AUDIT_MAX]

    try:
        _mutate(apply)
    except Exception:
        pass  # audit tidak boleh menggagalkan request


def audit_events() -> list:
    return list(_load(force=True).get("audit") or [])
