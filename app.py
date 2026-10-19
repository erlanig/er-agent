import hashlib
import hmac
import json
import os
import time

from dotenv import load_dotenv

load_dotenv()  # baca .env saat lokal; di Vercel dipakai Environment Variables

from flask import Flask, Response, jsonify, render_template, request, stream_with_context  # noqa: E402

from providers import MODELS, diagnose, run_chat  # noqa: E402
from tools import tool_specs  # noqa: E402

app = Flask(__name__)

MAX_MESSAGES = 40
MAX_CHARS = 40_000
MAX_INSTRUCTIONS = 6_000
SESSION_COOKIE = "bc_session"
SESSION_DAYS = 30


# --------------------------------------------------------------------------
# Auth: password sekali, lalu cookie sesi bertanda tangan (30 hari)
# --------------------------------------------------------------------------
def _secret() -> bytes:
    return (os.getenv("SESSION_SECRET") or os.getenv("APP_PASSWORD") or "").encode()


def _sign(exp: int) -> str:
    return hmac.new(_secret(), str(exp).encode(), hashlib.sha256).hexdigest()


def _make_token() -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    return f"{exp}.{_sign(exp)}"


def _token_ok(token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    return exp > time.time() and hmac.compare_digest(sig, _sign(exp))


def _password_ok(pw: str) -> bool:
    expected = os.getenv("APP_PASSWORD", "")
    return bool(expected) and hmac.compare_digest((pw or "").encode(), expected.encode())


def _auth_state() -> str:
    """'ok' | 'denied' | 'unconfigured'"""
    if not os.getenv("APP_PASSWORD"):
        return "unconfigured"
    if _token_ok(request.cookies.get(SESSION_COOKIE, "")):
        return "ok"
    if _password_ok(request.headers.get("X-App-Password", "")):
        return "ok"
    return "denied"


def _auth_error(state: str):
    if state == "unconfigured":
        return jsonify(error="APP_PASSWORD belum diset di environment."), 503
    return jsonify(error="Sesi berakhir atau password salah."), 401


def _is_https() -> bool:
    return request.is_secure or request.headers.get("X-Forwarded-Proto", "").startswith("https")


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.get("/api/config")
def config():
    return jsonify(
        models=[{"key": k, "label": v["label"], "provider": v["provider"]} for k, v in MODELS.items()],
        tools=[
            {"name": t["name"], "description": t["description"], "dangerous": t["dangerous"]}
            for t in tool_specs(allow_actions=True)
        ],
    )


@app.get("/api/session")
def session():
    state = _auth_state()
    return jsonify(ok=state == "ok", configured=state != "unconfigured")


@app.post("/api/login")
def login():
    if not os.getenv("APP_PASSWORD"):
        return _auth_error("unconfigured")
    body = request.get_json(silent=True) or {}
    pw = body.get("password") or request.headers.get("X-App-Password", "")
    if not _password_ok(pw):
        time.sleep(0.4)  # perlambat tebak-tebakan password
        return jsonify(error="Password salah."), 401
    resp = jsonify(ok=True)
    resp.set_cookie(
        SESSION_COOKIE, _make_token(), max_age=SESSION_DAYS * 86400,
        httponly=True, secure=_is_https(), samesite="Lax", path="/",
    )
    return resp


@app.post("/api/logout")
def logout():
    resp = jsonify(ok=True)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.post("/api/diagnose")
def diagnose_route():
    state = _auth_state()
    if state != "ok":
        return _auth_error(state)
    try:
        return jsonify(results=diagnose())
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@app.post("/api/chat")
def chat():
    state = _auth_state()
    if state != "ok":
        return _auth_error(state)

    body = request.get_json(silent=True) or {}
    model_key = body.get("model")
    if model_key not in MODELS:
        return jsonify(error="Model tidak dikenal."), 400

    history = []
    for m in (body.get("messages") or [])[-MAX_MESSAGES:]:
        role, content = m.get("role"), m.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            history.append({"role": role, "content": content[:MAX_CHARS]})
    while history and history[0]["role"] != "user":
        history.pop(0)
    if not history or history[-1]["role"] != "user":
        return jsonify(error="Pesan terakhir harus dari pengguna."), 400

    allow_actions = bool(body.get("allow_actions"))
    enabled = body.get("enabled_tools")
    enabled = {str(x) for x in enabled} if isinstance(enabled, list) else None
    extra = str(body.get("instructions") or "")[:MAX_INSTRUCTIONS]

    def generate():
        try:
            for event in run_chat(model_key, history, allow_actions, enabled, extra):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as e:  # tampilkan ke UI alih-alih memutus koneksi tanpa pesan
            yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", "5000")))