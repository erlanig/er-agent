"""Registry tool untuk chatbot.

Cara menambah tool baru: tulis fungsi Python lalu dekorasi dengan @tool(...).
Tool yang mengubah sesuatu di dunia luar (deploy, trigger CI/CD, dll.) harus
diberi dangerous=True; tool seperti itu hanya ditawarkan ke model bila
pengguna menyalakan toggle "Izinkan aksi" di UI.
"""
import ast
import ipaddress
import json
import operator
import os
import re
import socket
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

_REGISTRY: dict[str, dict] = {}


def tool(name, description, properties=None, required=(), dangerous=False):
    def deco(fn):
        _REGISTRY[name] = {
            "name": name,
            "description": description,
            "schema": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
            },
            "dangerous": dangerous,
            "fn": fn,
        }
        return fn

    return deco


def tool_specs(allow_actions: bool = False) -> list[dict]:
    """Daftar tool (tanpa fungsi) yang boleh dilihat model."""
    return [
        {k: v for k, v in t.items() if k != "fn"}
        for t in _REGISTRY.values()
        if allow_actions or not t["dangerous"]
    ]


def execute_tool(name: str, args: dict, allow_actions: bool = False) -> str:
    t = _REGISTRY.get(name)
    if not t:
        return json.dumps({"error": f"Tool '{name}' tidak ada."})
    if t["dangerous"] and not allow_actions:
        return json.dumps({"error": "Aksi ini dinonaktifkan oleh pengguna."})
    try:
        result = t["fn"](**(args or {}))
    except Exception as e:  # kembalikan ke model agar bisa menjelaskan/mencoba lagi
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------
# Tool dasar
# --------------------------------------------------------------------------
@tool(
    "get_current_time",
    "Ambil tanggal dan jam saat ini untuk sebuah zona waktu IANA (mis. Asia/Jakarta, Asia/Tokyo, UTC).",
    {"timezone": {"type": "string", "description": "Zona waktu IANA. Default Asia/Jakarta."}},
)
def get_current_time(timezone: str = "Asia/Jakarta"):
    now = datetime.now(ZoneInfo(timezone))
    return {"timezone": timezone, "iso": now.isoformat(timespec="seconds"), "hari": now.strftime("%A")}


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        v = _eval(node.operand)
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("Pangkat terlalu besar.")
        return _OPS[type(node.op)](left, right)
    raise ValueError("Ekspresi tidak didukung.")


@tool(
    "calculator",
    "Hitung ekspresi matematika (+ - * / // % ** dan tanda kurung). Gunakan untuk semua perhitungan angka.",
    {"expression": {"type": "string", "description": "Contoh: (2450000 * 18) / 100"}},
    ["expression"],
)
def calculator(expression: str):
    result = _eval(ast.parse(expression.strip(), mode="eval"))
    if isinstance(result, float):
        result = round(result, 10)
    return {"expression": expression, "result": result}


def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


@tool(
    "fetch_url",
    "Ambil isi teks sebuah halaman web publik (HTTP GET). Hanya http/https ke host publik.",
    {"url": {"type": "string", "description": "URL lengkap, mis. https://example.com"}},
    ["url"],
)
def fetch_url(url: str):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("URL harus http/https.")
    if not _is_public_host(u.hostname):
        raise ValueError("Host tidak publik atau tidak ditemukan.")
    r = requests.get(
        url, timeout=10, allow_redirects=False, stream=True,
        headers={"User-Agent": "bedrock-chat/1.0"},
    )
    if 300 <= r.status_code < 400:
        return {"status": r.status_code, "redirect_to": r.headers.get("Location")}
    raw = next(r.iter_content(chunk_size=200_000), b"")
    text = raw.decode(r.encoding or "utf-8", errors="replace")
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return {"status": r.status_code, "content": text[:6000]}


# --------------------------------------------------------------------------
# Tool CI/CD (GitHub Actions)
# --------------------------------------------------------------------------
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_WF_RE = re.compile(r"^[\w.\-]+$")


def _repo(repo: str | None) -> str:
    repo = repo or os.getenv("GITHUB_DEFAULT_REPO", "")
    if not _REPO_RE.match(repo):
        raise ValueError("Repo tidak valid. Format: pemilik/nama-repo")
    allowed = [x.strip() for x in os.getenv("GITHUB_ALLOWED_REPOS", "").split(",") if x.strip()]
    if allowed and repo not in allowed:
        raise PermissionError(f"Repo {repo} tidak ada di GITHUB_ALLOWED_REPOS.")
    return repo


def _gh(method: str, path: str, **kw):
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN belum diset di environment.")
    return requests.request(
        method, f"https://api.github.com{path}", timeout=15,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        **kw,
    )


@tool(
    "github_list_runs",
    "Lihat run GitHub Actions terbaru (status, hasil, branch, link). Opsional filter per workflow.",
    {
        "repo": {"type": "string", "description": "pemilik/nama-repo. Kosong = repo default."},
        "workflow": {"type": "string", "description": "Nama file workflow, mis. deploy.yml. Opsional."},
        "limit": {"type": "integer", "description": "Jumlah run (1-10). Default 5."},
    },
)
def github_list_runs(repo: str | None = None, workflow: str | None = None, limit: int = 5):
    repo = _repo(repo)
    path = f"/repos/{repo}/actions/runs"
    if workflow:
        if not _WF_RE.match(workflow):
            raise ValueError("Nama workflow tidak valid.")
        path = f"/repos/{repo}/actions/workflows/{workflow}/runs"
    r = _gh("GET", path, params={"per_page": max(1, min(int(limit), 10))})
    r.raise_for_status()
    return [
        {
            "id": x["id"], "name": x["name"], "status": x["status"],
            "conclusion": x["conclusion"], "branch": x["head_branch"],
            "event": x["event"], "created_at": x["created_at"], "url": x["html_url"],
        }
        for x in r.json().get("workflow_runs", [])
    ]


@tool(
    "github_dispatch_workflow",
    "Jalankan (trigger) sebuah workflow GitHub Actions yang punya workflow_dispatch. "
    "Ini AKSI yang mengubah sesuatu: jelaskan ke pengguna apa yang akan dijalankan sebelum memanggilnya.",
    {
        "repo": {"type": "string", "description": "pemilik/nama-repo. Kosong = repo default."},
        "workflow": {"type": "string", "description": "Nama file workflow, mis. deploy.yml"},
        "ref": {"type": "string", "description": "Branch atau tag. Default main."},
        "inputs": {"type": "object", "description": "Input workflow_dispatch (key-value string). Opsional."},
    },
    ["workflow"],
    dangerous=True,
)
def github_dispatch_workflow(workflow: str, repo: str | None = None, ref: str = "main", inputs: dict | None = None):
    repo = _repo(repo)
    if not _WF_RE.match(workflow):
        raise ValueError("Nama workflow tidak valid.")
    r = _gh(
        "POST", f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
        json={"ref": ref, "inputs": {k: str(v) for k, v in (inputs or {}).items()}},
    )
    if r.status_code != 204:
        raise RuntimeError(f"GitHub membalas {r.status_code}: {r.text[:300]}")
    return {"ok": True, "repo": repo, "workflow": workflow, "ref": ref,
            "catatan": "Workflow diminta berjalan. Cek statusnya dengan github_list_runs."}
