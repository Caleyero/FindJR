# -*- coding: utf-8 -*-
"""
Etiquetas manuales de FinderJR (etiquetas_<fuente>.db en la raíz del proyecto: un catálogo
independiente por cada fuente de documentos).

Las pone siempre el usuario: el programa nunca etiqueta por su cuenta (ver la decisión 1 de
CLAUDE.md). Viven en una base de datos aparte porque el índice (data/) se regenera; se vinculan
a cada documento por su ruta relativa a la Biblioteca, normalizada con «/», de modo que
sobreviven a la reindexación y a la modificación del archivo. Si un archivo se mueve o se
renombra, sus etiquetas quedan huérfanas (no se ven) y reaparecen si vuelve a su ruta.
"""

import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from . import config

ALIAS = "et"

# Paleta fija (texto blanco legible sobre todos)
PALETA = ["#2563eb", "#16a34a", "#dc2626", "#b45309", "#7c3aed",
          "#db2777", "#0e7490", "#4d7c0f", "#c2410c", "#475569"]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {ALIAS}.etiquetas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color      TEXT NOT NULL,
    tecla      INTEGER,                 -- atajo 1-9 (opcional, único)
    creada_en  TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS {ALIAS}.doc_etiqueta (
    ruta_rel     TEXT NOT NULL,         -- relativa a la Biblioteca, separador «/»
    etiqueta_id  INTEGER NOT NULL REFERENCES etiquetas(id) ON DELETE CASCADE,
    asignada_en  TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (ruta_rel, etiqueta_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS {ALIAS}.idx_de_etiqueta ON doc_etiqueta(etiqueta_id);
"""

# Expresión SQL de la clave de un documento del índice (alias «d» = documentos)
CLAVE_DOC = "replace(d.ruta_rel, char(92), '/')"


def clave(ruta_rel: str) -> str:
    return (ruta_rel or "").replace("\\", "/")


def attach(conn: sqlite3.Connection, fid: str) -> None:
    conn.execute(f"ATTACH DATABASE ? AS {ALIAS}", (config.etiquetas_db_path(fid),))
    conn.execute(f"PRAGMA {ALIAS}.journal_mode=WAL;")


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(_SCHEMA)


# ------------------------------------------------------------------------------
# Catálogo
# ------------------------------------------------------------------------------

def listar(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Etiquetas con el número de documentos indexados que llevan cada una."""
    cuentas = {r[0]: r[1] for r in conn.execute(
        f"""SELECT de.etiqueta_id, COUNT(*) FROM documentos d
            JOIN {ALIAS}.doc_etiqueta de ON de.ruta_rel = {CLAVE_DOC} GROUP BY de.etiqueta_id""")}
    sin = conn.execute(
        f"""SELECT COUNT(*) FROM documentos d
            WHERE {CLAVE_DOC} NOT IN (SELECT ruta_rel FROM {ALIAS}.doc_etiqueta)""").fetchone()[0]
    items = [dict(r, total=cuentas.get(r["id"], 0)) for r in conn.execute(
        f"SELECT id, nombre, color, tecla FROM {ALIAS}.etiquetas ORDER BY nombre COLLATE NOCASE")]
    return {"items": items, "sin_etiquetar": sin, "paleta": PALETA}


def _siguiente_tecla(conn: sqlite3.Connection) -> Optional[int]:
    usadas = {r[0] for r in conn.execute(f"SELECT tecla FROM {ALIAS}.etiquetas WHERE tecla IS NOT NULL")}
    return next((n for n in range(1, 10) if n not in usadas), None)


def crear(conn: sqlite3.Connection, nombre: str, color: Optional[str] = None) -> Dict[str, Any]:
    nombre = " ".join((nombre or "").split())[:40]
    if not nombre:
        raise ValueError("El nombre de la etiqueta está vacío.")
    r = conn.execute(f"SELECT id, nombre, color, tecla FROM {ALIAS}.etiquetas WHERE nombre = ?", (nombre,)).fetchone()
    if r:
        return dict(r)
    if color not in PALETA:  # el color de la paleta menos usado
        usos = {c: 0 for c in PALETA}
        for (c,) in conn.execute(f"SELECT color FROM {ALIAS}.etiquetas"):
            if c in usos:
                usos[c] += 1
        color = min(PALETA, key=lambda c: usos[c])
    with conn:
        cur = conn.execute(f"INSERT INTO {ALIAS}.etiquetas (nombre, color, tecla) VALUES (?, ?, ?)",
                           (nombre, color, _siguiente_tecla(conn)))
    return dict(conn.execute(f"SELECT id, nombre, color, tecla FROM {ALIAS}.etiquetas WHERE id = ?",
                             (cur.lastrowid,)).fetchone())


def modificar(conn: sqlite3.Connection, etiqueta_id: int, datos: Dict[str, Any]) -> None:
    with conn:
        if "nombre" in datos:
            nombre = " ".join(str(datos["nombre"] or "").split())[:40]
            if not nombre:
                raise ValueError("El nombre de la etiqueta está vacío.")
            otra = conn.execute(f"SELECT id FROM {ALIAS}.etiquetas WHERE nombre = ? AND id <> ?",
                                (nombre, etiqueta_id)).fetchone()
            if otra:
                raise ValueError(f"Ya existe una etiqueta llamada «{nombre}».")
            conn.execute(f"UPDATE {ALIAS}.etiquetas SET nombre = ? WHERE id = ?", (nombre, etiqueta_id))
        if datos.get("color") in PALETA:
            conn.execute(f"UPDATE {ALIAS}.etiquetas SET color = ? WHERE id = ?", (datos["color"], etiqueta_id))
        if "tecla" in datos:
            t = datos["tecla"]
            t = int(t) if str(t or "").isdigit() and 1 <= int(t) <= 9 else None
            if t is not None:  # la tecla pasa a esta etiqueta
                conn.execute(f"UPDATE {ALIAS}.etiquetas SET tecla = NULL WHERE tecla = ?", (t,))
            conn.execute(f"UPDATE {ALIAS}.etiquetas SET tecla = ? WHERE id = ?", (t, etiqueta_id))


def borrar(conn: sqlite3.Connection, etiqueta_id: int) -> None:
    with conn:
        conn.execute(f"DELETE FROM {ALIAS}.doc_etiqueta WHERE etiqueta_id = ?", (etiqueta_id,))
        conn.execute(f"DELETE FROM {ALIAS}.etiquetas WHERE id = ?", (etiqueta_id,))


# ------------------------------------------------------------------------------
# Asignación
# ------------------------------------------------------------------------------

def asignar(conn: sqlite3.Connection, etiqueta_id: int, rutas_rel: Iterable[str], poner: bool) -> int:
    """Pone o quita una etiqueta a varios documentos. Devuelve cuántos han cambiado."""
    if not conn.execute(f"SELECT 1 FROM {ALIAS}.etiquetas WHERE id = ?", (etiqueta_id,)).fetchone():
        raise ValueError("La etiqueta no existe.")
    filas = [(clave(r), etiqueta_id) for r in dict.fromkeys(rutas_rel) if r]
    with conn:
        antes = conn.total_changes
        if poner:
            conn.executemany(f"INSERT OR IGNORE INTO {ALIAS}.doc_etiqueta (ruta_rel, etiqueta_id) VALUES (?, ?)", filas)
        else:
            conn.executemany(f"DELETE FROM {ALIAS}.doc_etiqueta WHERE ruta_rel = ? AND etiqueta_id = ?", filas)
        return conn.total_changes - antes


def de_rutas(conn: sqlite3.Connection, rutas_rel: List[str]) -> Dict[str, List[int]]:
    """{ruta_rel original: [ids de etiqueta]} para un lote de documentos."""
    out: Dict[str, List[int]] = {r: [] for r in rutas_rel}
    por_clave = {clave(r): r for r in rutas_rel}
    claves = list(por_clave)
    for i in range(0, len(claves), 500):
        lote = claves[i:i + 500]
        for r in conn.execute(
                f"SELECT ruta_rel, etiqueta_id FROM {ALIAS}.doc_etiqueta WHERE ruta_rel IN ({','.join('?' * len(lote))})",
                lote):
            out[por_clave[r[0]]].append(r[1])
    return out


def filtros_sql(params: Dict[str, str]) -> "tuple[List[str], List[Any]]":
    """Cláusulas WHERE (sobre «d») para los filtros por etiqueta de la búsqueda.

    et=1,2 -> lleva todas esas etiquetas · etx=3 -> no lleva ninguna · sin_et=1 -> sin etiquetas."""
    def ids(k: str) -> List[int]:
        return [int(x) for x in (params.get(k) or "").split(",") if x.strip().isdigit()][:20]

    clauses: List[str] = []
    sp: List[Any] = []
    for eid in ids("et"):
        clauses.append(f"{CLAVE_DOC} IN (SELECT ruta_rel FROM {ALIAS}.doc_etiqueta WHERE etiqueta_id = ?)")
        sp.append(eid)
    exc = ids("etx")
    if exc:
        clauses.append(f"{CLAVE_DOC} NOT IN (SELECT ruta_rel FROM {ALIAS}.doc_etiqueta "
                       f"WHERE etiqueta_id IN ({','.join('?' * len(exc))}))")
        sp.extend(exc)
    if params.get("sin_et") == "1":
        clauses.append(f"{CLAVE_DOC} NOT IN (SELECT ruta_rel FROM {ALIAS}.doc_etiqueta)")
    return clauses, sp
