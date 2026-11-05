# -*- coding: utf-8 -*-
"""
Esquema del índice de FinderJR (data/finder_index_<fuente>.db, uno por fuente): archivos,
documentos y su índice de texto completo (SQLite FTS5, insensible a acentos).

Duplicados (v3): los archivos idénticos byte a byte (mismo SHA-256) se indexan una sola vez. La copia
principal tiene su fila en `documentos`; las demás solo están en `archivos`, con `copia_de` apuntando a
la principal y sin texto, así que no salen repetidas en los resultados ni engordan el FTS.
"""

import os
import sqlite3
import threading
from typing import Optional

from . import config, etiquetas
from .config import ensure_data_dir

SCHEMA_VERSION = 4

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
    indexado_en   TEXT,
    hash          TEXT,                     -- SHA-256; solo si otro archivo tiene el mismo tamaño
    copia_de      INTEGER REFERENCES archivos(id) ON DELETE SET NULL,  -- id de la copia principal
    ocr           INTEGER                   -- NULL no intentado · 1 texto por OCR · 2 sin resultado (finder/ocr.py)
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
    sin_texto     INTEGER DEFAULT 0,
    otras_rutas   TEXT                      -- rutas de sus copias idénticas (para encontrarlo también por ellas)
);
CREATE INDEX IF NOT EXISTS idx_docs_archivo ON documentos(archivo_id);
CREATE INDEX IF NOT EXISTS idx_docs_mtime ON documentos(mtime);

CREATE VIRTUAL TABLE IF NOT EXISTS documentos_fts USING fts5(
    nombre, ruta_rel, texto, otras_rutas,
    content='documentos', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS documentos_ai AFTER INSERT ON documentos BEGIN
    INSERT INTO documentos_fts(rowid, nombre, ruta_rel, texto, otras_rutas)
    VALUES (new.id, new.nombre, new.ruta_rel, new.texto, new.otras_rutas);
END;
CREATE TRIGGER IF NOT EXISTS documentos_ad AFTER DELETE ON documentos BEGIN
    INSERT INTO documentos_fts(documentos_fts, rowid, nombre, ruta_rel, texto, otras_rutas)
    VALUES ('delete', old.id, old.nombre, old.ruta_rel, old.texto, old.otras_rutas);
END;
CREATE TRIGGER IF NOT EXISTS documentos_au AFTER UPDATE OF nombre, ruta_rel, texto, otras_rutas ON documentos BEGIN
    INSERT INTO documentos_fts(documentos_fts, rowid, nombre, ruta_rel, texto, otras_rutas)
    VALUES ('delete', old.id, old.nombre, old.ruta_rel, old.texto, old.otras_rutas);
    INSERT INTO documentos_fts(rowid, nombre, ruta_rel, texto, otras_rutas)
    VALUES (new.id, new.nombre, new.ruta_rel, new.texto, new.otras_rutas);
END;
"""

_TABLAS_V1 = ("doc_entidad", "entidades", "alias", "nif_desconocidos")


_INICIADAS: set = set()
_INIT_LOCK = threading.Lock()


def _abrir(fid: str) -> sqlite3.Connection:
    ensure_data_dir()
    conn = sqlite3.connect(config.index_db_path(fid), timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    etiquetas.attach(conn, fid)  # etiquetas_<fuente>.db (manuales, fuera del índice regenerable) como «et»
    return conn


def get_conn(fid: Optional[str] = None) -> sqlite3.Connection:
    """Conexión al índice de una fuente (por defecto, la activa), con su esquema garantizado."""
    fid = fid or config.fuente_activa_id()
    if fid not in _INICIADAS:
        init_db(fid)
    return _abrir(fid)


def borrar_indice(fid: str) -> None:
    """Elimina el índice (regenerable) de una fuente. Sus etiquetas no se tocan."""
    with _INIT_LOCK:
        _INICIADAS.discard(fid)
        base = config.index_db_path(fid)
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(base + suf):
                os.remove(base + suf)


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


def _asegurar_v3(conn: sqlite3.Connection) -> None:
    """v2 -> v3: columnas de duplicados, sin volver a extraer nada. Idempotente.
    El índice FTS gana la columna otras_rutas: se recrea y se reconstruye desde `documentos` (una sola vez,
    alrededor de un minuto con ~14.000 documentos)."""
    if "otras_rutas" not in {r[1] for r in conn.execute("PRAGMA table_info(documentos)")}:
        with conn:
            conn.execute("ALTER TABLE documentos ADD COLUMN otras_rutas TEXT;")
    if "otras_rutas" not in {r[1] for r in conn.execute("PRAGMA table_info(documentos_fts)")}:
        with conn:
            _drop_fts(conn)
            conn.executescript(_SCHEMA)
            conn.execute("INSERT INTO documentos_fts(documentos_fts) VALUES ('rebuild');")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(archivos)")}
    with conn:
        if "hash" not in cols:
            conn.execute("ALTER TABLE archivos ADD COLUMN hash TEXT;")
        if "copia_de" not in cols:
            conn.execute("ALTER TABLE archivos ADD COLUMN copia_de INTEGER REFERENCES archivos(id) ON DELETE SET NULL;")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_arch_size ON archivos(size);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_arch_hash ON archivos(hash) WHERE hash IS NOT NULL;")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_arch_copia ON archivos(copia_de) WHERE copia_de IS NOT NULL;")


def _asegurar_v4(conn: sqlite3.Connection) -> None:
    """v3 -> v4: columna `ocr` (resultado del OCR de los escaneados). Idempotente, sin reindexar."""
    if "ocr" not in {r[1] for r in conn.execute("PRAGMA table_info(archivos)")}:
        with conn:
            conn.execute("ALTER TABLE archivos ADD COLUMN ocr INTEGER;")


def init_db(fid: Optional[str] = None) -> None:
    fid = fid or config.fuente_activa_id()
    with _INIT_LOCK:
        _init_db(fid)
        _INICIADAS.add(fid)


def _init_db(fid: str) -> None:
    conn = _abrir(fid)
    ver = conn.execute("PRAGMA user_version;").fetchone()[0]
    if ver == 1:
        _migrate_v1(conn)
    elif ver not in (0, 2, 3, SCHEMA_VERSION):
        with conn:
            _drop_fts(conn)
            for t in ("documentos", "archivos", "meta") + _TABLAS_V1:
                conn.execute(f"DROP TABLE IF EXISTS {t};")
    with conn:
        conn.executescript(_SCHEMA)
    _asegurar_v3(conn)
    _asegurar_v4(conn)
    with conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
    etiquetas.init_schema(conn)
    conn.close()


def meta_get(conn: sqlite3.Connection, k: str, default: str = "") -> str:
    r = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return r["v"] if r and r["v"] is not None else default


def meta_set(conn: sqlite3.Connection, k: str, v: str) -> None:
    conn.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))
