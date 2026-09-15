# -*- coding: utf-8 -*-
"""Traduce el código territorial del CODICE a (ccaa, lugar_ejecucion).

POR QUÉ EXISTE: `public.licitaciones` tiene las columnas `ccaa` y `lugar_ejecucion`
desde BG-3, pero están VACÍAS en las 624.204 filas: nunca se poblaron. El extractor
sí saca el dato (feeds.py:414-416), pero `backfill_catalogo.py` lo dejaba fuera a la
espera de este mapeo. Esto es ese mapeo.

=============================================================================
LAS REGLAS, Y POR QUÉ SON ASÍ (todo medido el 15/09/2026 contra datos reales)

1) SOLO SE USA EL CÓDIGO, NUNCA EL TEXTO. El feed trae además un texto libre
   (<cbc:CountrySubentity>), y es una trampa:
     · en la sindicación 1044 (agregadas, 157.752 filas del catálogo) NO VIENE NUNCA:
       0% de 1.106 entradas medidas. Rellenar con él dejaría un cuarto del catálogo
       en NULL y habríamos repetido el problema en pequeño;
     · donde viene, está sucio: el mismo ES523 llega como «VALENCIA», «Valencia»,
       «Valencia/València» y «Valencia / València»; ES111 como «A Coruña», «A CORUÑA»
       y «La Coruña»; y hay filas con el municipio en vez de la provincia.
   El código, en cambio, viene en el 100% de las entradas de las dos fuentes.

2) LA CCAA ES EL NUTS-2, o sea el prefijo de 4 caracteres. Pero se trunca Y SE BUSCA
   en el vocabulario oficial; nunca se trunca a ciegas. Si se truncara sin comprobar,
   ESZZZ («Extra-Regio NUTS 3») daría ESZZ, que existe pero NO es una comunidad, y un
   código extranjero (medidos: DK0, SK010, PT1A) daría basura.

3) LOS NIVELES VIENEN MEZCLADOS (medido sobre 12.219 entradas reales):
   NUTS-3 (5 chars, provincia/isla) 87%, NUTS-2 (4, CCAA) 11%, NUTS-1 (3) 0,7%,
   «ES» a secas 1,2%, sin código 0,01%.
   · NUTS-1 se resuelve SOLO si agrupa una única comunidad (ES3 = Madrid, ES7 =
     Canarias). Los demás (ES1 Noroeste, ES2 Noreste, ES4 Centro, ES5 Este, ES6 Sur)
     agrupan varias: van a NULL, no se inventa una. Esa correspondencia NO está
     escrita a mano aquí: se DERIVA del vocabulario, contando hijos.
   · «ES» a secas es ámbito nacional, no una comunidad: NULL.

4) UN NULL HONESTO ANTES QUE UN DATO INVENTADO. Lo que no se pueda resolver con
   seguridad se queda a NULL. Un filtro geográfico que miente es peor que uno vacío.

5) COLUMNA ESCALAR, NO ARRAY: medido, el 0,00% de las entradas (0 de 12.219) tiene
   más de un lugar de ejecución distinto. No hace falta ccaa_todas[] ni GIN.
=============================================================================

El vocabulario vive en data/nuts_nombres.json y lo genera `actualizar_nuts.py` desde
la lista oficial que el propio feed referencia (.../codice/cl/2.08/NUTS-2021.gc).
Los códigos de España no cambiaron entre NUTS-2016 y NUTS-2021: solo algunos nombres.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

RUTA_VOCABULARIO = Path(__file__).resolve().parent / "data" / "nuts_nombres.json"

# «Extra-Regio»: el CODICE los usa para lo que no cae en ningún territorio concreto.
# Están en la lista oficial, pero NO son comunidades autónomas.
NO_SON_CCAA = {"ESZ", "ESZZ", "ESZZZ"}


@lru_cache(maxsize=1)
def vocabulario() -> dict:
    """{codigo NUTS: nombre}. Cacheado: se lee una vez por proceso."""
    if not RUTA_VOCABULARIO.exists():
        raise FileNotFoundError(
            f"Falta {RUTA_VOCABULARIO}. Genéralo con:  python actualizar_nuts.py")
    return json.loads(RUTA_VOCABULARIO.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def ccaa_por_codigo() -> dict:
    """Las 19 comunidades/ciudades autónomas: NUTS-2 de España, menos los Extra-Regio."""
    return {c: n for c, n in vocabulario().items()
            if c.startswith("ES") and len(c) == 4 and c not in NO_SON_CCAA}


@lru_cache(maxsize=1)
def nuts1_resolubles() -> dict:
    """NUTS-1 que contienen UNA sola comunidad, y por tanto se pueden resolver sin
    inventar nada: {ES3: ES30, ES7: ES70}.

    Se DERIVA contando hijos en el vocabulario en vez de escribirlo a mano: si algún
    día la lista cambia, esto se ajusta solo en lugar de mentir en silencio.
    """
    hijos: dict[str, list] = {}
    for codigo in ccaa_por_codigo():
        hijos.setdefault(codigo[:3], []).append(codigo)
    return {padre: lista[0] for padre, lista in hijos.items() if len(lista) == 1}


def normaliza(codigo) -> str:
    return str(codigo or "").strip().upper()


def a_ccaa(codigo):
    """Código NUTS -> (codigo_ccaa, nombre_ccaa). (None, None) si no se puede resolver."""
    c = normaliza(codigo)
    if not c.startswith("ES") or c in NO_SON_CCAA:
        return None, None            # extranjero, vacío o Extra-Regio
    tabla = ccaa_por_codigo()
    if len(c) >= 4:
        prefijo = c[:4]
        if prefijo in tabla:
            return prefijo, tabla[prefijo]
        return None, None            # ESZZ y cualquier cosa que no esté en la lista
    if len(c) == 3:
        hijo = nuts1_resolubles().get(c)
        return (hijo, tabla[hijo]) if hijo else (None, None)
    return None, None                # 'ES' a secas: ámbito nacional, no una comunidad


def a_lugar(codigo):
    """Código NUTS -> nombre del lugar de ejecución, lo más fino que la fuente permita.

    Con NUTS-3 devuelve la provincia o isla («Sevilla», «Mallorca»); con NUTS-2, el
    nombre de la comunidad. Nunca devuelve algo más fino de lo que dice el código: si
    solo consta la comunidad, se queda en la comunidad.
    """
    c = normaliza(codigo)
    if not c.startswith("ES") or c in NO_SON_CCAA:
        return None
    voc = vocabulario()
    if len(c) == 5 and c in voc:
        return voc[c]
    _, nombre_ccaa = a_ccaa(c)
    return nombre_ccaa


def traduce(codigo):
    """Lo que consume el pipeline: (ccaa, lugar_ejecucion, codigo_normalizado)."""
    c = normaliza(codigo)
    _, nombre_ccaa = a_ccaa(c)
    return nombre_ccaa, a_lugar(c), (c or None)
