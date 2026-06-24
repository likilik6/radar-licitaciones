# fetch.py — script de EXPLORACIÓN para mirar rápido qué trae cada feed.
#
# OJO: esto NO es la automatización. El pipeline real es filtrar.py (descarga,
# clasifica y guarda) + generar_web.py (construye la web). Este script solo sirve
# para echar un vistazo a mano: descarga la LISTA de feeds con el mismo extractor
# (feeds.py) y muestra un resumen por fuente y la primera entrada de cada uno.
import sys

# La lista de feeds, el extractor (descarga + paginación) y el namespace ATOM
# viven en feeds.py, compartidos con filtrar.py para no duplicar la descarga.
from feeds import FEEDS, descarga_entradas, ATOM_NS

# Hacemos que la consola muestre los acentos y la "ñ" correctamente.
sys.stdout.reconfigure(encoding="utf-8")

# Recorremos los feeds (estatal + agregadas) y mostramos un resumen de cada uno.
for feed in FEEDS:
    entradas, paginas, tope = descarga_entradas(feed["url"])

    print("=" * 70)
    print(f"Feed «{feed['fuente']}»  —  {feed['url']}")
    aviso_tope = "  [TOPE de páginas alcanzado]" if tope else ""
    print(f"Entradas: {len(entradas)} en {paginas} página(s){aviso_tope}")

    # Mostramos algunos datos de la PRIMERA entrada, a modo de muestra.
    if entradas:
        primera = entradas[0]
        titulo = primera.findtext("atom:title", default="(sin título)", namespaces=ATOM_NS).strip()
        link = primera.find("atom:link", ATOM_NS)
        enlace = link.get("href") if link is not None else "(sin enlace)"
        actualizado = primera.findtext("atom:updated", default="", namespaces=ATOM_NS).strip()
        print("Primera entrada:")
        print("  Título:", titulo)
        print("  Enlace:", enlace)
        print("  Actualización:", actualizado)
