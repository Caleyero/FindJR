# -*- coding: utf-8 -*-
"""Prueba de humo de la API: arranca el servidor en un hilo y consulta los endpoints principales."""
import json, os, sys, threading, time, urllib.parse, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app_finder
from finder.db import init_db

t0 = time.time(); init_db(); print(f"init_db/migración: {time.time() - t0:.1f}s")
srv = app_finder.FinderHTTPServer(("127.0.0.1", 8798), app_finder.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

def get(u):
    t = time.time()
    with urllib.request.urlopen("http://127.0.0.1:8798" + u, timeout=60) as r:
        body = r.read()
    return body, round(time.time() - t, 2)

print("info:", json.loads(get("/api/info")[0])["stats"])
casos = [
    {},
    {"todas": "B06976"},
    {"todas": "1224,00"},
    {"todas": "davo pantalon"},
    {"todas": "davo pantalon", "excluye": "abono"},
    {"alguna": "fianza aval"},
    {"frase": "contrato de arrendamiento"},
    {"frase": "contrato de arrendamiento", "todas": "luanco"},
    {"todas": "credito"},
    {"excluye": "factura"},
    {"todas": "arrend", "sort_by": "fecha_desc"},
]
for c in casos:
    b, t = get("/api/search?" + urllib.parse.urlencode(c))
    d = json.loads(b)
    print(f"{c} ({t}s): {d['total_count']} docs · resaltar={d['terms']}")
    for i in d["items"][:2]:
        print("     ", i["nombre"][:60], "|", (i["snippet"] or "")[:90].replace("\n", " "))
doc_id = d["items"][0]["id"]
b, t = get(f"/api/doc/{doc_id}/page_image?page=1&t=arrend"); print("png", t, len(b), b[:4])
b, t = get(f"/api/doc/{doc_id}/pdf"); print("pdf", t, len(b), b[:5])
b, t = get("/api/export/csv?todas=davo"); print("csv", t, len(b))
b, t = get("/"); print("index", t, len(b))
srv.shutdown()
