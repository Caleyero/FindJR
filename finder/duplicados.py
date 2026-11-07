# -*- coding: utf-8 -*-
"""
Duplicados exactos: archivos idénticos byte a byte (mismo SHA-256) dentro de una fuente.

Solo se compara lo que es seguro: el contenido completo. No se usan «parecidos» (mismo texto, mismo
nombre…): con estos documentos, facturas distintas pueden dar el mismo texto extraído (ver la decisión 1
de CLAUDE.md). Para no leer 9 GB en cada sincronización, la huella solo se calcula cuando otro archivo
tiene exactamente el mismo tamaño.

Modelo (finder/db.py): de cada grupo se indexa una sola copia, la «principal» (tiene fila en
`documentos`); las demás quedan en `archivos` con `copia_de` = id de la principal. El programa nunca
borra nada por su cuenta: solo registra las copias y las muestra para que el usuario decida.
"""

import concurrent.futures as cf
import hashlib
import sqlite3
from typing import Any, Dict, Iterable, List, Optional

from . import etiquetas
from .config import to_display_path


def huella(path: str) -> Optional[str]:
    """SHA-256 del archivo, o None si no se puede leer."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for bloque in iter(lambda: f.read(1 << 20), b""):
                h.update(bloque)
    except OSError:
        return None
    return h.hexdigest()


def huellas(paths: Iterable[str]) -> Dict[str, Optional[str]]:
    """{ruta: SHA-256} en paralelo (hashlib libera el GIL al leer bloques grandes)."""
    paths = list(dict.fromkeys(paths))
    if not paths:
        return {}
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        return dict(zip(paths, ex.map(huella, paths)))


def principal_de(conn: sqlite3.Connection, h: str, excluir: Optional[int] = None) -> Optional[int]:
    """id del archivo indexado (con documento) que tiene esa huella."""
    r = conn.execute(
        """SELECT a.id FROM archivos a JOIN documentos d ON d.archivo_id = a.id
           WHERE a.hash = ? AND a.id <> ? LIMIT 1""", (h, excluir or 0)).fetchone()
    return r[0] if r else None


def copiar_etiquetas(conn: sqlite3.Connection, de_rutas: List[str], a_ruta: str) -> None:
    """Las etiquetas puestas a mano en una copia pasan también a la que se queda (no se quita ninguna).
    Las automáticas no: las decide la regla según el nombre de la que se queda."""
    claves = [etiquetas.clave(r) for r in de_rutas if r]
    if not claves:
        return
    conn.execute(
        f"""INSERT OR IGNORE INTO {etiquetas.ALIAS}.doc_etiqueta (ruta_rel, etiqueta_id)
            SELECT ?, etiqueta_id FROM {etiquetas.ALIAS}.doc_etiqueta
            WHERE origen = 'manual' AND ruta_rel IN ({','.join('?' * len(claves))})""",
        [etiquetas.clave(a_ruta)] + claves)


def promover(conn: sqlite3.Connection, principal_id: int, nuevo_id: Optional[int] = None) -> Optional[int]:
    """Convierte una copia en la principal de su grupo antes de que la principal desaparezca (borrada,
    movida, modificada o descartada por el usuario). El documento (texto ya extraído, idéntico porque los
    bytes lo son) pasa a la copia, sin volver a leer el PDF; las etiquetas de la principal se copian a la
    nueva ruta. La antigua principal queda como copia. Devuelve el id de la nueva principal o None si no
    había copias. Debe llamarse dentro de una transacción."""
    copias = conn.execute(
        """SELECT id, ruta_rel, nombre, carpeta, mtime FROM archivos WHERE copia_de = ?
           ORDER BY mtime, length(ruta_rel), id""", (principal_id,)).fetchall()
    if not copias:
        return None
    nuevo = copias[0] if nuevo_id is None else next((c for c in copias if c["id"] == nuevo_id), None)
    if nuevo is None:
        raise ValueError("La copia indicada no pertenece a ese grupo de duplicados.")
    viejo = conn.execute("SELECT ruta_rel FROM archivos WHERE id = ?", (principal_id,)).fetchone()
    # El trigger documentos_au actualiza nombre y ruta en el índice de texto completo
    conn.execute("UPDATE documentos SET archivo_id = ?, nombre = ?, ruta_rel = ?, carpeta = ?, mtime = ? WHERE archivo_id = ?",
                 (nuevo["id"], nuevo["nombre"], nuevo["ruta_rel"], nuevo["carpeta"], nuevo["mtime"], principal_id))
    conn.execute("UPDATE archivos SET copia_de = ? WHERE copia_de = ? OR id = ?", (nuevo["id"], principal_id, principal_id))
    conn.execute("UPDATE archivos SET copia_de = NULL WHERE id = ?", (nuevo["id"],))
    if viejo:
        copiar_etiquetas(conn, [viejo["ruta_rel"]], nuevo["ruta_rel"])
    return nuevo["id"]


def consolidar(conn: sqlite3.Connection) -> int:
    """Deja un solo documento por huella. Sirve para la primera sincronización tras la migración v2 -> v3
    (el índice ya tenía las copias indexadas) y para cuando un archivo antiguo resulta ser idéntico a uno
    nuevo de su mismo tamaño. Se queda como principal la que tiene etiquetas o, si no, la más antigua.
    Devuelve cuántos archivos han pasado a ser copias. Debe llamarse dentro de una transacción."""
    filas = conn.execute(
        f"""SELECT a.id, a.hash, a.mtime, a.ruta_rel,
                   EXISTS (SELECT 1 FROM {etiquetas.ALIAS}.doc_etiqueta de
                           WHERE de.ruta_rel = replace(a.ruta_rel, char(92), '/')) AS con_et
            FROM archivos a JOIN documentos d ON d.archivo_id = a.id
            WHERE a.hash IN (SELECT a2.hash FROM archivos a2 JOIN documentos d2 ON d2.archivo_id = a2.id
                             WHERE a2.hash IS NOT NULL GROUP BY a2.hash HAVING COUNT(*) > 1)""").fetchall()
    grupos: Dict[str, List[sqlite3.Row]] = {}
    for f in filas:
        grupos.setdefault(f["hash"], []).append(f)
    n = 0
    for miembros in grupos.values():
        miembros.sort(key=lambda f: (-f["con_et"], f["mtime"], len(f["ruta_rel"]), f["id"]))
        jefe, resto = miembros[0], miembros[1:]
        for f in resto:
            conn.execute("UPDATE archivos SET copia_de = ? WHERE copia_de = ? OR id = ?", (jefe["id"], f["id"], f["id"]))
            conn.execute("DELETE FROM documentos WHERE archivo_id = ?", (f["id"],))  # el trigger lo saca del FTS
            n += 1
        copiar_etiquetas(conn, [f["ruta_rel"] for f in resto], jefe["ruta_rel"])
    return n


def refrescar_rutas(conn: sqlite3.Connection) -> None:
    """Copia en documentos.otras_rutas las rutas de las copias de cada documento, para que también se
    encuentre buscando por el nombre o la carpeta de una copia. Solo escribe las filas que cambian (el
    trigger documentos_au actualiza el FTS). Debe llamarse dentro de una transacción."""
    rutas = "(SELECT group_concat(c.ruta_rel, char(10)) FROM archivos c WHERE c.copia_de = documentos.archivo_id)"
    conn.execute(f"UPDATE documentos SET otras_rutas = {rutas} WHERE otras_rutas IS NOT {rutas}")


def contar(conn: sqlite3.Connection) -> Dict[str, int]:
    r = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM archivos WHERE copia_de IS NOT NULL").fetchone()
    return {"copias": r[0], "bytes": r[1]}


def listar(conn: sqlite3.Connection, revisado: str = "") -> Dict[str, Any]:
    """Grupos de duplicados para el panel: la principal primero y después sus copias."""
    filas = conn.execute(
        """SELECT a.id, a.ruta, a.ruta_rel, a.nombre, a.size, a.mtime, a.hash, a.copia_de, a.indexado_en, d.id AS doc_id
           FROM archivos a LEFT JOIN documentos d ON d.archivo_id = a.id
           WHERE a.copia_de IS NOT NULL
              OR a.id IN (SELECT copia_de FROM archivos WHERE copia_de IS NOT NULL)""").fetchall()
    por_ruta = etiquetas.de_rutas(conn, [f["ruta_rel"] for f in filas])
    grupos: Dict[int, Dict[str, Any]] = {}
    for f in filas:
        pid = f["copia_de"] or f["id"]
        g = grupos.setdefault(pid, {"principal": pid, "size": f["size"], "hash": "", "miembros": [], "reciente": ""})
        if f["copia_de"] is None:
            g["hash"] = f["hash"] or ""
        nuevo = bool(f["copia_de"]) and bool(revisado) and (f["indexado_en"] or "") > revisado
        g["miembros"].append({
            "id": f["id"], "doc_id": f["doc_id"], "nombre": f["nombre"], "ruta_rel": f["ruta_rel"],
            "ruta_display": to_display_path(f["ruta"]), "mtime": f["mtime"], "principal": f["copia_de"] is None,
            "nuevo": nuevo, "etiquetas": por_ruta.get(f["ruta_rel"], []),
        })
        if f["copia_de"] and (f["indexado_en"] or "") > g["reciente"]:
            g["reciente"] = f["indexado_en"] or ""
    out = []
    for g in grupos.values():
        if len(g["miembros"]) < 2 or not any(m["principal"] for m in g["miembros"]):
            continue  # inconsistente (se arregla en la siguiente sincronización)
        g["miembros"].sort(key=lambda m: (not m["principal"], m["mtime"], len(m["ruta_rel"])))
        g["nuevos"] = sum(m["nuevo"] for m in g["miembros"])
        out.append(g)
    # Primero los que tienen copias nuevas; dentro, los detectados más recientemente
    out.sort(key=lambda g: g["reciente"], reverse=True)
    out.sort(key=lambda g: g["nuevos"] == 0)
    return {"grupos": out, **contar(conn)}
