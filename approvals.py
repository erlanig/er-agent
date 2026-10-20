"""Persetujuan aksi (tombol Setujui/Tolak) untuk tool bertanda dangerous.

Alur (tanpa state di server):
1. Model memanggil tool aksi, mis. gcal_create_event.
2. Server TIDAK menjalankannya. Server mengirim event `approval_request` berisi nama tool,
   argumen persis yang diminta model, dan token bertanda tangan (berlaku 15 menit).
3. UI menampilkan kartu. Bila pengguna menekan Setujui, UI memanggil POST /api/action
   dengan nama, argumen, exp, dan token. Server memverifikasi tanda tangan (argumen tidak boleh
   berubah) lalu menjalankan tool tepat satu kali.

Mode ini hanya aktif bila UI mengirim `approval_mode: true`; tanpa itu perilaku lama tetap berlaku.
"""
import hashlib
import hmac
import json
import os
import time

from tools import _REGISTRY, _available, execute_tool

TTL_SECONDS = 15 * 60
_USED: set[str] = set()  # best-effort per instance serverless

PENDING_RESULT = json.dumps({
    "status": "menunggu_persetujuan_pengguna",
    "catatan": "Kartu persetujuan sudah ditampilkan di UI. Jangan memanggil tool ini lagi. "
               "Beri tahu pengguna dalam satu kalimat bahwa aksi menunggu persetujuan.",
}, ensure_ascii=False)


def _secret() -> bytes:
    return (os.getenv("SESSION_SECRET") or os.getenv("APP_PASSWORD") or "").encode()


def _norm(v):
    """Samakan 1.0 dan 1 agar tanda tangan tidak berubah setelah bolak-balik JSON."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def canonical(args) -> str:
    return json.dumps(_norm(args or {}), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sig(name: str, args, exp: int) -> str:
    msg = f"{name}|{canonical(args)}|{exp}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def needs_approval(name: str, allow_actions: bool, enabled) -> bool:
    t = _REGISTRY.get(name)
    return bool(
        t and t["dangerous"] and allow_actions
        and (enabled is None or name in enabled) and _available(t)
    )


def make_request(call_id: str, name: str, args: dict) -> dict:
    exp = int(time.time()) + TTL_SECONDS
    t = _REGISTRY[name]
    return {
        "type": "approval_request", "id": call_id, "name": name, "group": t["group"],
        "description": t["description"], "args": args, "exp": exp, "token": _sig(name, args, exp),
    }


def run_approved(name: str, args: dict, exp: int, token: str) -> dict:
    t = _REGISTRY.get(name)
    if not t or not t["dangerous"]:
        raise PermissionError("Tool ini bukan aksi yang perlu persetujuan.")
    if exp < time.time():
        raise PermissionError("Persetujuan sudah kedaluwarsa. Minta agen membuat ulang aksinya.")
    if not hmac.compare_digest(token or "", _sig(name, args, exp)):
        raise PermissionError("Token tidak cocok: argumen aksi berubah atau token tidak valid.")
    if token in _USED:
        raise PermissionError("Aksi ini sudah pernah dijalankan.")
    _USED.add(token)
    parsed = json.loads(execute_tool(name, args, allow_actions=True, enabled=None))
    failed = isinstance(parsed, dict) and "error" in parsed
    return {"ok": not failed, "name": name, "result": parsed}