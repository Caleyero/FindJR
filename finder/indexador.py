# -*- coding: utf-8 -*-
"""
Indexador incremental de las fuentes de documentos y proceso en segundo plano.
Cada fuente (carpeta) tiene su propio índice; aquí «Biblioteca» es la carpeta de una fuente.

Ciclo de sincronización:
  1. Descubre archivos nuevos / modificados / eliminados (ruta + tamaño + fecha).
  2. Calcula la huella (SHA-256) de los que comparten tamaño con otro: los idénticos a uno ya indexado
     se registran como copias, sin extraer su texto (finder/duplicados.py).
  3. Extrae el texto en paralelo (varios procesos) y lo guarda en lotes en el índice FTS5.
"""

import concurrent.futures as cf
import datetime
import os
import re
import subprocess
import threading
import time
import traceback
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from . import duplicados, etiquetas
from .config import excluded_dirs, fuente, load_config, to_display_path, to_local_path
from .db import get_conn, init_db, meta_set
from .extraccion import extract_pdf_text

_SYNC_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
STATE: Dict[str, Any] = {
    "activo": False, "fase": "inactivo", "fuente": "", "total": 0, "hechos": 0, "mensaje": "",
    "inicio": None, "ultimo": None, "error": None,
}


def _set_state(**kw) -> None:
    with _STATE_LOCK:
        STATE.update(kw)


def get_state() -> Dict[str, Any]:
    with _STATE_LOCK:
        return dict(STATE)


# ------------------------------------------------------------------------------
# Descubrimiento
# ------------------------------------------------------------------------------

def biblioteca_path(fte: Dict[str, Any]) -> str:
    return to_local_path(fte.get("biblioteca", ""))


def discover(cfg: Dict[str, Any], fte: Dict[str, Any]) -> List[Tuple[str, str, int, float]]:
    """[(ruta_local, ruta_rel, size, mtime)] de los archivos indexables de una fuente."""
    root = biblioteca_path(fte)
    if not root or not os.path.isdir(root):
        return []
    exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in cfg.get("extensions", [".pdf"])}
    excl = set(excluded_dirs(fte))
    include_sub = bool(fte.get("include_subfolders", True))
    out = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in excl and not d.startswith(".")]
        if not include_sub:
            dirs[:] = []
        for fn in files:
            if fn.startswith("~$") or os.path.splitext(fn)[1].lower() not in exts:
                continue
            full = os.path.join(cur, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append((full, os.path.relpath(full, root), st.st_size, round(st.st_mtime, 2)))
    return out


# ------------------------------------------------------------------------------
# Extracción (se ejecuta en procesos hijos)
# ------------------------------------------------------------------------------

def _extract_worker(args: Tuple[str, int]) -> Dict[str, Any]:
    path, max_pages = args
    try:
        paginas, texto = extract_pdf_text(path, max_pages)
        return {"ok": True, "paginas": paginas, "texto": texto}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _carpeta(rel: str) -> str:
    parts = rel.replace("\\", "/").split("/")
    return parts[0] if len(parts) > 1 else "(raíz)"


def _quitar_previo(conn, prev_id: Optional[int]) -> None:
    """Quita la versión anterior de un archivo modificado; si era la principal de un grupo de duplicados,
    su documento pasa antes a una de sus copias (que conservan el contenido anterior)."""
    if prev_id:
        duplicados.promover(conn, prev_id)
        conn.execute("DELETE FROM archivos WHERE id = ?", (prev_id,))


def _store_copia(conn, item: Tuple[str, str, int, float], prev_id: Optional[int], h: str, principal_id: int) -> None:
    """Registra un archivo idéntico a otro ya indexado: sin extraer su texto ni crear documento."""
    full, rel, size, mtime = item
    _quitar_previo(conn, prev_id)
    p = conn.execute("SELECT paginas, necesita_ocr, estado, error FROM archivos WHERE id = ?", (principal_id,)).fetchone()
    nombre = os.path.basename(full)
    conn.execute(
        """INSERT INTO archivos (ruta, ruta_rel, carpeta, nombre, ext, size, mtime, paginas, necesita_ocr, estado, error,
                                 indexado_en, hash, copia_de)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (full, rel, _carpeta(rel), nombre, os.path.splitext(nombre)[1].lower(), size, mtime, p["paginas"], p["necesita_ocr"],
         p["estado"], p["error"], datetime.datetime.now().isoformat(timespec="seconds"), h, principal_id),
    )


def _store(conn, item: Tuple[str, str, int, float], er: Dict[str, Any], prev_id: Optional[int], h: Optional[str] = None) -> None:
    full, rel, size, mtime = item
    nombre = os.path.basename(full)
    carpeta = _carpeta(rel)
    _quitar_previo(conn, prev_id)
    ok = er.get("ok")
    texto = er.get("texto", "") if ok else ""
    sin_texto = 1 if len(texto.strip()) < 60 else 0
    paginas = er.get("paginas", 0) if ok else 0
    cur = conn.execute(
        """INSERT INTO archivos (ruta, ruta_rel, carpeta, nombre, ext, size, mtime, paginas, necesita_ocr, estado, error,
                                 indexado_en, hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (full, rel, carpeta, nombre, os.path.splitext(nombre)[1].lower(), size, mtime, paginas,
         sin_texto if ok else 0, "OK" if ok else "ERROR", None if ok else er.get("error"),
         datetime.datetime.now().isoformat(timespec="seconds"), h),
    )
    n_pag = max(1, paginas)
    conn.execute(
        """INSERT INTO documentos (archivo_id, primera_pag, ultima_pag, num_paginas, nombre, ruta_rel, carpeta, mtime, texto, sin_texto)
           VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cur.lastrowid, n_pag, n_pag, nombre, rel, carpeta, mtime, texto, sin_texto),
    )


# ------------------------------------------------------------------------------
# Sincronización
# ------------------------------------------------------------------------------

def sync(force_reindex: bool = False, fid: Optional[str] = None) -> Dict[str, Any]:
    """Sincroniza el índice de una fuente (por defecto, la activa) con su carpeta."""
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"status": "busy"}
    t0 = time.time()
    res: Dict[str, Any] = {"status": "ok", "added": 0, "updated": 0, "removed": 0, "errors": 0, "duplicados": 0}
    try:
        cfg = load_config()
        fte = fuente(cfg, fid)
        fid = fte["id"]
        res["fuente"] = fid
        root = biblioteca_path(fte)
        if not root or not os.path.isdir(root):
            # Carpeta sin configurar o inaccesible (p. ej. unidad desconectada): no se toca el índice,
            # que si no se vaciaría entero.
            res.update(status="sin_carpeta")
            return res
        _set_state(activo=True, fase="escaneando", total=0, hechos=0, error=None, fuente=fte["nombre"],
                   mensaje=f"Explorando {fte['nombre']}…", inicio=datetime.datetime.now().isoformat(timespec="seconds"))
        init_db(fid)
        conn = get_conn(fid)

        found = discover(cfg, fte)
        if force_reindex:
            with conn:
                conn.execute("DELETE FROM archivos")
        existing = {r["ruta"]: r for r in conn.execute("SELECT id, ruta, size, mtime, hash, copia_de FROM archivos")}
        found_paths = {f[0] for f in found}

        # 1. Desaparecidos. Primero las copias; después las principales, cuyo documento pasa a una copia que
        #    siga existiendo (así no se pierde el texto ni hay que volver a extraerlo).
        removed = sorted((v for k, v in existing.items() if k not in found_paths), key=lambda v: v["copia_de"] is None)
        if removed:
            with conn:
                for v in removed:
                    _quitar_previo(conn, v["id"])
        res["removed"] = sum(1 for v in removed if v["copia_de"] is None)

        # 2. Nuevos o modificados
        cambiados = []
        for item in found:
            prev = existing.get(item[0])
            if prev and prev["size"] == item[2] and abs(prev["mtime"] - item[3]) < 1.0:
                continue
            cambiados.append((item, prev["id"] if prev else None))

        # 3. Huellas, solo de los archivos cuyo tamaño coincide con el de otro (los únicos que pueden ser
        #    idénticos): los cambiados y los ya indexados que aún no la tenían.
        tallas = Counter(f[2] for f in found)
        rutas_cambiadas = {it[0] for it, _ in cambiados}
        sin_huella = [v for k, v in existing.items() if k in found_paths and k not in rutas_cambiadas
                      and v["hash"] is None and tallas[v["size"]] > 1]
        a_comparar = [v["ruta"] for v in sin_huella] + [it[0] for it, _ in cambiados if tallas[it[2]] > 1]
        if a_comparar:
            _set_state(fase="comparando", mensaje=f"{fte['nombre']}: buscando duplicados en {len(a_comparar):,} archivos…".replace(",", "."))
        hs = duplicados.huellas(a_comparar)
        with conn:
            conn.executemany("UPDATE archivos SET hash = ? WHERE id = ?",
                             [(hs[v["ruta"]], v["id"]) for v in sin_huella if hs.get(v["ruta"])])
            res["duplicados"] = duplicados.consolidar(conn)

        # 4. Los idénticos a otro ya indexado (o a otro anterior de este mismo lote) no se extraen
        todo, copias, en_lote = [], [], set()
        for item, prev_id in cambiados:
            h = hs.get(item[0])
            if h and (h in en_lote or duplicados.principal_de(conn, h, excluir=prev_id)):
                copias.append((item, prev_id, h))
                continue
            if h:
                en_lote.add(h)
            todo.append((item, prev_id, h))
        res["pending"] = len(todo)

        _set_state(fase="extrayendo", total=len(todo), hechos=0,
                   mensaje=f"{fte['nombre']}: indexando {len(todo)} archivos nuevos o modificados…")
        max_pages = int(cfg.get("max_pages_text", 60))
        done = 0
        if todo:
            workers = max(1, min(4, (os.cpu_count() or 2) - 1))
            try:
                pool = cf.ProcessPoolExecutor(max_workers=workers)
            except Exception:  # noqa: BLE001
                pool = None
            try:
                for i in range(0, len(todo), 48):
                    chunk = todo[i:i + 48]
                    args = [(it[0], max_pages) for it, _, _ in chunk]
                    results = None
                    if pool:
                        try:
                            results = list(pool.map(_extract_worker, args, chunksize=4))
                        except Exception:  # noqa: BLE001
                            pool.shutdown(cancel_futures=True)
                            pool = None
                    if results is None:
                        results = [_extract_worker(a) for a in args]
                    with conn:
                        for (it, prev_id, h), er in zip(chunk, results):
                            _store(conn, it, er, prev_id, h)
                            res["updated" if prev_id else "added"] += 1
                            if not er.get("ok"):
                                res["errors"] += 1
                    done += len(chunk)
                    _set_state(hechos=done, mensaje=f"{fte['nombre']}: indexando {done:,}/{len(todo):,} archivos…".replace(",", "."))
            finally:
                if pool:
                    pool.shutdown()

        # 5. Copias: después de extraer, porque su principal puede ser otro archivo de este mismo lote
        if copias:
            with conn:
                for it, prev_id, h in copias:
                    principal = duplicados.principal_de(conn, h, excluir=prev_id)
                    if principal:
                        _store_copia(conn, it, prev_id, h, principal)
                        res["duplicados"] += 1
                    else:  # caso raro (su principal ha fallado o ha cambiado): se indexa normalmente
                        _store(conn, it, _extract_worker((it[0], max_pages)), prev_id, h)
                        res["updated" if prev_id else "added"] += 1

        with conn:
            duplicados.refrescar_rutas(conn)
            meta_set(conn, "ultima_sync", datetime.datetime.now().isoformat(timespec="seconds"))
        conn.close()
        res["elapsed_sec"] = round(time.time() - t0, 1)
        _set_state(ultimo=res)
        return res
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _set_state(error=str(e))
        res.update(status="error", error=str(e))
        return res
    finally:
        _set_state(activo=False, fase="inactivo", mensaje="", fuente="")
        _SYNC_LOCK.release()


# ------------------------------------------------------------------------------
# Renombrado de archivos
# ------------------------------------------------------------------------------

_CARACTERES_PROHIBIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVADOS = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def validar_nombre(base: str) -> str:
    """Nombre sin extensión válido en Windows; lanza ValueError con un mensaje para el usuario."""
    base = (base or "").strip().rstrip(". ")
    if not base:
        raise ValueError("El nombre no puede estar vacío.")
    m = _CARACTERES_PROHIBIDOS.search(base)
    if m:
        car = m.group(0) if m.group(0).isprintable() else "de control"
        raise ValueError(f"El nombre no puede contener el carácter {car} (prohibidos: \\ / : * ? \" < > |).")
    if base.split(".")[0].upper() in _RESERVADOS:
        raise ValueError(f"«{base}» es un nombre reservado de Windows.")
    if len(base) > 200:
        raise ValueError("El nombre es demasiado largo (máximo 200 caracteres).")
    return base


def renombrar(doc_id: int, nueva_base: str, fid: Optional[str] = None) -> Dict[str, Any]:
    """Renombra en disco el archivo de un documento (conservando la extensión) y actualiza el índice
    y las etiquetas sin volver a extraer el texto. Devuelve el nombre anterior y el nuevo.
    Solo se impide repetir nombre en la misma carpeta (en carpetas distintas es legítimo)."""
    nueva_base = validar_nombre(nueva_base)
    if not _SYNC_LOCK.acquire(timeout=5):
        raise ValueError("Hay una sincronización en curso. Inténtalo de nuevo en cuanto termine.")
    try:
        conn = get_conn(fid)
        try:
            r = conn.execute(
                """SELECT a.id, a.ruta, a.ruta_rel, a.nombre FROM documentos d
                   JOIN archivos a ON a.id = d.archivo_id WHERE d.id = ?""", (doc_id,)).fetchone()
            if not r:
                raise ValueError("Documento no encontrado.")
            viejo, nombre_viejo = r["ruta"], r["nombre"]
            ext = os.path.splitext(nombre_viejo)[1]
            nombre_nuevo = nueva_base + ext
            if nombre_nuevo == nombre_viejo:
                return {"anterior": nombre_viejo, "nombre": nombre_viejo, "cambiado": False}
            if not os.path.exists(viejo):
                raise ValueError("El archivo ya no está en su ruta (¿se ha movido o borrado?). Sincroniza la Biblioteca.")
            nuevo = os.path.join(os.path.dirname(viejo), nombre_nuevo)
            solo_mayusculas = os.path.normcase(nuevo) == os.path.normcase(viejo) or nombre_nuevo.lower() == nombre_viejo.lower()
            if os.path.exists(nuevo) and not solo_mayusculas:
                raise ValueError(f"Ya existe un archivo llamado «{nombre_nuevo}» en esa misma carpeta.")
            if len(to_display_path(nuevo)) > 259:
                raise ValueError("La ruta completa superaría el límite de Windows (260 caracteres). Acorta el nombre.")
            try:
                os.rename(viejo, nuevo)
            except PermissionError:
                raise ValueError("Windows no permite renombrarlo: el archivo está abierto en otro programa "
                                 "(visor PDF, correo…) o no hay permisos. Ciérralo e inténtalo de nuevo.") from None
            except OSError as e:
                raise ValueError(f"No se ha podido renombrar: {e.strerror or e}") from None

            rel_viejo = r["ruta_rel"]
            rel_nuevo = rel_viejo[: len(rel_viejo) - len(nombre_viejo)] + nombre_nuevo
            try:
                with conn:
                    conn.execute("UPDATE archivos SET ruta = ?, ruta_rel = ?, nombre = ? WHERE id = ?",
                                 (nuevo, rel_nuevo, nombre_nuevo, r["id"]))
                    # El trigger documentos_au actualiza el índice de texto completo (nombre y ruta)
                    conn.execute("UPDATE documentos SET nombre = ?, ruta_rel = ? WHERE archivo_id = ?",
                                 (nombre_nuevo, rel_nuevo, r["id"]))
                    conn.execute(f"UPDATE OR REPLACE {etiquetas.ALIAS}.doc_etiqueta SET ruta_rel = ? WHERE ruta_rel = ?",
                                 (etiquetas.clave(rel_nuevo), etiquetas.clave(rel_viejo)))
            except Exception:
                os.rename(nuevo, viejo)  # el índice no se ha podido actualizar: se deja todo como estaba
                raise
            return {"anterior": nombre_viejo, "nombre": nombre_nuevo, "cambiado": True}
        finally:
            conn.close()
    finally:
        _SYNC_LOCK.release()


# ------------------------------------------------------------------------------
# Eliminación (a la Papelera de reciclaje)
# ------------------------------------------------------------------------------

def _a_papelera_windows(path: str) -> None:
    """Envía un archivo a la Papelera con SHFileOperationW (shell32), sin dependencias."""
    import ctypes
    from ctypes import wintypes

    path = os.path.abspath(path)
    raiz = os.path.splitdrive(path)[0] + "\\"
    # Las unidades de red y las rutas UNC no tienen Papelera: Windows borraría definitivamente
    if path.startswith("\\\\") or ctypes.windll.kernel32.GetDriveTypeW(raiz) == 4:  # DRIVE_REMOTE
        raise ValueError("El archivo está en una unidad de red, que no tiene Papelera de reciclaje. "
                         "Elimínalo desde el Explorador si de verdad quieres borrarlo.")

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0", None,  # pFrom termina en doble nulo
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError(f"SHFileOperation código {rc}")


def _a_papelera_wsl(path: str) -> None:
    """Desarrollo en WSL: la Papelera de Windows a través de PowerShell."""
    win = to_display_path(path).replace("'", "''")
    cmd = ("Add-Type -AssemblyName Microsoft.VisualBasic; [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
           f"'{win}', 'OnlyErrorDialogs', 'SendToRecycleBin')")
    r = subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise OSError((r.stderr or r.stdout or "").strip()[:300])


def _a_papelera(path: str) -> None:
    """Envía un archivo a la Papelera (nunca borrado definitivo). Si ya no existe, no hace nada.
    Lanza ValueError con un mensaje para el usuario."""
    if not os.path.exists(path):
        return
    try:
        (_a_papelera_windows if os.name == "nt" else _a_papelera_wsl)(path)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError("Windows no ha podido enviarlo a la Papelera: puede que el archivo esté abierto "
                         f"en otro programa o que no haya permisos. Ciérralo e inténtalo de nuevo. ({e})") from None
    if os.path.exists(path):
        raise ValueError("El archivo sigue en su sitio: Windows no ha podido eliminarlo.")


def eliminar(doc_id: int, con_copias: bool = False, fid: Optional[str] = None) -> Dict[str, Any]:
    """Envía el archivo de un documento a la Papelera de reciclaje y lo quita del índice.

    Si tiene copias idénticas y no se piden también (`con_copias`), el documento sigue existiendo: pasa a
    una de ellas con sus etiquetas, y se devuelve en «conservado». Las etiquetas se conservan además en
    etiquetas_<fuente>.db: si el archivo se restaura desde la Papelera, la siguiente sincronización lo
    vuelve a indexar y recupera sus etiquetas."""
    if not _SYNC_LOCK.acquire(timeout=5):
        raise ValueError("Hay una sincronización en curso. Inténtalo de nuevo en cuanto termine.")
    try:
        conn = get_conn(fid)
        try:
            r = conn.execute(
                """SELECT a.id, a.ruta, a.nombre FROM documentos d
                   JOIN archivos a ON a.id = d.archivo_id WHERE d.id = ?""", (doc_id,)).fetchone()
            if not r:
                raise ValueError("Documento no encontrado.")
            copias = conn.execute("SELECT id, ruta FROM archivos WHERE copia_de = ?", (r["id"],)).fetchall()
            n_copias = 0
            if con_copias:
                for c in copias:  # primero las copias: si algo falla, el documento sigue entero en su sitio
                    _a_papelera(c["ruta"])
                    with conn:
                        conn.execute("DELETE FROM archivos WHERE id = ?", (c["id"],))
                    n_copias += 1
            _a_papelera(r["ruta"])  # (si ya no existía en disco, solo se quita del índice)
            with conn:
                nuevo = None if con_copias else duplicados.promover(conn, r["id"])
                conn.execute("DELETE FROM archivos WHERE id = ?", (r["id"],))  # documentos y FTS en cascada
                duplicados.refrescar_rutas(conn)
            out: Dict[str, Any] = {"status": "ok", "nombre": r["nombre"], "ruta_display": to_display_path(r["ruta"]),
                                   "copias_eliminadas": n_copias}
            if nuevo:
                c = conn.execute("SELECT ruta FROM archivos WHERE id = ?", (nuevo,)).fetchone()
                out["conservado"] = to_display_path(c["ruta"])
            return out
        finally:
            conn.close()
    finally:
        _SYNC_LOCK.release()


def resolver_duplicados(grupos: List[Dict[str, Any]], fid: Optional[str] = None) -> Dict[str, Any]:
    """Elimina (a la Papelera) las copias que el usuario ha descartado en cada grupo de duplicados y
    deja indexada la que ha elegido conservar. grupos = [{conservar: archivo_id, eliminar: [archivo_id…]}].

    Antes de enviar nada a la Papelera se vuelve a calcular la huella de los dos archivos: solo se
    elimina lo que en ese momento sigue siendo idéntico byte a byte a lo que se conserva. Las etiquetas de
    lo eliminado pasan a lo conservado. Los errores de un archivo no detienen el resto."""
    if not _SYNC_LOCK.acquire(timeout=5):
        raise ValueError("Hay una sincronización en curso. Inténtalo de nuevo en cuanto termine.")
    res: Dict[str, Any] = {"status": "ok", "eliminados": 0, "bytes": 0, "errores": []}
    try:
        conn = get_conn(fid)
        try:
            for g in grupos:
                try:
                    keep_id = int(g.get("conservar") or 0)
                    del_ids = list(dict.fromkeys(int(x) for x in g.get("eliminar") or [] if int(x) != keep_id))
                except (TypeError, ValueError):
                    res["errores"].append("Petición no válida.")
                    continue
                if not del_ids:
                    continue
                filas = {f["id"]: f for f in conn.execute(
                    f"""SELECT id, ruta, ruta_rel, nombre, size, hash, copia_de FROM archivos
                        WHERE id IN ({','.join('?' * (len(del_ids) + 1))})""", [keep_id] + del_ids)}
                k = filas.get(keep_id)
                if not k or not k["hash"]:
                    res["errores"].append("El archivo que se iba a conservar ya no está en el índice. Sincroniza y vuelve a intentarlo.")
                    continue
                grupo = k["copia_de"] or k["id"]
                h_keep = duplicados.huella(k["ruta"])
                if h_keep != k["hash"]:
                    res["errores"].append(f"«{k['nombre']}» ha cambiado o no se puede leer: no se ha tocado su grupo. Sincroniza y revísalo.")
                    continue
                if k["copia_de"]:  # se conserva una copia: pasa a ser la indexada
                    with conn:
                        duplicados.promover(conn, k["copia_de"], nuevo_id=k["id"])
                for did in del_ids:
                    d = filas.get(did)
                    if not d or (d["copia_de"] or d["id"]) not in (grupo, k["id"]) or d["hash"] != k["hash"]:
                        res["errores"].append("Un archivo ya no pertenece a ese grupo de duplicados: se ha omitido.")
                        continue
                    if os.path.exists(d["ruta"]) and duplicados.huella(d["ruta"]) != h_keep:
                        res["errores"].append(f"«{d['nombre']}» ya no es idéntico: no se ha eliminado.")
                        continue
                    try:
                        _a_papelera(d["ruta"])
                    except ValueError as e:
                        res["errores"].append(f"«{d['nombre']}»: {e}")
                        continue
                    with conn:
                        duplicados.copiar_etiquetas(conn, [d["ruta_rel"]], k["ruta_rel"])
                        conn.execute("DELETE FROM archivos WHERE id = ?", (d["id"],))
                    res["eliminados"] += 1
                    res["bytes"] += d["size"]
            with conn:
                duplicados.refrescar_rutas(conn)
            return res
        finally:
            conn.close()
    finally:
        _SYNC_LOCK.release()


def start_sync_async(esperar: bool = False, **kw) -> bool:
    """Lanza una sincronización en segundo plano. Si ya hay una en curso, devuelve False; con
    `esperar`, la nueva se encola y arranca en cuanto termine la actual."""
    if not esperar:
        if _SYNC_LOCK.locked():
            return False
        threading.Thread(target=sync, kwargs=kw, daemon=True, name="finder-sync").start()
        return True

    def _cuando_se_pueda() -> None:
        try:
            while sync(**kw).get("status") == "busy":
                time.sleep(2)
        except ValueError:
            pass  # la fuente se ha quitado mientras tanto

    threading.Thread(target=_cuando_se_pueda, daemon=True, name="finder-sync").start()
    return True


def sync_todas() -> None:
    """Sincroniza todas las fuentes, empezando por la activa."""
    cfg = load_config()
    ids = [f["id"] for f in cfg["fuentes"]]
    ids.sort(key=lambda i: i != cfg.get("fuente_activa"))
    for fid in ids:
        try:
            # Si hay otra sincronización en marcha (p. ej. pedida desde la interfaz), se espera a que acabe
            while sync(fid=fid).get("status") == "busy":
                time.sleep(2)
        except ValueError:
            pass  # la fuente se ha quitado mientras tanto
        except Exception:  # noqa: BLE001
            traceback.print_exc()


def background_loop(stop_event: threading.Event) -> None:
    """Sincroniza todas las fuentes al arrancar y después cada `sync_interval_min` minutos."""
    while not stop_event.is_set():
        try:
            sync_todas()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        mins = max(1, int(load_config().get("sync_interval_min", 10)))
        stop_event.wait(mins * 60)


def folder_info(fid: Optional[str] = None) -> Dict[str, Any]:
    fte = fuente(load_config(), fid)
    p = biblioteca_path(fte)
    return {"display": to_display_path(p) or fte.get("biblioteca", ""), "exists": bool(p) and os.path.isdir(p),
            "configurada": bool(str(fte.get("biblioteca", "")).strip())}


if __name__ == "__main__":
    import json
    import sys

    from .config import migrar_archivos_legado

    # python -m finder.indexador [--full] [--fuente <id>]   (sin --fuente: la fuente activa)
    migrar_archivos_legado()
    fid = sys.argv[sys.argv.index("--fuente") + 1] if "--fuente" in sys.argv[:-1] else None
    print(json.dumps(sync(force_reindex="--full" in sys.argv, fid=fid), ensure_ascii=False, indent=2))
