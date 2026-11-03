"""Modul tool riset akademik (jurnal/referensi), pencarian Google index, olah data SQL, dan software engineering.
Diimpor otomatis oleh tools.py.
"""
import ast
import csv
import io
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, quote_plus, urlparse

import requests

from tools import _clip, _strip_html, tool

# --------------------------------------------------------------------------
# 1. JURNAL & REFERENSI ILMIAH
# --------------------------------------------------------------------------
@tool(
    "journal_search",
    "Cari artikel jurnal ilmiah, prosiding konferensi, dan paper penelitian dari Semantic Scholar, Crossref, "
    "dan arXiv. Mengembalikan judul, penulis, tahun, publikasi, jumlah sitasi, tautan DOI, ringkasan abstrak, "
    "serta format sitasi APA. Gunakan untuk mencari referensi akademis terpercaya.",
    {
        "query": {"type": "string", "description": "Topik atau judul penelitian yang dicari, mis. 'transformer attention mechanisms' atau 'deep learning medical imaging'."},
        "limit": {"type": "integer", "description": "Jumlah paper yang diambil (1-10). Default 6."},
        "year_min": {"type": "integer", "description": "Tahun publikasi minimal, mis. 2020. Opsional."},
    },
    ["query"],
    group="Riset & Jurnal",
)
def journal_search(query: str, limit: int = 6, year_min: int | None = None):
    limit = max(1, min(int(limit), 10))
    results = []

    # Sumber 1: Semantic Scholar Graph API (Gratis, sangat relevan, banyak metadata AI/CS/Medis)
    try:
        fields = "title,authors,year,venue,citationCount,abstract,openAccessPdf,externalIds,url"
        url = f"https://api.semanticscholar.org/graph/v1/paper/search?query={quote_plus(query)}&limit={limit}&fields={fields}"
        if year_min:
            url += f"&year={year_min}-2026"
        resp = requests.get(url, timeout=12, headers={"User-Agent": "er-agent/1.0"})
        if resp.ok:
            data = resp.json().get("data", [])
            for p in data:
                authors = [a.get("name", "") for a in p.get("authors", []) if a.get("name")]
                doi = (p.get("externalIds") or {}).get("DOI")
                doi_url = f"https://doi.org/{doi}" if doi else (p.get("url") or "")
                pdf_url = (p.get("openAccessPdf") or {}).get("url")
                
                # Format sitasi ringkas APA
                auth_str = (authors[0] + " et al.") if len(authors) > 2 else ", ".join(authors) if authors else "Anonim"
                year_str = str(p.get("year") or "n.d.")
                apa = f"{auth_str} ({year_str}). {p.get('title')}. {p.get('venue') or 'Journal'}. {doi_url}"

                results.append({
                    "judul": p.get("title"),
                    "penulis": authors[:6],
                    "tahun": p.get("year"),
                    "jurnal_venue": p.get("venue") or "-",
                    "sitasi_count": p.get("citationCount", 0),
                    "doi": doi,
                    "url_doi": doi_url,
                    "pdf_url": pdf_url,
                    "abstrak": _clip(p.get("abstract") or "Abstrak tidak tersedia bebas.", 450),
                    "sitasi_apa": apa,
                    "sumber": "Semantic Scholar"
                })
    except Exception:
        pass

    # Sumber 2: Crossref API (bila Semantic Scholar kurang dari limit)
    if len(results) < limit:
        try:
            needed = limit - len(results)
            cr_url = f"https://api.crossref.org/works?query={quote_plus(query)}&rows={needed}&select=DOI,title,author,published,container-title,abstract,is-referenced-by-count,URL"
            resp = requests.get(cr_url, timeout=10, headers={"User-Agent": "er-agent/1.0 (mailto:admin@er-agent.local)"})
            if resp.ok:
                items = resp.json().get("message", {}).get("items", [])
                for it in items:
                    doi = it.get("DOI")
                    title = (it.get("title") or [""])[0]
                    if not title or any(r.get("doi") == doi for r in results):
                        continue
                    authors = []
                    for a in it.get("author", []):
                        fn = a.get("family") or a.get("name")
                        gn = a.get("given", "")
                        if fn:
                            authors.append(f"{fn}, {gn}".strip(", "))
                    
                    year = None
                    pub_parts = (it.get("published") or {}).get("date-parts", [[]])[0]
                    if pub_parts:
                        year = pub_parts[0]
                    
                    if year_min and year and year < year_min:
                        continue

                    venue = (it.get("container-title") or [""])[0]
                    doi_url = it.get("URL") or (f"https://doi.org/{doi}" if doi else "")
                    
                    auth_str = (authors[0] + " et al.") if len(authors) > 2 else ", ".join(authors) if authors else "Anonim"
                    apa = f"{auth_str} ({year or 'n.d.'}). {title}. {venue}. {doi_url}"

                    results.append({
                        "judul": title,
                        "penulis": authors[:5],
                        "tahun": year,
                        "jurnal_venue": venue or "Crossref",
                        "sitasi_count": it.get("is-referenced-by-count", 0),
                        "doi": doi,
                        "url_doi": doi_url,
                        "abstrak": _clip(_strip_html(it.get("abstract") or "Lihat link publikasi untuk abstrak penuh."), 350),
                        "sitasi_apa": apa,
                        "sumber": "Crossref"
                    })
        except Exception:
            pass

    # Sumber 3: arXiv API (bila masih kurang hasil)
    if len(results) < limit:
        try:
            needed = limit - len(results)
            ar_url = f"http://export.arxiv.org/api/query?search_query=all:{quote_plus(query)}&start=0&max_results={needed}&sortBy=relevance"
            resp = requests.get(ar_url, timeout=10)
            if resp.ok:
                root = ET.fromstring(resp.text)
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                for entry in root.findall("atom:entry", ns):
                    title = " ".join((entry.findtext("atom:title", "", ns) or "").split())
                    summary = " ".join((entry.findtext("atom:summary", "", ns) or "").split())
                    published = entry.findtext("atom:published", "", ns)
                    year = int(published[:4]) if published and len(published) >= 4 else None
                    if year_min and year and year < year_min:
                        continue
                    
                    authors = [a.findtext("atom:name", "", ns) for a in entry.findall("atom:author", ns)]
                    arxiv_id = entry.findtext("atom:id", "", ns)
                    pdf_link = None
                    for link in entry.findall("atom:link", ns):
                        if link.attrib.get("title") == "pdf":
                            pdf_link = link.attrib.get("href")
                    
                    auth_str = (authors[0] + " et al.") if len(authors) > 2 else ", ".join(authors)
                    apa = f"{auth_str} ({year or 'n.d.'}). {title}. arXiv preprint {arxiv_id}"

                    results.append({
                        "judul": title,
                        "penulis": authors[:5],
                        "tahun": year,
                        "jurnal_venue": "arXiv Preprint",
                        "sitasi_count": 0,
                        "doi": None,
                        "url_doi": arxiv_id,
                        "pdf_url": pdf_link,
                        "abstrak": _clip(summary, 450),
                        "sitasi_apa": apa,
                        "sumber": "arXiv"
                    })
        except Exception:
            pass

    if not results:
        return {"pesan": f"Tidak ditemukan paper dengan kata kunci '{query}'. Coba istilah bahasa Inggris yang lebih umum atau periksa ejaan.", "hasil": []}

    return {"total_ditemukan": len(results), "query": query, "hasil": results}


@tool(
    "journal_get_citation",
    "Ambil format sitasi lengkap (APA, IEEE, BibTeX, MLA, Chicago) dari sebuah DOI atau paper ilmiah menggunakan Crossref Content Negotiation.",
    {
        "doi": {"type": "string", "description": "Digital Object Identifier, mis. '10.1038/s41586-020-2649-2' atau URL https://doi.org/..."},
        "format": {"type": "string", "description": "Format sitasi: 'apa' | 'bibtex' | 'ieee' | 'chicago' | 'mla'. Default 'apa'."}
    },
    ["doi"],
    group="Riset & Jurnal",
)
def journal_get_citation(doi: str, format: str = "apa"):
    clean_doi = doi.strip()
    if "doi.org/" in clean_doi:
        clean_doi = clean_doi.split("doi.org/", 1)[1].strip()
    
    fmt = format.lower().strip()
    accept_headers = {
        "apa": "text/x-bibliography; style=apa",
        "bibtex": "application/x-bibtex",
        "ieee": "text/x-bibliography; style=ieee",
        "mla": "text/x-bibliography; style=modern-language-association",
        "chicago": "text/x-bibliography; style=chicago-author-date",
    }
    header = accept_headers.get(fmt, accept_headers["apa"])

    url = f"https://doi.org/{clean_doi}"
    try:
        r = requests.get(url, headers={"Accept": header}, timeout=10, allow_redirects=True)
        if r.ok:
            return {"doi": clean_doi, "format": fmt, "sitasi": r.text.strip()}
    except Exception as e:
        return {"error": f"Gagal mengambil sitasi: {e}"}

    return {"error": f"Sitasi untuk DOI {clean_doi} tidak ditemukan atau format tidak didukung."}


@tool(
    "arxiv_search",
    "Cari riset pre-print paling mutakhir di arXiv (Computer Science, Artificial Intelligence, Math, Physics). "
    "Mendukung filter kategori seperti cs.AI, cs.LG, cs.CL, stat.ML.",
    {
        "query": {"type": "string", "description": "Kata kunci atau topik riset."},
        "category": {"type": "string", "description": "Kategori opsional, mis. 'cs.AI', 'cs.LG', 'cs.CV', 'cs.CL'."},
        "limit": {"type": "integer", "description": "Jumlah artikel (1-8). Default 5."}
    },
    ["query"],
    group="Riset & Jurnal",
)
def arxiv_search(query: str, category: str | None = None, limit: int = 5):
    limit = max(1, min(int(limit), 8))
    q = f"all:{query}"
    if category:
        q += f"+AND+cat:{category}"
    url = f"http://export.arxiv.org/api/query?search_query={quote_plus(q)}&start=0&max_results={limit}&sortBy=submittedDate&sortOrder=descending"
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = []
        for entry in root.findall("atom:entry", ns):
            title = " ".join((entry.findtext("atom:title", "", ns) or "").split())
            summary = " ".join((entry.findtext("atom:summary", "", ns) or "").split())
            published = entry.findtext("atom:published", "", ns)
            authors = [a.findtext("atom:name", "", ns) for a in entry.findall("atom:author", ns)]
            pdf = None
            for l in entry.findall("atom:link", ns):
                if l.attrib.get("title") == "pdf":
                    pdf = l.attrib.get("href")
            items.append({
                "judul": title,
                "penulis": authors[:6],
                "tanggal_publikasi": published[:10] if published else "-",
                "arxiv_url": entry.findtext("atom:id", "", ns),
                "pdf_url": pdf,
                "abstrak": _clip(summary, 500)
            })
        return {"query": query, "total": len(items), "hasil": items}
    except Exception as e:
        return {"error": f"Gagal menghubungi arXiv: {e}"}


# --------------------------------------------------------------------------
# 2. PENCARIAN GOOGLE INDEX & WEB RESMI
# --------------------------------------------------------------------------
@tool(
    "google_search",
    "Cari informasi dari index Google secara langsung. Menggunakan Google Custom Search API bila GOOGLE_CSE_API_KEY "
    "dan GOOGLE_CSE_ID terkonfigurasi, SerpApi bila SERPAPI_API_KEY tersedia, atau fallback mesin pencari web otomatis.",
    {
        "query": {"type": "string", "description": "Kata kunci pencarian Google."},
        "limit": {"type": "integer", "description": "Jumlah hasil pencarian (1-10). Default 5."},
        "site": {"type": "string", "description": "Batasi pencarian ke domain tertentu (mis. 'github.com', 'ac.id', 'gov'). Opsional."}
    },
    ["query"],
    group="Web",
)
def google_search(query: str, limit: int = 5, site: str | None = None):
    limit = max(1, min(int(limit), 10))
    full_query = f"{query} site:{site}" if site else query

    # Opsi 1: Google Custom Search JSON API resmi
    cse_key = os.getenv("GOOGLE_CSE_API_KEY")
    cse_id = os.getenv("GOOGLE_CSE_ID") or os.getenv("GOOGLE_SEARCH_CX")
    if cse_key and cse_id:
        try:
            r = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": cse_key, "cx": cse_id, "q": full_query, "num": limit},
                timeout=12
            )
            if r.ok:
                items = r.json().get("items", [])
                out = []
                for it in items:
                    out.append({
                        "judul": it.get("title"),
                        "url": it.get("link"),
                        "cuplikan": _clip(it.get("snippet", ""), 300),
                        "sumber": "Google Programmable Search"
                    })
                if out:
                    return out
        except Exception:
            pass

    # Opsi 2: SerpApi (Google Search Engine scraper resmi)
    serpapi_key = os.getenv("SERPAPI_API_KEY")
    if serpapi_key:
        try:
            r = requests.get(
                "https://serpapi.com/search.json",
                params={"engine": "google", "q": full_query, "api_key": serpapi_key, "num": limit},
                timeout=15
            )
            if r.ok:
                data = r.json()
                organic = data.get("organic_results", [])
                out = []
                for o in organic[:limit]:
                    out.append({
                        "judul": o.get("title"),
                        "url": o.get("link"),
                        "cuplikan": _clip(o.get("snippet", ""), 300),
                        "sumber": "SerpApi Google"
                    })
                if out:
                    return out
        except Exception:
            pass

    # Opsi 3: Tavily API bila ada
    tavily_key = os.getenv("TAVILY_API_KEY")
    if tavily_key:
        try:
            r = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": tavily_key, "query": full_query, "max_results": limit},
                timeout=15
            )
            if r.ok:
                results = r.json().get("results", [])
                return [
                    {"judul": x.get("title"), "url": x.get("url"), "cuplikan": _clip(x.get("content", ""), 300), "sumber": "Tavily"}
                    for x in results
                ]
        except Exception:
            pass

    # Opsi 4: DuckDuckGo HTML Fallback
    try:
        r = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": full_query},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
            timeout=12
        )
        if r.ok:
            from urllib.parse import parse_qs, unquote, urlparse
            out = []
            for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>(?:.*?class="result__snippet"[^>]*>(.*?)</a>)?', r.text, re.S):
                href = m.group(1)
                if "uddg=" in href:
                    href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
                out.append({
                    "judul": _strip_html(m.group(2)),
                    "url": href,
                    "cuplikan": _strip_html(m.group(3) or ""),
                    "sumber": "Web Search Index"
                })
                if len(out) >= limit:
                    break
            if out:
                return out
    except Exception:
        pass

    return [{"catatan": "Tidak dapat menyelesaikan pencarian index web. Cek koneksi internet server atau konfigurasi GOOGLE_CSE_API_KEY / SERPAPI_API_KEY / TAVILY_API_KEY."}]


# --------------------------------------------------------------------------
# 3. PENGOLAHAN DATA & ANALISIS SQL TANPA LIMIT
# --------------------------------------------------------------------------
@tool(
    "dataset_query_sql",
    "Jalankan query SQL SQLite langsung pada dataset tabular (CSV / data tabel). "
    "Memungkinkan aggregasi (SUM, AVG, COUNT, GROUP BY), filter kompleks (WHERE, LIKE, IN), "
    "sorting (ORDER BY), dan join. Sangat cepat dan mengolah data tanpa batasan token prompt.",
    {
        "csv_data": {"type": "string", "description": "Isi dataset dalam format CSV (baris pertama harus nama kolom)."},
        "sql_query": {"type": "string", "description": "Query SQL SQLite yang dijalankan. Nama tabel otomatis adalah 'data'. Contoh: 'SELECT status, count(*), avg(nilai) FROM data GROUP BY status ORDER BY count(*) DESC'"},
        "table_name": {"type": "string", "description": "Nama tabel dalam SQL. Default 'data'."}
    },
    ["csv_data", "sql_query"],
    group="Data & Analisis",
)
def dataset_query_sql(csv_data: str, sql_query: str, table_name: str = "data"):
    if not csv_data.strip():
        raise ValueError("Data CSV kosong.")
    
    # Bersihkan nama tabel
    tbl = re.sub(r"\W+", "_", table_name).strip("_") or "data"

    # Buat SQLite in-memory
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()

    try:
        reader = csv.reader(io.StringIO(csv_data.strip()))
        headers = next(reader, None)
        if not headers:
            raise ValueError("Data CSV tidak memiliki header kolom.")

        # Sanitasi nama kolom untuk SQL
        clean_cols = []
        for i, h in enumerate(headers):
            c = re.sub(r"\W+", "_", h.strip()).strip("_")
            clean_cols.append(c if c else f"col_{i}")

        col_defs = ", ".join(f'"{col}" TEXT' for col in clean_cols)
        cursor.execute(f'CREATE TABLE "{tbl}" ({col_defs})')

        # Insert rows
        placeholders = ", ".join(["?"] * len(clean_cols))
        rows_to_insert = []
        row_count = 0
        for row in reader:
            if not row:
                continue
            # Pad atau potong jika kolom tidak pas
            if len(row) < len(clean_cols):
                row = row + [""] * (len(clean_cols) - len(row))
            else:
                row = row[:len(clean_cols)]
            rows_to_insert.append(row)
            row_count += 1
            if len(rows_to_insert) >= 5000:
                cursor.executemany(f'INSERT INTO "{tbl}" VALUES ({placeholders})', rows_to_insert)
                rows_to_insert = []
        
        if rows_to_insert:
            cursor.executemany(f'INSERT INTO "{tbl}" VALUES ({placeholders})', rows_to_insert)

        # Eksekusi query pengguna
        t0 = time.time()
        cursor.execute(sql_query)
        col_names = [d[0] for d in cursor.description] if cursor.description else []
        res_rows = cursor.fetchall()
        duration_ms = round((time.time() - t0) * 1000, 2)

        # Format output Markdown Table
        max_rows_display = 150
        displayed_rows = res_rows[:max_rows_display]

        md_lines = []
        md_lines.append("| " + " | ".join(str(c) for c in col_names) + " |")
        md_lines.append("| " + " | ".join(["---"] * len(col_names)) + " |")
        for r in displayed_rows:
            md_lines.append("| " + " | ".join(str(v) if v is not None else "NULL" for v in r) + " |")

        truncated = len(res_rows) > max_rows_display
        return {
            "total_baris_sumber": row_count,
            "total_hasil_query": len(res_rows),
            "eksekusi_ms": duration_ms,
            "tabel_markdown": "\n".join(md_lines),
            "terpotong": f"Hanya menampilkan {max_rows_display} baris pertama dari total {len(res_rows)} baris hasil." if truncated else False
        }
    except Exception as e:
        return {"error": f"Error SQL: {e}"}
    finally:
        conn.close()


@tool(
    "dataset_summary_stats",
    "Hitung ringkasan statistik deskriptif menyeluruh dari sebuah dataset CSV (jumlah baris, tipe kolom, "
    "missing values, nilai unik, min, max, mean, persentil untuk numerik, serta modus/frekuensi tertinggi untuk kategori).",
    {
        "csv_data": {"type": "string", "description": "Isi dataset teks CSV."},
        "sample_rows": {"type": "integer", "description": "Maksimum baris yang dihitung. Default 50000."}
    },
    ["csv_data"],
    group="Data & Analisis",
)
def dataset_summary_stats(csv_data: str, sample_rows: int = 50000):
    if not csv_data.strip():
        raise ValueError("Data CSV kosong.")
    
    reader = csv.reader(io.StringIO(csv_data.strip()))
    headers = next(reader, None)
    if not headers:
        raise ValueError("Header CSV tidak ditemukan.")

    columns = {h: [] for h in headers}
    count = 0
    for row in reader:
        if not row:
            continue
        count += 1
        for i, h in enumerate(headers):
            val = row[i].strip() if i < len(row) else ""
            columns[h].append(val)
        if count >= sample_rows:
            break

    profile = []
    for h in headers:
        vals = columns[h]
        non_empty = [v for v in vals if v != ""]
        missing = len(vals) - len(non_empty)
        unique_cnt = len(set(non_empty))

        # Coba konversi numerik
        num_vals = []
        for v in non_empty:
            try:
                num_vals.append(float(v.replace(",", "")))
            except ValueError:
                pass
        
        is_numeric = len(num_vals) > (len(non_empty) * 0.75) if non_empty else False
        
        col_stat = {
            "kolom": h,
            "total_data": len(vals),
            "kosong": missing,
            "persen_kosong": f"{(missing / len(vals) * 100):.1f}%" if vals else "0%",
            "nilai_unik": unique_cnt,
            "tipe_terdeteksi": "Numerik" if is_numeric else "Kategori / Teks"
        }

        if is_numeric and num_vals:
            num_vals.sort()
            n = len(num_vals)
            col_stat.update({
                "min": num_vals[0],
                "max": num_vals[-1],
                "mean": round(sum(num_vals) / n, 2),
                "median": num_vals[n // 2],
            })
        else:
            # Top 3 most frequent values
            from collections import Counter
            freq = Counter(non_empty).most_common(3)
            col_stat["top_frekuensi"] = [f"{k} ({v}x)" for k, v in freq]

        profile.append(col_stat)

    return {
        "total_baris_dianalisis": count,
        "total_kolom": len(headers),
        "profil_kolom": profile
    }


# --------------------------------------------------------------------------
# 4. SOFTWARE ENGINEERING & CODE QUALITY
# --------------------------------------------------------------------------
@tool(
    "code_syntax_check",
    "Analisis sintaks dan validasi kode Python, JSON, atau regex sebelum dijalankan atau diimplementasikan.",
    {
        "code": {"type": "string", "description": "Potongan kode atau data yang akan diuji."},
        "language": {"type": "string", "description": "'python' | 'json' | 'regex'"}
    },
    ["code", "language"],
    group="Software Engineer",
)
def code_syntax_check(code: str, language: str):
    lang = language.lower().strip()
    if lang == "python":
        try:
            ast.parse(code)
            return {"valid": True, "bahasa": "python", "pesan": "Sintaks Python valid tanpa syntax error."}
        except SyntaxError as e:
            return {
                "valid": False, "bahasa": "python",
                "error": str(e.msg), "baris": e.lineno, "kolom": e.offset, "teks_baris": e.text
            }
    elif lang == "json":
        try:
            parsed = json.loads(code)
            return {"valid": True, "bahasa": "json", "tipe_root": type(parsed).__name__, "jumlah_elemen": len(parsed) if isinstance(parsed, (dict, list)) else 1}
        except json.JSONDecodeError as e:
            return {"valid": False, "bahasa": "json", "error": str(e.msg), "baris": e.lineno, "kolom": e.colno}
    elif lang == "regex":
        try:
            re.compile(code)
            return {"valid": True, "bahasa": "regex", "pesan": "Pola Regular Expression valid."}
        except re.error as e:
            return {"valid": False, "bahasa": "regex", "error": str(e)}
    else:
        return {"error": f"Bahasa '{language}' belum didukung untuk syntax check (pilihan: python, json, regex)."}


@tool(
    "code_diff_analyzer",
    "Bandingkan dua versi kode atau teks dan hasilkan Unified Diff terstruktur untuk pull request atau code review.",
    {
        "original_code": {"type": "string", "description": "Kode asli sebelum perubahan."},
        "modified_code": {"type": "string", "description": "Kode baru setelah perubahan."},
        "filename": {"type": "string", "description": "Nama file opsional, mis. 'app.py'."}
    },
    ["original_code", "modified_code"],
    group="Software Engineer",
)
def code_diff_analyzer(original_code: str, modified_code: str, filename: str = "file.py"):
    import difflib
    a_lines = original_code.splitlines(keepends=True)
    b_lines = modified_code.splitlines(keepends=True)
    diff = list(difflib.unified_diff(a_lines, b_lines, fromfile=f"a/{filename}", tofile=f"b/{filename}"))
    diff_text = "".join(diff)
    additions = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return {
        "filename": filename,
        "penambahan_baris": additions,
        "pengurangan_baris": deletions,
        "diff_patch": diff_text if diff_text else "(Tidak ada perbedaan antara kedua kode.)"
    }


# --------------------------------------------------------------------------
# 5. PEMBUATAN DOKUMEN & BERKAS (DOCS, EXCEL, PDF, PPT) - VERCEL SAFE DUAL-STORAGE
# --------------------------------------------------------------------------
import secrets
import tempfile
import db

_cached_gen_dir: str | None = None

def _get_gen_dir() -> str:
    global _cached_gen_dir
    if _cached_gen_dir:
        return _cached_gen_dir
    local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_files")
    try:
        os.makedirs(local_dir, exist_ok=True)
        test_file = os.path.join(local_dir, ".test")
        with open(test_file, "w") as f:
            f.write("1")
        os.remove(test_file)
        _cached_gen_dir = local_dir
        return local_dir
    except Exception:
        tmp_dir = os.path.join(tempfile.gettempdir(), "er_generated")
        os.makedirs(tmp_dir, exist_ok=True)
        _cached_gen_dir = tmp_dir
        return tmp_dir

def _prepare_gen_file(filename: str) -> tuple[str, str, str]:
    folder = _get_gen_dir()
    clean_name = re.sub(r"[^\w.\-]+", "_", filename).strip("_") or "dokumen"
    fid = f"{int(time.time())}_{secrets.token_hex(4)}_{clean_name}"
    filepath = os.path.join(folder, fid)
    return fid, clean_name, filepath

def _save_generated_file(fid: str, clean_name: str, filepath: str, content_type: str) -> None:
    try:
        with open(filepath, "rb") as f:
            file_bytes = f.read()
        if db.enabled():
            try:
                db.upsert_generated_file(fid, clean_name, content_type, file_bytes)
            except Exception:
                pass
    except Exception:
        pass

def _resolve_download_url(fid: str) -> tuple[str, str]:
    base = (os.getenv("APP_BASE_URL") or "https://er-agent-inky.vercel.app").rstrip("/")
    rel_url = f"/api/download/{fid}"
    full_url = f"{base}{rel_url}"
    return rel_url, full_url


@tool(
    "create_word_docx",
    "Buat dan unduh dokumen Microsoft Word (.docx) berformat rapi. Mendukung judul, subjudul, paragraf, daftar poin, dan tabel.",
    {
        "filename": {"type": "string", "description": "Nama berkas, mis. 'Laporan_Analisis.docx'."},
        "title": {"type": "string", "description": "Judul utama dokumen."},
        "sections": {
            "type": "array",
            "description": "Daftar seksi/bab. Tiap seksi berupa objek dengan 'heading' (opsional), 'text' (isi paragraf/teks, pisahkan baris dengan \\n), 'bullets' (daftar poin opsional), dan 'table' (daftar baris tabel opsional, misal [['Kolom 1', 'Kolom 2'], ['Nilai A', 'Nilai B']]).",
            "items": {"type": "object"}
        }
    },
    ["filename", "title", "sections"],
    group="Dokumen & Berkas",
)
def create_word_docx(filename: str, title: str, sections: list):
    try:
        import docx
        from docx.shared import Inches, Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.table import WD_TABLE_ALIGNMENT

        if not filename.lower().endswith(".docx"):
            filename += ".docx"
        fid, clean_name, path = _prepare_gen_file(filename)

        doc = docx.Document()
        
        # Judul Utama
        title_p = doc.add_paragraph()
        title_run = title_p.add_run(title)
        title_run.font.size = Pt(20)
        title_run.font.bold = True
        title_run.font.color.rgb = RGBColor(24, 106, 88)
        title_p.paragraph_format.space_after = Pt(14)

        for sec in sections:
            if not isinstance(sec, dict):
                continue
            h = sec.get("heading")
            if h:
                hp = doc.add_paragraph()
                hrun = hp.add_run(h)
                hrun.font.size = Pt(14)
                hrun.font.bold = True
                hrun.font.color.rgb = RGBColor(30, 41, 59)
                hp.paragraph_format.space_before = Pt(10)
                hp.paragraph_format.space_after = Pt(4)

            t = sec.get("text")
            if t:
                for line in str(t).split("\n"):
                    if line.strip():
                        p = doc.add_paragraph(line.strip())
                        p.paragraph_format.space_after = Pt(4)

            bullets = sec.get("bullets")
            if isinstance(bullets, list):
                for b in bullets:
                    doc.add_paragraph(str(b), style='List Bullet')

            tbl_data = sec.get("table")
            if isinstance(tbl_data, list) and tbl_data:
                rows_count = len(tbl_data)
                cols_count = max(len(r) for r in tbl_data if isinstance(r, list))
                if cols_count > 0:
                    table = doc.add_table(rows=rows_count, cols=cols_count)
                    table.style = 'Table Grid'
                    table.alignment = WD_TABLE_ALIGNMENT.CENTER
                    for r_idx, row in enumerate(tbl_data):
                        if not isinstance(row, list):
                            continue
                        for c_idx, cell in enumerate(row):
                            if c_idx < cols_count:
                                cell_obj = table.cell(r_idx, c_idx)
                                cell_obj.text = str(cell)
                                if r_idx == 0:
                                    for p in cell_obj.paragraphs:
                                        for r in p.runs:
                                            r.font.bold = True
                    doc.add_paragraph().paragraph_format.space_after = Pt(8)

        doc.save(path)
        _save_generated_file(fid, clean_name, path, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        rel_url, full_url = _resolve_download_url(fid)
        return {
            "ok": True,
            "filename": clean_name,
            "download_url": full_url,
            "path": rel_url,
            "pesan": f"Dokumen Word berhasil dibuat: [{clean_name}]({full_url})\nTautan Unduh: {full_url}"
        }
    except Exception as e:
        return {"error": f"Gagal membuat dokumen Word: {e}"}


@tool(
    "create_excel_xlsx",
    "Buat dan unduh berkas Microsoft Excel (.xlsx) dengan format tabel rapi, header tebal, dan auto-fit lebar kolom.",
    {
        "filename": {"type": "string", "description": "Nama berkas, mis. 'Laporan_Keuangan.xlsx'."},
        "sheets": {
            "type": "object",
            "description": "Kamus berisi nama sheet dan datanya (daftar baris). Contoh: {'Ringkasan': [['Kategori', 'Total'], ['Penjualan', 5000000], ['Biaya', 2000000]]}"
        }
    },
    ["filename", "sheets"],
    group="Dokumen & Berkas",
)
def create_excel_xlsx(filename: str, sheets: dict):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        if not filename.lower().endswith(".xlsx"):
            filename += ".xlsx"
        fid, clean_name, path = _prepare_gen_file(filename)

        wb = openpyxl.Workbook()
        # Hapus sheet default kosong jika kita buat sheet baru
        default_sheet = wb.active

        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        header_fill = PatternFill(start_color="186A58", end_color="186A58", fill_type="solid")
        thin_border = Border(
            left=Side(style="thin", color="CBD5E1"),
            right=Side(style="thin", color="CBD5E1"),
            top=Side(style="thin", color="CBD5E1"),
            bottom=Side(style="thin", color="CBD5E1")
        )

        norm_sheets = {}
        if isinstance(sheets, dict):
            norm_sheets = sheets
        elif isinstance(sheets, list):
            if sheets and isinstance(sheets[0], dict) and ("data" in sheets[0] or "rows" in sheets[0]):
                for item in sheets:
                    s_name = item.get("name") or "Sheet1"
                    s_rows = item.get("data") or item.get("rows") or []
                    norm_sheets[s_name] = s_rows
            elif sheets and isinstance(sheets[0], list):
                norm_sheets["Data"] = sheets

        if not norm_sheets:
            norm_sheets = {"Sheet1": [["Informasi"], ["Tidak ada data yang dicantumkan."]]}

        first = True
        for sheet_name, rows in norm_sheets.items():
            if not isinstance(rows, list) or not rows:
                continue
            ws = default_sheet if first else wb.create_sheet(title=str(sheet_name)[:30])
            first = False
            ws.title = str(sheet_name)[:30]

            for r_idx, row in enumerate(rows, start=1):
                if not isinstance(row, list):
                    continue
                for c_idx, val in enumerate(row, start=1):
                    cell = ws.cell(row=r_idx, column=c_idx, value=val)
                    cell.border = thin_border
                    if r_idx == 1:
                        cell.font = header_font
                        cell.fill = header_fill
                        cell.alignment = Alignment(horizontal="left", vertical="center")
                    else:
                        cell.font = Font(name="Calibri", size=11)

            # Auto-fit kolom
            for col in ws.columns:
                max_len = max(len(str(c.value or '')) for c in col)
                col_letter = get_column_letter(col[0].column)
                ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

        wb.save(path)
        _save_generated_file(fid, clean_name, path, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        rel_url, full_url = _resolve_download_url(fid)
        return {
            "ok": True,
            "filename": clean_name,
            "download_url": full_url,
            "path": rel_url,
            "pesan": f"Berkas Excel berhasil dibuat: [{clean_name}]({full_url})\nTautan Unduh: {full_url}"
        }
    except Exception as e:
        return {"error": f"Gagal membuat berkas Excel: {e}"}


@tool(
    "create_pdf_doc",
    "Buat dan unduh dokumen PDF (.pdf) resmi dan profesional dengan judul, bab, teks, dan tabel data.",
    {
        "filename": {"type": "string", "description": "Nama berkas, mis. 'Laporan_Riset.pdf'."},
        "title": {"type": "string", "description": "Judul laporan PDF."},
        "content_blocks": {
            "type": "array",
            "description": "Daftar blok konten: [{'heading': '...', 'text': '...', 'table': [['col1', 'col2'], ['a', 'b']]}]",
            "items": {"type": "object"}
        }
    },
    ["filename", "title", "content_blocks"],
    group="Dokumen & Berkas",
)
def create_pdf_doc(filename: str, title: str, content_blocks: list):
    try:
        import html
        from reportlab.lib.pagesizes import letter, A4
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors

        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        fid, clean_name, path = _prepare_gen_file(filename)

        doc = SimpleDocTemplate(path, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            'ReportTitle',
            parent=styles['Heading1'],
            fontName='Helvetica-Bold',
            fontSize=20,
            leading=24,
            textColor=colors.HexColor('#186A58'),
            spaceAfter=14
        )

        heading_style = ParagraphStyle(
            'SectionHeading',
            parent=styles['Heading2'],
            fontName='Helvetica-Bold',
            fontSize=13,
            leading=17,
            textColor=colors.HexColor('#0F172A'),
            spaceBefore=10,
            spaceAfter=4
        )

        body_style = ParagraphStyle(
            'BodyTextCustom',
            parent=styles['Normal'],
            fontName='Helvetica',
            fontSize=10,
            leading=14,
            textColor=colors.HexColor('#334155'),
            spaceAfter=6
        )

        elements = []
        elements.append(Paragraph(html.escape(title), title_style))
        elements.append(Spacer(1, 10))

        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            h = block.get("heading")
            if h:
                elements.append(Paragraph(html.escape(h), heading_style))

            t = block.get("text")
            if t:
                for line in str(t).split("\n"):
                    if line.strip():
                        elements.append(Paragraph(html.escape(line.strip()), body_style))

            tbl = block.get("table")
            if isinstance(tbl, list) and tbl:
                formatted_tbl = []
                for row in tbl:
                    if isinstance(row, list):
                        formatted_tbl.append([Paragraph(html.escape(str(c)), body_style) for c in row])
                if formatted_tbl:
                    t_obj = Table(formatted_tbl, colWidths=None)
                    t_obj.setStyle(TableStyle([
                        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E2E8F0')),
                        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
                        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                        ('TOPPADDING', (0, 0), (-1, -1), 5),
                        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
                    ]))
                    elements.append(t_obj)
                    elements.append(Spacer(1, 8))

        doc.build(elements)
        _save_generated_file(fid, clean_name, path, "application/pdf")
        rel_url, full_url = _resolve_download_url(fid)
        return {
            "ok": True,
            "filename": clean_name,
            "download_url": full_url,
            "path": rel_url,
            "pesan": f"Dokumen PDF berhasil dibuat: [{clean_name}]({full_url})\nTautan Unduh: {full_url}"
        }
    except Exception as e:
        return {"error": f"Gagal membuat dokumen PDF: {e}"}


@tool(
    "create_powerpoint_pptx",
    "Buat dan unduh presentasi slide Microsoft PowerPoint (.pptx) lengkap dengan slide judul dan slide isi.",
    {
        "filename": {"type": "string", "description": "Nama berkas, mis. 'Presentasi_Proyek.pptx'."},
        "title": {"type": "string", "description": "Judul presentasi."},
        "subtitle": {"type": "string", "description": "Subjudul presentasi."},
        "slides": {
            "type": "array",
            "description": "Daftar slide materi: [{'title': 'Judul Slide', 'bullets': ['Poin 1', 'Poin 2']}]",
            "items": {"type": "object"}
        }
    },
    ["filename", "title", "slides"],
    group="Dokumen & Berkas",
)
def create_powerpoint_pptx(filename: str, title: str, slides: list, subtitle: str = ""):
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
        from pptx.dml.color import RGBColor

        if not filename.lower().endswith(".pptx"):
            filename += ".pptx"
        fid, clean_name, path = _prepare_gen_file(filename)

        prs = Presentation()

        # Slide 1: Judul
        title_slide_layout = prs.slide_layouts[0]
        slide = prs.slides.add_slide(title_slide_layout)
        t_box = slide.shapes.title
        sub_box = slide.placeholders[1]
        t_box.text = title
        if subtitle:
            sub_box.text = subtitle

        # Slide-slide konten
        bullet_slide_layout = prs.slide_layouts[1]
        for s_data in slides:
            if not isinstance(s_data, dict):
                continue
            slide = prs.slides.add_slide(bullet_slide_layout)
            slide.shapes.title.text = s_data.get("title", "Materi")
            body_shape = slide.placeholders[1]
            tf = body_shape.text_frame

            bullets = s_data.get("bullets", [])
            if isinstance(bullets, list) and bullets:
                tf.text = str(bullets[0])
                for b in bullets[1:]:
                    p = tf.add_paragraph()
                    p.text = str(b)
                    p.level = 0
            elif s_data.get("text"):
                tf.text = str(s_data.get("text"))

        prs.save(path)
        _save_generated_file(fid, clean_name, path, "application/vnd.openxmlformats-officedocument.presentationml.presentation")
        rel_url, full_url = _resolve_download_url(fid)
        return {
            "ok": True,
            "filename": clean_name,
            "download_url": full_url,
            "path": rel_url,
            "pesan": f"Presentasi PowerPoint berhasil dibuat: [{clean_name}]({full_url})\nTautan Unduh: {full_url}"
        }
    except Exception as e:
        return {"error": f"Gagal membuat presentasi PowerPoint: {e}"}


@tool(
    "create_html_page",
    "Buat dan publikasikan halaman web HTML interaktif yang dapat dipratinjau langsung serta dibagikan dengan tautan ber-passcode acak.",
    {
        "title": {"type": "string", "description": "Judul halaman web/aplikasi HTML."},
        "html_content": {"type": "string", "description": "Kode HTML lengkap (termasuk tag <style> dan <script> bila ada)."}
    },
    ["title", "html_content"],
    group="Dokumen & Berkas",
)
def create_html_page(title: str, html_content: str):
    try:
        import share_store
        res = share_store.create_share(html=html_content, title=title)
        full_url = res.get("full_url") or f"https://er-agent-inky.vercel.app{res['path']}"
        return {
            "ok": True,
            "title": res["title"],
            "share_url": full_url,
            "path": res["path"],
            "passcode": res["passcode"],
            "pesan": (
                f"Halaman HTML berhasil dibuat dan siap dibagikan!\n"
                f"- Tautan Akses: {full_url}\n"
                f"- Passcode: {res['passcode']}\n"
                f"Pengguna dapat langsung mengklik tautan di atas dan memasukkan passcode untuk melihat halaman."
            )
        }
    except Exception as e:
        return {"error": f"Gagal membuat halaman HTML: {e}"}


