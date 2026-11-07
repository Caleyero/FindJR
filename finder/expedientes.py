# -*- coding: utf-8 -*-
"""
Expedientes (noviembre 2026, a petición del usuario): agrupan documentos relacionados bajo un nombre
(«Inspección Hacienda», un nº de diligencia…), con notas y color. Un documento puede estar en varios.

Los crea y llena siempre el usuario (decisión 1 de CLAUDE.md). Se guardan junto a las etiquetas, en
etiquetas_<fuente>.db (adjunta como «et»), y se vinculan igual: por la ruta relativa del documento con «/».
Eliminar un expediente solo borra el expediente y sus vínculos; los documentos no se tocan.
"""

import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from .etiquetas import ALIAS, CLAVE_DOC, PALETA, clave

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {ALIAS}.expedientes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    notas          TEXT NOT NULL DEFAULT '',
    color          TEXT NOT NULL,
    creado_en      TEXT DEFAULT (datetime('now', 'localtime')),
    modificado_en  TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS {ALIAS}.doc_expediente (
    ruta_rel       TEXT NOT NULL,         -- relativa a la carpeta de la fuente, separador «/»
    expediente_id  INTEGER NOT NULL REFERENCES expedientes(id) ON DELETE CASCADE,
    asignado_en    TEXT DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (ruta_rel, expediente_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS {ALIAS}.idx_dx_expediente ON doc_expediente(expediente_id);
"""

MAX_NOTAS = 4000


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        conn.executescript(_SCHEMA)


def _nombre(valor: Any) -> str:
    nombre = " ".join(str(valor or "").split())[:80]
    if not nombre:
        raise ValueError("El nombre del expediente está vacío.")
    return nombre


def _uno(conn: sqlite3.Connection, exp_id: int) -> Dict[str, Any]:
    r = conn.execute(f"SELECT id, nombre, notas, color FROM {ALIAS}.expedientes WHERE id = ?", (exp_id,)).fetchone()
    if not r:
        raise ValueError("El expediente no existe.")
    return dict(r)


def listar(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Expedientes con su número de documentos indexados y la fecha del más reciente; primero los
    modificados (creados, editados o con documentos añadidos) más recientemente."""
    cuentas = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"""SELECT dx.expediente_id, COUNT(*), MAX(d.mtime) FROM documentos d
            JOIN {ALIAS}.doc_expediente dx ON dx.ruta_rel = {CLAVE_DOC} GROUP BY dx.expediente_id""")}
    items = []
    for r in conn.execute(f"SELECT id, nombre, notas, color, creado_en, modificado_en FROM {ALIAS}.expedientes "
                          "ORDER BY modificado_en DESC, id DESC"):
        total, ultimo = cuentas.get(r["id"], (0, None))
        items.append(dict(r, total=total, ultimo_doc=ultimo))
    return {"items": items, "paleta": PALETA}


def crear(conn: sqlite3.Connection, nombre: str, notas: str = "", color: Optional[str] = None) -> Dict[str, Any]:
    nombre = _nombre(nombre)
    r = conn.execute(f"SELECT id FROM {ALIAS}.expedientes WHERE nombre = ?", (nombre,)).fetchone()
    if r:
        raise ValueError(f"Ya existe un expediente llamado «{nombre}».")
    if color not in PALETA:  # el color de la paleta menos usado
        usos = {c: 0 for c in PALETA}
        for (c,) in conn.execute(f"SELECT color FROM {ALIAS}.expedientes"):
            if c in usos:
                usos[c] += 1
        color = min(PALETA, key=lambda c: usos[c])
    with conn:
        cur = conn.execute(f"INSERT INTO {ALIAS}.expedientes (nombre, notas, color) VALUES (?, ?, ?)",
                           (nombre, str(notas or "")[:MAX_NOTAS], color))
    return _uno(conn, cur.lastrowid)


def modificar(conn: sqlite3.Connection, exp_id: int, datos: Dict[str, Any]) -> Dict[str, Any]:
    _uno(conn, exp_id)
    with conn:
        if "nombre" in datos:
            nombre = _nombre(datos["nombre"])
            if conn.execute(f"SELECT 1 FROM {ALIAS}.expedientes WHERE nombre = ? AND id <> ?", (nombre, exp_id)).fetchone():
                raise ValueError(f"Ya existe un expediente llamado «{nombre}».")
            conn.execute(f"UPDATE {ALIAS}.expedientes SET nombre = ? WHERE id = ?", (nombre, exp_id))
        if "notas" in datos:
            conn.execute(f"UPDATE {ALIAS}.expedientes SET notas = ? WHERE id = ?", (str(datos["notas"] or "")[:MAX_NOTAS], exp_id))
        if datos.get("color") in PALETA:
            conn.execute(f"UPDATE {ALIAS}.expedientes SET color = ? WHERE id = ?", (datos["color"], exp_id))
        conn.execute(f"UPDATE {ALIAS}.expedientes SET modificado_en = datetime('now', 'localtime') WHERE id = ?", (exp_id,))
    return _uno(conn, exp_id)


def borrar(conn: sqlite3.Connection, exp_id: int) -> None:
    """Elimina el expediente y sus vínculos. Los documentos no se tocan."""
    with conn:
        conn.execute(f"DELETE FROM {ALIAS}.doc_expediente WHERE expediente_id = ?", (exp_id,))
        conn.execute(f"DELETE FROM {ALIAS}.expedientes WHERE id = ?", (exp_id,))


def asignar(conn: sqlite3.Connection, exp_id: int, rutas_rel: Iterable[str], poner: bool) -> int:
    """Añade o quita documentos de un expediente. Devuelve cuántos han cambiado."""
    _uno(conn, exp_id)
    filas = [(clave(r), exp_id) for r in dict.fromkeys(rutas_rel) if r]
    with conn:
        antes = conn.total_changes
        if poner:
            conn.executemany(f"INSERT OR IGNORE INTO {ALIAS}.doc_expediente (ruta_rel, expediente_id) VALUES (?, ?)", filas)
        else:
            conn.executemany(f"DELETE FROM {ALIAS}.doc_expediente WHERE ruta_rel = ? AND expediente_id = ?", filas)
        cambiados = conn.total_changes - antes
        if cambiados:
            conn.execute(f"UPDATE {ALIAS}.expedientes SET modificado_en = datetime('now', 'localtime') WHERE id = ?", (exp_id,))
    return cambiados


def de_rutas(conn: sqlite3.Connection, rutas_rel: List[str]) -> Dict[str, List[int]]:
    """{ruta_rel original: [ids de expediente]} para un lote de documentos."""
    out: Dict[str, List[int]] = {r: [] for r in rutas_rel}
    por_clave = {clave(r): r for r in rutas_rel}
    claves = list(por_clave)
    for i in range(0, len(claves), 500):
        lote = claves[i:i + 500]
        for r in conn.execute(
                f"SELECT ruta_rel, expediente_id FROM {ALIAS}.doc_expediente "
                f"WHERE ruta_rel IN ({','.join('?' * len(lote))})", lote):
            out[por_clave[r[0]]].append(r[1])
    return out


def mover(conn: sqlite3.Connection, rel_viejo: str, rel_nuevo: str) -> None:
    """Los vínculos de un documento pasan a su nueva ruta (renombrado)."""
    conn.execute(f"UPDATE OR REPLACE {ALIAS}.doc_expediente SET ruta_rel = ? WHERE ruta_rel = ?",
                 (clave(rel_nuevo), clave(rel_viejo)))


def copiar(conn: sqlite3.Connection, de_rutas_rel: List[str], a_ruta: str) -> None:
    """Los expedientes de unas copias idénticas pasan también a la que se queda (duplicados)."""
    claves = [clave(r) for r in de_rutas_rel if r]
    if claves:
        conn.execute(
            f"""INSERT OR IGNORE INTO {ALIAS}.doc_expediente (ruta_rel, expediente_id)
                SELECT ?, expediente_id FROM {ALIAS}.doc_expediente WHERE ruta_rel IN ({','.join('?' * len(claves))})""",
            [clave(a_ruta)] + claves)


def filtros_sql(params: Dict[str, str]) -> "tuple[List[str], List[Any]]":
    """exp=ID -> solo los documentos de ese expediente."""
    exp = str(params.get("exp") or "")
    if not exp.isdigit():
        return [], []
    return [f"{CLAVE_DOC} IN (SELECT ruta_rel FROM {ALIAS}.doc_expediente WHERE expediente_id = ?)"], [int(exp)]
