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
- **Solo indexar**: `python -m finder.indexador` (`--full` reindexa todo desde cero).
- **Prueba de humo de la API**: `python tools/probar_api.py`.

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
- Se guardan en `etiquetas.db` (carpeta del programa, fuera de git): **incluirla en las copias de seguridad**.
  Se vinculan por la ruta del archivo dentro de la Biblioteca: si se mueve o renombra, pierde sus etiquetas.
- El Excel exportado incluye la columna «Etiquetas» y «Unir resultados en 1 PDF» respeta los filtros.

## Arquitectura

```
app_finder.py          Servidor HTTP + API REST (ThreadingHTTPServer, sin frameworks)
finder/config.py       finder_config.json y rutas Windows/WSL
finder/db.py           Índice SQLite + FTS5 (data/finder_index.db)
finder/etiquetas.py    Etiquetas manuales (etiquetas.db)
finder/extraccion.py   Extracción de texto con PyMuPDF
finder/indexador.py    Sincronización incremental en paralelo + proceso en segundo plano
static/                Interfaz (estilo de cBank)
tools/probar_api.py    Prueba de humo
```

## Pendiente

- **OCR** de los ~1.100 PDF escaneados (Tesseract en español) para que sean buscables por contenido.
- Formatos Office (docx/xlsx).
