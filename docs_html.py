"""Markdown -> HTML bergaya inline yang dikonversi rapi oleh Google Drive menjadi Google Docs.

Didukung:
- heading (jadi Heading 1-6 asli Docs), tebal/miring/coret, tautan, daftar bernomor/bullet, daftar centang (- [ ] / - [x])
- tabel: header tebal, garis sel, baris selang-seling, rata kiri/tengah/kanan
- blok kode: kotak abu-abu monospace (indentasi dipertahankan) + label bahasa; kode inline bermonospace
- kutipan (blockquote), garis pemisah
- diagram ```mermaid -> gambar (lewat mermaid.ink; ganti dengan MERMAID_INK_URL bila self-host)
- rumus LaTeX: $...$, $$...$$, \\(...\\), \\[...\\]
    * rumus sederhana (x^2, \\alpha, \\times, H_2O ...) -> teks Unicode yang bisa diedit
    * rumus kompleks (\\frac, \\sqrt, \\sum, \\int, matriks ...) -> gambar (lewat latex.codecogs.com;
      ganti dengan LATEX_IMG_URL bila self-host). Teks rumus dikirim ke layanan tersebut saat konversi.
"""
import base64
import html
import os
import re
from urllib.parse import quote

MERMAID_INK = os.getenv("MERMAID_INK_URL", "https://mermaid.ink").rstrip("/")
LATEX_IMG = os.getenv("LATEX_IMG_URL", "https://latex.codecogs.com/png.image").rstrip("/")

FONT = "Calibri,Arial,sans-serif"
MONO = "Consolas,'Courier New',monospace"
CELL = "border:1px solid #94a3b8;padding:5pt 8pt;"

# --------------------------------------------------------------------------
# LaTeX -> teks Unicode (untuk rumus sederhana)
# --------------------------------------------------------------------------
_SYM = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "infty": "∞", "rightarrow": "→", "to": "→",
    "leftarrow": "←", "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔", "degree": "°", "circ": "°",
    "partial": "∂", "nabla": "∇", "in": "∈", "notin": "∉", "subset": "⊂", "cup": "∪", "cap": "∩",
    "forall": "∀", "exists": "∃", "ldots": "…", "dots": "…", "cdots": "⋯", "angle": "∠", "propto": "∝",
    "sim": "∼", "quad": " ", "qquad": "  ",
}
_SUP = dict(zip("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ"))
_SUB = {**dict(zip("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")),
        **dict(zip("aehijklmnoprstuvx", "ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"))}
_COMPLEX = re.compile(
    r"\\(?:frac|dfrac|tfrac|sqrt|int|iint|oint|sum|prod|lim|begin|end|left|right|binom|overline|underline|"
    r"hat|bar|vec|dot|mathbb|mathcal|mathfrak|matrix|pmatrix|cases|over|choose)\b"
)


def _simple_tex(tex: str) -> str | None:
    """Ubah rumus sederhana jadi teks Unicode; None bila terlalu kompleks (pakai gambar)."""
    if _COMPLEX.search(tex):
        return None
    s = re.sub(r"\\(?:text|mathrm|mathbf|mathit|operatorname)\{([^{}]*)\}", r"\1", tex)
    s = re.sub(r"\\([%$&#])", r"\1", s)
    s = re.sub(r"\\[,;:! ]", " ", s)
    bad: list[str] = []

    def cmd(m):
        v = _SYM.get(m.group(1))
        if v is None:
            bad.append(m.group(1))
            return ""
        return v

    s = re.sub(r"\\([A-Za-z]+)", cmd, s)
    if bad:
        return None

    def script(m):
        table = _SUP if m.group(1) == "^" else _SUB
        out = []
        for ch in m.group(2).strip("{}"):
            if ch not in table:
                bad.append(ch)
                return ""
            out.append(table[ch])
        return "".join(out)

    s = re.sub(r"([\^_])(\{[^{}]*\}|[A-Za-z0-9+\-=()])", script, s)
    if bad or re.search(r"[\^_\\]", s):
        return None
    return re.sub(r"\s+", " ", s.replace("{", "").replace("}", "")).strip()


def _tex_img(tex: str, display: bool) -> str:
    t = re.sub(r"\s+", " ", tex.strip())
    alt = html.escape(t, quote=True)
    if display:
        url = f"{LATEX_IMG}?" + quote("\\dpi{200} " + t, safe="")
        return f'<p style="text-align:center;margin:8pt 0"><img src="{url}" alt="{alt}"></p>'
    url = f"{LATEX_IMG}?" + quote("\\inline \\dpi{150} " + t, safe="")
    return f'<img src="{url}" alt="{alt}" style="vertical-align:middle">'


def _inline_math(tex: str) -> str:
    simple = _simple_tex(tex)
    return html.escape(simple) if simple is not None else _tex_img(tex, False)


def _mermaid_img(code: str) -> str:
    b64 = base64.urlsafe_b64encode(code.strip().encode("utf-8")).decode()
    return f'<p style="text-align:center"><img src="{MERMAID_INK}/img/{b64}" alt="Diagram" width="600"></p>'


# --------------------------------------------------------------------------
# Konversi utama
# --------------------------------------------------------------------------
def md_to_doc_html(md_text: str) -> str:
    try:
        import markdown as _markdown
    except ImportError:
        raise RuntimeError(
            "Library 'markdown' belum terpasang di server. Tambahkan `markdown` ke requirements.txt lalu redeploy "
            "(pip install di komputer lokal tidak memengaruhi Vercel)."
        )

    blocks: list[str] = []
    inlines: list[str] = []

    def block(h: str) -> str:
        blocks.append(h)
        return f"\n\n@@BLK{len(blocks) - 1}@@\n\n"

    def inl(h: str) -> str:
        inlines.append(h)
        return f"@@INL{len(inlines) - 1}@@"

    # 1) Pisahkan kode (jangan disentuh) dari teks biasa; ambil rumus, diagram, coret, dan centang
    parts = re.split(r"(```[\s\S]*?(?:```|$)|`[^`\n]*`)", md_text)
    for i, p in enumerate(parts):
        if i % 2:
            m = re.match(r"```mermaid[ \t]*\r?\n([\s\S]*?)(?:```|$)", p)
            if m:
                parts[i] = block(_mermaid_img(m.group(1)))
            continue
        p = re.sub(r"\$\$([\s\S]+?)\$\$", lambda m: block(_tex_img(m.group(1), True)), p)
        p = re.sub(r"\\\[([\s\S]+?)\\\]", lambda m: block(_tex_img(m.group(1), True)), p)
        p = re.sub(r"\\\(([\s\S]+?)\\\)", lambda m: inl(_inline_math(m.group(1))), p)
        p = re.sub(r"(?<![\\$\w])\$(?!\s)([^\n$]+?)(?<!\s)\$(?![\d\w])", lambda m: inl(_inline_math(m.group(1))), p)
        p = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", p)
        p = re.sub(r"(?m)^(\s*(?:[-*+]|\d+[.)])\s+)\[ \]\s", r"\1☐ ", p)
        p = re.sub(r"(?m)^(\s*(?:[-*+]|\d+[.)])\s+)\[[xX]\]\s", r"\1☑ ", p)
        parts[i] = p
    text = "".join(parts)

    body = _markdown.markdown(text, extensions=["extra", "sane_lists", "nl2br"])

    # 2) Blok kode -> kotak abu-abu (tabel 1 sel; cara paling andal agar jadi kotak di Docs)
    codes: list[str] = []

    def take_code(m):
        lang = (re.search(r"language-([\w+-]+)", m.group(1) or "") or [None, ""])[1]
        lines = m.group(2).rstrip("\n").split("\n")
        rows = "<br>".join(
            re.sub(r"^( +)", lambda s: "&nbsp;" * len(s.group(1)), ln.replace("\t", "    ")) for ln in lines
        )
        label = f'<p style="font-size:8pt;color:#64748b;margin:0 0 2pt">{html.escape(lang)}</p>' if lang else ""
        codes.append(
            '<table border="0" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%">'
            f'<tr><td style="background:#f1f5f9;border:1px solid #cbd5e1">{label}'
            f'<p style="font-family:{MONO};font-size:9.5pt;margin:0;line-height:1.35">{rows}</p></td></tr></table>'
            '<p style="margin:0">&nbsp;</p>'
        )
        return f"@@COD{len(codes) - 1}@@"

    body = re.sub(r"<pre><code([^>]*)>([\s\S]*?)</code></pre>", take_code, body)
    body = re.sub(r"<p>\s*(@@(?:BLK|COD)\d+@@)\s*</p>", r"\1", body)

    # 3) Gaya inline untuk elemen lain
    body = body.replace("<p>", '<p style="margin:0 0 8pt;line-height:1.4">')
    body = body.replace("<blockquote>", '<blockquote style="margin:8pt 0;padding:4pt 10pt;border-left:3pt solid #4f46e5;color:#475569">')
    body = re.sub(r"<code>", f'<code style="font-family:{MONO};color:#b91c1c">', body)
    body = re.sub(r"<hr\s*/?>", '<hr style="border:0;border-top:1px solid #cbd5e1;margin:12pt 0">', body)

    # 4) Tabel: header tebal, garis sel, baris selang-seling, perataan kolom
    state = {"body": False, "n": 0, "bg": ""}

    def table_tag(m):
        tag = m.group(0)
        if tag.startswith("<table"):
            state.update(body=False, n=0, bg="")
            return '<table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;margin:6pt 0">'
        if tag == "<tbody>":
            state.update(body=True, n=0)
            return tag
        if tag == "<tr>":
            if state["body"]:
                state["n"] += 1
                state["bg"] = "#f8fafc" if state["n"] % 2 == 0 else "#ffffff"
            else:
                state["bg"] = ""
            return tag
        a = re.search(r'align="(\w+)"|text-align:\s*(\w+)', tag)
        al = f' align="{a.group(1) or a.group(2)}"' if a else ""
        if tag.startswith("<th"):
            return f'<th{al} style="{CELL}background:#e2e8f0;font-weight:bold">'
        bg = f"background:{state['bg']};" if state["bg"] else ""
        return f'<td{al} style="{CELL}{bg}">'

    body = re.sub(r"<table(?:\s[^>]*)?>|<tbody>|<tr>|<th(?:\s[^>]*)?>|<td(?:\s[^>]*)?>", table_tag, body)

    # 5) Kembalikan blok, kode, dan rumus inline
    body = re.sub(r"@@BLK(\d+)@@", lambda m: blocks[int(m.group(1))], body)
    body = re.sub(r"@@COD(\d+)@@", lambda m: codes[int(m.group(1))], body)
    body = re.sub(r"@@INL(\d+)@@", lambda m: inlines[int(m.group(1))], body)

    return (f'<html><head><meta charset="utf-8"></head>'
            f'<body style="font-family:{FONT};font-size:11pt">{body}</body></html>')