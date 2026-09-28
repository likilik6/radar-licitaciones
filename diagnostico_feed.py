#!/usr/bin/env python3
"""Huella del feed de la PCSP, para comparar QUÉ RECIBE CADA SITIO.

Por qué existe: si GitHub Actions recibe un contenido distinto —o más viejo— que un PC
normal, entonces el problema es DESDE DÓNDE SALE LA PETICIÓN, y paginar hacia atrás hasta
enlazar con lo que ya tenemos no arregla nada (estaríamos remontando una copia vieja).
Con la Junta de Andalucía pasó algo así de claro: bloqueo por IP. Aquí hay que comprobarlo
antes de programar ningún arreglo.

Qué hace: pide la PRIMERA página de cada feed (y, si se pide, alguna más siguiendo
rel="next") y saca una huella comparable:
  · qué contesta el servidor y qué dicen sus cabeceras de caché (Date, Age, X-Cache, Via…),
  · la marca de tiempo del propio feed (<updated>),
  · cuántas entradas trae, de qué fechas, y el identificador de las primeras,
  · un hash del contenido, para ver de un vistazo si son el MISMO fichero o no.

No escribe nada y no toca el Radar. Se ejecuta igual aquí que en Actions:
    python diagnostico_feed.py                 # primera página de cada feed
    python diagnostico_feed.py --paginas 3     # sigue 3 páginas
    python diagnostico_feed.py --json huella.json
"""
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone

import requests
from lxml import etree

from feeds import ATOM_NS, CABECERAS, FEEDS

# Cabeceras que delatan una caché intermedia sirviendo una copia distinta por origen.
CABECERAS_CACHE = ("date", "last-modified", "etag", "age", "x-cache", "x-cache-hits",
                   "via", "cf-cache-status", "server", "content-length")


def _texto(elemento, camino):
    hijo = elemento.find(camino, ATOM_NS)
    return (hijo.text or "").strip() if hijo is not None and hijo.text else ""


def _siguiente(raiz):
    for enlace in raiz.findall("atom:link", ATOM_NS):
        if enlace.get("rel") == "next":
            return enlace.get("href")
    return None


def huella_pagina(url, numero):
    """Huella de UNA página del feed. Devuelve (dict, url_siguiente)."""
    t0 = time.time()
    try:
        r = requests.get(url, headers=CABECERAS, timeout=60)
    except requests.RequestException as e:
        return {"pagina": numero, "url": url, "error": type(e).__name__,
                "segundos": round(time.time() - t0, 1)}, None
    salida = {
        "pagina": numero,
        "url": url,
        "http": r.status_code,
        "segundos": round(time.time() - t0, 1),
        "bytes": len(r.content),
        "sha256": hashlib.sha256(r.content).hexdigest()[:16],
        "cabeceras": {k: v for k, v in ((c, r.headers.get(c)) for c in CABECERAS_CACHE) if v},
    }
    if r.status_code != 200:
        return salida, None
    try:
        raiz = etree.fromstring(r.content)
    except etree.XMLSyntaxError as e:
        salida["error"] = f"XML inválido: {str(e)[:120]}"
        return salida, None

    entradas = raiz.findall("atom:entry", ATOM_NS)
    fechas = sorted(f for f in (_texto(e, "atom:updated") for e in entradas) if f)
    salida.update({
        "feed_updated": _texto(raiz, "atom:updated"),
        "entradas": len(entradas),
        "primera_fecha": fechas[0] if fechas else None,
        "ultima_fecha": fechas[-1] if fechas else None,
        # Los tres identificadores más recientes: si dos sitios ven feeds distintos, aquí
        # se nota enseguida.
        "ids_recientes": [_texto(e, "atom:id") for e in entradas[:3]],
    })
    return salida, _siguiente(raiz)


def main():
    ap = argparse.ArgumentParser(description="Huella del feed de la PCSP (para comparar orígenes).")
    ap.add_argument("--paginas", type=int, default=1, help="Cuántas páginas seguir por feed (por defecto 1).")
    ap.add_argument("--json", default=None, help="Guarda la huella completa en este fichero.")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    informe = {"cuando": datetime.now(timezone.utc).isoformat(timespec="seconds"), "feeds": []}
    print("=" * 78)
    print(f"HUELLA DEL FEED · {informe['cuando']}")
    print("=" * 78)

    for feed in FEEDS:
        bloque = {"fuente": feed["fuente"], "paginas": []}
        url, numero = feed["url"], 1
        while url and numero <= args.paginas:
            pagina, siguiente = huella_pagina(url, numero)
            bloque["paginas"].append(pagina)
            if "error" in pagina:
                print(f"  [{feed['fuente']}] página {numero}: {pagina['error']} ({pagina['segundos']} s)")
                break
            print(f"  [{feed['fuente']}] página {numero}: HTTP {pagina['http']} · "
                  f"{pagina['entradas']} entradas · feed {pagina.get('feed_updated', '?')} · "
                  f"de {str(pagina.get('primera_fecha'))[:19]} a {str(pagina.get('ultima_fecha'))[:19]} · "
                  f"{pagina['bytes']/1e6:.2f} MB · sha {pagina['sha256']}")
            for clave, valor in pagina["cabeceras"].items():
                print(f"        {clave}: {valor}")
            for identificador in pagina.get("ids_recientes", []):
                print(f"        id: {identificador[-70:]}")
            url, numero = siguiente, numero + 1
            if url and numero <= args.paginas:
                time.sleep(0.5)
        informe["feeds"].append(bloque)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(informe, f, ensure_ascii=False, indent=1)
        print(f"\nHuella completa en {args.json}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
