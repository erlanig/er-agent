"""Halaman publik untuk verifikasi Google OAuth (Branding): beranda, kebijakan privasi, dan verifikasi Search Console.

Pasang di app.py dengan:
    import legal
    legal.register(app)

Env (opsional):
    PRIVACY_CONTACT_EMAIL  email kontak yang tampil di kebijakan privasi (WAJIB diisi sebelum submit ke Google)
    APP_OPERATOR           nama pengelola aplikasi (default: Er)
    APP_NAME               nama aplikasi (default: ER Agent)

Semua halaman ini publik (tanpa login) agar bisa diakses reviewer Google.
"""
import html
import os
import re

from flask import Response, abort

UPDATED = "27 October 2026"

_CSS = """
:root{color-scheme:light dark;--bg:#f8fafc;--ink:#0f172a;--muted:#475569;--line:#e2e8f0;--accent:#4f46e5;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#0b0f17;--ink:#f1f5f9;--muted:#94a3b8;--line:#1e293b;--accent:#818cf8;--card:#111827}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:760px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:28px;line-height:1.25;margin:0 0 6px}
h2{font-size:19px;margin:32px 0 8px}
p,li{color:var(--ink)}
.muted{color:var(--muted);font-size:14px}
a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 18px;margin:12px 0}
table{border-collapse:collapse;width:100%;font-size:14.5px;margin:10px 0}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
th{background:var(--card)}
nav{display:flex;gap:16px;margin-bottom:24px;font-size:14px;flex-wrap:wrap}
footer{margin-top:40px;font-size:13px;color:var(--muted)}
"""


def _cfg():
    name = html.escape(os.getenv("APP_NAME", "ER Agent"))
    operator = html.escape(os.getenv("APP_OPERATOR", "Er"))
    email = html.escape(os.getenv("PRIVACY_CONTACT_EMAIL", "").strip()) or "[set PRIVACY_CONTACT_EMAIL]"
    return name, operator, email


def _page(title: str, body: str) -> Response:
    name, _, _ = _cfg()
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - {name}</title>
<meta name="description" content="{name} is a private AI assistant that helps its owner manage Google Calendar, Gmail, Drive, Docs, Sheets and Tasks, and automate routine tasks.">
<style>{_CSS}</style></head>
<body><main>
<nav><a href="/about">Home</a><a href="/privacy">Privacy Policy</a><a href="/">Open the app</a></nav>
{body}
<footer>&copy; {name}. Last updated: {UPDATED}.</footer>
</main></body></html>"""
    return Response(doc, mimetype="text/html")


def about():
    name, operator, email = _cfg()
    body = f"""
<h1>{name}</h1>
<p class="muted">A private AI assistant for productivity and automation, operated by {operator}.</p>

<p>{name} is a web-based AI assistant. The owner types a request in a chat window, and the assistant can
read and manage the owner's own Google data and run routine tasks on the owner's behalf.</p>

<h2>What {name} does</h2>
<ul>
  <li><b>Google Calendar:</b> view upcoming events, find free time slots, create and update events.</li>
  <li><b>Gmail:</b> search and read email, prepare drafts, send or schedule emails, archive and mark messages.</li>
  <li><b>Google Drive, Docs and Sheets:</b> find and read files, create formatted documents and spreadsheets from chat.</li>
  <li><b>Google Tasks and Contacts:</b> list and add tasks, look up contact names and email addresses.</li>
  <li><b>Other tools:</b> calculations, web search, weather, and publishing text posts to the owner's own social account.</li>
</ul>

<div class="card">
<b>You stay in control.</b> Any action that changes data (sending an email, creating an event or document,
posting) is shown to the owner first as an approval card with the exact content, and runs only after the owner clicks
&ldquo;Approve&rdquo;.
</div>

<h2>Your data</h2>
<p>{name} accesses Google data only after the owner signs in with Google and only to perform the owner's requests.
We do not sell Google user data, do not use it for advertising, and do not use it to train generalized AI models.
Read the full <a href="/privacy">Privacy Policy</a>.</p>

<h2>Contact</h2>
<p>Questions or data requests: <a href="mailto:{email}">{email}</a></p>
"""
    return _page("Home", body)


def privacy():
    name, operator, email = _cfg()
    body = f"""
<h1>Privacy Policy</h1>
<p class="muted">{name} &middot; Last updated: {UPDATED}</p>

<p>This policy explains what information {name} (&ldquo;the app&rdquo;) accesses, how it is used, where it is stored,
and the choices available to the user. The app is operated by {operator} (&ldquo;we&rdquo;).
It is a private, password-protected tool.</p>

<h2>1. Information we access</h2>
<p>When the owner connects a Google account, the app requests the following access. Each permission is used only for the
purpose shown.</p>
<table>
<tr><th>Google data</th><th>Why it is needed</th></tr>
<tr><td>Google Calendar (view and edit events)</td><td>Show the agenda, find free time, and create or change events when asked.</td></tr>
<tr><td>Gmail (read, compose, send, modify labels)</td><td>Search and read messages to answer questions, create drafts, send or schedule emails, and archive or mark messages when asked.</td></tr>
<tr><td>Google Drive (read files; create files created by the app)</td><td>Find and read files the user points to, and create new documents, spreadsheets and folders.</td></tr>
<tr><td>Google Docs</td><td>Read documents and create or edit documents requested by the user.</td></tr>
<tr><td>Google Sheets</td><td>Read, create and update spreadsheets requested by the user.</td></tr>
<tr><td>Google Tasks</td><td>List, add and complete tasks.</td></tr>
<tr><td>Google Contacts (read only)</td><td>Look up a person&rsquo;s email address or phone number before sending an invitation or email.</td></tr>
</table>
<p>We also store the information the user types into the app (chat messages and uploaded files), and basic technical data
needed to run the service.</p>

<h2>2. How we use information</h2>
<ul>
  <li>Google data is accessed <b>only when the user makes a request</b> that needs it, and used <b>only to provide that feature to the user</b>.</li>
  <li>Actions that modify data require the user&rsquo;s explicit approval in the app before they run.</li>
  <li>We do not use Google user data for advertising, profiling, or sale, and we do not allow humans to read it except
      with the user&rsquo;s consent, to investigate abuse or a security incident, or to comply with law.</li>
  <li>We do not use Google user data to develop, improve, or train generalized AI or machine-learning models.</li>
</ul>

<h2>3. Sharing and service providers</h2>
<p>We do not sell or rent personal information. To provide the service, content needed for a request (for example, the
text of an email the user asked the assistant to summarize) is sent to the following processors, only for that purpose:</p>
<ul>
  <li><b>AI model providers</b> (large language models accessed through Amazon Web Services Bedrock) to generate responses.</li>
  <li><b>Hosting and database providers</b> (Vercel for hosting; a PostgreSQL database provider for chat history and scheduled jobs).</li>
  <li><b>Google</b> itself, when the app calls Google APIs on the user&rsquo;s behalf.</li>
  <li>Diagram and formula images in generated documents may be rendered by public image services (for example mermaid.ink and latex.codecogs.com); only the diagram or formula text is sent.</li>
</ul>
<p>We may disclose information if required by law.</p>

<h2>4. Storage, retention and security</h2>
<ul>
  <li>The Google authorization (OAuth refresh token) is stored as a protected environment variable on the hosting platform and used only to obtain short-lived access tokens.</li>
  <li>Google data fetched to answer a request is not kept in a separate database. It may appear in the chat transcript, which is stored in the user&rsquo;s browser and, if enabled, in the app database so it is available across the user&rsquo;s devices.</li>
  <li>Emails and posts that the user schedules are stored until they are sent or cancelled.</li>
  <li>Data is transmitted over HTTPS. Access to the app requires a password, and sessions use signed, HTTP-only cookies.</li>
</ul>

<h2>5. User choices and deletion</h2>
<ul>
  <li>Delete individual chats, or all chat history, in the app (Settings &rarr; Delete all history).</li>
  <li>Revoke the app&rsquo;s access to your Google account at any time at
      <a href="https://myaccount.google.com/permissions" rel="noopener">myaccount.google.com/permissions</a>.</li>
  <li>To request deletion of data stored in the app database, email <a href="mailto:{email}">{email}</a>. We will delete it within a reasonable period.</li>
</ul>

<h2>6. Google API Services User Data Policy</h2>
<div class="card">
{name}&rsquo;s use and transfer to any other app of information received from Google APIs will adhere to the
<a href="https://developers.google.com/terms/api-services-user-data-policy" rel="noopener">Google API Services User Data Policy</a>,
including the Limited Use requirements.
</div>

<h2>7. Children</h2>
<p>The app is not directed to children and is not intended for users under 13.</p>

<h2>8. Changes</h2>
<p>We may update this policy. The date at the top shows when it was last changed.</p>

<h2>9. Contact</h2>
<p>{operator} &middot; <a href="mailto:{email}">{email}</a></p>

<h2>Ringkasan (Bahasa Indonesia)</h2>
<p>{name} adalah asisten AI pribadi. Data Google (Kalender, Gmail, Drive, Docs, Sheets, Tasks, Kontak) hanya diakses saat
pengguna meminta, hanya untuk menjalankan permintaan itu, tidak dijual, tidak dipakai untuk iklan, dan tidak dipakai untuk
melatih model AI umum. Aksi yang mengubah data selalu menunggu persetujuan pengguna. Akses dapat dicabut kapan saja di
myaccount.google.com/permissions, dan permintaan penghapusan data dapat dikirim ke {email}.</p>
"""
    return _page("Privacy Policy", body)


def google_site_verification(code: str):
    """Verifikasi Search Console metode 'HTML file': Google meminta /google<kode>.html berisi teks tertentu."""
    if not re.fullmatch(r"[0-9a-zA-Z_-]{8,64}", code):
        abort(404)
    return Response(f"google-site-verification: google{code}.html", mimetype="text/html")


def register(app) -> None:
    app.add_url_rule("/about", "legal_about", about)
    app.add_url_rule("/privacy", "legal_privacy", privacy)
    app.add_url_rule("/google<code>.html", "legal_gsv", google_site_verification)