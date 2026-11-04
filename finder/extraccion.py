# -*- coding: utf-8 -*-
"""Extracción del texto de los documentos de la Biblioteca.

- PDF con PyMuPDF.
- Word (.docx) y Excel (.xlsx/.xlsm) leyendo directamente su XML (son archivos ZIP), con la
  biblioteca estándar: sin dependencias nuevas. Los formatos binarios antiguos (.doc/.xls) no se leen.
"""

import datetime
import os
import re
import zipfile
from typing import Dict, Iterator, List, Optional, Tuple
from xml.etree import ElementTree as ET

import fitz  # PyMuPDF

try:
    fitz.TOOLS.mupdf_display_errors(False)
except Exception:  # noqa: BLE001
    pass

SEPARADOR_PAGINA = "\n--- [PÁGINA SIGUIENTE] ---\n"
TEXTO_MAX_CHARS = 400000

EXT_WORD = {".docx", ".docm"}
EXT_EXCEL = {".xlsx", ".xlsm"}
# Límite de lo que se descomprime de cada parte del ZIP (evita «bombas zip»)
_MAX_PARTE = 200 * 1024 * 1024


def es_pdf(nombre: str) -> bool:
    return os.path.splitext(nombre)[1].lower() == ".pdf"


def extract_text(path: str, max_pages: int) -> Tuple[int, str]:
    """(número de páginas u hojas, texto) según la extensión del archivo."""
    ext = os.path.splitext(path)[1].lower()
    if ext in EXT_WORD:
        return extract_docx_text(path)
    if ext in EXT_EXCEL:
        return extract_xlsx_text(path, max_pages)
    return extract_pdf_text(path, max_pages)


def extract_pdf_text(path: str, max_pages: int) -> Tuple[int, str]:
    """Devuelve (número de páginas, texto de las primeras `max_pages` páginas)."""
    with fitz.open(path) as doc:
        n = doc.page_count
        parts = []
        for i in range(min(n, max_pages)):
            try:
                parts.append(doc[i].get_text("text") or "")
            except Exception:  # noqa: BLE001
                parts.append("")
    return n, SEPARADOR_PAGINA.join(parts)[:TEXTO_MAX_CHARS]


# ------------------------------------------------------------------------------
# Utilidades OOXML
# ------------------------------------------------------------------------------

def _abrir_zip(path: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        # Los Office protegidos con contraseña se guardan cifrados (formato OLE, no ZIP)
        raise ValueError("No se puede leer: el archivo está protegido con contraseña o dañado") from None


def _parte(z: zipfile.ZipFile, nombre: str):
    """Abre una parte del ZIP (sin distinguir mayúsculas) o None si no existe o es desmesurada."""
    nombres = {n.lower(): n for n in z.namelist()}
    real = nombres.get(nombre.lower())
    if not real or z.getinfo(real).file_size > _MAX_PARTE:
        return None
    return z.open(real)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ------------------------------------------------------------------------------
# Word (.docx)
# ------------------------------------------------------------------------------

def _texto_word(fh) -> str:
    """Texto de una parte de Word: un párrafo por línea; cada fila de tabla, en una línea con las celdas
    separadas por « | »."""
    salida: List[str] = []
    parrafo: List[str] = []
    celdas: List[str] = []       # celdas de la fila en curso
    celda: List[str] = []        # párrafos de la celda en curso
    nivel_tabla = 0
    for ev, el in ET.iterparse(fh, events=("start", "end")):
        tag = _local(el.tag)
        if ev == "start":
            if tag == "tbl":
                nivel_tabla += 1
            continue
        if tag == "t":
            parrafo.append(el.text or "")
        elif tag == "tab":
            parrafo.append("\t")
        elif tag in ("br", "cr"):
            parrafo.append("\n" if not nivel_tabla else " ")
        elif tag == "p":
            txt = "".join(parrafo)
            parrafo = []
            if nivel_tabla:
                if txt.strip():
                    celda.append(txt.strip())
            else:
                salida.append(txt)
            el.clear()
        elif tag == "tc":
            celdas.append(" ".join(celda))
            celda = []
            el.clear()
        elif tag == "tr":
            if any(c.strip() for c in celdas):
                if nivel_tabla > 1:  # tabla anidada: su fila se incorpora a la celda exterior
                    celda.append(" | ".join(celdas))
                else:
                    salida.append(" | ".join(celdas))
            celdas = []
            el.clear()
        elif tag == "tbl":
            nivel_tabla -= 1
        elif tag == "body":
            el.clear()
    return "\n".join(salida)


def extract_docx_text(path: str) -> Tuple[int, str]:
    """(páginas según el propio Word, si las guardó; si no, 1) y texto: cuerpo, encabezados, pies y notas."""
    with _abrir_zip(path) as z:
        partes = ["word/document.xml"]
        extra = sorted(n for n in z.namelist()
                       if re.match(r"word/(header|footer|footnotes|endnotes)\d*\.xml$", n, re.I))
        bloques = []
        for nombre in partes + extra:
            fh = _parte(z, nombre)
            if fh is None:
                continue
            with fh:
                txt = _texto_word(fh).strip()
            if txt and txt not in bloques:  # los encabezados suelen repetirse
                bloques.append(txt)
        paginas = 1
        fh = _parte(z, "docProps/app.xml")
        if fh is not None:
            with fh:
                for el in ET.parse(fh).getroot():
                    if _local(el.tag) == "Pages" and (el.text or "").strip().isdigit():
                        paginas = max(1, int(el.text.strip()))
    return paginas, "\n\n".join(bloques)[:TEXTO_MAX_CHARS]


# ------------------------------------------------------------------------------
# Excel (.xlsx)
# ------------------------------------------------------------------------------

# Formatos de número integrados de Excel que son fechas u horas
_FMT_FECHA_INTEGRADOS = set(range(14, 23)) | {27, 30, 36, 45, 46, 47, 50, 57}
_EPOCA_EXCEL = datetime.datetime(1899, 12, 30)


def _es_formato_fecha(codigo: str) -> bool:
    """Si un código de formato personalizado es de fecha u hora (quitando literales, colores y rellenos)."""
    limpio = re.sub(r'"[^"]*"|\[[^\]]*\]|[\\_*].', "", codigo or "").lower()
    return bool(re.search(r"[dmyhs]", limpio))


def _estilos_fecha(z: zipfile.ZipFile) -> List[bool]:
    """Para cada estilo de celda (índice `s`), si su formato de número es de fecha."""
    fh = _parte(z, "xl/styles.xml")
    if fh is None:
        return []
    with fh:
        raiz = ET.parse(fh).getroot()
    propios: Dict[int, bool] = {}
    out: List[bool] = []
    for hijo in raiz:
        if _local(hijo.tag) == "numFmts":
            for nf in hijo:
                try:
                    propios[int(nf.get("numFmtId", "-1"))] = _es_formato_fecha(nf.get("formatCode", ""))
                except ValueError:
                    pass
        elif _local(hijo.tag) == "cellXfs":
            for xf in hijo:
                try:
                    fid = int(xf.get("numFmtId", "0"))
                except ValueError:
                    fid = 0
                out.append(propios.get(fid, fid in _FMT_FECHA_INTEGRADOS))
    return out


def _cadenas_compartidas(z: zipfile.ZipFile) -> List[str]:
    fh = _parte(z, "xl/sharedStrings.xml")
    if fh is None:
        return []
    out: List[str] = []
    partes: List[str] = []
    fonetica = 0  # <rPh>: guía fonética (japonés), no es texto visible
    with fh:
        for ev, el in ET.iterparse(fh, events=("start", "end")):
            tag = _local(el.tag)
            if tag == "rPh":
                fonetica += 1 if ev == "start" else -1
            elif ev == "end" and tag == "t" and not fonetica:
                partes.append(el.text or "")
            elif ev == "end" and tag == "si":
                out.append("".join(partes))
                partes = []
                el.clear()
    return out


def _numero_es(valor: str) -> str:
    """Número de Excel al formato español con el que se suele buscar: 1224.5 -> 1.224,50."""
    try:
        f = float(valor)
    except ValueError:
        return valor
    if f != f or f in (float("inf"), float("-inf")):
        return valor
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    txt = f"{f:,.2f}" if abs(f) >= 0.01 else f"{f:.6g}"
    return txt.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _fecha_es(valor: str) -> str:
    try:
        f = float(valor)
        if f < 1 or f > 2958465:  # fuera del rango de fechas de Excel (hasta 9999)
            return _numero_es(valor)
        d = _EPOCA_EXCEL + datetime.timedelta(days=f)
    except (ValueError, OverflowError):
        return valor
    if f == int(f):
        return d.strftime("%d/%m/%Y")
    if f < 1:
        return d.strftime("%H:%M")
    return d.strftime("%d/%m/%Y %H:%M")


def _hojas(z: zipfile.ZipFile) -> List[Tuple[str, str]]:
    """[(nombre de la hoja, parte XML)] en el orden del libro."""
    fh = _parte(z, "xl/workbook.xml")
    if fh is None:
        return []
    with fh:
        raiz = ET.parse(fh).getroot()
    rels: Dict[str, str] = {}
    fr = _parte(z, "xl/_rels/workbook.xml.rels")
    if fr is not None:
        with fr:
            for rel in ET.parse(fr).getroot():
                destino = rel.get("Target", "")
                destino = destino.lstrip("/") if destino.startswith("/") else "xl/" + destino
                rels[rel.get("Id", "")] = os.path.normpath(destino).replace("\\", "/")
    out = []
    for el in raiz.iter():
        if _local(el.tag) != "sheet":
            continue
        rid = next((v for k, v in el.attrib.items() if _local(k) == "id"), "")
        parte = rels.get(rid)
        if parte:
            out.append((el.get("name", ""), parte))
    return out


def _filas_hoja(fh, compartidas: List[str], fechas: List[bool]) -> Iterator[str]:
    """Filas de una hoja con las celdas no vacías separadas por tabuladores."""
    fila: List[str] = []
    valor: Optional[str] = None
    en_linea: List[str] = []
    tipo = estilo = None
    for ev, el in ET.iterparse(fh, events=("start", "end")):
        tag = _local(el.tag)
        if ev == "start":
            if tag == "c":
                tipo, valor, en_linea = el.get("t"), None, []
                try:
                    estilo = int(el.get("s", "0"))
                except ValueError:
                    estilo = 0
            continue
        if tag == "v":
            valor = el.text
        elif tag == "t":  # cadena en línea (<is><t>)
            en_linea.append(el.text or "")
        elif tag == "c":
            txt = ""
            if tipo == "s":
                try:
                    txt = compartidas[int(valor or "")]
                except (ValueError, IndexError):
                    txt = ""
            elif tipo == "inlineStr":
                txt = "".join(en_linea)
            elif tipo in ("str", "e"):
                txt = valor or ""
            elif tipo == "b":
                txt = "VERDADERO" if valor == "1" else "FALSO"
            elif valor is not None:
                es_fecha = tipo == "d" or (estilo is not None and estilo < len(fechas) and fechas[estilo])
                txt = valor if tipo == "d" else (_fecha_es(valor) if es_fecha else _numero_es(valor))
            txt = txt.strip()
            if txt:
                fila.append(txt.replace("\t", " ").replace("\r", "").replace("\n", " "))
            el.clear()
        elif tag == "row":
            if fila:
                yield "\t".join(fila)
            fila = []
            el.clear()


def extract_xlsx_text(path: str, max_hojas: int) -> Tuple[int, str]:
    """(número de hojas, texto): cada hoja empieza con su nombre y lleva una fila por línea."""
    with _abrir_zip(path) as z:
        compartidas = _cadenas_compartidas(z)
        fechas = _estilos_fecha(z)
        hojas = _hojas(z)
        bloques: List[str] = []
        total = 0
        for nombre, parte in hojas[:max_hojas]:
            fh = _parte(z, parte)
            if fh is None:
                continue
            lineas = [f"[Hoja: {nombre}]"]
            with fh:
                for linea in _filas_hoja(fh, compartidas, fechas):
                    lineas.append(linea)
                    total += len(linea) + 1
                    if total > TEXTO_MAX_CHARS:
                        break
            bloques.append("\n".join(lineas))
            if total > TEXTO_MAX_CHARS:
                break
    return max(1, len(hojas)), SEPARADOR_PAGINA.join(bloques)[:TEXTO_MAX_CHARS]
