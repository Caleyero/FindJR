# -*- coding: utf-8 -*-
"""
Etiquetado automático por el nombre del archivo (noviembre 2026, a petición del usuario).

Regla de facturas, configurable por fuente en Preferencias («etiquetado_auto» de la fuente):

    EMP_PROVEEDOR - NÚMERO.ext      p. ej. «TEX_TELEFONICA MOVILES SA - 6546594.pdf»

EMP es uno de los códigos de empresa configurados (TEX, FOR…). Un nombre que cumple el patrón recibe
la etiqueta de facturas («Factura») y la de su empresa. El guion necesita un espacio al menos a un lado,
para no confundir con facturas nombres como «DIS_MOD200-2022» o «EWA_CIRBE_02-2024».

Compatible con la decisión 1 de CLAUDE.md porque la regla la define el usuario y se basa en lo que dice
literalmente el nombre del archivo. Las etiquetas automáticas llevan `origen = 'auto'`; si el usuario
quita una, queda en `et.rechazos` y no se vuelve a poner. Si un archivo deja de cumplir la regla (se
renombra, se quita la empresa o se desactiva la regla), se le quitan las automáticas; las manuales nunca
se tocan.
"""

import re
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple

from . import etiquetas
from .etiquetas import ALIAS, clave

DEFAULT: Dict[str, Any] = {"activo": False, "etiqueta_factura": "Factura", "empresas": []}

_RE_CODIGO = re.compile(r"^[A-Za-z0-9]{1,10}$")


def normalizar(regla: Any) -> Dict[str, Any]:
    """Valida la regla recibida de la interfaz o leída de finder_config.json."""
    regla = regla if isinstance(regla, dict) else {}
    out = {"activo": bool(regla.get("activo")),
           "etiqueta_factura": " ".join(str(regla.get("etiqueta_factura") or "").split())[:40],
           "empresas": []}
    vistos: Set[str] = set()
    for e in regla.get("empresas") or []:
        if not isinstance(e, dict):
            continue
        cod = str(e.get("codigo") or "").strip().upper()
        if not _RE_CODIGO.match(cod):
            raise ValueError(f"Código de empresa no válido: «{cod}» (solo letras y números, sin espacios).")
        if cod in vistos:
            raise ValueError(f"El código de empresa «{cod}» está repetido.")
        vistos.add(cod)
        nombre = " ".join(str(e.get("etiqueta") or "").split())[:40] or cod
        out["empresas"].append({"codigo": cod, "etiqueta": nombre})
    if out["activo"] and not out["empresas"]:
        raise ValueError("Para activar el etiquetado automático añade al menos un código de empresa.")
    return out


def _patrones(regla: Dict[str, Any]) -> Tuple[Optional["re.Pattern[str]"], Optional["re.Pattern[str]"]]:
    """(patrón de factura, patrón de «empieza por un código de empresa»)."""
    cods = [re.escape(e["codigo"]) for e in regla["empresas"]]
    if not cods:
        return None, None
    alt = "|".join(sorted(cods, key=len, reverse=True))
    factura = re.compile(rf"^({alt})_\s*(\S.*?)(?:\s+-\s*|\s*-\s+)(\S.*)\.[^.\\/]+$", re.I)
    prefijo = re.compile(rf"^({alt})_.*-", re.I)
    return factura, prefijo


def _objetivo(regla: Dict[str, Any], nombre: str, pat) -> List[str]:
    """Nombres de las etiquetas que la regla pone a un archivo (vacío si no la cumple)."""
    m = pat.match(nombre) if pat else None
    if not m:
        return []
    cod = m.group(1).upper()
    emp = next(e["etiqueta"] for e in regla["empresas"] if e["codigo"] == cod)
    return [x for x in (regla["etiqueta_factura"], emp) if x]


def vista_previa(conn: sqlite3.Connection, regla: Any) -> Dict[str, Any]:
    """Qué haría la regla sobre el índice actual, sin tocar nada."""
    regla = normalizar(dict(regla or {}, activo=False))
    pat, prefijo = _patrones(regla)
    por_etiqueta: Dict[str, int] = {}
    ejemplos: List[str] = []
    casi: List[str] = []
    cumplen = 0
    for (nombre,) in conn.execute("SELECT nombre FROM documentos ORDER BY mtime DESC"):
        objetivo = _objetivo(regla, nombre, pat)
        if objetivo:
            cumplen += 1
            for n in objetivo:
                por_etiqueta[n] = por_etiqueta.get(n, 0) + 1
            if len(ejemplos) < 6:
                ejemplos.append(nombre)
        elif prefijo and prefijo.match(nombre):
            casi.append(nombre)
    total = conn.execute("SELECT COUNT(*) FROM documentos").fetchone()[0]
    return {"total": total, "cumplen": cumplen, "por_etiqueta": por_etiqueta,
            "ejemplos": ejemplos, "casi": casi[:40], "casi_total": len(casi)}


def aplicar(conn: sqlite3.Connection, regla: Any) -> Dict[str, int]:
    """Pone y quita las etiquetas automáticas de toda la fuente según la regla. Idempotente y rápido
    (expresión regular sobre los nombres, ~14.000 en milisegundos): se llama al final de cada
    sincronización, tras renombrar y al guardar la regla."""
    regla = normalizar(regla) if regla else dict(DEFAULT)
    deseadas: Set[Tuple[str, int]] = set()
    if regla["activo"]:
        pat, _ = _patrones(regla)
        ids: Dict[str, int] = {}
        nombres = {regla["etiqueta_factura"]} | {e["etiqueta"] for e in regla["empresas"]}
        for n in filter(None, nombres):
            ids[n.lower()] = etiquetas.crear(conn, n, con_tecla=False)["id"]
        for nombre, ruta_rel in conn.execute("SELECT nombre, ruta_rel FROM documentos"):
            for n in _objetivo(regla, nombre, pat):
                deseadas.add((clave(ruta_rel), ids[n.lower()]))
    existentes = {(r[0], r[1]): r[2] for r in conn.execute(
        f"SELECT ruta_rel, etiqueta_id, origen FROM {ALIAS}.doc_etiqueta")}
    rechazos = {(r[0], r[1]) for r in conn.execute(f"SELECT ruta_rel, etiqueta_id FROM {ALIAS}.rechazos")}
    poner = [k for k in deseadas if k not in existentes and k not in rechazos]
    quitar = [k for k, origen in existentes.items() if origen == "auto" and k not in deseadas]
    if poner or quitar:
        with conn:
            conn.executemany(f"INSERT OR IGNORE INTO {ALIAS}.doc_etiqueta (ruta_rel, etiqueta_id, origen) "
                             "VALUES (?, ?, 'auto')", poner)
            conn.executemany(f"DELETE FROM {ALIAS}.doc_etiqueta WHERE ruta_rel = ? AND etiqueta_id = ? "
                             "AND origen = 'auto'", quitar)
    return {"puestas": len(poner), "quitadas": len(quitar)}
