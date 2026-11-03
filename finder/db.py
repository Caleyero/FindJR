# -*- coding: utf-8 -*-
"""
Esquema del índice de FinderJR (data/finder_index.db): archivos, documentos y su
índice de texto completo (SQLite FTS5, insensible a acentos).
"""

import sqlite3

from .config import INDEX_DB_PATH, ensure_data_dir

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS archivos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ruta          TEXT UNIQUE NOT NULL,     -- ruta local del intérprete
    ruta_rel      TEXT NOT NULL,            -- relativa a la Biblioteca
    carpeta       TEXT NOT NULL,            -- primer nivel bajo la Biblioteca
    nombre        TEXT NOT NULL,
    ext           TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    paginas       INTEGER DEFAULT 0,
    necesita_ocr  INTEGER DEFAULT 0,
    estado        TEXT DEFAULT 'OK',        -- OK | ERROR
    error         TEXT,
    indexado_en   TEXT
);

CREATE TABLE IF NOT EXISTS documentos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    archivo_id    INTEGER NOT NULL REFERENCES archivos(id) ON DELETE CASCADE,
    primera_pag   INTEGER NOT NULL DEFAULT 1,
    ultima_pag    INTEGER NOT NULL DEFAULT 1,
    num_paginas   INTEGER NOT NULL DEFAULT 1,
    nombre        TEXT NOT NULL,
    ruta_rel      TEXT NOT NULL,
    carpeta       TEXT NOT NULL,
    mtime         REAL NOT NULL DEFAULT 0,  -- fecha de modificación del archivo
    texto         TEXT,
    sin_texto     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_docs_archivo ON documentos(archivo_id);
CREATE INDEX IF NOT EXISTS idx_docs_mtime ON documentos(mtime);

CREATE VIRTUAL TABLE IF NOT EXISTS documentos_fts USING fts5(
    nombre, ruta_rel, texto,
    content='documentos', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS documentos_ai AFTER INSERT ON documentos BEGIN
    INSERT INTO documentos_fts(rowid, nombre, ruta_rel, texto) VALUES (new.id, new.nombre, new.ruta_rel, new.texto);
END;
CREATE TRIGGER IF NOT EXISTS documentos_ad AFTER DELETE ON documentos BEGIN
    INSERT INTO documentos_fts(documentos_fts, rowid, nombre, ruta_rel, texto)
    VALUES ('delete', old.id, old.nombre, old.ruta_rel, old.texto);
END;
CREATE TRIGGER IF NOT EXISTS documentos_au AFTER UPDATE OF nombre, ruta_rel, texto ON documentos BEGIN
    INSERT INTO documentos_fts(documentos_fts, rowid, nombre, ruta_rel, texto)
    VALUES ('delete', old.id, old.nombre, old.ruta_rel, old.texto);
    INSERT INTO documentos_fts(rowid, nombre, ruta_rel, texto) VALUES (new.id, new.nombre, new.ruta_rel, new.texto);
END;
"""

_TABLAS_V1 = ("doc_entidad", "entidades", "alias", "nif_desconocidos")


def get_conn() -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(INDEX_DB_PATH, timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _drop_fts(conn: sqlite3.Connection) -> None:
    for tr in ("documentos_ai", "documentos_ad", "documentos_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {tr};")
    conn.execute("DROP TABLE IF EXISTS documentos_fts;")


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """v1 (con entidades y clasificación) -> v2 (solo texto), conservando el texto extraído."""
    conn.execute("PRAGMA foreign_keys=OFF;")
    with conn:
        for t in _TABLAS_V1:
            conn.execute(f"DROP TABLE IF EXISTS {t};")
        _drop_fts(conn)
        conn.execute("ALTER TABLE documentos RENAME TO documentos_v1;")
        conn.execute("DROP INDEX IF EXISTS idx_docs_archivo;")
        conn.executescript(_SCHEMA)
        conn.execute(
            """INSERT INTO documentos (id, archivo_id, primera_pag, ultima_pag, num_paginas, nombre, ruta_rel,
                                       carpeta, mtime, texto, sin_texto)
               SELECT d.id, d.archivo_id, d.primera_pag, d.ultima_pag, d.num_paginas, d.nombre, d.ruta_rel,
                      d.carpeta, a.mtime, d.texto, d.sin_texto
               FROM documentos_v1 d JOIN archivos a ON a.id = d.archivo_id"""
        )
        conn.execute("DROP TABLE documentos_v1;")
        conn.execute("DELETE FROM meta WHERE k LIKE 'dbges%' OR k = 'meta_ver';")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("VACUUM;")


def init_db() -> None:
    conn = get_conn()
    ver = conn.execute("PRAGMA user_version;").fetchone()[0]
    if ver == 1:
        _migrate_v1(conn)
    elif ver not in (0, SCHEMA_VERSION):
        with conn:
            _drop_fts(conn)
            for t in ("documentos", "archivos", "meta") + _TABLAS_V1:
                conn.execute(f"DROP TABLE IF EXISTS {t};")
    with conn:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
    conn.close()


def meta_get(conn: sqlite3.Connection, k: str, default: str = "") -> str:
    r = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return r["v"] if r and r["v"] is not None else default


def meta_set(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))
