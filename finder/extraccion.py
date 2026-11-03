# -*- coding: utf-8 -*-
"""Extracción del texto de los documentos de la Biblioteca (PyMuPDF)."""

from typing import Tuple

import fitz  # PyMuPDF

try:
    fitz.TOOLS.mupdf_display_errors(False)
except Exception:  # noqa: BLE001
    pass

SEPARADOR_PAGINA = "\n--- [PÁGINA SIGUIENTE] ---\n"
TEXTO_MAX_CHARS = 400000


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
