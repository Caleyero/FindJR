# -*- coding: utf-8 -*-
"""
OCR de los PDF escaneados (sin capa de texto) con el OCR integrado de Windows (Windows.Media.Ocr).

Sin dependencias nuevas: PyMuPDF dibuja cada página como imagen y `ocr_windows.ps1` la reconoce con el
motor de Windows en español (viene con Windows; no hay que instalar Tesseract ni nada). Solo en Windows.

- El texto reconocido se guarda en `ocr_cache.db` (fuera de data/, como las etiquetas) por huella SHA-256
  del archivo: si se reindexa, se mueve o se renombra el archivo, no se repite el OCR.
- `archivos.ocr`: NULL = no intentado · 1 = texto obtenido por OCR · 2 = intentado sin resultado
  (manuscritos, fotos...). Un documento con texto por OCR deja de ser «escaneado sin texto».
- En segundo plano (`ocr_loop`) se procesan los pendientes de todas las fuentes, de uno en uno;
  `ocr_documento` lo hace con uno concreto desde la interfaz (botón «OCR»).
"""

import concurrent.futures as cf
import datetime
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
import traceback
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF

from . import config, duplicados
from .extraccion import SEPARADOR_PAGINA, TEXTO_MAX_CHARS

_PS1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_windows.ps1")
DPI = 300            # resolución a la que se dibuja cada página
_MAX_LADO = 9000     # el motor de Windows admite imágenes de hasta 10.000 px de lado
MIN_CHARS = 60       # mismo umbral que «sin texto» en la indexación
_SIN_VENTANA = 0x08000000  # CREATE_NO_WINDOW: que no parpadee una consola (FinderJR corre con pythonw)

_LOCK = threading.Lock()          # un OCR cada vez (segundo plano y botón)
_CACHE_LOCK = threading.Lock()
_DESPERTAR = threading.Event()    # la sincronización avisa cuando hay archivos nuevos
_ESTADO: Dict[str, Any] = {"activo": False, "hechos": 0, "total": 0, "fuente": ""}
_ESTADO_LOCK = threading.Lock()
_disponible: Optional[bool] = None
_POOL: Optional[cf.ProcessPoolExecutor] = None  # dibujar las páginas en otro proceso no frena al servidor (GIL)


def estado() -> Dict[str, Any]:
    with _ESTADO_LOCK:
        return dict(_ESTADO, disponible=bool(_disponible))


def _set_estado(**kw: Any) -> None:
    with _ESTADO_LOCK:
        _ESTADO.update(kw)


def disponible() -> bool:
    """¿Hay OCR de Windows en español? Se comprueba una vez por proceso."""
    global _disponible
    if _disponible is None:
        if os.name != "nt":
            _disponible = False
        else:
            try:
                r = subprocess.run(_cmd(["-Comprobar"]), capture_output=True, timeout=60, creationflags=_SIN_VENTANA)
                _disponible = r.returncode == 0
            except Exception:  # noqa: BLE001
                _disponible = False
    return _disponible


def despertar() -> None:
    _DESPERTAR.set()


def _cmd(args: List[str]) -> List[str]:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", _PS1] + args


# ------------------------------------------------------------------------------
# Caché del texto reconocido (por huella del archivo)
# ------------------------------------------------------------------------------

def _cache() -> sqlite3.Connection:
    conn = sqlite3.connect(os.path.join(config.ETIQUETAS_DIR, "ocr_cache.db"), timeout=30.0)
    conn.execute("""CREATE TABLE IF NOT EXISTS ocr (
                        hash    TEXT PRIMARY KEY,
                        texto   TEXT NOT NULL,
                        paginas INTEGER,
                        fecha   TEXT)""")
    return conn


def cache_buscar(h: Optional[str]) -> Optional[str]:
    """Texto ya reconocido de un archivo ('' si se intentó sin resultado) o None si nunca se ha procesado."""
    if not h:
        return None
    with _CACHE_LOCK:
        conn = _cache()
        try:
            r = conn.execute("SELECT texto FROM ocr WHERE hash = ?", (h,)).fetchone()
        finally:
            conn.close()
    return r[0] if r else None


def _cache_guardar(h: str, texto: str, paginas: int) -> None:
    with _CACHE_LOCK:
        conn = _cache()
        try:
            with conn:
                conn.execute("INSERT OR REPLACE INTO ocr (hash, texto, paginas, fecha) VALUES (?, ?, ?, ?)",
                             (h, texto, paginas, datetime.datetime.now().isoformat(timespec="seconds")))
        finally:
            conn.close()


def con_texto(texto: Optional[str]) -> bool:
    return len((texto or "").strip()) >= MIN_CHARS


# ------------------------------------------------------------------------------
# Reconocimiento
# ------------------------------------------------------------------------------

def ocr_pdf(path: str, max_pages: int) -> str:
    """Texto reconocido de las primeras `max_pages` páginas, separadas como en la extracción normal."""
    with tempfile.TemporaryDirectory(prefix="finderjr_ocr_") as tmp:
        imagenes = []
        with fitz.open(path) as doc:
            for i in range(min(doc.page_count, max_pages)):
                pag = doc[i]
                lado = max(pag.rect.width, pag.rect.height) / 72 * DPI
                dpi = int(DPI * min(1.0, _MAX_LADO / lado)) if lado else DPI
                p = os.path.join(tmp, f"p{i:03d}.png")
                pag.get_pixmap(dpi=max(dpi, 72), colorspace=fitz.csGRAY).save(p)
                imagenes.append(p)
        if not imagenes:
            return ""
        lista, salida = os.path.join(tmp, "lista.txt"), os.path.join(tmp, "salida.json")
        with open(lista, "w", encoding="utf-8") as f:
            f.write("\n".join(imagenes))
        r = subprocess.run(_cmd(["-Lista", lista, "-Salida", salida]), capture_output=True,
                           timeout=120 + 30 * len(imagenes), creationflags=_SIN_VENTANA)
        if r.returncode != 0 or not os.path.exists(salida):
            err = (r.stderr or b"").decode("utf-8", "replace").strip() or f"código {r.returncode}"
            raise RuntimeError(f"OCR de Windows: {err}")
        with open(salida, encoding="utf-8-sig") as f:
            textos = json.load(f)
    textos = [str(t or "") for t in (textos if isinstance(textos, list) else [textos])]
    return SEPARADOR_PAGINA.join(textos)[:TEXTO_MAX_CHARS]


def _ocr_aparte(path: str, max_pages: int) -> str:
    """`ocr_pdf` en un proceso aparte (uno solo, reutilizado). Llamar con _LOCK tomado."""
    global _POOL
    try:
        if _POOL is None:
            _POOL = cf.ProcessPoolExecutor(max_workers=1)
        return _POOL.submit(ocr_pdf, path, max_pages).result(timeout=120 + 30 * max_pages)
    except (cf.process.BrokenProcessPool, OSError):
        _POOL = None
        return ocr_pdf(path, max_pages)  # sin procesos auxiliares: aquí mismo


def aplicar(conn: sqlite3.Connection, archivo_id: int, texto: str) -> bool:
    """Guarda en el índice el resultado del OCR de un archivo (y sus copias idénticas). Devuelve si hay texto."""
    if con_texto(texto):
        conn.execute("UPDATE documentos SET texto = ?, sin_texto = 0 WHERE archivo_id = ?", (texto, archivo_id))
        conn.execute("UPDATE archivos SET necesita_ocr = 0, ocr = 1 WHERE id = ? OR copia_de = ?", (archivo_id, archivo_id))
        return True
    conn.execute("UPDATE archivos SET ocr = 2 WHERE id = ? OR copia_de = ?", (archivo_id, archivo_id))
    return False


def _procesar(fid: str, archivo_id: int, forzar: bool = False) -> Dict[str, Any]:
    """OCR de un archivo del índice (usa la caché salvo con `forzar`). Debe llamarse con _LOCK tomado."""
    from .db import get_conn  # evita la importación circular db -> etiquetas -> ...

    conn = get_conn(fid)
    try:
        a = conn.execute("SELECT id, ruta, size, mtime, hash, ext FROM archivos WHERE id = ?", (archivo_id,)).fetchone()
        if not a:
            raise ValueError("El documento ya no está en el índice.")
        if a["ext"] != ".pdf":
            raise ValueError("El OCR solo se aplica a los PDF.")
        ruta = a["ruta"]
        h = a["hash"] or duplicados.huella(ruta)
        if not h:
            raise ValueError("No se puede leer el archivo (¿se ha movido o borrado?).")
        texto = None if forzar else cache_buscar(h)
        if texto is None:
            max_pages = int(config.load_config().get("max_pages_text", 60))
            texto = _ocr_aparte(ruta, max_pages)
            with fitz.open(ruta) as d:
                _cache_guardar(h, texto, d.page_count)
        with conn:
            # Si el archivo ha cambiado o desaparecido mientras tanto, no se toca (la sincronización lo rehará)
            sigue = conn.execute("SELECT 1 FROM archivos WHERE id = ? AND ruta = ? AND size = ? AND mtime = ?",
                                 (a["id"], ruta, a["size"], a["mtime"])).fetchone()
            if not sigue:
                return {"status": "cambiado", "con_texto": False, "caracteres": 0}
            if not a["hash"]:
                conn.execute("UPDATE archivos SET hash = ? WHERE id = ?", (h, a["id"]))
            ok = aplicar(conn, a["id"], texto)
        return {"status": "ok", "con_texto": ok, "caracteres": len(texto.strip())}
    finally:
        conn.close()


def ocr_documento(doc_id: int, fid: Optional[str] = None) -> Dict[str, Any]:
    """OCR de un documento concreto (botón de la interfaz). Siempre vuelve a reconocerlo."""
    from .db import get_conn

    if not disponible():
        raise ValueError("Este equipo no tiene el OCR de Windows en español.")
    fid = fid or config.fuente_activa_id()
    conn = get_conn(fid)
    try:
        r = conn.execute("SELECT archivo_id FROM documentos WHERE id = ?", (doc_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        raise ValueError("El documento ya no está en el índice.")
    if not _LOCK.acquire(timeout=300):
        raise ValueError("Hay otro OCR en marcha; inténtalo dentro de un momento.")
    try:
        return _procesar(fid, r["archivo_id"], forzar=True)
    finally:
        _LOCK.release()


# ------------------------------------------------------------------------------
# Segundo plano
# ------------------------------------------------------------------------------

def pendientes(conn: sqlite3.Connection) -> List[int]:
    return [r[0] for r in conn.execute(
        """SELECT id FROM archivos
           WHERE necesita_ocr = 1 AND ocr IS NULL AND copia_de IS NULL AND estado = 'OK' AND ext = '.pdf'
           ORDER BY mtime DESC""")]


def procesar_pendientes(fid: str, stop: threading.Event) -> int:
    """OCR de los escaneados aún no intentados de una fuente (los más recientes primero)."""
    from .db import get_conn

    conn = get_conn(fid)
    try:
        ids = pendientes(conn)
    finally:
        conn.close()
    if not ids:
        return 0
    nombre = config.fuente(config.load_config(), fid)["nombre"]
    hechos = 0
    _set_estado(activo=True, hechos=0, total=len(ids), fuente=nombre)
    try:
        for aid in ids:
            if stop.is_set():
                break
            with _LOCK:
                try:
                    _procesar(fid, aid)
                except Exception as e:  # noqa: BLE001
                    # No se reintenta en cada ciclo: queda como «sin resultado» (el botón permite repetirlo)
                    print(f"[OCR] archivo {aid}: {type(e).__name__}: {e}")
                    conn = get_conn(fid)
                    try:
                        with conn:
                            conn.execute("UPDATE archivos SET ocr = 2 WHERE id = ?", (aid,))
                    finally:
                        conn.close()
            hechos += 1
            _set_estado(hechos=hechos)
    finally:
        _set_estado(activo=False, hechos=0, total=0, fuente="")
    return hechos


def ocr_loop(stop: threading.Event) -> None:
    """Procesa los escaneados pendientes de todas las fuentes; se despierta tras cada sincronización
    (y, por si acaso, cada 15 minutos). Desactivable con `"ocr_auto": false` en finder_config.json."""
    stop.wait(20)  # deja arrancar primero el servidor y la primera sincronización
    while not stop.is_set():
        _DESPERTAR.clear()
        try:
            cfg = config.load_config()
            if cfg.get("ocr_auto", True) and disponible():
                ids = [f["id"] for f in cfg["fuentes"]]
                ids.sort(key=lambda i: i != cfg.get("fuente_activa"))
                for fid in ids:
                    if stop.is_set():
                        break
                    try:
                        procesar_pendientes(fid, stop)
                    except ValueError:
                        pass  # la fuente se ha quitado mientras tanto
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        deadline = time.monotonic() + 15 * 60
        while not stop.is_set() and not _DESPERTAR.is_set() and time.monotonic() < deadline:
            _DESPERTAR.wait(5)
