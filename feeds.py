# feeds.py — fuentes de datos del radar y "extractor" común (descarga + paginación).
#
# Aquí vive lo que comparten filtrar.py y fetch.py para NO duplicarlo:
#   - la LISTA de feeds ATOM/CODICE que procesa el radar (cada uno con su "fuente"),
#   - la cabecera de navegador (sin ella el WAF del servidor bloquea la petición),
#   - la función que descarga un feed siguiendo la paginación (atom:link rel="next").
#
# El MISMO extractor sirve para todos los feeds porque todos vienen en el mismo
# formato (ATOM con el bloque CODICE donde está el CPV). Añadir un feed nuevo es
# solo añadir una entrada a la lista FEEDS de abajo.
import time
import requests
from lxml import etree

# Espacio de nombres mínimo para navegar el ATOM: localizar las entradas
# (<atom:entry>) y el enlace a la página siguiente (<atom:link rel="next">).
# El resto de namespaces (CPV, presupuesto...) los usa filtrar.py al extraer.
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# El servidor rechaza las peticiones que no parezcan de un navegador, así que
# enviamos una cabecera "User-Agent" que imita a un navegador real. OBLIGATORIO.
CABECERAS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

# La LISTA de feeds que vigila el radar. Cada uno lleva su etiqueta de ORIGEN
# ("fuente") para poder distinguir de dónde salió cada licitación:
#   - "estatal":   sindicación 643 (Plataforma de Contratación del Sector Público).
#   - "agregadas": sindicación 1044 (CCAA y entidades locales con plataforma propia
#                  que se "enganchan" a la estatal; mismo formato ATOM/CODICE).
FEEDS = [
    {
        "fuente": "estatal",
        "url": "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom",
    },
    {
        "fuente": "agregadas",
        "url": "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_1044/PlataformasAgregadasSinMenores.atom",
    },
]

# Tope de páginas por feed en cada ejecución.
# Los feeds paginan con atom:link rel="next", pero NO son "500 fijos": cada "next"
# apunta a un snapshot anterior en el tiempo, así que la cadena se remonta hacia
# atrás muchos días (cientos de páginas si se agotara). Como la automatización
# corre de lunes a viernes, el lunes debe cubrir el hueco del fin de semana; con
# ~20 páginas (≈10.000 entradas por feed) hay margen de sobra sin que el job se
# dispare. Si se alcanza el tope, descarga_entradas avisa (no se trunca en silencio).
MAX_PAGINAS = 20

# Pausa (segundos) entre página y página, por cortesía con el servidor.
PAUSA_ENTRE_PAGINAS = 0.5


def _siguiente_pagina(raiz):
    """Devuelve la URL de la página siguiente (atom:link rel="next") o None si no hay."""
    for link in raiz.findall("atom:link", ATOM_NS):
        if link.get("rel") == "next":
            return link.get("href")
    return None


def descarga_entradas(url, max_paginas=MAX_PAGINAS, pausa=PAUSA_ENTRE_PAGINAS):
    """Descarga un feed ATOM siguiendo la paginación rel="next" hasta agotarla o
    hasta llegar a 'max_paginas'. Mantiene la cabecera de navegador en cada petición.

    Devuelve una tupla (entradas, paginas_leidas, tope_alcanzado):
      - entradas:        lista de elementos <atom:entry> (lxml) de TODAS las páginas.
      - paginas_leidas:  cuántas páginas se llegaron a descargar.
      - tope_alcanzado:  True si se paró por el tope (quedaba 'next' sin leer),
                         para poder avisar de que puede faltar histórico.

    Si una página falla (error de red o respuesta no válida), se avisa y se deja
    de paginar ESE feed con lo que se llevara descargado, para no tumbar todo el
    trabajo por un fallo puntual."""
    entradas = []
    paginas_leidas = 0
    while url and paginas_leidas < max_paginas:
        try:
            respuesta = requests.get(url, headers=CABECERAS, timeout=60)
            respuesta.raise_for_status()
            raiz = etree.fromstring(respuesta.content)
        except Exception as error:
            # Fallo puntual (red, 5xx, XML corrupto...): avisamos y paramos este feed.
            print(f"  AVISO: no se pudo leer una página ({error}); se deja de paginar este feed.")
            return entradas, paginas_leidas, False

        entradas.extend(raiz.findall("atom:entry", ATOM_NS))
        paginas_leidas += 1
        url = _siguiente_pagina(raiz)

        # Pausa solo si vamos a pedir otra página.
        if url and paginas_leidas < max_paginas:
            time.sleep(pausa)

    # Si todavía queda un 'next' es que paramos por el tope, no porque se agotara.
    tope_alcanzado = bool(url)
    return entradas, paginas_leidas, tope_alcanzado
