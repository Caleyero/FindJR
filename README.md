# FinderJR — Buscador de texto completo de la Biblioteca

Aplicación web local (`http://localhost:8766`) que indexa en segundo plano la carpeta **Biblioteca**
y permite buscar en el **texto**, el **nombre de archivo** y la **carpeta** de cada documento.
Interfaz replicada de cBank (Búsqueda | Resultados | Previsualización con resaltado).

Búsqueda bruta, sin clasificaciones ni interpretación del contenido: lo que aparece en los
resultados contiene literalmente lo buscado.

## Arranque

- **Windows (doble clic)**: `iniciar_finderjr.bat` (sin consola). Abre el navegador y, mientras haya
  una pestaña abierta, sincroniza la Biblioteca cada `sync_interval_min` minutos (10 por defecto).
- **Favorito en el navegador (recomendado)**: `menu.bat` → opción 10, o `arranque_automatico.ps1 -Accion Instalar`. Crea un acceso directo en la carpeta Inicio de Windows que arranca FinderJR en segundo plano al iniciar sesión (sin navegador y sin apagarse al cerrar la pestaña). Desde entonces basta con el favorito `http://localhost:8766/`. Tras actualizar el código: opción 13 (reiniciar). `-Accion Quitar|Iniciar|Detener|Estado` para el resto.
- **Terminal**: `python app_finder.py` (`--no-browser`, `--no-auto-shutdown`).
- **Solo indexar**: `python -m finder.indexador` (fuente activa; `--fuente <id>` otra; `--full` desde cero).
  Detener antes la instancia en segundo plano.
- **Prueba de humo de la API**: `python tools/probar_api.py`.

## Fuentes de documentos (Personal / Profesional…)

Selector en la barra superior (o **Alt+1**, **Alt+2**…) para alternar entre fuentes. Cada fuente tiene su
propia carpeta, su índice y su catálogo de etiquetas: **los resultados nunca se mezclan**. El color de la
barra superior indica en qué fuente se está trabajando. Al cambiar se conservan las palabras buscadas y se
descartan los filtros de etiqueta y los documentos marcados.

- Se crean, renombran y quitan en **Preferencias** (carpeta, subcarpetas y carpetas excluidas de cada una).
- Todas se sincronizan en segundo plano; quitar una borra su índice y conserva sus etiquetas.
- Si otra pestaña cambia de fuente, esta se pone al día sola y nunca modifica la fuente equivocada.

## Búsqueda

| Campo | Efecto |
|---|---|
| Contiene todas estas palabras | Deben aparecer todas |
| Contiene alguna de estas palabras | Al menos una |
| Contiene esta frase exacta | Literal y en ese orden (varias frases entre comillas) |
| No contiene ninguna de estas palabras | Descarta el documento si aparece cualquiera |

- Sin distinguir mayúsculas ni acentos; las palabras se buscan por su comienzo (`arrend` → arrendamiento).
- Cifras con o sin punto de miles (`1224,00` = `1.224,00`) y separadores indiferentes (`B-06976` = `B06976`).
- Orden: relevancia (BM25, con más peso al nombre de archivo y a la carpeta) o fecha de modificación del archivo.
- Los escaneados sin capa de texto (marcados **OCR**) solo se encuentran por nombre y carpeta.

## Etiquetas

Etiquetas manuales con color: las pone siempre el usuario, nunca el programa.

- **Filtrar**: en el panel de Búsqueda, clic en una etiqueta muestra solo los documentos que la llevan
  (varias = todas a la vez); **Ctrl+clic** oculta los que la llevan. «Sin etiquetar» ayuda a ir clasificando.
- **Poner/quitar**: «+ Etiqueta» en la previsualización, teclas **1-9** (documento actual o los marcados)
  o el botón **Etiquetar** sobre los documentos marcados (casillas; Mayús+clic marca un rango, Esc desmarca)
  o sobre todos los resultados de la búsqueda.
- **Gestionar**: renombrar, color, tecla rápida y borrar.
- Se guardan en `etiquetas_<fuente>.db` (carpeta del programa, fuera de git), una por fuente:
  **incluirlas en las copias de seguridad**.
  Se vinculan por la ruta del archivo dentro de la Biblioteca: si se mueve o renombra, pierde sus etiquetas.
- El Excel exportado incluye la columna «Etiquetas» y «Unir resultados en 1 PDF» respeta los filtros.

## Renombrar documentos

Doble clic en el nombre (en Resultados o en la previsualización), **F2** o el botón del lápiz.
Intro guarda, Esc cancela; la extensión no se toca. El archivo se renombra **en disco** y el índice y
las etiquetas se actualizan al momento (sin releer el PDF). Tras renombrar aparece «Deshacer».
Si el archivo está abierto en otro programa, Windows no deja renombrarlo: hay que cerrarlo antes.

## Eliminar documentos

Botón de la papelera en la previsualización o tecla **Supr**, con confirmación (Cancelar es la opción
por defecto). El archivo va a la **Papelera de reciclaje** de Windows y sale del índice. Si se restaura
desde la Papelera, la siguiente sincronización lo vuelve a indexar con sus etiquetas. En unidades de red
(sin Papelera) no se permite.

Si el documento tiene copias idénticas, el modal ofrece eliminarlas también; si no se marca, el documento
sigue en FinderJR (con sus etiquetas) en la ruta de una de sus copias.

## Duplicados

Los archivos **idénticos byte a byte** (misma huella SHA-256) se indexan una sola vez: en Resultados sale
un único documento con la marca **+N** (sus copias). También se encuentra buscando por el nombre o la
carpeta de cualquiera de sus copias. Solo se compara el contenido exacto: dos facturas distintas con el
mismo texto **no** son duplicados.

- Cuando llega un archivo idéntico a otro ya indexado, no se lee su texto: se registra como copia y
  aparece el botón **Duplicados (N)** en la barra superior. FinderJR nunca borra nada por su cuenta.
- En el panel **Duplicados** se marca en cada grupo el archivo que se quiere conservar (por defecto, el
  que tiene etiquetas o el más antiguo) y los demás van a la **Papelera de reciclaje**, grupo a grupo o
  todos a la vez. Justo antes de borrar se vuelve a comprobar que siguen siendo idénticos. Las etiquetas
  de lo eliminado pasan a lo que se conserva.

## Arquitectura

```
app_finder.py          Servidor HTTP + API REST (ThreadingHTTPServer, sin frameworks)
finder/config.py       finder_config.json (fuentes) y rutas Windows/WSL
finder/db.py           Índice SQLite + FTS5, uno por fuente (data/finder_index_<fuente>.db)
finder/etiquetas.py    Etiquetas manuales (etiquetas_<fuente>.db)
finder/duplicados.py   Duplicados exactos (SHA-256): copias registradas sin indexar su texto
finder/extraccion.py   Extracción de texto con PyMuPDF
finder/indexador.py    Sincronización incremental en paralelo + proceso en segundo plano
static/                Interfaz (estilo de cBank)
tools/probar_api.py    Prueba de humo
```

## Pendiente

- **OCR** de los ~1.100 PDF escaneados (Tesseract en español) para que sean buscables por contenido.
- Formatos Office (docx/xlsx).
