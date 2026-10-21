"""Penyimpanan riwayat obrolan di Postgres (Neon via Vercel Marketplace).

- Aktif hanya bila env DATABASE_URL diset (pakai URL "pooled" dari Neon, yang berisi -pooler).
- Satu baris per obrolan (JSONB), plus tabel kv kecil untuk daftar proyek.
- psycopg diimpor malas agar aplikasi tetap jalan tanpa database.
- Tabel dibuat otomatis saat pertama kali dipakai.
"""
import os

_ready = False


def enabled() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _connect():
    import psycopg

    # prepare_threshold=None: aman untuk pgbouncer/pooler; autocommit: tiap statement berdiri sendiri
    return psycopg.connect(
        os.environ["DATABASE_URL"], autocommit=True, connect_timeout=10, prepare_threshold=None
    )


def _ensure(conn) -> None:
    global _ready
    if _ready:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chats ("
        "id text PRIMARY KEY, data jsonb NOT NULL, updated bigint NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS kv (k text PRIMARY KEY, v jsonb NOT NULL)")
    _ready = True


def load_all() -> tuple[list, list]:
    """Kembalikan (daftar obrolan, daftar proyek)."""
    with _connect() as conn:
        _ensure(conn)
        chats = [r[0] for r in conn.execute("SELECT data FROM chats ORDER BY updated DESC LIMIT 500").fetchall()]
        row = conn.execute("SELECT v FROM kv WHERE k = 'projects'").fetchone()
    return chats, (row[0] if row else [])


def upsert_chat(chat_id: str, data: dict) -> None:
    from psycopg.types.json import Jsonb

    with _connect() as conn:
        _ensure(conn)
        conn.execute(
            "INSERT INTO chats (id, data, updated) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated = EXCLUDED.updated",
            (chat_id, Jsonb(data), int(data.get("updated") or 0)),
        )


def delete_chat(chat_id: str) -> None:
    with _connect() as conn:
        _ensure(conn)
        conn.execute("DELETE FROM chats WHERE id = %s", (chat_id,))


def set_projects(projects: list) -> None:
    from psycopg.types.json import Jsonb

    with _connect() as conn:
        _ensure(conn)
        conn.execute(
            "INSERT INTO kv (k, v) VALUES ('projects', %s) "
            "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
            (Jsonb(projects),),
        )