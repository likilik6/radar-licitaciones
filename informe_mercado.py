#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""informe_mercado.py · Vuelca a disco TODO lo que un estudio de mercado por producto necesita.

PARA QUÉ: informe_empresa.py responde «¿cómo le va a ESTA empresa en el mercado público?». Este
script responde la pregunta simétrica: «¿cómo es ESTE mercado?»: quién lo compra, quién lo gana,
dónde, con cuántas ofertas y con qué baja. Recibe un nombre, unos CPV y unas palabras, y deja en
disco un JSON a nivel de fila y un MD legible. Quien redacta lee los ficheros de la carpeta; la
clasificación de las filas y el informe los hace una persona. Aquí solo hay datos.

    python informe_mercado.py camas-hospitalarias --cpv 331921,33192000 --incluye "cama,camas" --meses 24
    python informe_mercado.py <nombre> --cpv <prefijos> [--incluye "..."] [--excluye "..."]
                              [--meses N] [--desde AAAA-MM-DD] [--salida carpeta]

SOLO LECTURA. GET contra PostgREST y nada más. Credenciales del mismo .env que informe_empresa.py
(SUPABASE_URL y SUPABASE_SECRET_KEY): si falta, mensaje claro y sin traza. La clave no sale en el
log ni en los ficheros. Ningún fichero del pipeline existente cambia: lo que se reutiliza se
IMPORTA de informe_empresa.py (cliente HTTP, regla del % de baja, expansión de CPV, fuentes...).

CRITERIO DE PERTENENCIA = CPV ∪ texto. Cada fila dice por cuál entró:
    "criterio": "cpv" | "texto" | "ambos"

=============================================================================
LO QUE SE COMPROBÓ ANTES DE ESCRIBIR ESTO (PASO 0, medido el 12/10/2026 contra la base real;
el detalle y las cifras están en PLAN_informe-mercado.md, en la carpeta del proyecto de OneDrive,
junto a los demás PLAN_*.md: no está en el repo):

· El Buscador busca texto con `tsv @@ websearch_to_tsquery('spanish', unaccent(texto))` sobre
  licitaciones.tsv (titulo peso A, objeto B, órgano C), índice GIN licitaciones_tsv_gin. NO hay
  trigram sobre titulo/objeto. PERO el stemmer español funde «cama/camas» con «cámara/cámaras»:
  con «cama OR camas» salen 1.785 filas en 24 meses y 1.278 son cámaras; la regex de palabra
  completa `\\mcamas?\\M` da 392. Reutilizar la criba del Buscador tal cual contamina el universo.
· La regex sola (imatch, sin índice) tarda 6-25 s por petición, también por tramos de 6 meses.
· SOLUCIÓN (medida): las dos cosas en la MISMA petición. El GIN del tsv como PREFILTRO
  (tsv=fts(spanish).cama:AB | camas:AB) y la regex de palabra completa como CRITERIO
  (or=(titulo.imatch.P,objeto.imatch.P)): 387 filas en 0,7 s. Pérdida conocida: las filas en las
  que el tokenizador del tsv pega la palabra a otra («soportes/camas»: token tipo file) no pasan
  el prefiltro: 5 de 392 en licitaciones y 6 de 909 en menores. Se declara en metadatos y se
  corrige en parte re-evaluando la regex EN CLIENTE sobre todas las filas traídas: una fila que
  entra por CPV y lleva la palabra pegada sale «ambos», no «cpv».
· Sintaxis PostgREST: dentro de `or=(...)` un valor con `(`, `)` o `,` rompe el parser salvo que
  vaya entre comillas con las barras dobladas, y esa forma NO vale en un filtro suelto. Por eso la
  regex se construye como alternancia de nivel superior SIN paréntesis ni comas
  (`\\mcama\\M|\\mcamas\\M`) y se manda sin entrecomillar. Un `count=exact` con un término que no
  casa nada tarda 6 s (traer las filas, 0,1 s): la consulta de texto NO pide conteos exactos.
· `order=fecha_publicacion.desc.nullslast,licitacion_id.asc` (coincide con los índices de orden
  del repo) hace estable la paginación y NO dispara la patología BG-3 (0,16-0,31 s medidos).
· `licitaciones` NO tiene procedimiento, sistema_contratacion, n_lotes, tipo_contrato ni
  estado/resultado: van a null. `adjudicaciones` no tiene descripcion_lote: null. Hay una fila por
  (lote, adjudicatario): un lote con varios ganadores repite el importe en cada fila.
· En la fuente agregada (Comunidad de Madrid) fecha_adjudicacion viene NULL. Por eso cada
  adjudicación lleva las fechas de su licitación y una `fecha_referencia` declarada.
· El conteo exacto de TODO el catálogo tarda 17,75 s y el de un año con 215.000 filas, 18,2 s:
  no se hacen. El tamaño del catálogo y su reparto por año van como ESTIMACIÓN del planificador
  (count=planned, 0,1 s, desviación medida 0,03 %), declarada como tal; solo son contexto.
=============================================================================
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import informe_empresa as ie
from informe_empresa import (PAGINA, TROZO_IDS, Supabase, aviso_fuente_menores, calcula_baja,
                             credenciales, desglose_por_fuente, en_lista, es_unitario, eur,
                             expande_cpv, lee_fuentes_menores, normaliza_cif, num, resta_meses,
                             tabla_md, trocea_terminos)

try:  # Rótulos de los códigos del CODICE. Son los del pipeline (feeds.py); si no se pueden
    from feeds import CONTRATO_TIPO_LABELS, SISTEMA_CONTRATACION_LABELS   # importar, null.
except Exception:  # pragma: no cover
    CONTRATO_TIPO_LABELS, SISTEMA_CONTRATACION_LABELS = {}, {}

TOPE_FILAS = 20000          # tope de seguridad por consulta; si se alcanza se DECLARA (topado)
UMBRAL_TROCEO = 15000       # candidatas del prefiltro a partir de las cuales se trocea por meses
SEG_PAGINA_LENTA = 8.0      # s por página a partir de los cuales se abandona y se trocea por meses
LIMITE_PETICION = 15.0      # criterio de aceptación: ninguna petición por encima (se mide, se declara)
TOP_ORGANOS = 20
TOP_ADJUDICATARIOS = 20
TOP_LICITACIONES = 30
TOP_ORGANOS_MENORES = 10
AVISO_CPV = 60              # códigos CPV a partir de los cuales se avisa (URL larga, or=() enorme en menores)
TOPE_CPV = 300              # códigos CPV a partir de los cuales se para: afina el prefijo

CAMPOS_LIC = ("licitacion_id,titulo,objeto,organo_contratacion,num_expediente,ccaa,nuts_codigo,"
              "lugar_ejecucion,cpv,fuente,presupuesto_sin_iva,valor_estimado,fecha_publicacion,"
              "fecha_fin_plazo,estado_adjudicacion,n_lotes_desiertos,enlace")
ORDEN_LIC = "order=fecha_publicacion.desc.nullslast,licitacion_id.asc"
CAMPOS_ADJ = ("licitacion_id,lote,resultado,resultado_code,importe_sin_iva,presupuesto_lote_sin_iva,"
              "n_ofertas,adjudicatario,cif_adjudicatario,id_scheme,es_pyme,fecha_adjudicacion,"
              "estado_expediente,sistema_contratacion,tipo_contrato")
CAMPOS_MEN = ("licitacion_id,objeto,organo_contratacion,adjudicatario,cif_adjudicatario,"
              "cifs_adjudicatarios,n_adjudicatarios,importe_sin_iva,fecha_adjudicacion,cpv,fuente,"
              "enlace,num_expediente")
ORDEN_MEN = "order=fecha_adjudicacion.desc.nullslast,licitacion_id.asc"
CAMPOS_COMP = ("cif,nombre_canonico,n_expedientes,n_lotes,importe_total_sin_iva,pct_una_oferta,"
               "primera_adjudicacion,ultima_adjudicacion")

VOCALES_SIN = str.maketrans("áàäâéèëêíìïîóòöôúùûü", "aaaaeeeeiiiioooouuuu")
TERMINO_VALIDO = re.compile(r"^[0-9a-zñ][0-9a-zñ -]*$")
SEP_ARE = r"[\s-]+"          # entre palabras de una frase, en la regex del servidor (ARE de Postgres)
SEP_PY = r"[\s\-]+"          # lo mismo en sintaxis de Python
CIF_FORMA = re.compile(r"^[A-Z0-9]{8,10}$")   # pareceCif de la web: alfanumérico 8-10 con algún dígito

MECANISMO_TEXTO = ("GIN del tsv como PREFILTRO (tsv=fts(spanish).<término:pesos>) y regex de palabra "
                   "completa como CRITERIO (imatch \\m...\\M) en la MISMA petición; el criterio se "
                   "re-evalúa en cliente sobre todas las filas traídas.")
PERDIDA_CONOCIDA = ("Las filas en las que el tokenizador del tsv pega la palabra a otra (p. ej. "
                    "«soportes/camas», «s.l.cama») no pasan el prefiltro y, si no entran por CPV, "
                    "no se ven. Medido el 12/10/2026 para «cama/camas»: 5 de 392 licitaciones y "
                    "6 de 909 menores; ninguna era de camas hospitalarias.")


# ---------------------------------------------------------------------------
# Texto: normalización, tsquery (prefiltro) y regex (criterio)
# ---------------------------------------------------------------------------
def valor_url(v) -> str:
    """TODO valor de filtro va por aquí: codifica también `+`, `\\`, `|`, `:` y espacios.
    requests deja `+` sin codificar y el servidor lo leería como espacio (rompería `\\s+` y
    el `+00:00` de las fechas ISO). La sintaxis (or=(, comas entre condiciones) no pasa por aquí."""
    return quote(str(v), safe="")


def sin_diacriticos(texto: str) -> str:
    """Como unaccent de Postgres: quita tildes, diéresis y la virgulilla (ñ -> n). Es la forma
    con la que se guardó el tsv, así que es la que hay que mandar en la tsquery."""
    return "".join(c for c in unicodedata.normalize("NFD", str(texto or ""))
                   if unicodedata.category(c) != "Mn")


def normaliza_termino(termino: str) -> str:
    """Minúsculas, vocales sin acento (la ñ se CONSERVA: en el texto real está), guiones como
    espacio, espacios colapsados. Es la base de la que salen la tsquery y las regex."""
    base = str(termino or "").strip().lower().translate(VOCALES_SIN)
    base = re.sub(r"[\s-]+", " ", base).strip()
    return base


def termino_valido(base: str) -> bool:
    return bool(base) and bool(TERMINO_VALIDO.match(base))


CLASES = {"a": "[aá]", "e": "[eé]", "i": "[ií]", "o": "[oó]", "u": "[uúü]", "ñ": "[ñn]"}


def patron_palabra(palabra: str) -> str:
    """Una palabra sin tildes -> patrón que casa la grafía real con o sin tilde: «camas» ->
    c[aá]m[aá]s. Vale igual en la ARE de Postgres y en `re` de Python, no lleva paréntesis ni
    comas (necesario dentro de or=(...) de PostgREST) y mide lo mismo para una frase de cinco
    palabras que para una: el producto cartesiano de variantes que se usó primero disparaba la
    URL a 25-40 KB con frases y, si se recortaba, perdía las tildes. Es un superconjunto
    inofensivo (admite dos tildes en una palabra). La «ñ» casa también «n»."""
    return "".join(CLASES.get(ch, re.escape(ch)) for ch in palabra)


def prepara_terminos(entrada: str) -> tuple[list[dict], list[str]]:
    """Trocea --incluye/--excluye y normaliza cada término. Devuelve (términos válidos, rechazados).
    Cada término válido: {entrada, base, palabras}."""
    validos, rechazados = [], []
    for bruto in trocea_terminos(entrada):
        base = normaliza_termino(bruto)
        if not termino_valido(base):
            rechazados.append(bruto)
            continue
        validos.append({"entrada": bruto, "base": base, "palabras": base.split()})
    return validos, rechazados


def construye_tsquery(terminos: list[dict], pesos: str) -> str:
    """Prefiltro para to_tsquery('spanish', ...): palabra -> `w:AB`; frase -> `a:AB <-> b:AB`
    (las stopwords de una frase se dejan: Postgres las convierte en distancia). Términos con `|`.
    Las palabras van como unaccent (ñ -> n), que es como está el tsv."""
    partes = []
    for t in terminos:
        palabras = [sin_diacriticos(p) for p in t["palabras"]]
        partes.append(" <-> ".join(f"{p}:{pesos}" for p in palabras))
    return " | ".join(partes)


def construye_regex_servidor(terminos: list[dict]) -> str:
    """Regex ARE para imatch: alternancia de NIVEL SUPERIOR, sin paréntesis, comas ni comillas
    (los paréntesis y las comas rompen el árbol lógico de PostgREST dentro de or=(...)).
    `\\mc[aá]m[aá]\\M|\\mc[aá]m[aá]s\\M`; frase: `\\mc[aá]m[aá][\\s-]+[aá]rt[ií]c[uúü]l[aá]d[aá]\\M`."""
    alternativas = ["\\m" + SEP_ARE.join(patron_palabra(p) for p in t["palabras"]) + "\\M" for t in terminos]
    patron = "|".join(alternativas)
    for prohibido in "(),\" \t\n":
        if prohibido in patron:
            raise ValueError(f"la regex del servidor no puede llevar {prohibido!r}: {patron[:80]}")
    return patron


def construye_regex_cliente(terminos: list[dict]):
    """La MISMA regex en sintaxis Python (re no entiende \\m/\\M): límite de palabra con
    lookarounds sobre \\w, insensible a mayúsculas, mismas clases de caracteres."""
    alternativas = [SEP_PY.join(patron_palabra(p) for p in t["palabras"]) for t in terminos]
    if not alternativas:
        return None
    return re.compile(r"(?<!\w)(?:" + "|".join(alternativas) + r")(?!\w)", re.IGNORECASE)


def casa_en_columnas(rx, fila: dict, campos_texto: tuple) -> bool:
    """La regex se evalúa COLUMNA A COLUMNA, como hace el servidor (titulo.imatch OR
    objeto.imatch): una frase no puede casar cruzando el final del título y el principio del objeto."""
    return bool(rx) and any(rx.search(str(fila.get(c) or "")) for c in campos_texto)


# ---------------------------------------------------------------------------
# HTTP: paginación medida, conteo estimado, tramos
# ---------------------------------------------------------------------------
def pagina(sb: Supabase, tabla: str, params: str, tope: int = TOPE_FILAS,
           max_seg_pagina: float | None = None) -> tuple[list[dict], dict]:
    """Todas las filas que casen, en páginas de PAGINA, midiendo cada página. `params` DEBE
    llevar un order con desempate por clave primaria (si no, las páginas pueden solaparse).
    Si se llega al tope se corta y se DECLARA; nunca en silencio. Con `max_seg_pagina`, si una
    página llena tarda más de eso se abandona (lenta=True) para que quien llama trocee."""
    if "limit=" in params:
        raise ValueError("pagina() pagina sola: quita el limit de los params")
    salida, desde, segundos, lenta = [], 0, [], False
    while True:
        arranque = time.time()
        trozo = sb._pide(tabla, f"{params}&limit={PAGINA}&offset={desde}").json()
        seg = round(time.time() - arranque, 2)
        segundos.append(seg)
        salida.extend(trozo)
        if len(trozo) < PAGINA:
            break
        if max_seg_pagina is not None and seg > max_seg_pagina:
            lenta = True
            break
        desde += PAGINA
        if len(salida) >= tope:
            break
    topado = (not lenta) and len(salida) >= tope and len(trozo) == PAGINA
    return salida[:tope], {"paginas": len(segundos), "segundos_por_pagina": segundos,
                           "topado": topado, "lenta": lenta}


def cuenta_planned(sb: Supabase, tabla: str, params: str):
    """Conteo ESTIMADO por el planificador (Prefer: count=planned): instantáneo, inexacto. Sirve
    para decidir si trocear y para declarar «más de N» cuando una consulta topa. Supabase.cuenta
    fija count=exact, por eso hay un helper propio (no se toca informe_empresa)."""
    cola = (params + "&") if params else ""
    r = sb._pide(tabla, f"{cola}select=licitacion_id",
                 {"Prefer": "count=planned", "Range-Unit": "items", "Range": "0-0"})
    rango = r.headers.get("Content-Range", "")
    total = rango.split("/")[-1] if "/" in rango else ""
    return int(total) if total.isdigit() else None


def tramos_mensuales(desde: datetime, hasta: datetime) -> list[tuple[datetime, datetime]]:
    """[(ini, fin), ...] de mes en mes, el último recortado a `hasta`."""
    tramos, ini = [], desde
    while ini < hasta:
        fin = min(resta_meses(ini, -1), hasta)
        tramos.append((ini, fin))
        ini = fin
    return tramos


def trae_universo(sb: Supabase, tabla: str, campos: str, orden: str, filtro: str, campo_fecha: str,
                  desde: datetime, hasta: datetime, prefiltro: str | None) -> tuple[list[dict], dict]:
    """Una de las dos consultas del universo (CPV o texto) acotada por fecha. Si el prefiltro de
    texto tiene más candidatas que UMBRAL_TROCEO (estimación del planificador) se trocea por meses:
    es la red que pide el encargo para no pasar de 15 s por petición."""
    desde_v = valor_url(desde.isoformat()) if campo_fecha == "fecha_publicacion" else desde.isoformat()[:10]
    base = f"select={campos}&{filtro}&{orden}"
    ventana = f"{filtro}&{campo_fecha}=gte.{desde_v}"
    info = {"paginas": 0, "segundos_por_pagina": [], "topado": False, "lenta": False,
            "filas_estimadas_si_topado": None, "troceo_por_meses": None,
            "candidatas_prefiltro_estimadas": None}

    def por_tramos(motivo: str) -> list[dict]:
        tramos = tramos_mensuales(desde, hasta)
        info.update({"paginas": 0, "segundos_por_pagina": [], "topado": False, "lenta": False,
                     "troceo_por_meses": {"aplicado": True, "n_tramos": len(tramos), "motivo": motivo}})
        filas = []
        for ini, fin in tramos:
            # El último tramo va SIN techo: así la ruta troceada devuelve lo mismo que la entera
            # (con una columna date, lt.<hoy> dejaría fuera lo adjudicado hoy).
            if campo_fecha == "fecha_publicacion":
                rango = f"{campo_fecha}=gte.{valor_url(ini.isoformat())}"
                if fin < hasta:
                    rango += f"&{campo_fecha}=lt.{valor_url(fin.isoformat())}"
            else:
                rango = f"{campo_fecha}=gte.{ini.isoformat()[:10]}"
                if fin < hasta:
                    rango += f"&{campo_fecha}=lt.{fin.isoformat()[:10]}"
            trozo, inf = pagina(sb, tabla, f"{base}&{rango}")
            filas.extend(trozo)
            info["paginas"] += inf["paginas"]
            info["segundos_por_pagina"] += inf["segundos_por_pagina"]
            info["topado"] = info["topado"] or inf["topado"]
        return filas

    if prefiltro:
        estim = cuenta_planned(sb, tabla, f"{prefiltro}&{campo_fecha}=gte.{desde_v}")
        info["candidatas_prefiltro_estimadas"] = estim
        if estim is not None and estim > UMBRAL_TROCEO:
            filas = por_tramos(f"{estim} candidatas estimadas > {UMBRAL_TROCEO}")
        else:
            info["troceo_por_meses"] = {"aplicado": False, "n_tramos": 1, "motivo": None}
            filas = None
    else:
        filas = None
    if filas is None:
        filas, inf = pagina(sb, tabla, f"{base}&{campo_fecha}=gte.{desde_v}", max_seg_pagina=SEG_PAGINA_LENTA)
        if inf.get("lenta"):
            # Una página llena por encima del límite: se tira lo traído y se vuelve por meses.
            filas = por_tramos(f"primera página de {inf['segundos_por_pagina'][0]} s > {SEG_PAGINA_LENTA} s")
        else:
            info.update(inf)
    if info["topado"]:
        # El tope NUNCA es silencioso: se declara cuántas filas hay en realidad (estimación).
        info["filas_estimadas_si_topado"] = cuenta_planned(sb, tabla, ventana)
    return filas, info


# ---------------------------------------------------------------------------
# Criterio: fusión CPV ∪ texto, --excluye, invariante
# ---------------------------------------------------------------------------
def fusiona(filas_cpv: list[dict], filas_texto: list[dict], rx_cliente, campos_texto: tuple) -> tuple[list[dict], dict]:
    """Una fila por id. `criterio` = CPV (pertenece a la consulta CPV) × texto (la regex de palabra
    completa casa EN CLIENTE sobre los campos de texto, o la fila vino de la consulta de texto).
    Devuelve también los ids repetidos dentro de una misma consulta (alarma de paginación)."""
    repetidos = {"cpv": 0, "texto": 0}
    por_id: dict[str, dict] = {}
    vistos = set()
    for f in filas_cpv:
        if f["licitacion_id"] in vistos:
            repetidos["cpv"] += 1
            continue
        vistos.add(f["licitacion_id"])
        por_id[f["licitacion_id"]] = {"fila": f, "cpv": True, "texto_consulta": False}
    vistos = set()
    for f in filas_texto:
        if f["licitacion_id"] in vistos:
            repetidos["texto"] += 1
            continue
        vistos.add(f["licitacion_id"])
        e = por_id.setdefault(f["licitacion_id"], {"fila": f, "cpv": False, "texto_consulta": False})
        e["texto_consulta"] = True
    salida = []
    for e in por_id.values():
        por_texto = casa_en_columnas(rx_cliente, e["fila"], campos_texto) or e["texto_consulta"]
        if e["cpv"] and por_texto:
            crit = "ambos"
        elif e["cpv"]:
            crit = "cpv"
        else:
            crit = "texto"
        fila = dict(e["fila"])
        fila["criterio"] = crit
        fila["_campos_texto"] = campos_texto
        salida.append(fila)
    return salida, repetidos


def aplica_excluye(filas: list[dict], rx_excluye, con_excluye: bool) -> tuple[list[dict], int]:
    """Regla del encargo: --excluye SOLO saca filas que entraron únicamente por texto. Una fila que
    también entró por CPV se queda con su criterio real y `excluye_casa: true` para que se vea."""
    salida, excluidas = [], 0
    for f in filas:
        casa = bool(con_excluye) and casa_en_columnas(rx_excluye, f, f.get("_campos_texto", ("titulo", "objeto")))
        if casa and f["criterio"] == "texto":
            excluidas += 1
            continue
        g = dict(f)
        g.pop("_campos_texto", None)
        g["excluye_casa"] = casa if con_excluye else None
        salida.append(g)
    return salida, excluidas


def resumen_topado(info: dict) -> dict:
    """{topado, filas_estimadas_cpv, filas_estimadas_texto} de las dos consultas de un bloque."""
    c, t = info.get("consulta_cpv") or {}, info.get("consulta_texto") or {}
    return {"topado": bool(c.get("topado") or t.get("topado")),
            "filas_estimadas_cpv": c.get("filas_estimadas_si_topado"),
            "filas_estimadas_texto": t.get("filas_estimadas_si_topado")}


def filas_por_criterio(filas: list[dict]) -> dict:
    c = Counter(f["criterio"] for f in filas)
    salida = {"solo_cpv": c.get("cpv", 0), "solo_texto": c.get("texto", 0),
              "ambos": c.get("ambos", 0), "total": len(filas)}
    assert salida["solo_cpv"] + salida["solo_texto"] + salida["ambos"] == salida["total"]
    return salida


def filas_por_criterio_y_fuente(filas: list[dict]) -> dict:
    por_fuente = defaultdict(list)
    for f in filas:
        por_fuente[f.get("fuente") or "(sin fuente)"].append(f)
    return {k: filas_por_criterio(v) for k, v in sorted(por_fuente.items())}


# ---------------------------------------------------------------------------
# Bloque 1 · licitaciones
# ---------------------------------------------------------------------------
def fila_licitacion(f: dict) -> dict:
    return {
        "licitacion_id": f.get("licitacion_id"),
        "fecha_publicacion": f.get("fecha_publicacion"),
        "fecha_fin_plazo": f.get("fecha_fin_plazo"),
        "titulo": f.get("titulo"),
        "objeto": f.get("objeto"),
        "organo_contratacion": f.get("organo_contratacion"),
        "num_expediente": f.get("num_expediente"),
        "tipo_contrato": None,            # no existe en licitaciones (ver *_expediente, derivado)
        "procedimiento": None,            # no existe en ninguna tabla
        "sistema_contratacion": None,     # no existe en licitaciones (ver *_expediente, derivado)
        "n_lotes": None,                  # no existe (ver n_lotes_con_resultado, derivado)
        "ccaa": f.get("ccaa"),
        "nuts_codigo": f.get("nuts_codigo"),
        "lugar_ejecucion": f.get("lugar_ejecucion"),
        "cpv": f.get("cpv"),
        "fuente": f.get("fuente"),
        "presupuesto_sin_iva": num(f.get("presupuesto_sin_iva")),
        "valor_estimado": num(f.get("valor_estimado")),
        "estado_adjudicacion": f.get("estado_adjudicacion"),
        "n_lotes_desiertos": f.get("n_lotes_desiertos"),
        "enlace": f.get("enlace"),
        "criterio": f.get("criterio"),
        "excluye_casa": f.get("excluye_casa"),
        # Derivados del bloque 2 (se rellenan después): con nombre propio, nunca se confunden
        # con columnas de la tabla.
        "tiene_resultado": False,
        "n_lotes_con_resultado": None,
        "n_filas_adjudicacion": None,
        "tipo_contrato_expediente": None,
        "tipo_contrato_expediente_rotulo": None,
        "sistema_contratacion_expediente": None,
        "sistema_contratacion_expediente_rotulo": None,
    }


def bloque1_licitaciones(sb: Supabase, cpvs: list[str], tsquery: str | None, regex_srv: str | None,
                         rx_cliente, rx_excluye, con_excluye: bool, desde: datetime, hasta: datetime,
                         campo_fecha_nulo: bool = False) -> tuple[list[dict], dict]:
    """Universo de licitaciones: consulta (a) CPV + consulta (b) texto, fusionadas. Con
    `campo_fecha_nulo` se traen las que no tienen fecha_publicacion (apartado aparte)."""
    info = {"consulta_cpv": None, "consulta_texto": None, "ids_repetidos_entre_paginas": None,
            "excluidas_por_texto": 0}
    filas_cpv, filas_txt = [], []
    if cpvs:
        ov = "%7B" + ",".join(cpvs) + "%7D"
        filtro = f"cpv=ov.{ov}"
        if campo_fecha_nulo:
            filas_cpv, inf = pagina(sb, "licitaciones", f"select={CAMPOS_LIC}&{filtro}&fecha_publicacion=is.null&{ORDEN_LIC}")
        else:
            filas_cpv, inf = trae_universo(sb, "licitaciones", CAMPOS_LIC, ORDEN_LIC, filtro,
                                           "fecha_publicacion", desde, hasta, None)
        info["consulta_cpv"] = inf
    if tsquery and regex_srv:
        pre = f"tsv=fts(spanish).{valor_url(tsquery)}"
        rx = valor_url(regex_srv)
        filtro = f"{pre}&or=(titulo.imatch.{rx},objeto.imatch.{rx})"
        if campo_fecha_nulo:
            filas_txt, inf = pagina(sb, "licitaciones", f"select={CAMPOS_LIC}&{filtro}&fecha_publicacion=is.null&{ORDEN_LIC}")
        else:
            filas_txt, inf = trae_universo(sb, "licitaciones", CAMPOS_LIC, ORDEN_LIC, filtro,
                                           "fecha_publicacion", desde, hasta, pre)
        info["consulta_texto"] = inf
    fusion, repetidos = fusiona(filas_cpv, filas_txt, rx_cliente, ("titulo", "objeto"))
    info["ids_repetidos_entre_paginas"] = repetidos
    finales, excluidas = aplica_excluye(fusion, rx_excluye, con_excluye)
    info["excluidas_por_texto"] = excluidas
    finales.sort(key=lambda f: ((f.get("fecha_publicacion") or ""), f["licitacion_id"]), reverse=True)
    return [fila_licitacion(f) for f in finales], info


# ---------------------------------------------------------------------------
# Bloque 2 · adjudicaciones
# ---------------------------------------------------------------------------
def rotulo(mapa: dict, codigo) -> str | None:
    if codigo is None or codigo == "":
        return None
    return mapa.get(str(codigo).strip())


def enriquece_adjudicacion(a: dict, lic: dict, hoy: str) -> dict:
    """Una fila del bloque 2 a partir de la fila cruda de `adjudicaciones` y su licitación."""
    baja = calcula_baja(a, lic.get("presupuesto_sin_iva"))
    unitario = es_unitario(a.get("sistema_contratacion"), a.get("importe_sin_iva"))
    cif = normaliza_cif(a.get("cif_adjudicatario"))
    lote = (a.get("lote") or "").strip()
    code = (a.get("resultado_code") or "").strip()
    es_desierto = cif == "" and code == "3"
    es_retirado = cif == "" and code in ("4", "5")
    fecha_adj = a.get("fecha_adjudicacion")
    fecha_pub = lic.get("fecha_publicacion")
    if fecha_adj:
        referencia, origen = str(fecha_adj)[:10], "adjudicacion"
    elif fecha_pub:
        referencia, origen = str(fecha_pub)[:10], "publicacion"
    else:
        referencia, origen = None, None
    baja_aviso = None
    if (baja["pct_baja"] is None and not unitario and lote
            and num(a.get("presupuesto_lote_sin_iva")) is None
            and num(lic.get("presupuesto_sin_iva")) is not None
            and num(a.get("importe_sin_iva")) is not None):
        baja_aviso = ("Lote numerado sin presupuesto de lote: por la regla de informe_empresa NO se "
                      "compara con el presupuesto del expediente (el Buscador sí lo hace cuando el "
                      "expediente tiene una sola adjudicación). Se puede rehacer a mano con "
                      "presupuesto_expediente_sin_iva si n_lotes_con_resultado es 1.")
    return {
        "licitacion_id": a.get("licitacion_id"),
        "lote": a.get("lote") or None,
        "descripcion_lote": None,                     # no existe en adjudicaciones
        "resultado": a.get("resultado"),
        "resultado_code": a.get("resultado_code"),
        "estado_expediente": a.get("estado_expediente"),
        "importe_sin_iva": num(a.get("importe_sin_iva")),
        "presupuesto_lote_sin_iva": num(a.get("presupuesto_lote_sin_iva")),
        "presupuesto_expediente_sin_iva": num(lic.get("presupuesto_sin_iva")),
        "pct_baja": baja["pct_baja"],
        "baja_base": baja["base"],
        "baja_origen": baja["origen_base"],
        "baja_nota": baja["nota"],
        "baja_aviso": baja_aviso,
        "n_ofertas": a.get("n_ofertas"),
        "adjudicatario": a.get("adjudicatario"),
        "cif_adjudicatario": a.get("cif_adjudicatario"),
        "id_scheme": a.get("id_scheme"),
        "es_pyme": a.get("es_pyme"),
        "fecha_adjudicacion": fecha_adj,
        "sistema_contratacion": a.get("sistema_contratacion"),
        "sistema_contratacion_rotulo": rotulo(SISTEMA_CONTRATACION_LABELS, a.get("sistema_contratacion")),
        "tipo_contrato": a.get("tipo_contrato"),
        "tipo_contrato_rotulo": rotulo(CONTRATO_TIPO_LABELS, a.get("tipo_contrato")),
        "es_unitario": unitario,
        "es_desierto": es_desierto,
        "es_retirado": es_retirado,
        # Desnormalizado de su licitación: lo que hace falta para recortar «últimos 12 meses» a
        # mano sin vaciar las fuentes que no traen fecha de adjudicación.
        "fecha_publicacion": fecha_pub,
        "fecha_fin_plazo": lic.get("fecha_fin_plazo"),
        "fuente": lic.get("fuente"),
        "ccaa": lic.get("ccaa"),
        "organo_contratacion": lic.get("organo_contratacion"),
        "criterio": lic.get("criterio"),
        "fecha_referencia": referencia,
        "fecha_referencia_origen": origen,
        "fecha_adjudicacion_futura": bool(fecha_adj and str(fecha_adj)[:10] > hoy),
    }


def bloque2_adjudicaciones(sb: Supabase, licitaciones: list[dict], hoy: str) -> list[dict]:
    """TODOS los lotes de las licitaciones del bloque 1 (desiertas y retiradas incluidas, con su
    resultado), en trozos de 100 ids."""
    por_id = {l["licitacion_id"]: l for l in licitaciones}
    ids = sorted(por_id)
    salida = []
    for i in range(0, len(ids), TROZO_IDS):
        trozo = ids[i:i + TROZO_IDS]
        crudas = sb.filas("adjudicaciones",
                          f"select={CAMPOS_ADJ}&licitacion_id={valor_url(en_lista(trozo))}"
                          "&order=licitacion_id.asc,lote.asc.nullsfirst,id.asc")
        for a in crudas:
            salida.append(enriquece_adjudicacion(a, por_id.get(a["licitacion_id"], {}), hoy))
    return salida


def deriva_bloque1(licitaciones: list[dict], adjudicaciones: list[dict]) -> None:
    """Rellena en cada licitación los derivados del bloque 2 (nombre propio, origen declarado)."""
    grupos = defaultdict(list)
    for a in adjudicaciones:
        grupos[a["licitacion_id"]].append(a)
    for l in licitaciones:
        filas = grupos.get(l["licitacion_id"], [])
        if not filas:
            continue
        lotes = {(a.get("lote") or "") for a in filas}
        tipos = sorted({str(a["tipo_contrato"]) for a in filas if a.get("tipo_contrato")})
        sistemas = sorted({str(a["sistema_contratacion"]) for a in filas if a.get("sistema_contratacion")})
        l["tiene_resultado"] = True
        l["n_lotes_con_resultado"] = len(lotes)
        l["n_filas_adjudicacion"] = len(filas)
        l["tipo_contrato_expediente"] = (tipos[0] if len(tipos) == 1 else (tipos or None))
        l["tipo_contrato_expediente_rotulo"] = rotulo(CONTRATO_TIPO_LABELS, tipos[0]) if len(tipos) == 1 else None
        l["sistema_contratacion_expediente"] = (sistemas[0] if len(sistemas) == 1 else (sistemas or None))
        l["sistema_contratacion_expediente_rotulo"] = (rotulo(SISTEMA_CONTRATACION_LABELS, sistemas[0])
                                                       if len(sistemas) == 1 else None)


# ---------------------------------------------------------------------------
# Bloque 3 · menores
# ---------------------------------------------------------------------------
def fila_menor(f: dict) -> dict:
    return {
        "licitacion_id": f.get("licitacion_id"),
        "objeto": f.get("objeto"),
        "organo_contratacion": f.get("organo_contratacion"),
        "adjudicatario": f.get("adjudicatario"),
        "cif_adjudicatario": f.get("cif_adjudicatario"),
        "cifs_adjudicatarios": f.get("cifs_adjudicatarios"),
        "n_adjudicatarios": f.get("n_adjudicatarios"),
        "importe_sin_iva": num(f.get("importe_sin_iva")),
        "fecha_adjudicacion": f.get("fecha_adjudicacion"),
        "num_expediente": f.get("num_expediente"),
        "cpv": f.get("cpv"),
        "fuente": f.get("fuente"),
        "enlace": f.get("enlace"),
        "criterio": f.get("criterio"),
        "excluye_casa": f.get("excluye_casa"),
    }


def bloque3_menores(sb: Supabase, cpvs: list[str], tsquery: str | None, regex_srv: str | None,
                    rx_cliente, rx_excluye, con_excluye: bool, desde: datetime, hasta: datetime,
                    sin_fecha: bool = False) -> tuple[list[dict], dict]:
    """Menores del universo (todas las fuentes cargadas), ventana por fecha_adjudicacion. CPV por
    cpv_txt (trigram, como menores_api.js); texto por prefiltro tsv (objeto, peso A) + regex en
    objeto. Con `sin_fecha` se traen los que no tienen fecha (apartado aparte)."""
    info = {"consulta_cpv": None, "consulta_texto": None, "ids_repetidos_entre_paginas": None,
            "excluidas_por_texto": 0}
    filas_cpv, filas_txt = [], []
    if cpvs:
        filtro = "or=(" + ",".join(f"cpv_txt.ilike.*%20{c}*" for c in cpvs) + ")"
        if sin_fecha:
            filas_cpv, inf = pagina(sb, "menores", f"select={CAMPOS_MEN}&{filtro}&fecha_adjudicacion=is.null&{ORDEN_MEN}")
        else:
            filas_cpv, inf = trae_universo(sb, "menores", CAMPOS_MEN, ORDEN_MEN, filtro,
                                           "fecha_adjudicacion", desde, hasta, None)
        info["consulta_cpv"] = inf
    if tsquery and regex_srv:
        pre = f"tsv=fts(spanish).{valor_url(tsquery)}"
        filtro = f"{pre}&objeto=imatch.{valor_url(regex_srv)}"
        if sin_fecha:
            filas_txt, inf = pagina(sb, "menores", f"select={CAMPOS_MEN}&{filtro}&fecha_adjudicacion=is.null&{ORDEN_MEN}")
        else:
            filas_txt, inf = trae_universo(sb, "menores", CAMPOS_MEN, ORDEN_MEN, filtro,
                                           "fecha_adjudicacion", desde, hasta, pre)
        info["consulta_texto"] = inf
    fusion, repetidos = fusiona(filas_cpv, filas_txt, rx_cliente, ("objeto",))
    info["ids_repetidos_entre_paginas"] = repetidos
    finales, excluidas = aplica_excluye(fusion, rx_excluye, con_excluye)
    info["excluidas_por_texto"] = excluidas
    finales.sort(key=lambda f: ((f.get("fecha_adjudicacion") or ""), f["licitacion_id"]), reverse=True)
    return [fila_menor(f) for f in finales], info


# ---------------------------------------------------------------------------
# Bloque 4 · adjudicatarios
# ---------------------------------------------------------------------------
def cif_con_forma_valida(cif: str) -> bool:
    return bool(CIF_FORMA.match(cif or "")) and any(ch.isdigit() for ch in (cif or ""))


def agrega_adjudicatarios(adjudicaciones: list[dict], menores: list[dict]) -> dict:
    """Agregados por CIF de ESTE mercado. Por fila: en un lote con varios ganadores cada uno se
    lleva el lote entero (declarado en metadatos). Los importes de precios unitarios NO se suman
    al importe real: van aparte."""
    g = defaultdict(lambda: {"nombre_en_mercado": None, "licitaciones": set(), "lotes": 0,
                             "importe": 0.0, "lotes_unitarios": 0, "tarifas": 0.0,
                             "con_ofertas": 0, "sin_ofertas": 0, "una_oferta": 0, "bajas": [],
                             "menores": 0, "importe_menores": 0.0, "menores_secundario": 0,
                             "principal": False})
    for a in adjudicaciones:
        cif = normaliza_cif(a.get("cif_adjudicatario"))
        if not cif:
            continue
        x = g[cif]
        x["principal"] = True
        x["nombre_en_mercado"] = x["nombre_en_mercado"] or a.get("adjudicatario")
        x["licitaciones"].add(a["licitacion_id"])
        x["lotes"] += 1
        imp = a.get("importe_sin_iva") or 0.0
        if a.get("es_unitario"):
            x["lotes_unitarios"] += 1
            x["tarifas"] += imp
        else:
            x["importe"] += imp
        if a.get("n_ofertas") is None:
            x["sin_ofertas"] += 1
        else:
            x["con_ofertas"] += 1
            if a["n_ofertas"] == 1:
                x["una_oferta"] += 1
        if a.get("pct_baja") is not None:
            x["bajas"].append(a["pct_baja"])
    for m in menores:
        principal = normaliza_cif(m.get("cif_adjudicatario"))
        if principal:
            x = g[principal]
            x["principal"] = True
            x["nombre_en_mercado"] = x["nombre_en_mercado"] or m.get("adjudicatario")
            x["menores"] += 1
            x["importe_menores"] += m.get("importe_sin_iva") or 0.0
        for sec in (m.get("cifs_adjudicatarios") or []):
            s = normaliza_cif(sec)
            if s and s != principal:
                g[s]["menores_secundario"] += 1
    return g


def bloque4_adjudicatarios(sb: Supabase, adjudicaciones: list[dict], menores: list[dict]) -> list[dict]:
    agregados = agrega_adjudicatarios(adjudicaciones, menores)
    cifs = sorted(agregados)
    fichas = {}
    for i in range(0, len(cifs), TROZO_IDS):
        trozo = cifs[i:i + TROZO_IDS]
        for f in sb.filas("competidores", f"select={CAMPOS_COMP}&cif={valor_url(en_lista(trozo))}&order=cif.asc"):
            fichas[f["cif"]] = f
    salida = []
    for cif in cifs:
        x = agregados[cif]
        f = fichas.get(cif)
        salida.append({
            "cif": cif,
            "nombre": f.get("nombre_canonico") if f else None,
            "nombre_en_mercado": x["nombre_en_mercado"],
            "en_competidores": bool(f),
            "cif_con_forma_valida": cif_con_forma_valida(cif),
            "solo_secundario": not x["principal"],
            "n_expedientes": f.get("n_expedientes") if f else None,
            "n_lotes": f.get("n_lotes") if f else None,
            "importe_total_sin_iva": num(f.get("importe_total_sin_iva")) if f else None,
            "pct_una_oferta": num(f.get("pct_una_oferta")) if f else None,
            "primera_adjudicacion": f.get("primera_adjudicacion") if f else None,
            "ultima_adjudicacion": f.get("ultima_adjudicacion") if f else None,
            "n_licitaciones_mercado": len(x["licitaciones"]),
            "n_lotes_mercado": x["lotes"],
            "importe_mercado_sin_iva": round(x["importe"], 2),
            "n_lotes_precios_unitarios_mercado": x["lotes_unitarios"],
            "importe_tarifas_unitarias_mercado": round(x["tarifas"], 2),
            "n_con_ofertas_mercado": x["con_ofertas"],
            "n_sin_ofertas_mercado": x["sin_ofertas"],
            "n_una_oferta_mercado": x["una_oferta"],
            "pct_una_oferta_mercado": (round(100.0 * x["una_oferta"] / x["con_ofertas"], 1)
                                       if x["con_ofertas"] else None),
            "baja_media_pct_mercado": (round(sum(x["bajas"]) / len(x["bajas"]), 2) if x["bajas"] else None),
            "n_con_baja_calculable": len(x["bajas"]),
            "n_menores_mercado": x["menores"],
            "importe_menores_mercado": round(x["importe_menores"], 2),
            "n_menores_secundario_mercado": x["menores_secundario"],
        })
    salida.sort(key=lambda s: (-s["importe_mercado_sin_iva"], -s["n_lotes_mercado"], s["cif"]))
    return salida


# ---------------------------------------------------------------------------
# Agregados para el MD y los metadatos (por lote DISTINTO, no por fila)
# ---------------------------------------------------------------------------
def agrega_por_clave(licitaciones: list[dict], adjudicaciones: list[dict], clave: str) -> list[dict]:
    """Por órgano o por comunidad: nº licitaciones, presupuesto (bloque 1) e importe adjudicado real
    y a tarifa (bloque 2, contando cada (licitacion_id, lote) UNA vez)."""
    g = defaultdict(lambda: {"n": 0, "presupuesto": 0.0, "con_presupuesto": 0, "importe": 0.0,
                             "lotes": 0, "lotes_tarifa": 0, "tarifas": 0.0, "lotes_desiertos": 0,
                             "con_resultado": 0})
    for l in licitaciones:
        k = l.get(clave) or "(sin dato)"
        x = g[k]
        x["n"] += 1
        if l.get("presupuesto_sin_iva") is not None:
            x["presupuesto"] += l["presupuesto_sin_iva"]
            x["con_presupuesto"] += 1
        if l.get("tiene_resultado"):
            x["con_resultado"] += 1
    vistos = set()
    for a in adjudicaciones:
        k = a.get(clave) or "(sin dato)"
        lote_k = (a["licitacion_id"], a.get("lote") or "")
        if lote_k in vistos:
            continue
        vistos.add(lote_k)
        x = g[k]
        if a.get("es_desierto"):
            x["lotes_desiertos"] += 1
            continue
        if not normaliza_cif(a.get("cif_adjudicatario")):
            continue
        x["lotes"] += 1
        imp = a.get("importe_sin_iva") or 0.0
        if a.get("es_unitario"):
            x["lotes_tarifa"] += 1
            x["tarifas"] += imp
        else:
            x["importe"] += imp
    salida = [{clave: k, "n_licitaciones": x["n"], "n_con_resultado": x["con_resultado"],
               "presupuesto_sin_iva": round(x["presupuesto"], 2) if x["con_presupuesto"] else None,
               "importe_adjudicado_sin_iva": round(x["importe"], 2),
               "n_lotes_adjudicados": x["lotes"], "n_lotes_precios_unitarios": x["lotes_tarifa"],
               "importe_tarifas_unitarias": round(x["tarifas"], 2), "n_lotes_desiertos": x["lotes_desiertos"]}
              for k, x in g.items()]
    salida.sort(key=lambda s: (-(s["presupuesto_sin_iva"] or 0), -s["importe_adjudicado_sin_iva"], str(s[clave])))
    return salida


def resumen_adjudicaciones(adjudicaciones: list[dict], licitaciones: list[dict]) -> dict:
    """Recuentos declarados del bloque 2: desiertas por tres medidas distintas (lote, licitación
    total, licitación parcial), discrepancias, fechas, tarifas, CIF."""
    lotes = {(a["licitacion_id"], a.get("lote") or "") for a in adjudicaciones}
    por_lote = Counter((a["licitacion_id"], a.get("lote") or "") for a in adjudicaciones)
    sin_ganador_otros = Counter(a.get("resultado_code") or "(sin código)" for a in adjudicaciones
                                if not normaliza_cif(a.get("cif_adjudicatario"))
                                and not a.get("es_desierto") and not a.get("es_retirado"))
    sin_fecha_por_fuente = Counter((a.get("fuente") or "(sin fuente)") for a in adjudicaciones
                                   if not a.get("fecha_adjudicacion"))
    fechas = sorted(str(a["fecha_adjudicacion"])[:10] for a in adjudicaciones if a.get("fecha_adjudicacion"))
    desfases = []
    for a in adjudicaciones:
        if a.get("fecha_adjudicacion") and a.get("fecha_publicacion"):
            try:
                d1 = datetime.fromisoformat(str(a["fecha_publicacion"])[:10])
                d2 = datetime.fromisoformat(str(a["fecha_adjudicacion"])[:10])
                desfases.append((d2 - d1).days)
            except ValueError:
                pass
    desfases.sort()
    estado_lic = {l["licitacion_id"]: l.get("estado_adjudicacion") for l in licitaciones}
    con_lotes = defaultdict(lambda: {"gan": 0, "des": 0})
    for a in adjudicaciones:
        x = con_lotes[a["licitacion_id"]]
        if normaliza_cif(a.get("cif_adjudicatario")):
            x["gan"] += 1
        elif a.get("es_desierto"):
            x["des"] += 1
    discrepancias = 0
    for lid, x in con_lotes.items():
        esperado = ("desierta_total" if x["gan"] == 0 and x["des"] > 0 else
                    "desierta_parcial" if x["gan"] > 0 and x["des"] > 0 else
                    "adjudicada" if x["gan"] > 0 else None)
        if esperado and estado_lic.get(lid) and estado_lic[lid] != esperado:
            discrepancias += 1
    cifs = [normaliza_cif(a.get("cif_adjudicatario")) for a in adjudicaciones]
    return {
        "n_filas": len(adjudicaciones),
        "n_lotes_distintos": len(lotes),
        "n_lotes_multiadjudicatario": sum(1 for v in por_lote.values() if v > 1),
        "n_licitaciones_con_resultado": len({a["licitacion_id"] for a in adjudicaciones}),
        "n_lotes_desiertos": sum(1 for a in adjudicaciones if a.get("es_desierto")),
        "n_lotes_retirados": sum(1 for a in adjudicaciones if a.get("es_retirado")),
        "n_lotes_sin_ganador_otros_por_codigo": dict(sin_ganador_otros),
        "n_licitaciones_desierta_total": sum(1 for l in licitaciones if l.get("estado_adjudicacion") == "desierta_total"),
        "n_licitaciones_desierta_parcial": sum(1 for l in licitaciones if l.get("estado_adjudicacion") == "desierta_parcial"),
        "n_discrepancias_estado_vs_lotes": discrepancias,
        "n_sin_fecha_adjudicacion_por_fuente": dict(sin_fecha_por_fuente),
        "rango_fecha_adjudicacion": {"min": fechas[0] if fechas else None, "max": fechas[-1] if fechas else None},
        "n_con_fecha_adjudicacion_futura": sum(1 for a in adjudicaciones if a.get("fecha_adjudicacion_futura")),
        "n_ofertas_null": sum(1 for a in adjudicaciones if a.get("n_ofertas") is None),
        "n_baja_null_lote_sin_presupuesto": sum(1 for a in adjudicaciones if a.get("baja_aviso")),
        "n_precios_unitarios": sum(1 for a in adjudicaciones if a.get("es_unitario")),
        "n_lotes_sin_cif": sum(1 for c in cifs if not c),
        "n_cif_sin_forma_valida": len({c for c in cifs if c and not cif_con_forma_valida(c)}),
        "desfase_publicacion_adjudicacion_dias": {
            "n": len(desfases),
            "n_negativos": sum(1 for d in desfases if d < 0),
            "n_cero": sum(1 for d in desfases if d == 0),
            "n_positivos": sum(1 for d in desfases if d > 0),
            "mediana": statistics.median(desfases) if desfases else None,
            "p90": desfases[min(len(desfases) - 1, int(0.9 * len(desfases)))] if desfases else None,
            "nota": ("NO interpretable como plazo de adjudicación: en la fuente estatal fecha_publicacion es "
                     "la del ÚLTIMO aviso del expediente (a menudo el de adjudicación o formalización), por "
                     "eso hay desfases cero y negativos. La tabla no guarda la fecha del primer anuncio."),
        },
    }


def posibles_duplicados(licitaciones: list[dict]) -> list[dict]:
    """Mismo expediente publicado por dos sindicaciones (estatal y agregadas) tendría dos ids. Se
    agrupa por nº de expediente normalizado (misma regla que norm_expediente) y órgano; se lista,
    NO se fusiona."""
    grupos = defaultdict(list)
    for l in licitaciones:
        exp = re.sub(r"[\s/.\-]", "", str(l.get("num_expediente") or "").upper())
        if len(exp) < 3:
            continue
        org = re.sub(r"\s+", " ", sin_diacriticos(str(l.get("organo_contratacion") or "")).lower()).strip()
        grupos[(exp, org)].append(l)
    salida = []
    for (exp, org), filas in grupos.items():
        if len(filas) > 1:
            salida.append({"num_expediente_normalizado": exp, "organo_contratacion": filas[0].get("organo_contratacion"),
                           "ids": [f["licitacion_id"] for f in filas], "fuentes": [f.get("fuente") for f in filas]})
    return salida


# ---------------------------------------------------------------------------
# Bloque 5 · metadatos (catálogo sin el conteo de 17 s)
# ---------------------------------------------------------------------------
def catalogo(sb: Supabase) -> dict:
    """Tamaño del catálogo y reparto por año, ESTIMADOS por el planificador (count=planned).

    POR QUÉ estimados: medido el 12/10/2026, el conteo exacto de todo el catálogo tarda 17,75 s y
    el de un solo año con 215.000 filas, 18,2 s (índice + comprobación de visibilidad fila a fila):
    rompen el límite de 15 s por petición y se comen 40 s de un informe que tarda 15. La
    estimación del planificador (ANALYZE) sale en 0,1 s y se desvió un 0,03 % del exacto
    (642.652 frente a 642.843). Es contexto, no dato del mercado; se declara como estimación.
    Lo que sí es exacto y barato: la primera y última fecha (una fila cada una) y las filas sin
    fecha de publicación (1.423, por índice)."""
    prim = sb.una("licitaciones", "select=fecha_publicacion&fecha_publicacion=not.is.null&order=fecha_publicacion.asc")
    ult = sb.una("licitaciones", "select=fecha_publicacion&order=fecha_publicacion.desc.nullslast")
    desde = (prim or {}).get("fecha_publicacion")
    hasta = (ult or {}).get("fecha_publicacion")
    por_anno = []
    if desde and hasta:
        for anno in range(int(desde[:4]), int(hasta[:4]) + 1):
            n = cuenta_planned(sb, "licitaciones",
                               f"fecha_publicacion=gte.{anno}-01-01&fecha_publicacion=lt.{anno + 1}-01-01")
            por_anno.append({"anno": anno, "n_licitaciones_estimadas": n})
    sin_fecha = sb.cuenta("licitaciones", "fecha_publicacion=is.null")
    estimado = cuenta_planned(sb, "licitaciones", "")
    return {"total_estimado": estimado, "sin_fecha_publicacion_exacto": sin_fecha,
            "por_anno_estimado": por_anno, "ventana_catalogo": {"desde": desde, "hasta": hasta},
            "nota": "Total y reparto por año son ESTIMACIONES del planificador (count=planned, año UTC): el "
                    "conteo exacto tarda 15-18 s por petición (medido el 12/10/2026) y rompería el límite de "
                    "15 s; desviación medida frente al exacto, 0,03 % en el total. En los años con muy pocas "
                    "filas la estimación es gruesa (el planificador no baja de su mínimo). Las filas sin fecha "
                    "sí son exactas."}


# ---------------------------------------------------------------------------
# Salida MD
# ---------------------------------------------------------------------------
def pct(parte, total) -> str:
    return "—" if not total else f"{100.0 * parte / total:.1f} %"


def escribe_md(inf: dict) -> str:
    m = inf["metadatos"]
    lic, adj, men, adjs = inf["licitaciones"], inf["adjudicaciones"], inf["menores"], inf["adjudicatarios"]
    v = m["ventana"]
    p = [f"# Informe de mercado — {m['nombre_etiqueta']}", "",
         f"Consultado el {m['consultado_en'][:19].replace('T', ' ')} (UTC) · ventana "
         f"{v['desde_efectivo'][:10]} → {v['hasta'][:10]} ({v['meses_solicitados']} meses"
         f"{' · --desde manda' if v.get('motivo') else ''}) · CPV: {', '.join(m['cpv']['codigos']) or '—'} · "
         f"palabras: {', '.join(m['texto']['incluye']) or '—'}"
         f"{' · excluye: ' + ', '.join(m['texto']['excluye']) if m['texto']['excluye'] else ''}", "",
         f"> Criterio de pertenencia = CPV ∪ texto. {m['texto']['mecanismo']}", ""]

    p += ["## 1 · Recuentos por criterio", ""]
    c = m["filas_por_criterio"]
    p.append(tabla_md(["Licitaciones", "Solo CPV", "Solo texto", "Ambos", "Total"],
                      [["bloque 1", c["solo_cpv"], c["solo_texto"], c["ambos"], c["total"]]]))
    p.append(f"Buscando solo por código se pierden **{c['solo_texto']}** licitaciones "
             f"({pct(c['solo_texto'], c['total'])} de la unión).\n")
    filas_men = [[fuente, d["solo_cpv"], d["solo_texto"], d["ambos"], d["total"]]
                 for fuente, d in m["filas_por_criterio_menores"].items()]
    p.append(tabla_md(["Menores por fuente", "Solo CPV", "Solo texto", "Ambos", "Total"], filas_men))
    if m["excluidas_por_texto"]["licitaciones"] or m["excluidas_por_texto"]["menores"]:
        p.append(f"Excluidas por `--excluye` (solo filas que entraron únicamente por texto): "
                 f"{m['excluidas_por_texto']['licitaciones']} licitaciones, {m['excluidas_por_texto']['menores']} menores.\n")
    ra = m["adjudicaciones"]
    p.append(f"Adjudicaciones: **{ra['n_filas']}** filas ({ra['n_lotes_distintos']} lotes distintos, "
             f"{ra['n_lotes_multiadjudicatario']} con varios adjudicatarios) de {ra['n_licitaciones_con_resultado']} "
             f"licitaciones con resultado. Lotes desiertos (código 3): **{ra['n_lotes_desiertos']}**; retirados: "
             f"{ra['n_lotes_retirados']}. Licitaciones desiertas: {ra['n_licitaciones_desierta_total']} totales y "
             f"{ra['n_licitaciones_desierta_parcial']} parciales (estado agregado). Lotes a precios unitarios "
             f"(AM/SDA): {ra['n_precios_unitarios']}, cuyos importes NO se suman al importe adjudicado.\n")

    p += ["## 2 · Quién compra (top órganos, por presupuesto licitado)", ""]
    p.append(tabla_md(["Órgano", "Licit.", "Con resultado", "Presupuesto s/IVA", "Adjudicado real s/IVA",
                       "Lotes adj.", "Lotes a tarifa", "Lotes desiertos"],
                      [[o["organo_contratacion"][:70], o["n_licitaciones"], o["n_con_resultado"],
                        eur(o["presupuesto_sin_iva"]), eur(o["importe_adjudicado_sin_iva"]),
                        o["n_lotes_adjudicados"], o["n_lotes_precios_unitarios"], o["n_lotes_desiertos"]]
                       for o in inf["por_organo"][:TOP_ORGANOS]]))
    p.append(f"_{len(inf['por_organo'])} órganos en total. «Adjudicado real» cuenta cada lote una vez y "
             "deja fuera los precios unitarios de acuerdos marco y sistemas dinámicos; «Lotes adj.» incluye "
             "los lotes a tarifa._\n")

    p += ["## 3 · Quién gana (top adjudicatarios, por importe adjudicado real)", ""]
    p.append(tabla_md(["Adjudicatario", "CIF", "Licit.", "Lotes", "Importe real s/IVA", "Lotes a tarifa",
                       "% 1 oferta (n)", "Baja media (n)", "Menores", "Ficha global: lotes / importe / % 1 of."],
                      [[(a["nombre"] or a["nombre_en_mercado"] or "")[:45],
                        a["cif"] + ("" if a["cif_con_forma_valida"] else " *"),
                        a["n_licitaciones_mercado"], a["n_lotes_mercado"], eur(a["importe_mercado_sin_iva"]),
                        a["n_lotes_precios_unitarios_mercado"],
                        ("—" if a["pct_una_oferta_mercado"] is None else f"{a['pct_una_oferta_mercado']} % ({a['n_con_ofertas_mercado']})"),
                        ("—" if a["baja_media_pct_mercado"] is None else f"{a['baja_media_pct_mercado']:.1f} % ({a['n_con_baja_calculable']})"),
                        a["n_menores_mercado"],
                        (f"{'—' if a['n_lotes'] is None else a['n_lotes']} / {eur(a['importe_total_sin_iva'])} / "
                         f"{'—' if a['pct_una_oferta'] is None else str(a['pct_una_oferta']) + ' %'}"
                         if a["en_competidores"] else "no está en competidores")]
                       for a in adjs[:TOP_ADJUDICATARIOS]]))
    p.append(f"_{len(adjs)} CIF distintos. `*` = CIF sin forma válida (UTE o dato de origen). «Lotes» incluye los "
             "lotes a tarifa. «% 1 oferta» del mercado divide por los lotes con nº de ofertas conocido; la ficha "
             "global de `competidores` divide por todos sus lotes: no son la misma medida._\n")

    p += ["## 4 · Dónde (por comunidad autónoma)", ""]
    cob = m["cobertura_ccaa"]
    p.append(f"> {cob['aviso']}\n")
    p.append(tabla_md(["Comunidad", "Licit.", "Presupuesto s/IVA", "Adjudicado real s/IVA", "Lotes adj.", "Lotes desiertos"],
                      [[o["ccaa"], o["n_licitaciones"], eur(o["presupuesto_sin_iva"]), eur(o["importe_adjudicado_sin_iva"]),
                        o["n_lotes_adjudicados"], o["n_lotes_desiertos"]] for o in inf["por_ccaa"]]))

    p += [f"## 5 · Las {TOP_LICITACIONES} licitaciones mayores (por presupuesto s/IVA; si falta, valor estimado)", ""]
    mayores = sorted(lic, key=lambda l: ((l.get("presupuesto_sin_iva") if l.get("presupuesto_sin_iva") is not None
                                          else (l.get("valor_estimado") or 0)), l["licitacion_id"]), reverse=True)
    ids_dup = {i for g in m["posibles_duplicados"] for i in g["ids"]}
    p.append(tabla_md(["Publicada", "Título", "Órgano", "Comunidad", "Presupuesto", "Valor estimado", "Criterio",
                       "Estado", "Sistema", "Lotes c/ res.", "Fuente", "Dup", "Enlace"],
                      [[(l.get("fecha_publicacion") or "")[:10], (l.get("titulo") or "")[:70],
                        (l.get("organo_contratacion") or "")[:45], l.get("ccaa"), eur(l.get("presupuesto_sin_iva")),
                        eur(l.get("valor_estimado")), l["criterio"], l.get("estado_adjudicacion"),
                        l.get("sistema_contratacion_expediente_rotulo") or l.get("sistema_contratacion_expediente"),
                        l.get("n_lotes_con_resultado"), l.get("fuente"),
                        ("sí" if l["licitacion_id"] in ids_dup else ""), l.get("enlace")]
                       for l in mayores[:TOP_LICITACIONES]]))
    if ids_dup:
        p.append("_«Dup» = mismo nº de expediente y órgano que otra fila del universo (ver posibles_duplicados "
                 "en metadatos): no sumar las dos._\n")

    p += ["## 6 · Contratos menores del mercado", "", f"> ⚠️ {m['fuentes']['menores_aviso']}", ""]
    p.append(f"**Por fuente:** " + " · ".join(
        f"{d['etiqueta']}: {d['n']} {'menor' if d['n'] == 1 else 'menores'}, {eur(d['importe_total_sin_iva'])}"
        for d in inf["menores_por_fuente"]) + "\n" if inf["menores_por_fuente"] else "_Sin menores._\n")
    p.append(tabla_md(["Órgano comprador", "Nº menores", "Importe s/IVA"],
                      [[o["organo_contratacion"][:70], o["n_menores"], eur(o["importe_sin_iva"])]
                       for o in inf["menores_top_organos"][:TOP_ORGANOS_MENORES]]))
    p.append(f"_{m['filas_por_bloque']['3b_menores_sin_fecha']} menores sin fecha de adjudicación casan los "
             "criterios y van en `menores_sin_fecha`, fuera de la ventana y sin sumar. Los importes de menores "
             "NO se suman con los del bloque de adjudicaciones._\n")

    p += ["## 7 · Nota de fuentes y método", ""]
    cat = m["catalogo"]
    p.append(tabla_md(["Dato", "Valor"], [
        ["Consultado", m["consultado_en"]],
        ["Catálogo (filas, estimación del planificador)", f"{cat['total_estimado'] or 0:,}".replace(",", ".")
         + f" · sin fecha de publicación (exacto): {cat['sin_fecha_publicacion_exacto']}"],
        ["Ventana del catálogo", f"{(cat['ventana_catalogo']['desde'] or '')[:10]} → {(cat['ventana_catalogo']['hasta'] or '')[:10]}"],
        ["Licitaciones sin fecha de publicación que casan", m["filas_por_bloque"]["1b_licitaciones_sin_fecha"]],
        ["Peticiones HTTP", m["peticiones_http"]],
        ["Segundos en consultas", m["segundos_en_consultas"]],
        ["Consulta más lenta", f"{m['consulta_mas_lenta']['segundos']} s · {m['consulta_mas_lenta']['que']}"],
        ["Adjudicaciones sin fecha de adjudicación (por fuente)", json.dumps(ra["n_sin_fecha_adjudicacion_por_fuente"], ensure_ascii=False)],
        ["Desfase publicación → adjudicación (días) · NO interpretable, ver nota en metadatos",
         f"mediana {ra['desfase_publicacion_adjudicacion_dias']['mediana']} · p90 {ra['desfase_publicacion_adjudicacion_dias']['p90']} "
         f"(n={ra['desfase_publicacion_adjudicacion_dias']['n']}, negativos {ra['desfase_publicacion_adjudicacion_dias']['n_negativos']})"],
        ["Bajas a null por lote numerado sin presupuesto de lote", ra["n_baja_null_lote_sin_presupuesto"]],
        ["Posibles duplicados (mismo expediente y órgano)",
         f"{len(m['posibles_duplicados'])} grupos · por fuentes: "
         + (", ".join(f"{k}: {n}" for k, n in sorted(Counter('/'.join(sorted(set(str(x) for x in g['fuentes'])))
                                                              for g in m["posibles_duplicados"]).items())) or "—")],
        ["Tiempo total del script (s)", m.get("segundos_totales")],
    ]))
    p.append("\n**Para recortar «últimos 12 meses» a mano**: use `fecha_referencia` del bloque de adjudicaciones "
             "(fecha de adjudicación si existe; si no, la de publicación de la licitación). "
             f"{sum(ra['n_sin_fecha_adjudicacion_por_fuente'].values())} filas van por publicación.\n")
    p.append(f"\n- Texto: {m['texto']['mecanismo']}\n- Pérdida conocida: {m['texto']['perdida_conocida']}\n"
             f"- tsquery licitaciones: `{m['texto']['tsquery_licitaciones'] or '—'}` · regex servidor: "
             f"`{(m['texto']['regex_servidor'] or '—')[:120]}`\n"
             f"- {m['notas_metodo']['criterio']}\n- {m['notas_metodo']['excluye']}\n- {m['notas_metodo']['derivados']}\n"
             f"- {m['notas_metodo']['baja']}\n- {m['notas_metodo']['tarifas']}\n- {m['fuentes']['adjudicaciones']}\n")
    p += ["", "**Filas devueltas por bloque**", ""]
    p.append(tabla_md(["Bloque", "Filas"], [[k, v] for k, v in m["filas_por_bloque"].items()]))
    return "\n".join(p) + "\n"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def slug(nombre: str) -> str:
    base = sin_diacriticos(str(nombre or "")).lower().replace("ñ", "n")
    base = re.sub(r"[^a-z0-9_-]+", "-", base).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    return base or "mercado"


def main() -> None:
    ap = argparse.ArgumentParser(description="Vuelca a disco los datos de un mercado (CPV ∪ texto) para "
                                             "un estudio por producto.")
    ap.add_argument("nombre", help="etiqueta libre que da nombre a los ficheros (p. ej. camas-hospitalarias)")
    ap.add_argument("--cpv", required=True,
                    help="CPV separados por comas: códigos exactos (33192000) y PREFIJOS (331921), que se "
                         "expanden con el vocabulario del repo")
    ap.add_argument("--incluye", default="", help="palabras o frases separadas por comas: la fila entra si "
                                                  "alguna aparece como palabra completa en titulo u objeto")
    ap.add_argument("--excluye", default="", help="palabras o frases que sacan la fila SI entró solo por texto")
    ap.add_argument("--meses", type=int, default=24, help="ventana por fecha_publicacion (por defecto 24)")
    ap.add_argument("--desde", default="", help="AAAA-MM-DD: fecha inicial explícita (manda sobre --meses)")
    ap.add_argument("--salida", default="", help="carpeta de salida (por defecto, la del proyecto)")
    args = ap.parse_args()

    if args.meses < 1:
        sys.exit("ERROR: --meses tiene que ser 1 o más.")
    url, clave, dir_env = credenciales()
    sb = Supabase(url, clave)
    arranque = time.time()
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    # Fecha de Madrid, no la UTC: entre las 00:00 y las 02:00 la UTC aún es ayer y una
    # adjudicación fechada hoy saldría como «futura» (misma regla que informe_empresa.py).
    hoy = ie.hoy_madrid(ahora).isoformat()
    motivo_ventana = None
    if args.desde:
        try:
            desde = datetime.strptime(args.desde, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            sys.exit("ERROR: --desde tiene que ser AAAA-MM-DD.")
        if desde >= ahora:
            sys.exit("ERROR: --desde tiene que ser anterior a hoy.")
        motivo_ventana = f"--desde {args.desde} manda sobre --meses {args.meses}"
        print(f"· AVISO: {motivo_ventana}")
    else:
        desde = resta_meses(ahora, args.meses)

    # --- CPV y texto -------------------------------------------------------
    cpvs, expansion = expande_cpv([c for c in args.cpv.split(",") if c.strip()])
    incluye, rechazados_inc = prepara_terminos(args.incluye)
    excluye, rechazados_exc = prepara_terminos(args.excluye)
    if rechazados_inc or rechazados_exc:
        sys.exit("ERROR: términos no válidos (solo letras, dígitos, ñ, espacios y guiones): "
                 + ", ".join(repr(t) for t in rechazados_inc + rechazados_exc))
    if len(cpvs) > TOPE_CPV:
        sys.exit(f"ERROR: --cpv se expande a {len(cpvs)} códigos (tope {TOPE_CPV}): la URL no cabe. "
                 "Afina el prefijo (p. ej. 331921 en vez de 33).")
    if len(cpvs) > AVISO_CPV:
        print(f"· AVISO: --cpv se expande a {len(cpvs)} códigos; la consulta de menores lleva un ilike por "
              "código y será más lenta.")
    tsq_lic = construye_tsquery(incluye, "AB") if incluye else None
    tsq_men = construye_tsquery(incluye, "A") if incluye else None
    regex_srv = construye_regex_servidor(incluye) if incluye else None
    rx_cliente = construye_regex_cliente(incluye) if incluye else None
    rx_excluye = construye_regex_cliente(excluye) if excluye else None

    print(f"· Mercado «{args.nombre}» · ventana desde {desde.isoformat()[:10]} · {len(cpvs)} CPV · "
          f"{len(incluye)} términos" + (f" · {len(excluye)} excluidos" if excluye else ""))
    if not cpvs and not incluye:
        print("· AVISO: ni CPV ni palabras que buscar: los bloques saldrán vacíos.")

    # Términos sin ninguna candidata en TODO el catálogo (stopword o palabra inexistente): se avisa.
    sin_candidatas = []
    for t in incluye:
        q = construye_tsquery([t], "AB")
        if not sb._pide("licitaciones", f"select=licitacion_id&tsv=fts(spanish).{valor_url(q)}&limit=1").json():
            sin_candidatas.append(t["entrada"])
    if sin_candidatas:
        print(f"· AVISO: sin candidatas por FTS en todo el catálogo (¿palabra vacía del español o inexistente?): "
              + ", ".join(sin_candidatas))

    # --- Bloques -----------------------------------------------------------
    licitaciones, info_lic = bloque1_licitaciones(sb, cpvs, tsq_lic, regex_srv, rx_cliente, rx_excluye,
                                                  bool(excluye), desde, ahora)
    lic_sin_fecha, info_lic_sf = bloque1_licitaciones(sb, cpvs, tsq_lic, regex_srv, rx_cliente, rx_excluye,
                                                      bool(excluye), desde, ahora, campo_fecha_nulo=True)
    crit_lic = filas_por_criterio(licitaciones)
    print(f"· bloque 1: {crit_lic['total']} licitaciones (solo CPV {crit_lic['solo_cpv']} · solo texto "
          f"{crit_lic['solo_texto']} · ambos {crit_lic['ambos']}) · {len(lic_sin_fecha)} sin fecha aparte")

    adjudicaciones = bloque2_adjudicaciones(sb, licitaciones + lic_sin_fecha, hoy)
    deriva_bloque1(licitaciones, adjudicaciones)
    deriva_bloque1(lic_sin_fecha, adjudicaciones)
    ids_ventana = {l["licitacion_id"] for l in licitaciones}
    adj_ventana = [a for a in adjudicaciones if a["licitacion_id"] in ids_ventana]
    adj_sin_fecha = [a for a in adjudicaciones if a["licitacion_id"] not in ids_ventana]
    res_adj = resumen_adjudicaciones(adj_ventana, licitaciones)
    print(f"· bloque 2: {res_adj['n_filas']} adjudicaciones · {res_adj['n_lotes_desiertos']} lotes desiertos")

    fuentes = lee_fuentes_menores()
    menores, info_men = bloque3_menores(sb, cpvs, tsq_men, regex_srv, rx_cliente, rx_excluye, bool(excluye),
                                        desde, ahora)
    men_sin_fecha, info_men_sf = bloque3_menores(sb, cpvs, tsq_men, regex_srv, rx_cliente, rx_excluye,
                                                 bool(excluye), desde, ahora, sin_fecha=True)
    men_por_fuente = desglose_por_fuente(menores, fuentes)
    print(f"· bloque 3: {len(menores)} menores (" + " · ".join(f"{d['etiqueta']} {d['n']}" for d in men_por_fuente)
          + f") · {len(men_sin_fecha)} sin fecha aparte")

    adjudicatarios = bloque4_adjudicatarios(sb, adj_ventana, menores)
    print(f"· bloque 4: {len(adjudicatarios)} CIF distintos")

    # --- Agregados y metadatos ----------------------------------------------
    por_organo = agrega_por_clave(licitaciones, adj_ventana, "organo_contratacion")
    por_ccaa = agrega_por_clave(licitaciones, adj_ventana, "ccaa")
    n_sin_ccaa = sum(1 for l in licitaciones if not l.get("ccaa"))
    con_ccaa = len(licitaciones) - n_sin_ccaa
    pct_ccaa = round(100.0 * con_ccaa / len(licitaciones), 1) if licitaciones else None
    cobertura_ccaa = {"n_sin_ccaa": n_sin_ccaa, "pct_con_ccaa": pct_ccaa, "aviso": (
        f"El desglose por comunidad cubre el {pct_ccaa}% de estas licitaciones ({con_ccaa} de {len(licitaciones)}). "
        "El resto no lleva comunidad en la fuente: son contratos de ámbito nacional o con un código territorial "
        "que no identifica una sola comunidad; van en la fila «(sin dato)» en vez de asignarles una inventada."
        if licitaciones else "Sin licitaciones.")}
    organos_men = defaultdict(lambda: {"n": 0, "importe": 0.0})
    for mrow in menores:
        k = mrow.get("organo_contratacion") or "(sin órgano)"
        organos_men[k]["n"] += 1
        organos_men[k]["importe"] += mrow.get("importe_sin_iva") or 0.0
    menores_top_organos = sorted(({"organo_contratacion": k, "n_menores": v["n"], "importe_sin_iva": round(v["importe"], 2)}
                                  for k, v in organos_men.items()), key=lambda x: -x["importe_sin_iva"])
    presentes = [d["fuente"] for d in men_por_fuente]
    cat = catalogo(sb)
    peticion_lenta = sb.mas_lenta[1]

    filas_por_bloque = {
        "1_licitaciones": len(licitaciones), "1b_licitaciones_sin_fecha": len(lic_sin_fecha),
        "2_adjudicaciones": len(adj_ventana), "2b_adjudicaciones_de_sin_fecha": len(adj_sin_fecha),
        "3_menores": len(menores), "3b_menores_sin_fecha": len(men_sin_fecha),
        "4_adjudicatarios": len(adjudicatarios),
    }
    metadatos = {
        "consultado_en": ahora.isoformat(),
        "nombre_etiqueta": args.nombre,
        "ventana": {"meses_solicitados": args.meses, "desde_efectivo": desde.isoformat(), "hasta": ahora.isoformat(),
                    "motivo": motivo_ventana,
                    "campo": {"licitaciones": "fecha_publicacion", "menores": "fecha_adjudicacion"},
                    "nota": "Las filas sin fecha en el campo de la ventana no entran en los bloques 1/3: van en "
                            "licitaciones_sin_fecha y menores_sin_fecha, contadas y no sumadas."},
        "cpv": {"entrada": args.cpv, "expansion": expansion, "codigos": cpvs},
        "texto": {"incluye": [t["entrada"] for t in incluye], "excluye": [t["entrada"] for t in excluye],
                  "terminos_normalizados": [t["base"] for t in incluye],
                  "tsquery_licitaciones": tsq_lic, "tsquery_menores": tsq_men,
                  "regex_servidor": regex_srv, "regex_cliente": rx_cliente.pattern if rx_cliente else None,
                  "terminos_sin_candidatas": sin_candidatas,
                  "mecanismo": MECANISMO_TEXTO, "perdida_conocida": PERDIDA_CONOCIDA},
        "catalogo": cat,
        "filas_por_bloque": filas_por_bloque,
        "filas_por_criterio": crit_lic,
        "filas_por_criterio_menores": filas_por_criterio_y_fuente(menores),
        "filas_por_criterio_sin_fecha": {"licitaciones": filas_por_criterio(lic_sin_fecha),
                                         "menores": filas_por_criterio(men_sin_fecha)},
        "licitaciones_con_resultado_por_criterio": dict(Counter(l["criterio"] for l in licitaciones if l["tiene_resultado"])),
        "excluidas_por_texto": {"licitaciones": info_lic["excluidas_por_texto"] + info_lic_sf["excluidas_por_texto"],
                                "menores": info_men["excluidas_por_texto"] + info_men_sf["excluidas_por_texto"]},
        "consultas": {"licitaciones": info_lic, "licitaciones_sin_fecha": info_lic_sf,
                      "menores": info_men, "menores_sin_fecha": info_men_sf},
        "topado": {"tope": TOPE_FILAS,
                   "nota": "Si una consulta llega al tope se declara aquí con la estimación del planificador de "
                           "cuántas filas había en realidad; nunca se recorta en silencio.",
                   "licitaciones": resumen_topado(info_lic), "menores": resumen_topado(info_men)},
        "adjudicaciones": res_adj,
        "cobertura_ccaa": cobertura_ccaa,
        "posibles_duplicados": posibles_duplicados(licitaciones),
        "peticiones_http": sb.peticiones,
        "peticiones_por_tabla": dict(sb.por_tabla),
        "segundos_en_consultas": round(sb.segundos, 2),
        "consulta_mas_lenta": {"que": sb.mas_lenta[0], "segundos": peticion_lenta,
                               "supera_limite": peticion_lenta > LIMITE_PETICION, "limite": LIMITE_PETICION},
        "fuentes": {
            "adjudicaciones": "Sindicaciones 643 (estatal) + 1044 (plataformas agregadas). La fuente agregada no trae "
                              "fecha de adjudicación: ver fecha_referencia.",
            "menores_aviso": aviso_fuente_menores(fuentes, presentes),
            "aviso": "Los importes de menores y de adjudicaciones NO se suman entre sí.",
        },
        "notas_metodo": {
            "criterio": "criterio: «cpv» si la licitación casa cpv && códigos; «texto» si la regex de palabra completa "
                        "casa en titulo/objeto (menores: objeto), evaluada en cliente sobre todas las filas; «ambos» si "
                        "las dos. Admite usos no hospitalarios de la palabra (municipio Camas, camas de vivero...): es "
                        "universo crudo, se criba a mano.",
            "excluye": "--excluye solo elimina filas que entraron únicamente por texto; las que también entran por CPV "
                       "se quedan con excluye_casa=true. filas_por_criterio se calcula sobre la lista final escrita.",
            "derivados": "tiene_resultado, n_lotes_con_resultado, n_filas_adjudicacion, tipo_contrato_expediente y "
                         "sistema_contratacion_expediente salen del bloque 2 (dato de expediente replicado en sus "
                         "lotes); null = sin resultado publicado, no «desconocido». tipo_contrato, procedimiento, "
                         "sistema_contratacion y n_lotes a nivel de licitación no existen en la tabla: null.",
            "baja": "pct_baja con calcula_baja() de informe_empresa.py: AM/SDA de precios unitarios -> null; sin "
                    "presupuesto de lote -> presupuesto del expediente solo si el contrato no tiene lotes; lote "
                    "numerado sin presupuesto de lote -> null (baja_aviso lo marca).",
            "tarifas": "Las adjudicaciones con es_unitario (sistema 1 o 2, o importe < 1.000 € sin sistema) son "
                       "precios unitarios: no se suman al importe adjudicado real; van en *_tarifas_unitarias. En un "
                       "lote con varios adjudicatarios cada ganador se lleva el lote entero en el bloque 4; en "
                       "órganos y comunidades el lote se cuenta una vez.",
            "pct_una_oferta": "pct_una_oferta (ficha de competidores) divide por TODOS los lotes del CIF, con los "
                              "n_ofertas nulos en el denominador; pct_una_oferta_mercado divide solo por los lotes "
                              "de este mercado con nº de ofertas conocido (n_con_ofertas_mercado). No son la misma "
                              "medida: no compararlas sin mirar los denominadores.",
            "duplicados": "posibles_duplicados agrupa filas con el mismo nº de expediente normalizado y el mismo "
                          "órgano (sean de la misma sindicación o de dos distintas); se listan, no se fusionan.",
        },
    }
    informe = {
        "nombre": slug(args.nombre), "licitaciones": licitaciones, "licitaciones_sin_fecha": lic_sin_fecha,
        "adjudicaciones": adj_ventana, "adjudicaciones_de_sin_fecha": adj_sin_fecha,
        "menores": menores, "menores_sin_fecha": men_sin_fecha, "menores_por_fuente": men_por_fuente,
        "menores_top_organos": menores_top_organos,
        "adjudicatarios": adjudicatarios, "por_organo": por_organo, "por_ccaa": por_ccaa,
        "metadatos": metadatos,
    }

    destino = Path(args.salida) if args.salida else (
        Path(dir_env) if dir_env else
        (ie.ONEDRIVE_INFORMES if ie.ONEDRIVE_INFORMES.parent.exists() else ie.RAIZ / "informes"))
    destino.mkdir(parents=True, exist_ok=True)
    sello = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ruta_json = destino / f"mercado_{slug(args.nombre)}_{sello}.json"
    ruta_md = destino / f"mercado_{slug(args.nombre)}_{sello}.md"
    total_s = round(time.time() - arranque, 1)       # antes de escribir: es lo que mide el criterio 2
    metadatos["segundos_totales"] = total_s
    metadatos["cumple_limites"] = {"total_menor_90s": total_s < 90.0,
                                   "ninguna_peticion_sobre_15s": peticion_lenta <= LIMITE_PETICION}
    ruta_json.write_text(json.dumps(informe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    ruta_md.write_text(escribe_md(informe), encoding="utf-8")
    print(f"\nOK · {ruta_json}\n   · {ruta_md}")
    print("\n== Cierre ==")
    print(f"1. Bloque 1: {crit_lic['total']} licitaciones · solo CPV {crit_lic['solo_cpv']} · solo texto "
          f"{crit_lic['solo_texto']} · ambos {crit_lic['ambos']}")
    print(f"2. Adjudicaciones: {res_adj['n_filas']} filas ({res_adj['n_lotes_distintos']} lotes) · lotes desiertos "
          f"{res_adj['n_lotes_desiertos']} · licitaciones desiertas {res_adj['n_licitaciones_desierta_total']} totales / "
          f"{res_adj['n_licitaciones_desierta_parcial']} parciales")
    print("3. Menores por fuente: " + (" · ".join(f"{d['etiqueta']} {d['n']} ({eur(d['importe_total_sin_iva'])})"
                                                 for d in men_por_fuente) or "ninguno"))
    print(f"4. CIF distintos: {len(adjudicatarios)}")
    print("5. Top 5 órganos (presupuesto): " + " · ".join(f"{o['organo_contratacion'][:40]} {eur(o['presupuesto_sin_iva'])}"
                                                           for o in por_organo[:5]))
    print("   Top 5 adjudicatarios (importe real): " + " · ".join(
        f"{(a['nombre'] or a['nombre_en_mercado'] or a['cif'])[:30]} {eur(a['importe_mercado_sin_iva'])}" for a in adjudicatarios[:5]))
    print(f"6. {sb.peticiones} peticiones · {total_s} s en total ({metadatos['segundos_en_consultas']} s en consultas) · "
          f"la más lenta {peticion_lenta} s" + (" ¡SUPERA 15 s!" if peticion_lenta > LIMITE_PETICION else ""))


if __name__ == "__main__":
    main()
