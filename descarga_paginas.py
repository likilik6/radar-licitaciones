# -*- coding: utf-8 -*-
"""descarga_paginas.py — baja los feeds UNA vez por ejecución y guarda las páginas en bruto (26/11/2026).

Por qué: hasta el 26/11 cada ejecución de radar.yml bajaba los feeds dos veces, una en el job «generar»
(filtrar.py) y otra en el job «catalogo» (backfill_catalogo.py --diario): unas 100 peticiones al día a la
Plataforma. Ahora las baja el job «descargar» con este script, y los otros dos leen las páginas guardadas
(RADAR_PAGINAS, ver feeds.descarga_entradas): la mitad de peticiones.

Qué guarda, en el directorio de --salida:
  manifiesto.json                          qué se bajó, cuándo, y qué pasó si algo falló
  <fuente>/<NNN>_<nombre de la página>     cada página tal cual llegó (NNN = orden en el feed; 000 = cabecera)

El job lo sube como artefacto «paginas-feed» (14 días). Radar Farma lo usa para su base sin pedir nada más a la
Plataforma: por eso se guardan los bytes exactos y su SHA-256.

Un solo intento por página y sin reintentos: si el servidor rechaza o corta, ese feed se para, se anota en el
manifiesto y se sigue con el siguiente (los jobs de después verán lo que haya). El script termina bien (código 0)
aunque un feed falle, para que el Radar publique igual con lo que tenga; solo falla si no puede escribir.

Uso:
    python descarga_paginas.py --salida paginas
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from feeds import FEEDS, MANIFIESTO, MAX_PAGINAS, baja_paginas, nombre_pagina
from backfill_catalogo import MENORES_ATOM

# Los feeds que se guardan: los dos del Radar y el de menores (1143) del catálogo.
FEEDS_A_GUARDAR = FEEDS + [{"fuente": "menores", "url": MENORES_ATOM}]


def _ahora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def guarda_feed(salida, fuente, url, max_paginas=MAX_PAGINAS):
    """Baja un feed y deja sus páginas en salida/<fuente>/. Devuelve su entrada del manifiesto."""
    inicio = _ahora()
    paginas, tope, fallo = baja_paginas(url, max_paginas=max_paginas)
    carpeta = Path(salida) / fuente
    carpeta.mkdir(parents=True, exist_ok=True)
    filas = []
    for n, (url_pag, datos, _raiz) in enumerate(paginas):
        fichero = f"{fuente}/{n:03d}_{nombre_pagina(url_pag)}"
        (Path(salida) / fichero).write_bytes(datos)
        filas.append({"n": n, "url": url_pag, "nombre": nombre_pagina(url_pag), "fichero": fichero,
                      "bytes": len(datos), "sha256": hashlib.sha256(datos).hexdigest()})
    return {"fuente": fuente, "url": url, "inicio": inicio, "fin": _ahora(), "paginas": filas,
            "tope": bool(tope), "fallo": fallo}


def main(argv=None, feeds=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--salida", required=True, help="directorio donde se guardan las páginas")
    ap.add_argument("--max-paginas", type=int, default=MAX_PAGINAS)
    a = ap.parse_args(argv)
    salida = Path(a.salida)
    salida.mkdir(parents=True, exist_ok=True)
    manifiesto = {"version": 1, "inicio": _ahora(), "ejecucion": os.environ.get("GITHUB_RUN_ID"),
                  "intento": os.environ.get("GITHUB_RUN_ATTEMPT"), "feeds": {}}
    for feed in (feeds if feeds is not None else FEEDS_A_GUARDAR):
        d = guarda_feed(salida, feed["fuente"], feed["url"], max_paginas=a.max_paginas)
        manifiesto["feeds"][feed["url"]] = d
        que = (f"parado en la página {d['fallo']['pagina']}: {d['fallo']['tipo']}"
               + (f" (support ID {d['fallo']['support_id']})" if d["fallo"].get("support_id") else "")
               if d["fallo"] else ("tope de páginas" if d["tope"] else "completo"))
        print(f"Feed «{feed['fuente']}»: {len(d['paginas'])} página(s) guardadas; {que}.")
    manifiesto["fin"] = _ahora()
    tmp = salida / (MANIFIESTO + ".tmp")
    tmp.write_text(json.dumps(manifiesto, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(salida / MANIFIESTO)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
