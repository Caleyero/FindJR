# CLAUDE.md — FinderJR

Contexto técnico para continuar el desarrollo. El README cubre el uso; aquí va lo que no se deduce
leyendo el código: decisiones tomadas, su porqué y convenciones.

## Qué es

Buscador local de texto completo sobre la carpeta **Biblioteca** (`C:\Users\ofici\Documents\Biblioteca`):
~14.500 archivos (9,5 GB), ~13.900 PDF (facturas, contratos, escrituras, modelos AEAT…) que crece a diario.
Se usa solo en el PC del usuario, con un favorito a `http://localhost:8766/`.

## Decisiones de diseño (no reabrir sin que lo pida el usuario)

1. **Búsqueda bruta, sin clasificación.** La primera versión asociaba documentos a entidades de dbGes26
   (tabla `terceros`, por NIF/IBAN/nombre de archivo), clasificaba por tipo de documento y extraía
   fechas y números. Se **eliminó por completo** a petición del usuario: con documentos tan variados los
   filtros llevaban a documentos que no tenían nada que ver (p. ej. una póliza de AXA vinculada a
   «Santander Seguros Generales») y eso restaba confianza a todo lo demás. Principio rector: *lo que
   aparece en resultados contiene literalmente lo buscado*. No proponer de nuevo listboxes de entidades,
   tipos de documento, fechas «adivinadas» del texto ni lectura de dbGes.
   - **Etiquetas manuales (noviembre 2026, a petición del usuario):** compatibles con el principio
     porque las pone siempre el usuario. Nunca sugerir ni asignar etiquetas automáticamente.
     Planas (sin grupos) y de un solo puesto (sin sincronización entre equipos).
   - Restos de esa etapa: `db._migrate_v1` (migración v1→v2 del índice) y el descarte de la clave
     obsoleta `dbges_sources` en `config.load_config`. Se pueden mantener; no hacen daño.
2. **Interfaz replicada de cBank** (`C:\Users\ofici\Documents\Python\cBank`): tres paneles
   (Búsqueda | Resultados | Previsualización con resaltado), redimensionables, mismos estilos.
   Ante dudas de UI, consultar cBank y mantener la coherencia con ella.
3. **Pila mínima, sin frameworks**, igual que cBank: `ThreadingHTTPServer` de la stdlib, SQLite FTS5,
   PyMuPDF y JS/CSS sin librerías. Única dependencia: `PyMuPDF`. No añadir Flask, React, etc.
4. **Solo local, sin servidor.** Se estudió desplegarlo en SERVER2022 como CVS-Web (servicio NSSM,
   puerto propio, `--server` con `0.0.0.0`…) y el usuario lo **descartó** por simplicidad. En su lugar:
   arranque automático al iniciar sesión en Windows en segundo plano permanente
   (`arranque_automatico.ps1`, opciones 10-13 de `menu.bat`). Escucha solo en `127.0.0.1`.
5. **Solo PDF de momento** (`extensions: [".pdf"]`). Hay ~430 Office (doc/docx/xls/xlsx) pendientes.

## Arquitectura

```
app_finder.py          Servidor HTTP + API REST + motor de búsqueda (parse_search/run_query)
finder/config.py       finder_config.json (por puesto, en .gitignore) y conversión de rutas Windows <-> WSL
finder/db.py           Esquema SQLite (data/finder_index.db), FTS5, migraciones por PRAGMA user_version
finder/extraccion.py   Texto con PyMuPDF (máx. max_pages_text páginas y 400.000 caracteres)
finder/indexador.py    Sincronización incremental en paralelo + bucle en segundo plano
finder/etiquetas.py    Etiquetas manuales (etiquetas.db, adjunta como «et» en cada conexión)
static/                index.html, app.js, styles.css (estilo cBank)
tools/probar_api.py    Prueba de humo contra el servidor en marcha
```

### Índice (`finder/db.py`, `SCHEMA_VERSION = 2`)
- `archivos`: un registro por archivo físico (ruta, size, mtime, páginas, `necesita_ocr`, estado OK/ERROR).
- `documentos`: hoy 1:1 con `archivos` (los campos `primera_pag/ultima_pag` existen por si algún día se
  parte un PDF en varios documentos). `mtime` es la fecha de modificación del archivo: la única fecha que
  se muestra y por la que se ordena.
- `documentos_fts`: FTS5 *external content* sobre (`nombre`, `ruta_rel`, `texto`) con
  `unicode61 remove_diacritics 2`, mantenida por triggers. Si se cambia el esquema, subir
  `SCHEMA_VERSION` y escribir la migración en `init_db` (versiones desconocidas se borran y se reindexa).
- `sin_texto`/`necesita_ocr` = texto extraído < 60 caracteres (escaneados, ~1.100 PDF).

### Etiquetas (`finder/etiquetas.py`)
- Base de datos **aparte** (`etiquetas.db` en la raíz; `FINDERJR_ETIQUETAS_DB` la cambia para pruebas)
  porque `data/` se regenera. `get_conn` la adjunta con `ATTACH ... AS et`; `init_db` crea su esquema.
- Tablas `et.etiquetas` (nombre único sin mayúsculas, color de `PALETA`, `tecla` 1-9 única opcional) y
  `et.doc_etiqueta` (`ruta_rel`, `etiqueta_id`). La clave es `ruta_rel` con `/` (`CLAVE_DOC` normaliza
  la del índice), así sobrevive a reindexar y a modificar el archivo; mover/renombrar la deja huérfana.
- Filtros de búsqueda: `et=1,2` (todas), `etx=3` (ninguna), `sin_et=1`; los aplica `filtros_sql` en las
  dos ramas de `run_query`, así que CSV y `pdf_bundle` los respetan.
- Pendiente opcional: fusionar etiquetas; reenganchar etiquetas huérfanas tras mover archivos (hash).

### Indexación (`finder/indexador.py`)
- Detección de cambios por ruta + tamaño + mtime (tolerancia 1 s). Archivo modificado = borrar e insertar.
- Extracción con `ProcessPoolExecutor` (hasta 4 procesos), lotes de 48, con caída a modo secuencial si
  el pool falla. `_SYNC_LOCK` impide dos sincronizaciones en el mismo proceso; **no** entre procesos:
  antes de `python -m finder.indexador --full` hay que detener la instancia en segundo plano.
- `background_loop` sincroniza al arrancar y cada `sync_interval_min` (10). Primera indexación completa:
  ~3 min; después solo lo nuevo.
- `excluded_dirs` excluye por nombre de carpeta en cualquier nivel (`dbGes26_Backup`).

### Renombrado (`indexador.renombrar`, `POST /api/doc/<id>/renombrar {nombre}`)
- Renombra en disco en la misma carpeta, conservando la extensión original (también sus mayúsculas).
  `validar_nombre` aplica las reglas de Windows (caracteres prohibidos, reservados, punto/espacio final,
  200 caracteres, ruta < 260). Se permiten cambios solo de mayúsculas.
- Toma `_SYNC_LOCK` (espera hasta 5 s) para no cruzarse con la sincronización. Actualiza `archivos` y
  `documentos` (el trigger `documentos_au` rehace el FTS) y mueve las etiquetas a la nueva `ruta_rel`.
  Si falla la BD, deshace el renombrado en disco. El mtime no cambia, así que la siguiente
  sincronización no relee el archivo.
- UI: edición en el sitio (doble clic en el nombre de Resultados o de la previsualización, F2, botón del
  lápiz). Intro/salir del campo guarda, Esc cancela; aviso con «Deshacer». Mientras se edita
  (`state.renaming`) no se refrescan los resultados tras una sincronización.

### Eliminación (`indexador.eliminar`, `POST /api/doc/<id>/eliminar`)
- **Nunca borrado definitivo**: a la Papelera con `SHFileOperationW` + `FOF_ALLOWUNDO` (ctypes, sin
  dependencias); en WSL, vía PowerShell (`Microsoft.VisualBasic...DeleteFile SendToRecycleBin`).
  Se rechazan unidades de red/UNC (`GetDriveTypeW == DRIVE_REMOTE`): allí Windows borraría sin Papelera.
- Bajo `_SYNC_LOCK`; comprueba que el archivo ya no existe antes de borrar la fila de `archivos`
  (documentos y FTS en cascada). Archivo abierto en otro programa = código 32 → mensaje claro.
- Las etiquetas se conservan en `etiquetas.db` (huérfanas): si se restaura, se recuperan.
- UI: botón papelera / Supr → modal con foco en «Cancelar»; tras borrar se selecciona el siguiente.

### Búsqueda (`app_finder.py`)
- Cuatro campos combinables: `todas` (AND), `alguna` (OR), `frase` (literal; varias entre comillas),
  `excluye` (NOT IN). Máx. 10 palabras por campo y 4 frases.
- Cada palabra se busca como prefijo (`"arrend"*`). `expand_numeric_variants`: `1224,00` ⇄ `1.224,00`.
  Los separadores se ignoran: `B-06976` genera `"B 06976"*` OR `"B06976"*`.
- Relevancia: `bm25(documentos_fts, 8.0, 4.0, 1.0)`, con más peso para el nombre de archivo y la ruta
  que para el texto. Otras ordenaciones: fecha (por defecto descendente) y nombre.
- Resaltado: `terms` se devuelve al cliente; el fragmento se localiza con `_fold` (sin acentos y con la
  misma longitud). La página PDF se renderiza a PNG con los términos resaltados (`/api/doc/<id>/page_image`).
- Referencia de rendimiento: < 0,1 s típico; el peor caso es ~1,2 s (prefijo corto con muchos aciertos
  ordenado por fecha).

### API
GET `/api/info`, `/api/search`, `/api/status`, `/api/config`, `/api/doc/<id>[/pdf|/page_image]`,
`/api/export/csv`, `/api/export/pdf_bundle` (une hasta 300 PDF filtrados), `/api/etiquetas` (con recuentos).
POST `/api/heartbeat`, `/api/sync`, `/api/open_location` (abre el Explorador en el archivo),
`/api/layout` (guarda anchos de paneles en `finder_config.json`), `/api/config`,
`/api/etiquetas` (crear), `/api/etiquetas/<id>` (nombre/color/tecla), `/api/etiquetas/<id>/borrar`,
`/api/etiquetar` (`etiqueta_id`, `accion` poner|quitar, `doc_ids` o `busqueda`), `/api/doc/<id>/renombrar`, `/api/doc/<id>/eliminar`.

### Ciclo de vida del proceso
- Por defecto (`iniciar_finderjr.bat` con doble clic) abre el navegador y **se apaga solo** al cerrar la
  última pestaña (heartbeat cada pocos s; `lifecycle_watchdog`), salvo que haya una indexación en curso.
- Modo segundo plano permanente: `--no-browser --no-auto-shutdown` (lo que usa `finderjr_fondo.vbs` →
  `iniciar_finderjr.bat --silent --fondo`). Así está instalado en el PC del usuario.
- Si el puerto 8766 ya está ocupado, `main()` solo abre el navegador y sale (instancia única).
  Puerto configurable con la variable `FINDERJR_PORT`.
- **Tras cambiar código Python hay que reiniciar** el proceso (menú opción 13); los estáticos basta con
  recargar el navegador.

## Entorno de desarrollo

- Producción: Windows (Python de Windows, `pythonw`). Desarrollo: **WSL** sobre la misma carpeta
  (`/mnt/c/Users/ofici/...`). `config.to_local_path` convierte `C:\...` ⇄ `/mnt/c/...`; las rutas
  guardadas en el índice son las del intérprete que indexó. **No mezclar** un índice creado desde WSL con
  el servidor de Windows (o viceversa) sin reindexar.
- **No arrancar `app_finder.py` desde WSL sobre el índice real**: `background_loop` sincroniza al
  arrancar y reescribe el índice con rutas de WSL (y, sin PyMuPDF, deja todo en ERROR; pasó en noviembre
  de 2026). Para probar, levantar solo `FinderHTTPServer` sin el bucle (como `tools/probar_api.py`),
  a ser posible sobre una copia del índice y con `FINDERJR_ETIQUETAS_DB` apuntando a un archivo temporal.
- Capturas de pantalla: el MCP de puppeteer y el Chromium de Playwright en WSL no funcionan (faltan
  bibliotecas). Sí funciona el Chrome de Windows en headless con `--remote-debugging-port=9333`
  (WSL en red espejo ve `127.0.0.1`) + `playwright-core` (`chromium.connectOverCDP`).
- No existen tests automatizados. `tools/probar_api.py` es la prueba de humo: levanta su propio servidor
  en el puerto 8798 sobre el índice real y lanza búsquedas de ejemplo.

## Convenciones

- Todo en **castellano de España**: UI, mensajes, comentarios, commits y documentación.
- Nombres de módulos y funciones de dominio en español (`indexador`, `extraccion`, `carpeta`, `ruta_rel`);
  utilidades genéricas en inglés (`get_conn`, `sync`). Seguir el estilo existente de cada archivo.
- Scripts de Windows: `.bat` llama a `.ps1`. Los `.ps1` usan `$ErrorActionPreference = "Stop"` → cuidado
  con comandos nativos que escriben en stderr (lección aprendida: `git remote get-url origin` sin remoto
  cerraba el menú; se usa `git config --get remote.origin.url`). Evitar caracteres no ASCII en `.ps1`/`.bat`
  (el menú ya los evita: «pestana», «Configuracion»).
- Finales de línea definidos en `.gitattributes` (`.bat`, `.ps1`, `.vbs` en CRLF).
- Git: `menu.bat` (estado, bajar, subir, vincular). Remoto: `https://github.com/Caleyero/FindJR.git`.
  `subir.bat` hace add + commit + push, sin versionado ni changelog (a diferencia de CVS-Web).
- Fuera del repo (`.gitignore`): `data/` (índice, ~180 MB, se regenera), `finder_config.json` (por puesto)
  y `etiquetas.db` (trabajo manual del usuario: **no** se regenera, debe ir en las copias de seguridad).

## Proyectos relacionados (misma carpeta `Documents\Python`)

- **cBank**: modelo de arquitectura e interfaz.
- **CVS-Web**: modelo del `menu.bat`/scripts de git; desplegado en SERVER2022 (no aplica a FinderJR).
- **dbGes26**: ya no se usa (ver la decisión 1).

## Pendiente (por prioridad)

1. **OCR** de los ~1.100 PDF escaneados (Tesseract en español) para que se encuentren por contenido;
   hoy solo se encuentran por nombre y carpeta. Debe integrarse en la indexación incremental sin
   repetir el OCR de lo ya procesado.
2. **Formatos Office** (docx/xlsx; ~430 archivos).
