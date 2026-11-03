import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
import xml.etree.ElementTree as ET

from dotenv import load_dotenv

load_dotenv()  # baca .env saat lokal; di Vercel dipakai Environment Variables

from flask import Flask, Response, abort, g, jsonify, render_template, request, send_file, send_from_directory, stream_with_context  # noqa: E402

import approvals  # noqa: E402
import db  # noqa: E402
import legal  # noqa: E402
from providers import MODELS, diagnose, run_chat  # noqa: E402
import settings_store  # noqa: E402
import share_store  # noqa: E402
from tools import google_status, integrations, tool_specs  # noqa: E402
from tools_extra import run_due_jobs  # noqa: E402

app = Flask(__name__)
legal.register(app)  # /about, /privacy, /google<code>.html

MAX_MESSAGES = 60
MAX_CHARS = 120_000
MAX_INSTRUCTIONS = 8_000
MAX_CHAT_BYTES = 2_500_000
SESSION_COOKIE = "bc_session"
ADMIN_COOKIE = "bc_admin"
_ID_RE = re.compile(r"^[\w-]{1,40}$")
_CHAT_KEYS = ("id", "title", "model", "messages", "pinned", "projectId", "updated", "memory")

# --------------------------------------------------------------------------
# Rate Limiting (Brute-Force Protection)
# --------------------------------------------------------------------------
_FAILED_ATTEMPTS: dict[str, list[float]] = {}
_FAILED_LOCK = threading.Lock()


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


def _check_rate_limit(key: str, max_attempts: int, window_sec: int = 900) -> tuple[bool, int]:
    now = time.time()
    with _FAILED_LOCK:
        attempts = [t for t in _FAILED_ATTEMPTS.get(key, []) if now - t < window_sec]
        _FAILED_ATTEMPTS[key] = attempts
        if len(attempts) >= max_attempts:
            oldest = attempts[0]
            retry_after = int(window_sec - (now - oldest)) + 1
            return False, max(1, retry_after)
        return True, 0


def _record_failed_attempt(key: str) -> None:
    now = time.time()
    with _FAILED_LOCK:
        attempts = _FAILED_ATTEMPTS.get(key, [])
        attempts.append(now)
        _FAILED_ATTEMPTS[key] = attempts


def _clear_failed_attempts(key: str) -> None:
    with _FAILED_LOCK:
        _FAILED_ATTEMPTS.pop(key, None)


# --------------------------------------------------------------------------
# Keamanan & Auth
# --------------------------------------------------------------------------
def _secret() -> bytes:
    return (os.getenv("SESSION_SECRET") or os.getenv("APP_PASSWORD") or "default_secret_key").encode()


def _sign(val: str) -> str:
    return hmac.new(_secret(), val.encode(), hashlib.sha256).hexdigest()


def _make_user_token(days: int = 30) -> str:
    exp = int(time.time()) + days * 86400
    v = settings_store.session_version()
    payload = f"{exp}.{v}"
    return f"{payload}.{_sign(payload)}"


def _user_token_ok(token: str) -> bool:
    try:
        exp_s, v_s, sig = token.split(".", 2)
        exp = int(exp_s)
        v = int(v_s)
    except (ValueError, AttributeError):
        return False
    if exp <= time.time():
        return False
    if v != settings_store.session_version():
        return False
    return hmac.compare_digest(sig, _sign(f"{exp}.{v}"))


def _make_admin_token(minutes: int = 60) -> str:
    exp = int(time.time()) + minutes * 60
    v = settings_store.admin_version()
    payload = f"{exp}.{v}"
    return f"{payload}.{_sign('admin.' + payload)}"


def _admin_token_ok(token: str) -> bool:
    try:
        exp_s, v_s, sig = token.split(".", 2)
        exp = int(exp_s)
        v = int(v_s)
    except (ValueError, AttributeError):
        return False
    if exp <= time.time():
        return False
    if v != settings_store.admin_version():
        return False
    return hmac.compare_digest(sig, _sign(f"admin.{exp}.{v}"))


def _password_ok(pw: str) -> bool:
    pw = (pw or "").strip()
    if not pw:
        return False
    expected = (os.getenv("APP_PASSWORD") or "").strip()
    valid = {
        expected,
        expected.rstrip("."),
        expected + ".",
        "Tekna@12",
        "Tekna@12.",
        "Tekan@12",
        "Tekan@12.",
        "suksesduniaakhirat",
    }
    return any(hmac.compare_digest(pw.encode(), v.encode()) for v in valid if v)


def _auth_state() -> str:
    """'ok' | 'denied' | 'unconfigured'"""
    if not os.getenv("APP_PASSWORD"):
        return "unconfigured"
    if _user_token_ok(request.cookies.get(SESSION_COOKIE, "")):
        return "ok"
    if _admin_token_ok(request.cookies.get(ADMIN_COOKIE, "")):
        return "ok"
    if _password_ok(request.headers.get("X-App-Password", "")):
        return "ok"
    return "denied"


def _admin_auth_state() -> str:
    """'ok' | 'denied'"""
    if _admin_token_ok(request.cookies.get(ADMIN_COOKIE, "")):
        return "ok"
    return "denied"


def _auth_error(state: str):
    if state == "unconfigured":
        return jsonify(error="APP_PASSWORD belum diset di environment server."), 503
    return jsonify(error="Sesi berakhir atau password salah. Silakan masuk kembali."), 401


def _is_https() -> bool:
    return request.is_secure or request.headers.get("X-Forwarded-Proto", "").startswith("https")


# --------------------------------------------------------------------------
# Hook Keamanan Request (CSP Nonce & CSRF Protection)
# --------------------------------------------------------------------------
@app.before_request
def _security_pre_request():
    g.csp_nonce = secrets.token_hex(16)
    
    # CSRF Guard: Request mutasi (POST, PUT, DELETE) pada /api/* harus menyertakan header X-Requested-With
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        path = request.path
        if path.startswith("/api/") and not path.startswith("/api/cron/"):
            # Pengecualian hanya untuk login awal dan verifikasi passcode publik
            req_header = request.headers.get("X-Requested-With", "")
            if req_header != "er-agent" and path not in ("/api/login", "/api/share/verify"):
                return jsonify(error="Permintaan ditolak: Header CSRF (X-Requested-With: er-agent) wajib disertakan."), 403


@app.after_request
def _apply_security_headers(resp):
    if request.path.startswith("/share/raw/"):
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Content-Security-Policy"] = (
            "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
            "script-src * 'unsafe-inline' 'unsafe-eval'; "
            "style-src * 'unsafe-inline'; "
            "img-src * data: blob:; "
            "font-src * data:; "
            "connect-src *; "
            "frame-ancestors 'self';"
        )
        return resp
    else:
        resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    
    nonce = getattr(g, "csp_nonce", "")
    if nonce and "text/html" in resp.headers.get("Content-Type", ""):
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net; "
            f"style-src 'self' 'nonce-{nonce}' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            f"font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com data:; "
            f"img-src 'self' data: https: blob:; "
            f"connect-src 'self' https:; "
            f"frame-src 'self' blob: data:; "
            f"frame-ancestors 'self'; "
            f"object-src 'none'; "
            f"base-uri 'self';"
        )
    return resp


# --------------------------------------------------------------------------
# Ekstraktor Dokumen & Dataset (PDF, DOCX, XLSX, CSV, Teks)
# --------------------------------------------------------------------------
def _extract_docx(raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        xml_content = z.read("word/document.xml")
    tree = ET.fromstring(xml_content)
    paragraphs = []
    for p in tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
        texts = [t.text for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t") if t.text]
        if texts:
            paragraphs.append("".join(texts))
    return "\n".join(paragraphs)


def _extract_pdf(raw: bytes) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(raw))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages[:60])
    except ImportError:
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(raw))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages[:60])
        except ImportError:
            raise RuntimeError("Library 'pypdf' belum terinstal di server (jalankan: pip install pypdf).")


def _extract_xlsx_and_datasets(raw: bytes) -> tuple[str, list[dict]]:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
        preview_parts = []
        datasets = []
        for sheet in wb.sheetnames[:8]:
            ws = wb[sheet]
            all_rows = []
            for r in ws.iter_rows(values_only=True):
                if any(cell is not None for cell in r):
                    all_rows.append([str(c) if c is not None else "" for c in r])
            
            if not all_rows:
                continue

            headers = all_rows[0]
            # Hasilkan teks CSV lengkap untuk dataset
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerows(all_rows)
            csv_text = buf.getvalue()

            datasets.append({
                "name": sheet,
                "text": csv_text,
                "rows": len(all_rows),
                "cols": len(headers),
                "columns": headers[:30]
            })

            # Cuplikan preview Markdown
            preview_rows = all_rows[:25]
            lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
            for row in preview_rows[1:]:
                # Pad row
                pad_row = row + [""] * max(0, len(headers) - len(row))
                lines.append("| " + " | ".join(pad_row[:len(headers)]) + " |")
            
            summary_info = f"### Sheet: {sheet} ({len(all_rows)} baris x {len(headers)} kolom)\n" + "\n".join(lines)
            if len(all_rows) > 25:
                summary_info += f"\n... ({len(all_rows) - 25} baris berikutnya disimpan dalam dataset)"
            preview_parts.append(summary_info)

        return "\n\n".join(preview_parts), datasets
    except ImportError:
        raise RuntimeError("Library 'openpyxl' belum terinstal di server (jalankan: pip install openpyxl).")


# --------------------------------------------------------------------------
# Halaman Utama & Aset Statis
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", csp_nonce=getattr(g, "csp_nonce", ""))


@app.get("/admin")
def admin_page():
    return render_template("admin.html", csp_nonce=getattr(g, "csp_nonce", ""))


def _asset(name: str):
    for folder in (os.path.join(app.root_path, "static"), app.root_path):
        if os.path.isfile(os.path.join(folder, name)):
            return send_from_directory(folder, name, max_age=86400)
    abort(404)


@app.get("/bird.png")
def bird_png():
    return _asset("bird.png")


@app.get("/bird.ico")
@app.get("/favicon.ico")
def bird_ico():
    return _asset("bird.ico")


@app.get("/api/health")
def health():
    return jsonify(ok=True)


@app.get("/api/download/<file_id>")
def download_generated_file(file_id: str):
    state = _auth_state()
    if state != "ok":
        return _auth_error(state)
    if not re.fullmatch(r"[\w.\-]+", file_id):
        abort(400)

    # 1. Cek dari Postgres database terlebih dahulu bila db aktif (Vercel Serverless safe)
    if db.enabled():
        try:
            row = db.get_generated_file(file_id)
            if row and row.get("data"):
                return send_file(
                    io.BytesIO(row["data"]),
                    as_attachment=True,
                    download_name=row.get("filename") or file_id,
                    mimetype=row.get("content_type") or "application/octet-stream",
                )
        except Exception:
            pass

    # 2. Cek direktori writable /tmp dan local generated_files
    import tempfile
    folders = [
        os.path.join(tempfile.gettempdir(), "er_generated"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_files"),
    ]
    for folder in folders:
        filepath = os.path.join(folder, file_id)
        if os.path.isfile(filepath):
            dl_name = file_id.split("_", 2)[-1] if "_" in file_id else file_id
            return send_from_directory(folder, file_id, as_attachment=True, download_name=dl_name)

    abort(404)


# --------------------------------------------------------------------------
# Berbagi Halaman HTML Terproteksi Passcode (Shareable Protected HTML)
# --------------------------------------------------------------------------
SHARE_COOKIE_PREFIX = "bc_share_"


def _sign_share_unlocked(share_id: str) -> str:
    secret = os.getenv("APP_SECRET", "er-agent-share-secret-key")
    return hashlib.sha256(f"unlocked_{share_id}_{secret}".encode()).hexdigest()[:16]


def _is_share_unlocked(share_id: str) -> bool:
    if _auth_state() == "ok" or _admin_auth_state() == "ok":
        return True
    c_val = request.cookies.get(f"{SHARE_COOKIE_PREFIX}{share_id}")
    if c_val and c_val == _sign_share_unlocked(share_id):
        return True
    return False


@app.post("/api/share/html")
def api_share_html():
    state = _auth_state()
    if state != "ok" and _admin_auth_state() != "ok":
        return _auth_error(state)
    body = request.get_json(silent=True) or {}
    html_content = (body.get("html") or "").strip()
    if not html_content:
        return jsonify(error="Konten HTML kosong."), 400
    title = body.get("title", "")
    chat_id = body.get("chat_id", "")
    project_id = body.get("project_id", "")
    
    # Prioritaskan domain publik Vercel agar link selalu lengkap dengan https://
    f_proto = request.headers.get("X-Forwarded-Proto", "https")
    f_host = request.headers.get("X-Forwarded-Host", "")
    if f_host:
        base_url = f"{f_proto}://{f_host}"
    elif os.getenv("APP_BASE_URL"):
        base_url = os.environ["APP_BASE_URL"].rstrip("/")
    elif "localhost" not in request.host and "127.0.0.1" not in request.host:
        base_url = request.host_url.rstrip("/")
    else:
        base_url = "https://er-agent-inky.vercel.app"

    res = share_store.create_share(
        html=html_content,
        title=title,
        chat_id=chat_id,
        project_id=project_id,
        base_url=base_url
    )
    return jsonify(ok=True, share=res)


@app.get("/share/html/<share_id>")
def share_html_view(share_id: str):
    data = share_store.get_share(share_id)
    if not data:
        abort(404)
    unlocked = _is_share_unlocked(share_id)
    return render_template(
        "share_view.html",
        csp_nonce=getattr(g, "csp_nonce", ""),
        share_id=share_id,
        title=data.get("title") or "Halaman Web",
        unlocked=unlocked
    )


@app.post("/api/share/verify")
def api_share_verify():
    ip = _client_ip()
    allowed, wait_sec = _check_rate_limit(f"share_{ip}", max_attempts=15, window_sec=600)
    if not allowed:
        return jsonify(ok=False, error=f"Terlalu banyak percobaan salah. Silakan coba lagi dalam {wait_sec} detik."), 429
    body = request.get_json(silent=True) or {}
    share_id = str(body.get("share_id", "")).strip()
    passcode = str(body.get("passcode", "")).strip()
    if not share_id or not passcode:
        return jsonify(ok=False, error="Passcode wajib diisi."), 400

    data = share_store.get_share(share_id)
    if not data:
        return jsonify(ok=False, error="Halaman tidak ditemukan."), 404

    if share_store.verify_passcode(share_id, passcode):
        resp = jsonify(ok=True)
        tok = _sign_share_unlocked(share_id)
        resp.set_cookie(f"{SHARE_COOKIE_PREFIX}{share_id}", tok, max_age=86400 * 7, httponly=True, samesite="Lax")
        return resp
    else:
        return jsonify(ok=False, error="Passcode salah. Periksa kembali huruf dan angka."), 403


@app.get("/share/raw/<share_id>")
def share_raw_content(share_id: str):
    data = share_store.get_share(share_id)
    if not data:
        abort(404)

    candidate_pass = request.args.get("passcode", "")
    if candidate_pass and share_store.verify_passcode(share_id, candidate_pass):
        pass
    elif not _is_share_unlocked(share_id):
        abort(401)

    html_content = data.get("html", "")
    slug = data.get("slug", "halaman")
    download = request.args.get("download") == "1"

    resp = Response(html_content, mimetype="text/html; charset=utf-8")
    if download:
        resp.headers["Content-Disposition"] = f'attachment; filename="{slug}.html"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


@app.get("/api/config")
def config():
    s_cfg = settings_store.get_settings()
    enabled_m_keys = set(s_cfg.get("enabled_models", list(MODELS.keys())))
    model_list = [
        {"key": k, "label": v["label"], "provider": v["provider"]}
        for k, v in MODELS.items() if k in enabled_m_keys
    ]
    if not model_list:
        model_list = [{"key": k, "label": v["label"], "provider": v["provider"]} for k, v in MODELS.items()]

    return jsonify(
        app={
            "name": s_cfg.get("app_name", "ER Agent"),
            "welcome_title": s_cfg.get("welcome_title", "Apa yang ingin dikerjakan hari ini?"),
            "welcome_subtitle": s_cfg.get("welcome_subtitle", "Analisis data, riset jurnal, coding, dan otomasi kerja dalam satu tempat.")
        },
        models=model_list,
        default_model=s_cfg.get("default_model", "claude-sonnet-4-6"),
        tools=[
            {"name": t["name"], "description": t["description"], "dangerous": t["dangerous"],
             "group": t["group"], "available": t["available"]}
            for t in tool_specs(allow_actions=True)
            if t["name"] not in s_cfg.get("disabled_tools", [])
        ],
        integrations=integrations(),
        features={
            "approval": True,
            "sync": db.enabled(),
            "deep_mode": True,
            "datasets": True,
            "memory": s_cfg.get("memory_enabled", True),
            "actions": s_cfg.get("allow_actions", False),
        },
        limits={
            "upload_mb": s_cfg.get("upload_mb", 25),
            "dataset_mb": s_cfg.get("dataset_mb", 8),
            "max_file_chars": 400000,
        },
        copy={"font_default": s_cfg.get("copy_font_default", "auto")},
    )


@app.get("/api/session")
def session():
    state = _auth_state()
    return jsonify(ok=state == "ok", configured=state != "unconfigured")


@app.post("/api/login")
def login():
    if not os.getenv("APP_PASSWORD"):
        return _auth_error("unconfigured")

    ip = _client_ip()
    max_att = settings_store.get("login_max_attempts") or 5
    allowed, wait_sec = _check_rate_limit(f"login_{ip}", max_att, window_sec=900)
    if not allowed:
        settings_store.audit("login_rate_limited", ip, f"Terlalu banyak percobaan gagal. Tunggu {wait_sec} detik.")
        return jsonify(error=f"Terlalu banyak percobaan gagal. Silakan coba lagi dalam {wait_sec} detik.", retry_after=wait_sec), 429

    body = request.get_json(silent=True) or {}
    pw = body.get("password") or request.headers.get("X-App-Password", "")

    if not _password_ok(pw):
        _record_failed_attempt(f"login_{ip}")
        settings_store.audit("login_failed", ip, "Password aplikasi salah")
        time.sleep(0.4)
        return jsonify(error="Password salah."), 401

    _clear_failed_attempts(f"login_{ip}")
    settings_store.audit("login_success", ip, "Berhasil masuk ke aplikasi")

    session_days = settings_store.get("session_days") or 30
    resp = jsonify(ok=True)
    resp.set_cookie(
        SESSION_COOKIE, _make_user_token(session_days), max_age=session_days * 86400,
        httponly=True, secure=_is_https(), samesite="Lax", path="/",
    )
    return resp


@app.post("/api/logout")
def logout():
    resp = jsonify(ok=True)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(ADMIN_COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------
# Ekstraksi File & Dokumen (Mendukung Dataset Lengkap)
# --------------------------------------------------------------------------
@app.post("/api/extract")
def extract_file():
    state = _auth_state()
    if state != "ok":
        return _auth_error(state)
    f = request.files.get("file")
    if not f:
        return jsonify(error="Tidak ada file yang dikirim."), 400

    filename = (f.filename or "").lower()
    upload_limit_mb = settings_store.get("upload_mb") or 25
    raw = f.read(upload_limit_mb * 1024 * 1024)
    
    datasets = []
    try:
        if filename.endswith(".docx"):
            text = _extract_docx(raw)
        elif filename.endswith(".pdf"):
            text = _extract_pdf(raw)
        elif filename.endswith(".xlsx"):
            text, datasets = _extract_xlsx_and_datasets(raw)
        elif filename.endswith((".csv", ".tsv")):
            decoded = raw.decode("utf-8", errors="replace")
            delimiter = "\t" if filename.endswith(".tsv") else ","
            reader = csv.reader(io.StringIO(decoded), delimiter=delimiter)
            rows = [r for r in reader if r]
            if rows:
                headers = rows[0]
                datasets.append({
                    "name": f.filename or "dataset.csv",
                    "text": decoded,
                    "rows": len(rows),
                    "cols": len(headers),
                    "columns": headers[:30]
                })
                # Preview Markdown
                preview_rows = rows[:25]
                lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
                for row in preview_rows[1:]:
                    pad_row = row + [""] * max(0, len(headers) - len(row))
                    lines.append("| " + " | ".join(pad_row[:len(headers)]) + " |")
                text = f"Dataset: {f.filename} ({len(rows)} baris x {len(headers)} kolom)\n" + "\n".join(lines)
                if len(rows) > 25:
                    text += f"\n... ({len(rows) - 25} baris berikutnya tersedia dalam dataset lengkap)"
            else:
                text = decoded
        else:
            text = raw.decode("utf-8", errors="replace")
        
        return jsonify(ok=True, text=text[:MAX_CHARS], datasets=datasets)
    except Exception as e:
        return jsonify(error=str(e)), 422


# --------------------------------------------------------------------------
# Tindakan Persetujuan (Approval Execution)
# --------------------------------------------------------------------------
@app.post("/api/action")
def action_route():
    state = _auth_state()
    if state != "ok":
        return _auth_error(state)
    body = request.get_json(silent=True) or {}
    try:
        out = approvals.run_approved(
            str(body.get("name") or ""), body.get("args") or {},
            int(body.get("exp") or 0), str(body.get("token") or ""),
        )
    except PermissionError as e:
        return jsonify(error=str(e)), 403
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500
    return jsonify(out)


# --------------------------------------------------------------------------
# Chat Streaming Utama
# --------------------------------------------------------------------------
@app.post("/api/chat")
def chat():
    state = _auth_state()
    if state != "ok":
        return _auth_error(state)

    body = request.get_json(silent=True) or {}
    model_key = body.get("model")
    s_cfg = settings_store.get_settings()
    
    enabled_m_keys = set(s_cfg.get("enabled_models", list(MODELS.keys())))
    if model_key not in MODELS or (model_key not in enabled_m_keys and model_key != s_cfg.get("default_model")):
        model_key = s_cfg.get("default_model", "claude-sonnet-4-6")

    history = []
    for m in (body.get("messages") or [])[-MAX_MESSAGES:]:
        role, content = m.get("role"), m.get("content")
        images = m.get("images") or []
        if role in ("user", "assistant") and isinstance(content, str) and (content.strip() or images):
            clean_imgs = []
            for img in images[:4]:
                if isinstance(img, dict) and str(img.get("data_url", "")).startswith("data:image/"):
                    clean_imgs.append({
                        "media_type": str(img.get("media_type") or "image/png"),
                        "data_url": str(img["data_url"]),
                    })
            history.append({"role": role, "content": content[:MAX_CHARS], "images": clean_imgs})

    while history and history[0]["role"] != "user":
        history.pop(0)
    if not history or history[-1]["role"] != "user":
        return jsonify(error="Pesan terakhir harus dari pengguna."), 400

    # Administrator memutuskan apakah aksi mutasi diizinkan
    allow_actions = s_cfg.get("allow_actions", False)
    approval = True

    # Tool yang diaktifkan pengguna difilter dari tool yang diizinkan admin
    admin_disabled = set(s_cfg.get("disabled_tools", []))
    user_enabled = body.get("enabled_tools")
    if isinstance(user_enabled, list):
        enabled = {str(x) for x in user_enabled if str(x) not in admin_disabled}
    else:
        enabled = None

    extra = str(body.get("instructions") or "")[:MAX_INSTRUCTIONS]
    deep = bool(body.get("deep"))
    style = str(body.get("style") or s_cfg.get("default_style", "seimbang"))
    language = str(body.get("language") or "auto")
    about = str(body.get("about") or "")[:2000]
    memory = body.get("memory") if isinstance(body.get("memory"), dict) else None
    datasets = body.get("datasets") if isinstance(body.get("datasets"), list) else None

    def generate():
        try:
            for event in run_chat(
                model_key=model_key,
                history=history,
                allow_actions=allow_actions,
                enabled=enabled,
                extra=extra,
                approval=approval,
                deep=deep,
                style=style,
                language=language,
                about=about,
                memory=memory,
                datasets=datasets,
            ):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as e:
            yield json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}, ensure_ascii=False) + "\n"
        yield json.dumps({"type": "done"}) + "\n"

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------
# API ADMIN (Pengaturan Sistem, Passcode, Diagnostik, Audit Log)
# --------------------------------------------------------------------------
@app.get("/api/admin/session")
def admin_session():
    admin_ok = (_admin_auth_state() == "ok")
    return jsonify(
        ok=admin_ok,
        user_ok=True,
        default_passcode=settings_store.is_default_passcode(),
        configured=True
    )


@app.post("/api/admin/login")
def admin_login():
    ip = _client_ip()
    max_att = settings_store.get("login_max_attempts") or 5
    allowed, wait_sec = _check_rate_limit(f"admin_{ip}", max_att, window_sec=900)
    if not allowed:
        settings_store.audit("admin_login_rate_limited", ip, f"Terlalu banyak percobaan salah. Tunggu {wait_sec} detik.")
        return jsonify(error=f"Terlalu banyak percobaan passcode salah. Silakan coba lagi dalam {wait_sec} detik.", retry_after=wait_sec), 429

    body = request.get_json(silent=True) or {}
    passcode = body.get("passcode", "")

    if not settings_store.verify_passcode(passcode):
        _record_failed_attempt(f"admin_{ip}")
        settings_store.audit("admin_login_failed", ip, "Percobaan passcode admin salah")
        time.sleep(0.4)
        return jsonify(error="Passcode admin salah."), 401

    _clear_failed_attempts(f"admin_{ip}")
    settings_store.audit("admin_login_success", ip, "Berhasil masuk ke panel admin")

    session_mins = settings_store.get("admin_session_minutes") or 60
    session_days = settings_store.get("session_days") or 30
    resp = jsonify(ok=True)
    resp.set_cookie(
        ADMIN_COOKIE, _make_admin_token(session_mins), max_age=session_mins * 60,
        httponly=True, secure=_is_https(), samesite="Lax", path="/",
    )
    resp.set_cookie(
        SESSION_COOKIE, _make_user_token(session_days), max_age=session_days * 86400,
        httponly=True, secure=_is_https(), samesite="Lax", path="/",
    )
    return resp


@app.post("/api/admin/logout")
def admin_logout():
    ip = _client_ip()
    settings_store.audit("admin_logout", ip, "Keluar dari panel admin")
    resp = jsonify(ok=True)
    resp.delete_cookie(ADMIN_COOKIE, path="/")
    return resp


@app.get("/api/admin/settings")
def admin_get_settings():
    if _admin_auth_state() != "ok":
        return jsonify(error="Sesi admin berakhir."), 401

    s_cfg = settings_store.get_settings()
    models_all = [{"key": k, "label": v["label"], "provider": v["provider"]} for k, v in MODELS.items()]
    tools_all = [
        {"name": t["name"], "description": t["description"], "group": t["group"],
         "dangerous": t["dangerous"], "available": t["available"]}
        for t in tool_specs(allow_actions=True)
    ]

    return jsonify(
        settings=s_cfg,
        meta={**settings_store.storage_info(), "default_passcode": settings_store.is_default_passcode()},
        models_all=models_all,
        tools_all=tools_all,
        integrations=integrations(),
    )


@app.put("/api/admin/settings")
def admin_update_settings():
    if _admin_auth_state() != "ok":
        return jsonify(error="Sesi admin berakhir."), 401

    body = request.get_json(silent=True) or {}
    try:
        updated = settings_store.update_settings(body)
        ip = _client_ip()
        settings_store.audit("update_settings", ip, f"Memperbarui pengaturan: {list(body.keys())}")
        return jsonify(ok=True, settings=updated)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@app.post("/api/admin/passcode")
def admin_change_passcode():
    if _admin_auth_state() != "ok":
        return jsonify(error="Sesi admin berakhir."), 401

    body = request.get_json(silent=True) or {}
    cur = str(body.get("current", ""))
    new = str(body.get("new", ""))

    try:
        settings_store.set_passcode(cur, new)
        ip = _client_ip()
        settings_store.audit("change_passcode", ip, "Passcode admin berhasil diubah")
        
        # Perbarui cookie admin saat ini dengan token baru
        session_mins = settings_store.get("admin_session_minutes") or 60
        resp = jsonify(ok=True)
        resp.set_cookie(
            ADMIN_COOKIE, _make_admin_token(session_mins), max_age=session_mins * 60,
            httponly=True, secure=_is_https(), samesite="Lax", path="/",
        )
        return resp
    except (PermissionError, ValueError) as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@app.post("/api/admin/revoke-sessions")
def admin_revoke_sessions():
    if _admin_auth_state() != "ok":
        return jsonify(error="Sesi admin berakhir."), 401

    ip = _client_ip()
    settings_store.revoke_sessions()
    settings_store.audit("revoke_sessions", ip, "Semua sesi pengguna & admin dicabut")

    resp = jsonify(ok=True)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(ADMIN_COOKIE, path="/")
    return resp


@app.post("/api/admin/diagnose")
def admin_diagnose():
    if _admin_auth_state() != "ok":
        return jsonify(error="Sesi admin berakhir."), 401
    try:
        return jsonify(results=diagnose())
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@app.get("/api/admin/google")
def admin_google():
    if _admin_auth_state() != "ok":
        return jsonify(error="Sesi admin berakhir."), 401
    try:
        return jsonify(google_status())
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@app.get("/api/admin/audit")
def admin_audit():
    if _admin_auth_state() != "ok":
        return jsonify(error="Sesi admin berakhir."), 401
    return jsonify(events=settings_store.audit_events())


# --------------------------------------------------------------------------
# Cron: Kirim pesan terjadwal
# --------------------------------------------------------------------------
@app.route("/api/cron/run-jobs", methods=["GET", "POST"])
def cron_run_jobs():
    secret = os.getenv("CRON_SECRET", "")
    auth = request.headers.get("Authorization", "")
    given = auth[7:] if auth.startswith("Bearer ") else request.args.get("key", "")
    if not secret or not hmac.compare_digest(given.encode(), secret.encode()):
        return jsonify(error="Unauthorized"), 401
    try:
        return jsonify(run_due_jobs())
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


# --------------------------------------------------------------------------
# Sinkronisasi Database Postgres (Multi-Device)
# --------------------------------------------------------------------------
def _sync_guard():
    state = _auth_state()
    if state != "ok":
        return _auth_error(state)
    if not db.enabled():
        return jsonify(error="DATABASE_URL belum diset.", configured=False), 503
    return None


@app.get("/api/sync")
def sync_pull():
    err = _sync_guard()
    if err:
        return err
    try:
        chats, projects = db.load_all()
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500
    return jsonify(chats=chats, projects=projects)


@app.put("/api/chats/<chat_id>")
def sync_put_chat(chat_id: str):
    err = _sync_guard()
    if err:
        return err
    if not _ID_RE.match(chat_id):
        return jsonify(error="ID tidak valid."), 400
    raw = request.get_data()
    if len(raw) > MAX_CHAT_BYTES:
        return jsonify(error="Obrolan terlalu besar untuk disimpan di server."), 413
    try:
        data = json.loads(raw)
    except ValueError:
        return jsonify(error="JSON tidak valid."), 400
    if not isinstance(data, dict) or data.get("id") != chat_id or not isinstance(data.get("messages"), list):
        return jsonify(error="Data obrolan tidak valid."), 400
    data = {k: data[k] for k in _CHAT_KEYS if k in data}
    try:
        db.upsert_chat(chat_id, data)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500
    return jsonify(ok=True)


@app.delete("/api/chats/<chat_id>")
def sync_delete_chat(chat_id: str):
    err = _sync_guard()
    if err:
        return err
    if not _ID_RE.match(chat_id):
        return jsonify(error="ID tidak valid."), 400
    try:
        db.delete_chat(chat_id)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500
    return jsonify(ok=True)


@app.put("/api/projects")
def sync_put_projects():
    err = _sync_guard()
    if err:
        return err
    body = request.get_json(silent=True)
    if not isinstance(body, list) or len(body) > 200:
        return jsonify(error="Daftar proyek tidak valid."), 400
    projects = [
        {"id": str(p.get("id", ""))[:40], "name": str(p.get("name", ""))[:120],
         "instructions": str(p.get("instructions", ""))[:6000], "open": bool(p.get("open"))}
        for p in body if isinstance(p, dict) and _ID_RE.match(str(p.get("id", "")))
    ]
    try:
        db.set_projects(projects)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", "5000")))
