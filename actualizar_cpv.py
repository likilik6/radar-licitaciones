# Genera data/cpv_nombres.json: un diccionario «código CPV -> nombre en español».
#
# Lo usa generar_web.py para poner NOMBRE a cada código CPV (en el desplegable de
# filtro y en el tooltip de las tarjetas). El feed solo trae el CÓDIGO del CPV; el
# nombre vive en la lista oficial de CPV de la Plataforma, en formato "genericode"
# (.gc), cuya dirección viene en el atributo listURI de cada CPV del feed.
#
# La lista CPV (CPV2008) es ESTABLE, así que este script NO va en la automatización
# diaria: se ejecuta a mano y solo cuando haga falta (p.ej. si sale una versión nueva):
#   .\.venv\Scripts\python.exe actualizar_cpv.py
import sys
import json
import requests
from pathlib import Path
from lxml import etree

# Reutilizamos la cabecera de navegador del proyecto (el servidor la exige).
from feeds import CABECERAS

sys.stdout.reconfigure(encoding="utf-8")

# Lista oficial de CPV 2008 (formato genericode) de la Plataforma de Contratación.
# Es la misma que referencia el feed en el atributo listURI de cada <ItemClassificationCode>.
URL_CPV = "http://contrataciondelestado.es/codice/cl/2.04/CPV2008-2.04.gc"


def descarga_nombres():
    """Descarga el .gc y devuelve un dict {codigo: nombre_es}.
    El genericode es un XML con <Row> por cada CPV; dentro, cada <Value ColumnRef="...">
    lleva una columna: 'code' (el código), 'nombre' (texto en español) y 'name' (inglés)."""
    respuesta = requests.get(URL_CPV, headers=CABECERAS, timeout=120)
    respuesta.raise_for_status()
    raiz = etree.fromstring(respuesta.content)

    mapa = {}
    # Usamos comodín de namespace ({*}) para no depender del prefijo exacto del genericode.
    for fila in raiz.findall(".//{*}Row"):
        valores = {}
        for valor in fila.findall("{*}Value"):
            columna = valor.get("ColumnRef")
            simple = valor.find("{*}SimpleValue")
            valores[columna] = (simple.text or "").strip() if simple is not None else ""
        codigo = valores.get("code", "")
        nombre = valores.get("nombre", "")
        # Solo guardamos filas con código Y nombre (descartamos cabeceras/filas vacías).
        if codigo and nombre:
            mapa[codigo] = nombre
    return mapa


def main():
    mapa = descarga_nombres()
    if not mapa:
        sys.exit("ERROR: no se han extraído nombres de CPV; ¿cambió el formato del .gc?")

    ruta = Path("data") / "cpv_nombres.json"
    ruta.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys deja el archivo ordenado por código (diff de Git estable);
    # ensure_ascii=False conserva tildes y "ñ"; indent=1 lo deja legible.
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(mapa, f, ensure_ascii=False, sort_keys=True, indent=1)

    print(f"OK: {len(mapa)} nombres de CPV guardados en {ruta}.")


if __name__ == "__main__":
    main()
