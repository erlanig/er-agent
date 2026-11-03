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
