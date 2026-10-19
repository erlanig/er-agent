import hmac
import json
import os

from dotenv import load_dotenv

load_dotenv()  # baca .env saat lokal; di Vercel dipakai Environment Variables

from flask import Flask, Response, jsonify, render_template, request, stream_with_context  # noqa: E402

from providers import MODELS, run_chat  # noqa: E402
from tools import tool_specs  # noqa: E402

app = Flask(__name__)

MAX_MESSAGES = 40
MAX_CHARS = 40_000


def _auth_state() -> str:
    """'ok' | 'denied' | 'unconfigured'"""
    expected = os.getenv("APP_PASSWORD", "")
    if not expected:
        return "unconfigured"
    sent = request.headers.get("X-App-Password", "")
    return "ok" if hmac.compare_digest(sent.encode(), expected.encode()) else "denied"


def _auth_error(state: str):
    if state == "unconfigured":
        return jsonify(error="APP_PASSWORD belum diset di environment."), 503
    return jsonify(error="Password salah."), 401


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
        tools=[{"name": t["name"], "dangerous": t["dangerous"]} for t in tool_specs(allow_actions=True)],
    )


@app.post("/api/login")
def login():
    state = _auth_state()
    return jsonify(ok=True) if state == "ok" else _auth_error(state)


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
    if not history or history[-1]["role"] != "user":
        return jsonify(error="Pesan terakhir harus dari pengguna."), 400
    while history and history[0]["role"] != "user":
        history.pop(0)

    allow_actions = bool(body.get("allow_actions"))

    def generate():
        try:
            for event in run_chat(model_key, history, allow_actions):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as e:  # tampilkan ke UI alih-alih memutus koneksi tanpa pesan
            yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", "5000")))
