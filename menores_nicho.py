"""menores_nicho.py — el NICHO de la vista Menores, construido en un solo sitio.

Lo usan generar_web.py (lo inyecta en la web) y medir_menores.py (mide la MISMA
consulta que hace la web). Si cada uno lo montara por su cuenta, podrían separarse sin
que nadie lo viera.

El nicho sale de intereses.yaml (grupos «criticas» y «a_revisar»: CPV y palabras) y de
data/menores_nicho_exclusiones.json. La lista de exclusión NO va en intereses.yaml:
filtrar.py (el Radar) trata cada clave de primer nivel de ese fichero como una categoría.

POR QUÉ HAY EXCLUSIONES (medido el 17/09/2026, tras cargar Andalucía): de los 551 menores
andaluces que entraban en «Solo mi nicho», 514 eran falsos positivos. 389 eran material
de ventilación CLÍNICA del Servicio Andaluz de Salud, que entraba por la palabra suelta
«ventilación»; los demás, formol de laboratorio, cámaras de ionización, purificadores de
agua o material de infusión. Con los grupos activos quedan 111, sin perder ninguno de los
29 menores reales del nicho, y en la parte estatal se quitan 143, todos falsos positivos
revisados a mano. No se sustituye «ventilación» por frases: se perderían contratos como
«limpieza de conductos de ventilación».

SEMÁNTICA ACOTADA: un grupo solo quita filas que entran al nicho por una de sus palabras
(«aplica_a»). Lo que entra por CPV o por otra palabra no se toca. Aplicar las exclusiones
a todo el texto quitaba 20 filas más, alguna de nicho real («Inspección con cámara en los
conductos de ventilación», que caía por «filtraciones de agua»).
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
EXCLUSIONES_JSON = RAIZ / "data" / "menores_nicho_exclusiones.json"


def sin_tildes(texto) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", str(texto).lower())
                   if unicodedata.category(c) != "Mn")


def nicho_de_criterios(criterios: dict | None) -> tuple[list[str], list[str]]:
    """(prefijos CPV, palabras sin tildes) de los grupos criticas + a_revisar, sin repetir.
    Nunca «pruebas»."""
    cpv, palabras = [], []
    for grupo in ("criticas", "a_revisar"):
        crit = (criterios or {}).get(grupo) or {}
        for c in crit.get("cpv") or []:
            s = str(c).strip()
            if s and s not in cpv:
                cpv.append(s)
        for p in crit.get("palabras_clave") or []:
            s = sin_tildes(p).strip()
            if s and s not in palabras:
                palabras.append(s)
    return cpv, palabras


def lee_grupos_activos(ruta: Path = EXCLUSIONES_JSON) -> list[dict]:
    """Grupos con activo = true. Si el fichero falta o está mal, lista vacía: la web se
    queda con el nicho sin exclusiones (el de antes), no se cae la publicación."""
    datos = json.loads(Path(ruta).read_text(encoding="utf-8-sig"))
    return [g for g in (datos.get("grupos") or []) if isinstance(g, dict) and g.get("activo") is True]


def cadena_websearch(terminos) -> str:
    """Términos -> consulta websearch_to_tsquery: los que llevan espacio van entre comillas
    (FRASE); los demás, como lexema. Sin tildes, sin comillas sueltas."""
    partes = []
    for t in terminos or []:
        s = sin_tildes(t).replace('"', " ").strip()
        s = " ".join(s.split())
        if not s:
            continue
        partes.append(f'"{s}"' if " " in s else s)
    return " or ".join(partes)


def aplica_exclusiones(palabras: list[str], grupos: list[dict]) -> tuple[list[str], list[dict]]:
    """Separa las palabras del nicho en LIBRES (entran tal cual) y CON EXCLUSIÓN.

    Devuelve (libres, excl), con excl = [{"kw": palabra, "excluye": [websearch_grupo, ...]}]
    en el orden de las palabras del nicho. Un grupo cuyo aplica_a no está en el nicho se
    ignora."""
    por_palabra: dict[str, list[str]] = {}
    for g in grupos:
        cadena = cadena_websearch(g.get("terminos"))
        if not cadena:
            continue
        for kw in g.get("aplica_a") or []:
            k = sin_tildes(kw).strip()
            if k in palabras:
                por_palabra.setdefault(k, []).append(cadena)
    libres = [p for p in palabras if p not in por_palabra]
    excl = [{"kw": p, "excluye": por_palabra[p]} for p in palabras if p in por_palabra]
    return libres, excl


def nicho(criterios: dict | None, ruta_exclusiones: Path = EXCLUSIONES_JSON) -> dict:
    """{"cpv": [...], "palabras": "a or b ...", "excl": [...], "aviso": str | None}.

    "palabras" son las libres unidas con « or » (lo que antes era todo el nicho de texto)."""
    cpv, palabras = nicho_de_criterios(criterios)
    aviso = None
    try:
        grupos = lee_grupos_activos(ruta_exclusiones)
    except (OSError, ValueError) as e:
        grupos, aviso = [], f"no se pudo leer {Path(ruta_exclusiones).name} ({e}); nicho sin exclusiones"
    libres, excl = aplica_exclusiones(palabras, grupos)
    return {"cpv": cpv, "palabras": " or ".join(libres), "excl": excl, "aviso": aviso}


def sql_condicion(n: dict) -> tuple[str, list]:
    """La condición del nicho en SQL, con parámetros, EQUIVALENTE al or() de menores_api.js:
    (cpv_txt ILIKE prefijo) OR (tsv @@ libres) OR (tsv @@ kw AND NOT tsv @@ excl1 AND ...)."""
    partes, params = [], []
    for c in n["cpv"]:
        partes.append("cpv_txt ilike %s")
        params.append(f"% {c}%")
    if n["palabras"]:
        partes.append("tsv @@ websearch_to_tsquery('spanish', %s)")
        params.append(n["palabras"])
    for e in n["excl"]:
        trozo = "(tsv @@ websearch_to_tsquery('spanish', %s)"
        params.append(e["kw"])
        for x in e["excluye"]:
            trozo += " and not tsv @@ websearch_to_tsquery('spanish', %s)"
            params.append(x)
        partes.append(trozo + ")")
    return "(" + " or ".join(partes) + ")", params
