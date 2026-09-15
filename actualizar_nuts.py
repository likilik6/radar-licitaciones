# Genera data/nuts_nombres.json: el vocabulario NUTS de la Plataforma, para poder
# traducir el código territorial del feed a una CCAA y a un lugar de ejecución.
#
# POR QUÉ ESTE FICHERO: el CODICE trae el territorio SOLO como código NUTS
# (<cbc:CountrySubentityCode>), y el texto libre de al lado (<cbc:CountrySubentity>)
# no sirve: en la sindicación 1044 (agregadas, 157.752 filas del catálogo) NO VIENE
# NUNCA —0% medido el 15/09/2026—, y donde viene está sucio: el mismo ES523 llega como
# «VALENCIA», «Valencia», «Valencia/València» y «Valencia / València», y hay filas con
# el municipio en vez de la provincia. Así que el nombre lo ponemos nosotros desde la
# lista oficial, no lo copiamos del feed.
#
# La dirección de la lista no es inventada: viene en el atributo listURI de cada
# <cbc:CountrySubentityCode> del propio feed («...codice/cl/2.08/NUTS-2021.gc»).
#
# Mismo patrón, mismo formato y mismo uso que actualizar_cpv.py (que genera
# data/cpv_nombres.json). La lista NUTS es ESTABLE —los códigos de España en NUTS-2016
# y NUTS-2021 son idénticos, solo cambian algunos nombres—, así que este script NO va
# en la automatización diaria: se ejecuta a mano y solo si sale una versión nueva:
#   .\.venv\Scripts\python.exe actualizar_nuts.py
import json
import sys
from pathlib import Path

import requests
from lxml import etree

# Reutilizamos la cabecera de navegador del proyecto (el servidor la exige).
from feeds import CABECERAS

sys.stdout.reconfigure(encoding="utf-8")

URL_NUTS = "http://contrataciondelestado.es/codice/cl/2.08/NUTS-2021.gc"


def descarga_nombres():
    """Descarga el .gc y devuelve {codigo: nombre_es}. Mismo formato genericode que
    el de los CPV: un <Row> por código, con columnas 'code', 'nombre' (es) y 'name'."""
    respuesta = requests.get(URL_NUTS, headers=CABECERAS, timeout=120)
    respuesta.raise_for_status()
    raiz = etree.fromstring(respuesta.content)

    mapa = {}
    for fila in raiz.findall(".//{*}Row"):
        valores = {}
        for valor in fila.findall("{*}Value"):
            columna = valor.get("ColumnRef")
            simple = valor.find("{*}SimpleValue")
            valores[columna] = (simple.text or "").strip() if simple is not None else ""
        codigo = valores.get("code", "")
        nombre = valores.get("nombre", "") or valores.get("name", "")
        if codigo and nombre:
            mapa[codigo] = nombre
    return mapa


def main():
    mapa = descarga_nombres()
    if not mapa:
        sys.exit("ERROR: no se han extraído códigos NUTS; ¿cambió el formato del .gc?")

    espanoles = {c: n for c, n in mapa.items() if c.startswith("ES")}
    if len(espanoles) < 80:
        sys.exit(f"ERROR: solo {len(espanoles)} códigos ES; se esperaban ~89. "
                 "Revisa la lista antes de usarla: poblar 624k filas con esto a medias "
                 "es peor que no poblarlas.")

    ruta = Path("data") / "nuts_nombres.json"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(mapa, f, ensure_ascii=False, sort_keys=True, indent=1)

    porniveles = {}
    for c in espanoles:
        porniveles[len(c)] = porniveles.get(len(c), 0) + 1
    print(f"OK: {len(mapa)} códigos NUTS guardados en {ruta} "
          f"({len(espanoles)} de España).")
    print(f"   por longitud de código: {dict(sorted(porniveles.items()))} "
          "(2=país, 3=NUTS1, 4=NUTS2/CCAA, 5=NUTS3/provincia)")


if __name__ == "__main__":
    main()
