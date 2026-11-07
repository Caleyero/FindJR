# CLAUDE.md — FinderJR

Contexto técnico para continuar el desarrollo. El README cubre el uso; aquí va lo que no se deduce
leyendo el código: decisiones tomadas, su porqué y convenciones.

## Qué es

Buscador local de texto completo sobre una o varias **fuentes** de documentos (carpetas aisladas entre sí;
en el puesto original, la **Biblioteca** `C:\Users\ofici\Documents\Biblioteca`):
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
     porque las pone siempre el usuario. Nunca sugerir etiquetas ni inventar reglas por cuenta propia.
     Planas (sin grupos) y de un solo puesto (sin sincronización entre equipos).
   - **Etiquetado automático de facturas por el nombre del archivo (noviembre 2026, a petición del
     usuario)**: única excepción, porque la regla la configura él en Preferencias y se basa en lo que dice
     literalmente el nombre. Ver «Etiquetado automático» más abajo. El proveedor **no** se convierte en
     etiqueta (345 distintos inundarían la lista); se encuentra escribiéndolo en la búsqueda.
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
   - **Acceso desde Internet (noviembre 2026, a petición del usuario)**: en el PC de casa (`jorge`, el
     mini PC con Docker de `Aplicaciones\Webserver`) sustituye a Oráculo en `https://oraculo.noobe.org`
     para consultar desde el móvil. Ver «Acceso desde Internet» más abajo. Sigue sin `0.0.0.0` ni servicio.
5. **PDF, .docx y .xlsx** (`extensions: [".pdf", ".docx", ".xlsx"]`, noviembre 2026, a petición del usuario).
   Word y Excel se leen con `zipfile` + `ElementTree` de la stdlib (son ZIP con XML), **sin dependencias
   nuevas**; PyMuPDF no abre Office (solo la versión Pro). `.docm`/`.xlsm` usan el mismo extractor pero no
   están activados (7 .xlsm). Los binarios antiguos `.doc`/`.xls` (~200) siguen sin leerse.

## Arquitectura

```
app_finder.py          Servidor HTTP + API REST + motor de búsqueda (parse_search/run_query)
finder/config.py       finder_config.json (por puesto, en .gitignore), fuentes y conversión de rutas Windows <-> WSL
finder/db.py           Esquema SQLite (data/finder_index_<fuente>.db), FTS5, migraciones por PRAGMA user_version
finder/extraccion.py   Texto: PDF con PyMuPDF (máx. max_pages_text páginas); docx/xlsx con la stdlib. Máx. 400.000 caracteres
finder/indexador.py    Sincronización incremental en paralelo + bucle en segundo plano
finder/etiquetas.py    Etiquetas manuales (etiquetas_<fuente>.db, adjunta como «et» en cada conexión)
finder/autoetiquetas.py Regla de facturas por el nombre del archivo (EMP_PROVEEDOR - NÚMERO)
finder/ocr.py          OCR de los escaneados con el OCR de Windows (ocr_windows.ps1) + caché ocr_cache.db
finder/remoto.py       Acceso desde Internet: verificación del JWT de Cloudflare Access y lista blanca de rutas
finder/duplicados.py   Duplicados exactos (SHA-256): huellas, promoción de copias, consolidación, listado
static/                index.html, app.js, styles.css (estilo cBank)
tools/probar_api.py    Prueba de humo contra el servidor en marcha
```

### Fuentes (`config.fuente`, `/api/fuentes`, noviembre 2026, a petición del usuario)
- Uso personal y profesional (o más) **totalmente aislados**: cada fuente tiene carpeta, exclusiones,
  índice `data/finder_index_<id>.db` y etiquetas `etiquetas_<id>.db` propios. El aislamiento es físico
  (archivos SQLite distintos), no un filtro: ninguna consulta puede mezclar fuentes.
- `finder_config.json`: `fuentes: [{id, nombre, color, biblioteca, include_subfolders, excluded_dirs}]` y
  `fuente_activa`. El `id` (slug ASCII) es estable y da nombre a los archivos; el `nombre` se puede cambiar.
  `extensions`, `max_pages_text` y `sync_interval_min` son globales.
- Migración (`_normalizar_fuentes` + `migrar_archivos_legado`, al arrancar): la antigua `biblioteca` pasa a
  ser la primera fuente (nombre = carpeta) y sus `finder_index.db`/`etiquetas.db` se renombran a esa fuente;
  se añade otra «Profesional» (o «Personal») vacía.
- La fuente activa es **global en el servidor** (un usuario, un puesto): `get_conn()` sin argumento usa la
  activa, así que URLs de PDF, CSV y unión no llevan fuente (el cliente añade `&f=` solo para la caché).
  Las operaciones que modifican (`/api/doc/*`, `/api/etiquet*`, `/api/sync`, `/api/open_location`, `/api/open_file`, `/api/duplicados/*`) llevan
  la cabecera `X-FinderJR-Fuente`; si no coincide con la activa → 409 y la pestaña se pone al día.
- `background_loop` sincroniza todas las fuentes (la activa primero). Una fuente sin carpeta o con la
  carpeta inaccesible **no** se sincroniza (antes se vaciaba el índice entero). Quitar una fuente borra su
  índice y conserva `etiquetas_<id>.db` (si se recrea con el mismo nombre, las recupera).

### Acceso desde Internet (`finder/remoto.py`, `remoto` en `finder_config.json`)
- Cadena: móvil → Cloudflare Access (código por correo, «Solo yo») → Cloudflare Tunnel (contenedor
  `cloudflare-tunnel`) → Nginx Proxy Manager (proxy host `oraculo.noobe.org` → `http 172.18.0.1 8768`) →
  relé `finderjr-relay` (`Webserver/apps/finderjr-relay.yml`, socat con `network_mode: host`) →
  `127.0.0.1:8767` de Windows → **segundo listener** de FinderJR (`servir_remoto`, `server.remoto = True`).
- Por qué el relé: Docker corre dentro de Ubuntu (WSL, red *mirrored*) y los contenedores **no** llegan al
  `127.0.0.1` de Windows (comprobado: `host.docker.internal` → *connection refused*); Ubuntu sí.
  Con *mirrored*, un puerto abierto en Ubuntu queda reservado también en Windows (WinError 10013/10048,
  incluso por conexiones en TIME-WAIT): por eso relé (8768) y FinderJR (8767) usan puertos distintos y
  `servir_remoto` reintenta cada 5 s si el puerto está ocupado.
- Todo lo que entra por el puerto remoto es remoto (no se mira ninguna cabecera para decidirlo). Cada
  petición debe traer `Cf-Access-Jwt-Assertion`: RS256 verificado con la stdlib (`pow` + PKCS#1 v1.5),
  claves de `https://<cf_team>/cdn-cgi/access/certs` (caché 1 h), `aud` = `cf_aud`, `iss`, `exp` y `emails`.
  **Falla cerrado** (sin configurar o sin poder descargar las claves → 503). Así, ni desde la LAN
  saltándose Access se entra.
- Desde fuera **solo consultar y etiquetar**: GET libre; POST solo `RUTAS_POST_REMOTAS` (heartbeat,
  etiquetar, crear etiqueta, cambiar fuente, sincronizar). El resto → 403. `/api/info` devuelve
  `remoto: true` y la UI oculta lo `.solo-local` (renombrar, Papelera, duplicados, Preferencias, gestionar
  etiquetas, abrir ubicación); «Abrir» de Word/Excel descarga el archivo.
- Configuración por puesto: `"remoto": {"activo", "puerto": 8767, "cf_team", "cf_aud", "emails"}`
  (desactivado por defecto: el PC de la oficina no cambia). Tras cambiarla, reiniciar (opción 13).
- Requisitos: FinderJR en segundo plano permanente y sesión de Windows iniciada (Docker arranca sin
  sesión, FinderJR no). En el propio PC lo normal es `http://localhost:8766` (sin Access, todos los
  permisos). Se quitó `127.0.0.1 oraculo.noobe.org` del `hosts` de Windows (copia en
  `hosts.antes-finderjr`): con esa línea se iba directo a NPM sin pasar por Access y daba 403.
- Interfaz móvil (`@media (max-width: 760px)`): un panel cada vez (`body[data-vista]`, barra
  `#mobileNav`, el botón «atrás» vuelve al panel anterior con `history.pushState`); sin la pestaña
  «Visor PDF» (el iframe de PDF no funciona en Android). Sirve igual en local con ventana estrecha.

### Índice (`finder/db.py`, `SCHEMA_VERSION = 4`)
- `archivos`: un registro por archivo físico (ruta, size, mtime, páginas, `necesita_ocr`, estado OK/ERROR).
- `documentos`: hoy 1:1 con `archivos` (los campos `primera_pag/ultima_pag` existen por si algún día se
  parte un PDF en varios documentos). `mtime` es la fecha de modificación del archivo: la única fecha que
  se muestra y por la que se ordena.
- `documentos_fts`: FTS5 *external content* sobre (`nombre`, `ruta_rel`, `texto`, `otras_rutas`) con
  `unicode61 remove_diacritics 2`, mantenida por triggers. Si se cambia el esquema, subir
  `SCHEMA_VERSION` y escribir la migración en `init_db` (versiones desconocidas se borran y se reindexa).
- `sin_texto`/`necesita_ocr` = texto extraído < 60 caracteres (escaneados, ~1.100 PDF).
- v3 (noviembre 2026): `archivos.hash` y `archivos.copia_de`, `documentos.otras_rutas` (también en el FTS).
  `_asegurar_v3` migra un índice v2 sin reindexar (recrea y reconstruye el FTS: ~3 s). Ojo: el código
  anterior a v3 trata la versión 3 como desconocida y **borra el índice** (habría que reindexar).
- v4 (noviembre 2026): `archivos.ocr` (NULL no intentado · 1 texto por OCR · 2 sin resultado). `_asegurar_v4`
  añade la columna sin reindexar. Mismo aviso: el código anterior a v4 borraría un índice v4.

### OCR de los escaneados (`finder/ocr.py`, noviembre 2026, a petición del usuario)
- Motor: **OCR integrado de Windows** (`Windows.Media.Ocr`, idioma `es-ES`), sin instalar nada ni
  dependencias nuevas (se descartó Tesseract por eso). PyMuPDF dibuja cada página a 300 ppp en grises
  (máx. 9.000 px de lado; el motor admite 10.000) y `finder/ocr_windows.ps1` (PowerShell + WinRT) devuelve el
  texto en un JSON. ~1 s por página. Reconoce bien lo impreso; lo manuscrito sale como basura o vacío.
  Solo en Windows (`ocr.disponible()`, comprobado una vez por proceso); en WSL no hay OCR.
- El dibujado va en un proceso aparte (`_ocr_aparte`, un `ProcessPoolExecutor` de 1): en el mismo proceso
  el GIL frenaba al servidor (búsquedas de 1,7 s mientras trabajaba el OCR).
- Caché **`ocr_cache.db`** (raíz, junto a las etiquetas; fuera de `data/`) por SHA-256 del archivo: reindexar,
  mover o renombrar no repite el OCR. `_store` la consulta al indexar un escaneado. Guarda también los
  intentos sin resultado (`texto = ''`) para no repetirlos.
- Con ≥ 60 caracteres el texto sustituye al de `documentos` y `sin_texto`/`necesita_ocr` pasan a 0
  (`ocr.aplicar`, también en sus copias idénticas). Lo reconocido se busca como cualquier texto; la vista de
  página no lo resalta (el PDF sigue sin capa de texto), la pestaña «Texto» sí.
- Segundo plano: `ocr_loop` (hilo propio) procesa de uno en uno los pendientes de todas las fuentes
  (los más recientes primero); lo despierta `sync` cuando hay archivos nuevos o modificados, y si no, cada
  15 min. Un error marca `ocr = 2` (no se reintenta solo). `"ocr_auto": false` en `finder_config.json`
  lo desactiva. Progreso en `/api/status` → `ocr` (la barra de la cabecera dice «OCR n/total»).
- Botón «OCR» / «Repetir OCR» de la previsualización (solo PDF escaneados o ya reconocidos, solo en el PC):
  `POST /api/doc/<id>/ocr`, síncrono y sin caché.

### Duplicados (`finder/duplicados.py`, noviembre 2026, a petición del usuario)
- Solo **idénticos byte a byte** (SHA-256). Se descartó comparar el texto: facturas distintas
  (p. ej. `DIS_ARVAL … 2300616087` y `… 2400022256`) dan el mismo texto extraído. Al migrar había 161 grupos,
  194 copias sobrantes, 218 MB.
- La huella solo se calcula si otro archivo de la fuente tiene **el mismo tamaño** (~1.600 de 14.000), en
  hilos, en la fase «comparando» de `sync`. Una vez calculada se guarda; no se repite.
- Una sola copia por grupo, la **principal**, tiene fila en `documentos`; las demás solo en `archivos`
  (`copia_de` = id de la principal, sin texto ni documento) y **no se extraen**. Sus rutas van a
  `documentos.otras_rutas` (`refrescar_rutas`, al final de `sync`, `eliminar` y `resolver_duplicados`),
  así el documento se encuentra también por el nombre/carpeta de una copia (la fila lo indica: «Copia: …»).
- `promover`: antes de que desaparezca una principal (borrada o movida fuera, modificada, eliminada desde la
  app) su documento pasa a una copia sin volver a leer el PDF, y sus etiquetas se copian a la nueva ruta.
  `consolidar`: deja un documento por huella (migración y casos tardíos); prefiere la que tiene etiquetas y
  después la más antigua.
- **Nunca se borra automáticamente** (decisión del usuario: «registrar y avisar»). Botón «Duplicados (N)»;
  el usuario elige qué conservar en cada grupo. `indexador.resolver_duplicados` vuelve a calcular las dos
  huellas justo antes de enviar a la Papelera. `meta.duplicados_revisados` marca las copias «nuevas».
- Invariantes (comprobados en las pruebas): ninguna copia tiene documento; ninguna principal sin documento;
  sin cadenas (copia de una copia); la copia tiene la misma huella que su principal.

### Etiquetas (`finder/etiquetas.py`)
- Base de datos **aparte** y una por fuente (`etiquetas_<id>.db` en la raíz; `FINDERJR_ETIQUETAS_DIR` cambia
  la carpeta para pruebas) porque `data/` se regenera. `get_conn` la adjunta con `ATTACH ... AS et`.
- Tablas `et.etiquetas` (nombre único sin mayúsculas, color de `PALETA`, `tecla` 1-9 única opcional) y
  `et.doc_etiqueta` (`ruta_rel`, `etiqueta_id`). La clave es `ruta_rel` con `/` (`CLAVE_DOC` normaliza
  la del índice), así sobrevive a reindexar y a modificar el archivo; mover/renombrar la deja huérfana.
- Filtros de búsqueda: `et=1,2` (todas), `etx=3` (ninguna), `sin_et=1`; los aplica `filtros_sql` en las
  dos ramas de `run_query`, así que CSV y `pdf_bundle` los respetan.
- `doc_etiqueta.origen` = `manual` | `auto` (añadida con `ALTER` en `init_schema`). Quitar una etiqueta a mano
  la apunta en `et.rechazos` (la regla no la vuelve a poner); ponerla a mano la convierte en manual y
  borra el rechazo. `mover` traslada etiquetas y rechazos al renombrar. `copiar_etiquetas` (duplicados) solo
  copia las manuales.
- Pendiente opcional: fusionar etiquetas; reenganchar etiquetas huérfanas tras mover archivos (hash).

### Etiquetado automático (`finder/autoetiquetas.py`, noviembre 2026, a petición del usuario)
- Regla por fuente en `finder_config.json`: `fuentes[i].etiquetado_auto = {activo, etiqueta_factura,
  empresas: [{codigo, etiqueta}]}` (se edita en Preferencias; los códigos no están en el código).
- Patrón `EMP_PROVEEDOR - NÚMERO.ext` (código sin distinguir mayúsculas). El guion necesita un espacio al
  menos a un lado: así `DIS_MOD200-2022` o `EWA_CIRBE_02-2024` no cuentan como factura. Al activarla había
  ~7.870 facturas de ~13.840 documentos. La vista previa (`POST /api/autoetiquetas/probar`, solo local)
  lista también los «casi» (empiezan por un código y tienen guion, pero no cumplen) para renombrarlos.
- `aplicar` es idempotente y recorre toda la fuente (~0,15 s): al final de cada `sync`, tras `renombrar` y,
  al guardar la regla, en la sincronización que lanza `fuente_modificar`. Pone las que faltan (salvo
  rechazos) y quita las `auto` que ya no se cumplen (archivo renombrado, empresa quitada, regla
  desactivada). Nunca toca las manuales. Las etiquetas que crea no reciben tecla 1-9.
- Las etiquetas se identifican por **nombre**: si se renombra «TEX» en Gestionar etiquetas, la regla
  volverá a crear «TEX». Para cambiarle el nombre, hacerlo en Preferencias.
- UI: las automáticas se ven en la previsualización con ⚙ y rayado (`tag-pill.auto`, `get_doc` →
  `etiquetas_auto`).

### Word y Excel (`finder/extraccion.py`, `extract_text` elige por extensión)
- **docx**: cuerpo, encabezados, pies y notas (sin repetir bloques idénticos); un párrafo por línea y cada
  fila de tabla en una línea con las celdas separadas por « | ». `num_paginas` = `<Pages>` de
  `docProps/app.xml` (lo que guardó Word; 1 si no existe).
- **xlsx**: una hoja por «página» (`num_paginas` = hojas; la UI dice «N hojas»), cada una con la cabecera
  `[Hoja: nombre]` y una fila por línea con las celdas no vacías separadas por tabuladores. Se leen hasta
  `max_pages_text` hojas. Los números se escriben **en formato español** (`1224.5` → `1.224,50`) y las
  celdas con formato de fecha como `dd/mm/aaaa`, para que se encuentren igual que en un PDF
  (`expand_numeric_variants` ya cubre `1224,50` ⇄ `1.224,50`). Es el valor guardado, no el mostrado
  (no se aplican los formatos de número personalizados).
- Protegido con contraseña (no es ZIP) → estado ERROR con mensaje claro. Límite de 200 MB por parte del ZIP.
- `necesita_ocr`/`sin_texto` solo se marcan en PDF: un Word/Excel sin texto no es un escaneado.
- UI: en los no PDF solo existe la pestaña «Texto» (las otras se desactivan, y se recuerda la elegida para
  el siguiente PDF); `page_image` responde 400; «Abrir» llama a `POST /api/open_file` (`os.startfile`,
  aplicación predeterminada) en vez de abrir el archivo en el navegador. `pdf_bundle` ya omitía los no PDF.

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
- Las etiquetas se conservan en `etiquetas_<fuente>.db` (huérfanas): si se restaura, se recuperan.
- UI: botón papelera / Supr → modal con foco en «Cancelar»; tras borrar se selecciona el siguiente.

### Búsqueda (`app_finder.py`)
- Cuatro campos combinables: `todas` (AND), `alguna` (OR), `frase` (literal; varias entre comillas),
  `excluye` (NOT IN). Máx. 10 palabras por campo y 4 frases.
- Cada palabra se busca como prefijo (`"arrend"*`). `expand_numeric_variants`: `1224,00` ⇄ `1.224,00`.
  Los separadores se ignoran: `B-06976` genera `"B 06976"*` OR `"B06976"*`.
- Relevancia: `bm25(documentos_fts, 8.0, 4.0, 1.0, 4.0)` (nombre, ruta, texto, rutas de las copias), con
  más peso para el nombre de archivo y la ruta que para el texto. Otras ordenaciones: fecha (por defecto descendente) y nombre.
- Resaltado: `terms` se devuelve al cliente; el fragmento se localiza con `_fold` (sin acentos y con la
  misma longitud). La página PDF se renderiza a PNG con los términos resaltados (`/api/doc/<id>/page_image`).
- Filtro de fecha `fecha=AAAA-MM-DD` (día) o `AAAA-MM` (mes) sobre `d.mtime` en hora local
  (`filtros_sql`, junto a los de etiquetas: CSV y `pdf_bundle` lo respetan). Lo pone el **calendario**
  del panel de búsqueda (noviembre 2026, a petición del usuario): `GET /api/calendario?mes=AAAA-MM` + los
  parámetros de la búsqueda devuelve los documentos por día que cumplen la búsqueda **sin** el filtro de
  fecha, y el primer y último mes con resultados. Clic en un día o en el mes filtra; otro clic lo quita.
- Referencia de rendimiento: < 0,1 s típico; el peor caso es ~1,2 s (prefijo corto con muchos aciertos
  ordenado por fecha).

### API
Por el listener remoto solo se admiten los GET y los POST de `remoto.RUTAS_POST_REMOTAS`.
GET `/api/info`, `/api/search`, `/api/status`, `/api/calendario`, `/api/config`, `/api/doc/<id>[/pdf|/page_image]`,
`/api/export/csv`, `/api/export/pdf_bundle` (une hasta 300 PDF filtrados), `/api/etiquetas` (con recuentos),
`/api/fuentes`, `/api/duplicados`, `/api/archivo/<id>/pdf` (también copias).
POST `/api/heartbeat`, `/api/sync`, `/api/open_location` (abre el Explorador en el archivo), `/api/open_file` (lo abre con su programa),
`/api/layout` (guarda anchos de paneles en `finder_config.json`), `/api/config`,
`/api/etiquetas` (crear), `/api/etiquetas/<id>` (nombre/color/tecla), `/api/etiquetas/<id>/borrar`,
`/api/etiquetar` (`etiqueta_id`, `accion` poner|quitar, `doc_ids` o `busqueda`), `/api/doc/<id>/renombrar`, `/api/doc/<id>/eliminar {copias}`, `/api/doc/<id>/ocr`,
`/api/duplicados/resolver {grupos: [{conservar, eliminar}]}`, `/api/duplicados/visto`, `/api/autoetiquetas/probar {fuente_id, regla}`,
`/api/fuente/activar {id}`, `/api/fuentes` (crear), `/api/fuentes/<id>` (modificar), `/api/fuentes/<id>/borrar`.

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
  a ser posible sobre una copia: parchear `config.CONFIG_PATH`, `config.DATA_DIR` y `config.ETIQUETAS_DIR`
  (o `FINDERJR_DATA_DIR`/`FINDERJR_ETIQUETAS_DIR`). Con el Python de Windows (PyMuPDF real) el script de
  prueba **necesita** `if __name__ == "__main__":`: los procesos de extracción (spawn) lo reimportan.
- Capturas de pantalla: el MCP de puppeteer y el Chromium de Playwright en WSL no funcionan (faltan
  bibliotecas). Sí funciona el Chrome de Windows en headless con `--remote-debugging-port=9333`
  (WSL en red espejo ve `127.0.0.1`) + `playwright-core` (`chromium.connectOverCDP`).
- Pruebas con el Python de Windows: el índice de prueba debe estar en una carpeta de Windows (p. ej.
  `%TEMP%`), **no** en el scratchpad de WSL (`\\wsl.localhost\...`): SQLite se bloquea en ese recurso de red.
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
  y `etiquetas_*.db` (trabajo manual del usuario: **no** se regenera, debe ir en las copias de seguridad).

## Proyectos relacionados (misma carpeta `Documents\Python`)

- **cBank**: modelo de arquitectura e interfaz.
- **CVS-Web**: modelo del `menu.bat`/scripts de git; desplegado en SERVER2022 (no aplica a FinderJR).
- **dbGes26**: ya no se usa (ver la decisión 1).

## Pendiente (por prioridad)

1. **Formatos Office antiguos** (.doc/.xls, ~200 archivos): binarios OLE, no se pueden leer con la stdlib
   de forma fiable. Opciones: convertirlos con Office/LibreOffice o una dependencia nueva (consultarlo).
