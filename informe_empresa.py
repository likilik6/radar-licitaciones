#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""informe_empresa.py · Vuelca a disco TODO lo que un informe de empresa necesita.

PARA QUÉ: preparar un informe comercial sobre una empresa obligaba a ir sacando los
datos a mano por pantalla (la web del Radar + el SQL Editor de Supabase). Este script
quita el navegador de la ecuación: recibe un CIF y deja en disco un JSON con todo y un
MD legible. Quien redacta lee el fichero de la carpeta; ni capturas, ni claves por chat.

    python informe_empresa.py B01947753
    python informe_empresa.py B01947753 --meses 36 --cpv 45441000,4544

SOLO LECTURA. El script hace GET contra PostgREST y nada más: ni INSERT, ni UPDATE, ni
DELETE, en ninguna circunstancia. La clave se lee de un .env LOCAL (ver CREDENCIALES) y
no se escribe nunca en el log ni en los ficheros de salida.

=============================================================================
LO QUE SE COMPROBÓ ANTES DE ESCRIBIR ESTO (PASO 0, medido el 14/09/2026 contra la
base real; si algo de esto cambia, el script hay que revisarlo):

· `licitaciones.cpv` es text[] y TIENE índice GIN (`licitaciones_cpv_gin`). Por eso se
  filtra SIEMPRE con `cpv && array[...]` (`cpv=ov.{...}` en PostgREST): 27 ms. Un
  `unnest(cpv)` con LIKE sobre las 624.204 filas NO termina: está prohibido aquí.
· `menores.cpv` NO tiene GIN: `cpv && ...` son 8,3 s de Seq Scan paralelo. La vía buena
  es `cpv_txt LIKE '% <codigo>%'`, que sí usa índice trigram (`menores_cpv_txt_trgm`):
  las MISMAS 395 filas en 409 ms. Es lo que ya hace menores_api.js.
· `licitaciones.ccaa` YA está poblada (re-backfill del 15/09/2026, derivada del código
  NUTS del CODICE; ver nuts.py). Antes era NULL en el 100% de las filas y el bloque 5
  solo podía desglosar por órgano. Ahora desglosa por LAS DOS cosas, porque no se
  sustituyen: hay 7.472 órganos y los que más licitan son entes estatales sin geografía
  (TRAGSA, Renfe, ADIF, Correos). NO llega al 100% ni llegará —un ~1,3% son de ámbito
  nacional o de código ambiguo— así que el informe declara SIEMPRE la cobertura.
· Umbral de genericidad (2% del catálogo = 12.484 licitaciones): hoy solo lo superan 2
  CPV de todo el vocabulario, así que es una red de seguridad, no un recorte habitual.
=============================================================================

CREDENCIALES · fichero `.env` en la raíz del repo (ya está en .gitignore):

    SUPABASE_URL=https://xxxxxxxx.supabase.co
    SUPABASE_SECRET_KEY=sb_secret_...

La *Secret key* mapea a `service_role`, que se salta la RLS: por eso NUNCA sale de este
equipo. Si falta el .env el script muere con un mensaje que dice qué falta, sin traza.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

RAIZ = Path(__file__).resolve().parent
CPV_VOCABULARIO = RAIZ / "data" / "cpv_nombres.json"   # 9.454 códigos; NO se consulta la BD

# Carpeta por defecto: la del proyecto en OneDrive, que es de donde se leen los informes.
# Se puede cambiar con --salida o con INFORMES_DIR en el .env. Si no existe ninguna, cae
# a ./informes junto al script.
ONEDRIVE_INFORMES = Path.home() / ("OneDrive - LODEPASL/LODEPA (NUEVO)/CONCURSO/"
                                   "RADAR-LICITACIONES/informes")

TIEMPO_ESPERA = 60           # s por petición HTTP
PAGINA = 1000                # filas por página de PostgREST
TROZO_IDS = 100              # ids por lote en los filtros in.(...)  (URLs cortas)
MAX_CPV = 15                 # CPV que entran en los bloques de mercado (tope del encargo)
PCT_GENERICO = 0.02          # CPV presente en >2% del catálogo = no describe nicho
CEROS_GENERICO = 6           # 6+ ceros finales = división/grupo del árbol CPV, no un nicho
TOPE_MERCADO = 20000         # filas de licitaciones que se traen para agregar (bloque 5)
TOPE_MENORES_NICHO = 3000    # filas de menores del nicho (bloque 3b)
TOP_ADJUDICATARIOS = 15

UMBRAL_AM = 1000             # mismo valor que COMP_UMBRAL_AM en generar_web.py
DESIERTAS = ("desierta_total", "desierta_parcial")


# ---------------------------------------------------------------------------
# Credenciales y cliente HTTP
# ---------------------------------------------------------------------------
def lee_env(ruta: Path) -> dict:
    """Lee un .env sencillo (CLAVE=valor). Sin dependencias nuevas: requirements.txt
    fija requests/PyYAML/lxml/tzdata y no vamos a añadir python-dotenv por esto."""
    datos = {}
    if not ruta.exists():
        return datos
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        datos[clave.strip()] = valor.strip().strip('"').strip("'")
    return datos


def credenciales() -> tuple[str, str, str | None]:
    entorno = lee_env(RAIZ / ".env")
    url = os.environ.get("SUPABASE_URL") or entorno.get("SUPABASE_URL")
    clave = os.environ.get("SUPABASE_SECRET_KEY") or entorno.get("SUPABASE_SECRET_KEY")
    faltan = [n for n, v in (("SUPABASE_URL", url), ("SUPABASE_SECRET_KEY", clave)) if not v]
    if faltan:
        sys.exit(
            "ERROR: falta " + " y ".join(faltan) + ".\n"
            f"Crea el fichero {RAIZ / '.env'} con estas dos líneas:\n"
            "    SUPABASE_URL=https://xxxxxxxx.supabase.co\n"
            "    SUPABASE_SECRET_KEY=sb_secret_...\n"
            "La Secret key está en Supabase → Project Settings → API keys.\n"
            "El .env ya está en .gitignore: no se sube al repo."
        )
    return url.rstrip("/"), clave, (os.environ.get("INFORMES_DIR") or entorno.get("INFORMES_DIR"))


class Supabase:
    """GET contra PostgREST. Cuenta las peticiones y el tiempo para el bloque 8, y
    mide cada consulta por separado (criterio: ninguna por encima de 10 s)."""

    def __init__(self, url: str, clave: str):
        self.url = url
        self.ses = requests.Session()
        self.ses.headers.update({
            "apikey": clave,
            "Authorization": "Bearer " + clave,
            "Accept": "application/json",
            "Accept-Profile": "public",
        })
        self.peticiones = 0
        self.por_tabla = defaultdict(int)
        self.segundos = 0.0
        self.mas_lenta = ("", 0.0)

    def _pide(self, tabla: str, params: str, cabeceras: dict | None = None):
        destino = f"{self.url}/rest/v1/{tabla}?{params}"
        arranque = time.time()
        try:
            r = self.ses.get(destino, headers=cabeceras or {}, timeout=TIEMPO_ESPERA)
        except requests.RequestException as err:
            sys.exit(f"ERROR de red consultando «{tabla}»: {err}")
        tardanza = time.time() - arranque
        self.peticiones += 1
        self.por_tabla[tabla] += 1
        self.segundos += tardanza
        if tardanza > self.mas_lenta[1]:
            self.mas_lenta = (f"{tabla}?{params[:90]}", round(tardanza, 2))
        if r.status_code >= 400:
            # El cuerpo del error de PostgREST NO lleva la clave; la cabecera sí, y no se
            # imprime nunca.
            sys.exit(f"ERROR {r.status_code} consultando «{tabla}»: {r.text[:300]}")
        return r

    def una(self, tabla: str, params: str) -> dict | None:
        """UNA fila, en UNA petición. No confundir con filas(): ésta no pagina.

        Existe por un fallo que costó 278 s: pedir la primera/última fecha con
        `filas(..., '...&limit=1')` metía DOS `limit` en la URL, PostgREST se quedaba
        con el de filas() (1000), el bucle veía la página llena y se recorría las
        624.204 filas del catálogo entero. Dos veces: 1.248 peticiones de más.
        """
        datos = self._pide(tabla, f"{params}&limit=1").json()
        return datos[0] if datos else None

    def filas(self, tabla: str, params: str, tope: int | None = None) -> list[dict]:
        """Todas las filas que casen, paginando. `tope` corta y lo dice quien llama."""
        if "limit=" in params:
            # Guardia contra el fallo de arriba: si quien llama ya puso un limit, la
            # paginación de aquí lo pisa y el resultado es un recorrido completo.
            raise ValueError(f"filas() pagina sola: quita el 'limit' de los params "
                             f"o usa una(). Recibido: {params[:120]}")
        salida, desde = [], 0
        while True:
            trozo = self._pide(tabla, f"{params}&limit={PAGINA}&offset={desde}").json()
            salida.extend(trozo)
            if len(trozo) < PAGINA:
                break
            desde += PAGINA
            if tope is not None and len(salida) >= tope:
                break
        return salida[:tope] if tope is not None else salida

    def cuenta(self, tabla: str, params: str) -> int:
        """Conteo SIN traerse las filas: Prefer count=exact + Range 0-0 y se lee la
        cabecera Content-Range. Sobre un subconjunto ya acotado por índice es barato."""
        cola = (params + "&") if params else ""
        r = self._pide(tabla, f"{cola}select=licitacion_id",
                       {"Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"})
        rango = r.headers.get("Content-Range", "")
        total = rango.split("/")[-1] if "/" in rango else ""
        return int(total) if total.isdigit() else 0


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def normaliza_cif(texto: str) -> str:
    """Igual que feeds.normaliza_cif y que compNormCif del front: los CIF se guardan en
    MAYÚSCULAS y sin espacios ni . / -, y las comparaciones son exactas."""
    return "".join(c for c in str(texto or "").upper() if c not in " ./-")


def en_lista(valores) -> str:
    """Valores para un filtro in.(...) de PostgREST, entrecomillados y escapados."""
    partes = ['"' + str(v).replace('"', '""') + '"' for v in valores]
    return "in.(" + ",".join(partes) + ")"


def resta_meses(momento: datetime, meses: int) -> datetime:
    """Resta meses recortando el día al último del mes destino.

    Con `replace(month=...)` a secas, un 31 de marzo menos 1 mes pedía «31 de febrero»
    y reventaba con ValueError. Pasa poco, pero pasa: no vamos a dejar que el informe
    se caiga según el día en que se lance.
    """
    total = momento.year * 12 + (momento.month - 1) - meses
    anno, mes = divmod(total, 12)
    mes += 1
    dia = min(momento.day, calendar.monthrange(anno, mes)[1])
    return momento.replace(year=anno, month=mes, day=dia)


def num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def es_unitario(sistema, importe) -> bool:
    """¿Los importes de esta adjudicación son PRECIOS UNITARIOS y no el valor del
    contrato? Réplica exacta de compEsUnitario (generar_web.py, D1.1):
       sistema_contratacion 1 (acuerdo marco) o 2 (sistema dinámico) -> tarifas;
       0, 3 o 4 -> importe real; sin dato -> heurística de E.5 (importe < 1.000 €)."""
    s = (sistema or "").strip()
    if s in ("1", "2"):
        return True
    if s in ("0", "3", "4"):
        return False
    imp = num(importe)
    return imp is not None and 0 < imp < UMBRAL_AM


def calcula_baja(adj: dict, presupuesto_expediente) -> dict:
    """% de BAJA de una adjudicación. Réplica de compBajaAdjHtml (generar_web.py).

    La base NO es solo `presupuesto_lote_sin_iva`: cuando el contrato no tiene lotes
    (el 59% de la tabla) cae al presupuesto del EXPEDIENTE, que es de donde sale el
    «baja 0,0 %» de Hardolass. Sin ese fallback el informe daría null y estaría mal.
    """
    if es_unitario(adj.get("sistema_contratacion"), adj.get("importe_sin_iva")):
        return {"pct_baja": None, "base": None, "origen_base": None,
                "nota": "Acuerdo Marco · precios unitarios (el % no aplica)"}
    lote = (adj.get("lote") or "").strip()
    base_lote = num(adj.get("presupuesto_lote_sin_iva"))
    if base_lote is not None:
        base, origen = base_lote, "lote"
    elif not lote:
        base, origen = num(presupuesto_expediente), "expediente"
    else:
        base, origen = None, None
    importe = num(adj.get("importe_sin_iva"))
    if base is None or base <= 0 or importe is None:
        return {"pct_baja": None, "base": None, "origen_base": None,
                "nota": "Sin presupuesto comparable: el CODICE no lo publica"}
    pct_sobre = 100.0 * importe / base
    return {"pct_baja": round(100.0 - pct_sobre, 2), "base": base, "origen_base": origen,
            "nota": "Sobre el presupuesto del " + ("lote (dato del CODICE)" if origen == "lote"
                                                   else "expediente (contrato sin lotes)")}


def expande_cpv(entrada: list[str]) -> tuple[list[str], list[dict]]:
    """Convierte lo que venga en --cpv a códigos EXACTOS de 8 dígitos.

    Un prefijo (p. ej. «4544») se expande contra el vocabulario estático del repo
    (data/cpv_nombres.json, 9.454 códigos) y NUNCA con un unnest en vivo sobre las
    624.204 filas del catálogo. Motivo del encargo: el Radar casa por «empieza por»;
    si el informe casara solo por código exacto perdería los subcódigos hermanos sin
    avisar. Se devuelve además el detalle de cada expansión, que va al informe.
    """
    if not CPV_VOCABULARIO.exists():
        sys.exit(f"ERROR: falta el vocabulario CPV en {CPV_VOCABULARIO}. "
                 "Es un fichero del repo; recupéralo antes de usar prefijos.")
    vocabulario = json.loads(CPV_VOCABULARIO.read_text(encoding="utf-8"))
    codigos, detalle = [], []
    for bruto in entrada:
        pieza = "".join(ch for ch in str(bruto).strip() if ch.isdigit())
        if not pieza:
            continue
        if len(pieza) == 8:
            codigos.append(pieza)
            detalle.append({"entrada": bruto, "tipo": "codigo", "expandido_a": 1,
                            "codigos": [pieza]})
            continue
        hijos = sorted(c for c in vocabulario if c.startswith(pieza))
        codigos.extend(hijos)
        detalle.append({"entrada": bruto, "tipo": "prefijo", "expandido_a": len(hijos),
                        "codigos": hijos})
    vistos, unicos = set(), []
    for c in codigos:
        if c not in vistos:
            vistos.add(c)
            unicos.append(c)
    return unicos, detalle


# ---------------------------------------------------------------------------
# Bloques
# ---------------------------------------------------------------------------
def bloque1_identidad(sb: Supabase, cif: str) -> dict | None:
    filas = sb.filas("competidores",
                     f"select=cif,nombre_canonico,n_expedientes,n_lotes,"
                     f"importe_total_sin_iva,pct_una_oferta,primera_adjudicacion,"
                     f"ultima_adjudicacion&cif=eq.{quote(cif)}")
    return filas[0] if filas else None


def hidrata_licitaciones(sb: Supabase, ids: list[str]) -> dict:
    """Trae del catálogo las licitaciones de esos ids, en trozos de 100 (URLs cortas)."""
    mapa = {}
    campos = ("licitacion_id,titulo,objeto,organo_contratacion,num_expediente,ccaa,"
              "lugar_ejecucion,enlace,presupuesto_sin_iva,valor_estimado,cpv,fuente,"
              "fecha_publicacion,fecha_fin_plazo,estado_adjudicacion")
    for i in range(0, len(ids), TROZO_IDS):
        trozo = ids[i:i + TROZO_IDS]
        for fila in sb.filas("licitaciones",
                             f"select={campos}&licitacion_id={quote(en_lista(trozo))}"):
            mapa[fila["licitacion_id"]] = fila
    return mapa


def bloque2_adjudicaciones(sb: Supabase, cif: str) -> tuple[list[dict], dict]:
    crudas = sb.filas(
        "adjudicaciones",
        "select=licitacion_id,lote,resultado,resultado_code,importe_sin_iva,"
        "presupuesto_lote_sin_iva,n_ofertas,es_pyme,fecha_adjudicacion,"
        "sistema_contratacion,tipo_contrato,adjudicatario"
        f"&cif_adjudicatario=eq.{quote(cif)}&order=fecha_adjudicacion.desc.nullslast")
    catalogo = hidrata_licitaciones(sb, sorted({a["licitacion_id"] for a in crudas}))
    salida = []
    for a in crudas:
        lic = catalogo.get(a["licitacion_id"], {})
        baja = calcula_baja(a, lic.get("presupuesto_sin_iva"))
        salida.append({
            "licitacion_id": a["licitacion_id"],
            "fecha_adjudicacion": a.get("fecha_adjudicacion"),
            "titulo": lic.get("titulo"),
            "objeto": lic.get("objeto"),
            "organo_contratacion": lic.get("organo_contratacion"),
            "num_expediente": lic.get("num_expediente"),
            "ccaa": lic.get("ccaa"),                       # derivada del NUTS (ver nuts.py)
            "lote": a.get("lote") or None,
            "resultado": a.get("resultado"),
            "resultado_code": a.get("resultado_code"),
            "importe_sin_iva": num(a.get("importe_sin_iva")),
            "presupuesto_lote_sin_iva": num(a.get("presupuesto_lote_sin_iva")),
            "presupuesto_expediente_sin_iva": num(lic.get("presupuesto_sin_iva")),
            "pct_baja": baja["pct_baja"],
            "baja_base": baja["base"],
            "baja_origen": baja["origen_base"],
            "baja_nota": baja["nota"],
            "n_ofertas": a.get("n_ofertas"),
            "sistema_contratacion": a.get("sistema_contratacion"),
            "tipo_contrato": a.get("tipo_contrato"),
            "enlace": lic.get("enlace"),
            "en_catalogo": bool(lic),
        })
    return salida, catalogo


def bloque3_menores_cif(sb: Supabase, cif: str) -> dict:
    filas = sb.filas("menores",
                     "select=licitacion_id,objeto,organo_contratacion,adjudicatario,"
                     "cif_adjudicatario,cifs_adjudicatarios,n_adjudicatarios,"
                     "importe_sin_iva,fecha_adjudicacion,cpv,enlace"
                     f"&cifs_adjudicatarios=ov.%7B{quote(cif)}%7D"
                     "&order=fecha_adjudicacion.desc.nullslast")
    total = sum(num(f.get("importe_sin_iva")) or 0 for f in filas)
    fechas = [f["fecha_adjudicacion"] for f in filas
              if f.get("fecha_adjudicacion") and f["fecha_adjudicacion"] >= "2018-01-01"]
    # Contratos repartidos: `importe_sin_iva` es el del contrato ENTERO y
    # `cif_adjudicatario` el del adjudicatario PRINCIPAL. A un ganador secundario se le
    # imputaría dinero ajeno, así que se cuenta aparte y el informe lo dice.
    compartidos = [f for f in filas
                   if (f.get("n_adjudicatarios") or 1) > 1 and f.get("cif_adjudicatario") != cif]
    return {
        "filas": filas,
        "n": len(filas),
        "importe_total_sin_iva": round(total, 2) if filas else None,
        "ultimo": max(fechas) if fechas else None,
        "n_sin_fecha": sum(1 for f in filas if not f.get("fecha_adjudicacion")),
        "n_compartidos": len(compartidos),
        "importe_compartido": round(sum(num(f.get("importe_sin_iva")) or 0
                                        for f in compartidos), 2),
        "aviso_fuente": ("Sindicación 1143 (contratos menores del Estado). Universo DISTINTO "
                         "del de las adjudicaciones (643 + 1044): estos importes NO se suman "
                         "con los del bloque 2."),
        "aviso_repartidos": ("En los menores repartidos el importe es el del contrato completo "
                             "y se atribuye al adjudicatario principal: no es la parte que se "
                             "llevó esta empresa."),
    }


TILDES = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú"}


def variantes_acentuadas(termino: str) -> list[str]:
    """Un término escrito SIN tildes -> él mismo más una variante por cada vocal
    acentuada. «purificacion» -> purificacion, purificación, purifícacion...

    POR QUÉ HACE FALTA: `ilike` NO ignora los acentos, y los objetos reales sí los
    llevan. Medido sobre los menores del nicho: «purificación» aparece con tilde en 19
    de 20 casos y «desinfección» en 60 de 62. Un ilike con el término sin tilde
    perdería el 95% SIN AVISAR, que es la peor clase de fallo.

    Se genera UNA vocal acentuada por variante, no el producto cartesiano: en español
    la tilde cae en una sola vocal, así que con esto se cubren los casos reales sin
    que la lista explote (8 términos -> 58 condiciones, no miles).
    """
    t = str(termino or "").strip().lower()
    if not t:
        return []
    salida = {t}
    for i, ch in enumerate(t):
        if ch in TILDES:
            salida.add(t[:i] + TILDES[ch] + t[i + 1:])
    return sorted(salida)


def trocea_terminos(texto: str) -> list[str]:
    return [p.strip() for p in str(texto or "").split(",") if p.strip()]


def bloque3b_menores_nicho(sb: Supabase, cpvs: list[str],
                           incluye: list[str] | None = None,
                           excluye: list[str] | None = None) -> dict:
    """Menores de esos CPV: quién compra esto por adjudicación directa.

    Va por `cpv_txt LIKE '% <codigo>%'` y NO por `cpv && [...]`: menores.cpv no tiene
    GIN (8,3 s de Seq Scan) y cpv_txt sí lo tiene (trigram) -> mismas filas en 0,4 s.
    El espacio delante ancla el inicio del código; todos los CPV son de 8 dígitos, así
    que ningún código es prefijo de otro.

    CRIBA POR TEXTO (incluye/excluye), APLICADA EN LA CONSULTA. El código CPV solo no
    basta: este nicho comparte vocabulario con otros mercados y se colaban contratos de
    legionela, plagas, termitas y piscinas por compartir palabras como «desinfección».
    Y filtrar en Python DESPUÉS no serviría: el bloque topa en TOPE_MENORES_NICHO filas,
    así que se cribaría sobre una muestra truncada y el universo real quedaría sin medir
    (con LODEPA el universo son 22.854 menores, no los 3.000 del tope).
    """
    if not cpvs:
        return {"filas": [], "n": 0, "topado": False, "importe_total_sin_iva": None,
                "criba": None}
    incluye = [t for t in (incluye or []) if t]
    excluye = [t for t in (excluye or []) if t]

    base = "or=(" + ",".join(f"cpv_txt.ilike.*%20{c}*" for c in cpvs) + ")"
    partes = [base]
    inc_v = [v for t in incluye for v in variantes_acentuadas(t)]
    exc_v = [v for t in excluye for v in variantes_acentuadas(t)]
    if inc_v:
        # Segundo grupo or=(...): PostgREST combina los grupos repetidos con AND.
        partes.append("or=(" + ",".join(f"objeto.ilike.*{quote(v)}*" for v in inc_v) + ")")
    for v in exc_v:
        # Filtros repetidos sobre la misma columna -> AND. Cada término, uno.
        partes.append(f"objeto=not.ilike.*{quote(v)}*")
    filtro = "&".join(partes)

    campos = ("select=licitacion_id,objeto,organo_contratacion,adjudicatario,"
              "cif_adjudicatario,importe_sin_iva,fecha_adjudicacion,cpv,enlace")
    filas = sb.filas("menores", f"{campos}&{filtro}&order=fecha_adjudicacion.desc.nullslast",
                     tope=TOPE_MENORES_NICHO)
    # Cuántos había ANTES de cribar, para poder declarar el efecto de cada lista. Son
    # conteos, no filas: no traen datos por el cable.
    n_por_codigo = sb.cuenta("menores", base)
    n_tras_incluir = (sb.cuenta("menores", "&".join(partes[:2])) if inc_v else n_por_codigo)

    total = sum(num(f.get("importe_sin_iva")) or 0 for f in filas)
    compradores = defaultdict(lambda: {"n": 0, "importe": 0.0})
    for f in filas:
        clave = f.get("organo_contratacion") or "(sin órgano)"
        compradores[clave]["n"] += 1
        compradores[clave]["importe"] += num(f.get("importe_sin_iva")) or 0
    top = sorted(({"organo_contratacion": k, "n_menores": v["n"],
                   "importe_sin_iva": round(v["importe"], 2)}
                  for k, v in compradores.items()),
                 key=lambda x: x["importe_sin_iva"], reverse=True)[:20]
    topado = len(filas) >= TOPE_MENORES_NICHO
    return {"filas": filas, "n": len(filas), "topado": topado,
            "importe_total_sin_iva": round(total, 2) if filas else None,
            "top_organos_compradores": top,
            "criba": {
                "incluye": incluye, "excluye": excluye,
                "variantes_incluye": len(inc_v), "variantes_excluye": len(exc_v),
                "n_solo_por_codigo": n_por_codigo,
                "n_tras_incluir": n_tras_incluir,
                "n_tras_excluir": len(filas),
                "tope": TOPE_MENORES_NICHO, "topado": topado,
                "nota": ("La criba se aplica EN LA CONSULTA, no sobre las filas traídas: "
                         "si no, se cribaría sobre la muestra truncada por el tope. Los "
                         "términos se expanden a sus variantes acentuadas porque ilike no "
                         "ignora las tildes y los objetos reales sí las llevan."),
            }}



def bloque_competidores(sb: Supabase, cifs: list[str]) -> list[dict]:
    """Identidad y adjudicaciones de VARIOS rivales en la misma ejecución.

    Antes había que lanzar el script una vez por competidor, así que comparar cinco
    rivales eran cinco informes sueltos que alguien tenía que cuadrar a mano.

    NO duplica lógica: llama a las MISMAS bloque1_identidad y bloque2_adjudicaciones
    que usa el CIF principal, así que el % de baja, el rótulo de acuerdo marco y el
    resto de reglas son por construcción idénticos. Si mañana cambia la regla de la
    baja, cambia a la vez para el principal y para los rivales.
    """
    salida = []
    for bruto in cifs:
        cif = normaliza_cif(bruto)
        if not cif:
            continue
        identidad = bloque1_identidad(sb, cif)
        adjudicaciones, _catalogo = bloque2_adjudicaciones(sb, cif)
        salida.append({
            "cif": cif,
            "identidad": identidad,
            "adjudicaciones": adjudicaciones,
            "n_adjudicaciones": len(adjudicaciones),
            "en_competidores": identidad is not None,
        })
    return salida


def bloque4_cpv(adjudicaciones: list[dict], catalogo: dict, menores: list[dict]) -> dict:
    """CPV deducidos de lo que la empresa YA ha ganado (adjudicaciones + menores),
    con frecuencia e importe. Es la semilla de la taxonomía de vigilancia."""
    conteo = defaultdict(lambda: {"n": 0, "importe": 0.0, "origen": set()})
    for a in adjudicaciones:
        lic = catalogo.get(a["licitacion_id"], {})
        for c in (lic.get("cpv") or []):
            conteo[c]["n"] += 1
            conteo[c]["importe"] += a.get("importe_sin_iva") or 0
            conteo[c]["origen"].add("adjudicaciones")
    for m in menores:
        for c in (m.get("cpv") or []):
            conteo[c]["n"] += 1
            conteo[c]["importe"] += num(m.get("importe_sin_iva")) or 0
            conteo[c]["origen"].add("menores")
    lista = [{"cpv": c, "n_contratos": v["n"], "importe_sin_iva": round(v["importe"], 2),
              "origen": sorted(v["origen"])} for c, v in conteo.items()]
    lista.sort(key=lambda x: (-x["n_contratos"], -x["importe_sin_iva"]))
    textos = [{"licitacion_id": a["licitacion_id"], "titulo": a["titulo"],
               "objeto": a["objeto"], "organo_contratacion": a["organo_contratacion"]}
              for a in adjudicaciones]
    textos += [{"licitacion_id": m.get("licitacion_id"), "titulo": None,
                "objeto": m.get("objeto"),
                "organo_contratacion": m.get("organo_contratacion")} for m in menores]
    return {"cpv_deducidos": lista, "textos_ganados": textos,
            "nota": ("Las palabras clave NO se generan aquí a propósito: estos son los "
                     "títulos y objetos en crudo, que es la materia prima para escribirlas.")}


def ceros_finales(cpv: str) -> int:
    """Ceros al final del código. Es la medida del NIVEL en el árbol CPV: 45000000 es
    una división entera (7 ceros), 45440000 un grupo (4), 45441000 una clase (3)."""
    limpio = str(cpv or "").strip()
    return len(limpio) - len(limpio.rstrip("0"))


def criba_cpv(sb: Supabase, candidatos: list[str], total_catalogo: int) -> dict:
    """Deja los CPV que describen un NICHO. Descarta por dos criterios distintos, y
    declara los dos: un tope silencioso se lee como «esto es todo» cuando no lo es.

    1) NIVEL (nuevo). Un código con 6 o más ceros finales es una división o un grupo
       del árbol CPV, nunca un nicho. El criterio de frecuencia NO los cazaba: con
       LODEPA se colaron 50000000 y 51000000 —divisiones enteras— y arrastraron 5.472
       licitaciones por 3.408 M€ que son el mercado de mantenimiento integral de
       edificios, no el suyo. Y `descartados_genericos` salía VACÍO, porque ninguno
       llegaba al 2% del catálogo: son genéricos por lo que SIGNIFICAN, no por lo que
       aparecen. Este criterio va PRIMERO porque no cuesta una consulta.
    2) FRECUENCIA. Presente en más del 2% del catálogo. Red de seguridad para códigos
       específicos pero omnipresentes (hoy solo 2 de los 9.454 del vocabulario).
    """
    umbral = int(total_catalogo * PCT_GENERICO)
    usados, descartados = [], []
    for c in candidatos:
        ceros = ceros_finales(c)
        if ceros >= CEROS_GENERICO:
            descartados.append({"cpv": c, "motivo": "nivel", "ceros_finales": ceros,
                                "detalle": ("división o grupo del árbol CPV: describe un "
                                            "sector entero, no un nicho")})
            continue
        n = sb.cuenta("licitaciones", f"cpv=ov.%7B{c}%7D")
        if n > umbral:
            descartados.append({"cpv": c, "motivo": "frecuencia", "n_licitaciones": n,
                                "pct_catalogo": round(100.0 * n / total_catalogo, 2),
                                "detalle": f"aparece en más del {PCT_GENERICO * 100}% del catálogo"})
        else:
            usados.append(c)
        if len(usados) >= MAX_CPV:
            break
    fuera = {d["cpv"] for d in descartados}
    fuera_por_tope = [c for c in candidatos if c not in usados and c not in fuera]
    return {"usados": usados, "descartados_genericos": descartados,
            "descartados_por_tope": fuera_por_tope, "umbral_generico": umbral,
            "pct_generico": PCT_GENERICO * 100, "ceros_generico": CEROS_GENERICO,
            "max_cpv": MAX_CPV}


def bloque5_mercado(sb: Supabase, cpvs: list[str], desde_iso: str) -> dict:
    """Mercado en esos CPV, desglosado POR COMUNIDAD AUTÓNOMA y POR ÓRGANO.

    Las dos dimensiones, porque no se sustituyen: hay 7.472 órganos distintos y los
    que más licitan son entes estatales sin geografía (TRAGSA 21.046 licitaciones,
    Renfe 10.658, Paradores, ADIF, Correos). El órgano dice A QUIÉN visitar; la
    comunidad dice DÓNDE está el mercado.

    Agregar por CCAA sale gratis: estas filas ya se traen para el resto del bloque, así
    que es un contador más en el mismo bucle. Cero peticiones y cero índices extra.

    HONESTIDAD: `ccaa` se pobló con el re-backfill del 15/09/2026 y no llega al 100% —
    ni llegará: un ~1,3% de los contratos son de ámbito nacional («ES» a secas) o de
    código ambiguo, y se quedan a NULL a propósito. Por eso se devuelve SIEMPRE la
    cobertura (`pct_con_ccaa`): un desglose geográfico sobre una columna a medias
    ocultaría mercado en silencio, que es justo lo que no queremos.
    """
    if not cpvs:
        return {"n_licitaciones": 0, "importe_total_sin_iva": None, "abiertas_hoy": 0,
                "desiertas": 0, "por_organo": [], "por_ccaa": [], "n_sin_ccaa": 0,
                "pct_con_ccaa": None, "topado": False}
    ov = "%7B" + ",".join(cpvs) + "%7D"
    filtro = f"cpv=ov.{ov}&fecha_publicacion=gte.{quote(desde_iso)}"
    filas = sb.filas("licitaciones",
                     "select=licitacion_id,organo_contratacion,presupuesto_sin_iva,"
                     f"fecha_fin_plazo,estado_adjudicacion,ccaa&{filtro}", tope=TOPE_MERCADO)
    ahora = datetime.now(timezone.utc).isoformat()
    por_organo = defaultdict(lambda: {"n": 0, "importe": 0.0})
    por_ccaa = defaultdict(lambda: {"n": 0, "importe": 0.0})
    total_importe, abiertas, desiertas, sin_ccaa = 0.0, 0, 0, 0
    for f in filas:
        imp = num(f.get("presupuesto_sin_iva")) or 0
        total_importe += imp
        if f.get("fecha_fin_plazo") and f["fecha_fin_plazo"] >= ahora:
            abiertas += 1
        if f.get("estado_adjudicacion") in DESIERTAS:
            desiertas += 1
        clave = f.get("organo_contratacion") or "(sin órgano)"
        por_organo[clave]["n"] += 1
        por_organo[clave]["importe"] += imp
        comunidad = f.get("ccaa")
        if comunidad:
            por_ccaa[comunidad]["n"] += 1
            por_ccaa[comunidad]["importe"] += imp
        else:
            sin_ccaa += 1
    desglose = sorted(({"organo_contratacion": k, "n_licitaciones": v["n"],
                        "importe_sin_iva": round(v["importe"], 2)}
                       for k, v in por_organo.items()),
                      key=lambda x: x["importe_sin_iva"], reverse=True)
    geografico = sorted(({"ccaa": k, "n_licitaciones": v["n"],
                          "importe_sin_iva": round(v["importe"], 2)}
                         for k, v in por_ccaa.items()),
                        key=lambda x: x["importe_sin_iva"], reverse=True)
    con_ccaa = len(filas) - sin_ccaa
    pct = round(100.0 * con_ccaa / len(filas), 1) if filas else None
    return {"n_licitaciones": len(filas), "importe_total_sin_iva": round(total_importe, 2),
            "abiertas_hoy": abiertas, "desiertas": desiertas,
            "por_organo": desglose[:30], "n_organos": len(desglose),
            "por_ccaa": geografico, "n_sin_ccaa": sin_ccaa, "pct_con_ccaa": pct,
            "topado": len(filas) >= TOPE_MERCADO,
            "aviso_desglose": (
                f"El desglose por comunidad cubre el {pct}% de estas licitaciones "
                f"({con_ccaa} de {len(filas)}). El resto no lleva comunidad en la fuente: "
                "son contratos de ámbito nacional o con un código territorial que no "
                "identifica una sola comunidad, y se dejan fuera a propósito en vez de "
                "asignarles una inventada."),
            "ids": [f["licitacion_id"] for f in filas]}


def bloque6_quien_gana(sb: Supabase, ids: list[str]) -> list[dict]:
    """Top adjudicatarios de ese mercado, con baja media (misma regla de AM del bloque
    2: las adjudicaciones de precios unitarios NO entran en la media) y % a oferta única."""
    if not ids:
        return []
    presupuestos = {}
    for i in range(0, len(ids), TROZO_IDS):
        for fila in sb.filas("licitaciones",
                             "select=licitacion_id,presupuesto_sin_iva"
                             f"&licitacion_id={quote(en_lista(ids[i:i + TROZO_IDS]))}"):
            presupuestos[fila["licitacion_id"]] = fila.get("presupuesto_sin_iva")
    agregados = defaultdict(lambda: {"nombre": None, "lotes": 0, "importe": 0.0,
                                     "bajas": [], "una_oferta": 0, "con_ofertas": 0})
    for i in range(0, len(ids), TROZO_IDS):
        adjs = sb.filas("adjudicaciones",
                        "select=licitacion_id,cif_adjudicatario,adjudicatario,lote,"
                        "importe_sin_iva,presupuesto_lote_sin_iva,n_ofertas,"
                        "sistema_contratacion"
                        f"&licitacion_id={quote(en_lista(ids[i:i + TROZO_IDS]))}"
                        "&cif_adjudicatario=not.is.null")
        for a in adjs:
            g = agregados[a["cif_adjudicatario"]]
            g["nombre"] = g["nombre"] or a.get("adjudicatario")
            g["lotes"] += 1
            g["importe"] += num(a.get("importe_sin_iva")) or 0
            baja = calcula_baja(a, presupuestos.get(a["licitacion_id"]))
            if baja["pct_baja"] is not None:
                g["bajas"].append(baja["pct_baja"])
            if a.get("n_ofertas") is not None:
                g["con_ofertas"] += 1
                if a["n_ofertas"] == 1:
                    g["una_oferta"] += 1
    salida = []
    for cif, g in agregados.items():
        salida.append({
            "cif": cif, "nombre": g["nombre"], "n_lotes": g["lotes"],
            "importe_total_sin_iva": round(g["importe"], 2),
            "baja_media_pct": round(sum(g["bajas"]) / len(g["bajas"]), 2) if g["bajas"] else None,
            "n_con_baja_calculable": len(g["bajas"]),
            "pct_una_oferta": (round(100.0 * g["una_oferta"] / g["con_ofertas"], 1)
                               if g["con_ofertas"] else None),
        })
    salida.sort(key=lambda x: x["importe_total_sin_iva"], reverse=True)
    return salida[:TOP_ADJUDICATARIOS]


def bloque7_oportunidades(sb: Supabase, cpvs: list[str]) -> list[dict]:
    if not cpvs:
        return []
    ahora = datetime.now(timezone.utc)
    ov = "%7B" + ",".join(cpvs) + "%7D"
    filas = sb.filas("licitaciones",
                     "select=licitacion_id,titulo,organo_contratacion,presupuesto_sin_iva,"
                     "valor_estimado,fecha_fin_plazo,enlace,num_expediente"
                     f"&cpv=ov.{ov}&fecha_fin_plazo=gte.{quote(ahora.isoformat())}"
                     "&order=fecha_fin_plazo.asc")
    salida = []
    for f in filas:
        dias = None
        if f.get("fecha_fin_plazo"):
            try:
                fin = datetime.fromisoformat(f["fecha_fin_plazo"].replace("Z", "+00:00"))
                dias = (fin - ahora).days
            except ValueError:
                dias = None
        salida.append({**f, "dias_restantes": dias})
    return salida


def bloque8_metadatos(sb: Supabase, consulta_iso: str, meses: int, criba: dict,
                      expansion: list[dict], filas_por_bloque: dict, total: int) -> dict:
    prim = sb.una("licitaciones", "select=fecha_publicacion&order=fecha_publicacion.asc")
    ult = sb.una("licitaciones", "select=fecha_publicacion&order=fecha_publicacion.desc.nullslast")
    # Distribución por año SIN traerse 624k filas: un conteo indexado por año.
    ahora_anno = datetime.now(timezone.utc).year
    por_anno = []
    for anno in range(2019, ahora_anno + 1):
        n = sb.cuenta("licitaciones",
                      f"fecha_publicacion=gte.{anno}-01-01&fecha_publicacion=lt.{anno + 1}-01-01")
        if n:
            por_anno.append({"anno": anno, "n_licitaciones": n})
    return {
        "consultado_en": consulta_iso,
        "catalogo_total_filas": total,
        "catalogo_ventana": {"desde": (prim["fecha_publicacion"] if prim else None),
                             "hasta": (ult["fecha_publicacion"] if ult else None)},
        "catalogo_por_anno": por_anno,
        "ventana_informe_meses": meses,
        "cpv_expansion": expansion,
        "cpv_criba": criba,
        "filas_por_bloque": filas_por_bloque,
        "peticiones_http": sb.peticiones,
        "peticiones_por_tabla": dict(sb.por_tabla),
        "segundos_en_consultas": round(sb.segundos, 2),
        "consulta_mas_lenta": {"que": sb.mas_lenta[0], "segundos": sb.mas_lenta[1]},
        "fuentes": {
            "adjudicaciones": "Sindicaciones 643 (estatal) + 1044 (plataformas agregadas)",
            "menores": "Sindicación 1143 (contratos menores del Estado; NO incluye agregadas)",
            "aviso": "Los importes de menores y de adjudicaciones NO se suman entre sí.",
        },
    }


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------
def eur(v) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f} €".replace(",", "\u00a0").replace(".", ",", 1).replace("\u00a0", ".")


def tabla_md(cabeceras: list[str], filas: list[list]) -> str:
    if not filas:
        return "_Sin datos._\n"
    def celda(x):
        return "—" if x is None else str(x).replace("|", "\\|").replace("\n", " ")
    salida = ["| " + " | ".join(cabeceras) + " |",
              "|" + "|".join("---" for _ in cabeceras) + "|"]
    salida += ["| " + " | ".join(celda(c) for c in f) + " |" for f in filas]
    return "\n".join(salida) + "\n"


def escribe_md(informe: dict) -> str:
    b1 = informe["identidad"]
    m = informe["metadatos"]
    p = [f"# Informe de empresa — {(b1 or {}).get('nombre_canonico') or informe['cif']}",
         "",
         f"**CIF** `{informe['cif']}` · consultado el {m['consultado_en'][:19].replace('T', ' ')} (UTC)",
         ""]
    p += ["## 1 · Identidad y totales", ""]
    if b1:
        p.append(tabla_md(["Dato", "Valor"], [
            ["Nombre canónico", b1.get("nombre_canonico")],
            ["Expedientes adjudicados", b1.get("n_expedientes")],
            ["Lotes adjudicados", b1.get("n_lotes")],
            ["Importe total s/IVA", eur(num(b1.get("importe_total_sin_iva")))],
            ["% a oferta única", b1.get("pct_una_oferta")],
            ["Primera / última adjudicación",
             f"{b1.get('primera_adjudicacion')} / {b1.get('ultima_adjudicacion')}"],
        ]))
    else:
        p.append("_Este CIF no figura en `competidores`: no tiene adjudicaciones "
                 "registradas en las sindicaciones 643/1044._\n")

    p += ["", "## 2 · Todas sus adjudicaciones", ""]
    p.append(tabla_md(
        ["Fecha", "Título", "Órgano", "Comunidad", "Expediente", "Lote", "Resultado",
         "Importe s/IVA", "Presup. base", "% baja", "Ofertas", "Enlace"],
        [[a["fecha_adjudicacion"], (a["titulo"] or "")[:70], (a["organo_contratacion"] or "")[:50],
          a["ccaa"], a["num_expediente"], a["lote"], a["resultado"], eur(a["importe_sin_iva"]),
          eur(a["baja_base"]), ("—" if a["pct_baja"] is None else f"{a['pct_baja']:.1f} %"),
          a["n_ofertas"], a["enlace"]] for a in informe["adjudicaciones"]]))

    mc = informe["menores_empresa"]
    p += ["", "## 3 · Contratos menores", "",
          f"> ⚠️ {mc['aviso_fuente']}", ""]
    p.append(tabla_md(["Fecha", "Objeto", "Órgano", "Importe s/IVA"],
                      [[f["fecha_adjudicacion"], (f["objeto"] or "")[:70],
                        (f["organo_contratacion"] or "")[:50], eur(num(f["importe_sin_iva"]))]
                       for f in mc["filas"]]))
    p.append(f"\n**Total menores:** {eur(mc['importe_total_sin_iva'])} · "
             f"**nº:** {mc['n']} · **último:** {mc['ultimo'] or '—'}\n")
    if mc["n_compartidos"]:
        p.append(f"\n> ⚠️ {mc['aviso_repartidos']} Afecta a {mc['n_compartidos']} "
                 f"contrato(s) por {eur(mc['importe_compartido'])}.\n")

    mn = informe["menores_nicho"]
    p += ["", "### 3b · Menores del nicho (quién compra esto a dedo)", ""]
    p.append(tabla_md(["Órgano comprador", "Nº menores", "Importe s/IVA"],
                      [[o["organo_contratacion"][:60], o["n_menores"], eur(o["importe_sin_iva"])]
                       for o in mn.get("top_organos_compradores", [])]))
    cr_men = mn.get("criba") or {}
    if cr_men.get("incluye") or cr_men.get("excluye"):
        p.append("\n> **Criba por texto, aplicada en la consulta.** De "
                 f"{cr_men['n_solo_por_codigo']:,} menores que casan por código CPV, "
                 f"{cr_men['n_tras_incluir']:,} contienen alguna palabra de «incluye» y "
                 f"{cr_men['n_tras_excluir']:,} quedan tras descartar las de «excluye».  \n"
                 f"> Incluye: _{', '.join(cr_men['incluye']) or '—'}_.  \n"
                 f"> Excluye: _{', '.join(cr_men['excluye']) or '—'}_.\n")
    if mn.get("topado"):
        p.append(f"\n> ⚠️ Topado en {TOPE_MENORES_NICHO} filas: hay MÁS de los que se ven, "
                 "así que los totales de este bloque son un suelo, no el universo.\n")

    p += ["", "## 4 · CPV deducidos", ""]
    p.append(tabla_md(["CPV", "Nº contratos", "Importe s/IVA", "Origen"],
                      [[c["cpv"], c["n_contratos"], eur(c["importe_sin_iva"]),
                        ", ".join(c["origen"])] for c in informe["cpv"]["cpv_deducidos"]]))

    b5 = informe["mercado"]
    p += ["", f"## 5 · Mercado en esos CPV (últimos {m['ventana_informe_meses']} meses)", "",
          f"> ⚠️ {b5.get('aviso_desglose', '')}", ""]
    p.append(tabla_md(["Licitaciones", "Importe s/IVA", "Abiertas hoy", "Desiertas"],
                      [[b5["n_licitaciones"], eur(b5["importe_total_sin_iva"]),
                        b5["abiertas_hoy"], b5["desiertas"]]]))
    p += ["", "**Dónde está el mercado** (por comunidad autónoma):", ""]
    p.append(tabla_md(["Comunidad", "Licitaciones", "Importe s/IVA", "% del importe"],
                      [[c["ccaa"], c["n_licitaciones"], eur(c["importe_sin_iva"]),
                        (f"{100.0 * c['importe_sin_iva'] / b5['importe_total_sin_iva']:.1f} %"
                         if b5.get("importe_total_sin_iva") else "—")]
                       for c in b5.get("por_ccaa", [])]))
    if b5.get("n_sin_ccaa"):
        p.append(f"\n> {b5['aviso_desglose']}\n")
    p += ["", "**A quién visitar** (por órgano de contratación):", ""]
    p.append(tabla_md(["Órgano", "Licitaciones", "Importe s/IVA"],
                      [[o["organo_contratacion"][:60], o["n_licitaciones"], eur(o["importe_sin_iva"])]
                       for o in b5["por_organo"]]))

    p += ["", "## 6 · Quién gana ese mercado", ""]
    p.append(tabla_md(["Adjudicatario", "CIF", "Lotes", "Importe s/IVA", "Baja media", "% 1 oferta"],
                      [[(g["nombre"] or "")[:45], g["cif"], g["n_lotes"],
                        eur(g["importe_total_sin_iva"]),
                        ("—" if g["baja_media_pct"] is None else f"{g['baja_media_pct']:.1f} %"),
                        ("—" if g["pct_una_oferta"] is None else f"{g['pct_una_oferta']} %")]
                       for g in informe["quien_gana"]]))

    p += ["", "## 7 · Oportunidades vivas", ""]
    p.append(tabla_md(["Cierra", "Días", "Título", "Órgano", "Presupuesto s/IVA", "Enlace"],
                      [[(o.get("fecha_fin_plazo") or "")[:10], o.get("dias_restantes"),
                        (o.get("titulo") or "")[:60], (o.get("organo_contratacion") or "")[:40],
                        eur(num(o.get("presupuesto_sin_iva"))), o.get("enlace")]
                       for o in informe["oportunidades"]]))

    comps = informe.get("competidores") or []
    if comps:
        p += ["", "## 9 · Competidores comparados", ""]
        p.append(tabla_md(["Empresa", "CIF", "Expedientes", "Lotes", "Importe s/IVA", "% 1 oferta"],
                          [[((c["identidad"] or {}).get("nombre_canonico") or "(sin adjudicaciones registradas)")[:40],
                            c["cif"],
                            (c["identidad"] or {}).get("n_expedientes"),
                            (c["identidad"] or {}).get("n_lotes"),
                            eur(num((c["identidad"] or {}).get("importe_total_sin_iva"))),
                            (c["identidad"] or {}).get("pct_una_oferta")] for c in comps]))
        for c in comps:
            nombre = (c["identidad"] or {}).get("nombre_canonico") or c["cif"]
            p += ["", f"**{nombre}** · {c['n_adjudicaciones']} adjudicaciones", ""]
            p.append(tabla_md(["Fecha", "Título", "Órgano", "Importe s/IVA", "% baja", "Ofertas"],
                              [[a["fecha_adjudicacion"], (a["titulo"] or "")[:55],
                                (a["organo_contratacion"] or "")[:40], eur(a["importe_sin_iva"]),
                                ("—" if a["pct_baja"] is None else f"{a['pct_baja']:.1f} %"),
                                a["n_ofertas"]] for a in c["adjudicaciones"][:25]]))
            if c["n_adjudicaciones"] > 25:
                p.append(f"\n_(se muestran 25 de {c['n_adjudicaciones']} en el MD; "
                         "el JSON las trae todas)_\n")

    p += ["", "## 8 · Nota de fuentes", ""]
    p.append(tabla_md(["Dato", "Valor"], [
        ["Consultado", m["consultado_en"]],
        ["Filas del catálogo", f"{m['catalogo_total_filas']:,}".replace(",", ".")],
        ["Ventana del catálogo",
         f"{(m['catalogo_ventana']['desde'] or '')[:10]} → {(m['catalogo_ventana']['hasta'] or '')[:10]}"],
        ["Ventana del informe", f"{m['ventana_informe_meses']} meses"],
        ["Peticiones HTTP", m["peticiones_http"]],
        ["Segundos en consultas", m["segundos_en_consultas"]],
    ]))
    p += ["", "**Cobertura del catálogo por año de publicación**", ""]
    p.append(tabla_md(["Año", "Licitaciones"],
                      [[a["anno"], f"{a['n_licitaciones']:,}".replace(",", ".")]
                       for a in m["catalogo_por_anno"]]))
    p += ["", "**Filas devueltas por bloque**", ""]
    p.append(tabla_md(["Bloque", "Filas"], [[k, v] for k, v in m["filas_por_bloque"].items()]))
    cr = m["cpv_criba"]
    if cr.get("descartados_genericos") or cr.get("descartados_por_tope"):
        p += ["", "**CPV dejados fuera** (nada de recortes silenciosos)", ""]
        # Dos motivos con formas distintas: «nivel» no tiene conteo (se descarta sin
        # consultar) y «frecuencia» sí. Se pintan los dos, cada uno con lo suyo.
        def _fila_descarte(g):
            if g.get("motivo") == "nivel":
                return [g["cpv"], "genérico por NIVEL",
                        f"{g['ceros_finales']} ceros finales · {g.get('detalle', '')}"]
            return [g["cpv"], "genérico por FRECUENCIA",
                    f"{g.get('n_licitaciones', '?')} licitaciones ({g.get('pct_catalogo', '?')} %)"]

        p.append(tabla_md(["CPV", "Motivo", "Detalle"],
                          [_fila_descarte(g) for g in cr["descartados_genericos"]] +
                          [[c, f"fuera del tope de {cr['max_cpv']}", ""]
                           for c in cr["descartados_por_tope"]]))
    for e in m["cpv_expansion"]:
        if e["tipo"] == "prefijo":
            p.append(f"\n- El prefijo `{e['entrada']}` se expandió a **{e['expandido_a']}** códigos.")
    return "\n".join(p) + "\n"


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(
        description="Vuelca a disco los datos de una empresa para redactar un informe.")
    ap.add_argument("cif", help="CIF de la empresa (p. ej. B01947753)")
    ap.add_argument("--meses", type=int, default=24,
                    help="ventana de los bloques de mercado (por defecto 24)")
    ap.add_argument("--cpv", default="",
                    help="CPV separados por comas. Admite códigos exactos (45441000) y "
                         "PREFIJOS (4544), que se expanden con el vocabulario del repo. "
                         "Si no se pasa, se deducen de lo que la empresa ha ganado.")
    ap.add_argument("--nicho-incluye", default="",
                    help="palabras separadas por comas: el menor del nicho debe contener "
                         "ALGUNA en su objeto. Se aplica EN LA CONSULTA e ignora mayúsculas "
                         "y tildes. Vacío = sin criba (comportamiento de siempre).")
    ap.add_argument("--nicho-excluye", default="",
                    help="palabras separadas por comas: el menor del nicho NO debe contener "
                         "NINGUNA en su objeto. Mismas reglas que --nicho-incluye.")
    ap.add_argument("--competidores", default="",
                    help="CIFs separados por comas: añade identidad y adjudicaciones de cada "
                         "uno en la clave «competidores» del JSON, además del CIF principal.")
    ap.add_argument("--salida", default="", help="carpeta de salida (por defecto, la del proyecto)")
    args = ap.parse_args()

    cif = normaliza_cif(args.cif)
    if not cif:
        sys.exit("ERROR: el CIF no puede estar vacío.")
    url, clave, dir_env = credenciales()
    sb = Supabase(url, clave)
    arranque = time.time()
    ahora = datetime.now(timezone.utc).replace(microsecond=0)
    consulta_iso = ahora.isoformat()
    if args.meses < 1:
        sys.exit("ERROR: --meses tiene que ser 1 o más.")
    desde_iso = resta_meses(ahora, args.meses).isoformat()

    print(f"· Empresa {cif} · ventana {args.meses} meses (desde {desde_iso[:10]})")
    identidad = bloque1_identidad(sb, cif)
    adjudicaciones, catalogo = bloque2_adjudicaciones(sb, cif)
    print(f"· {len(adjudicaciones)} adjudicaciones")
    menores_emp = bloque3_menores_cif(sb, cif)
    print(f"· {menores_emp['n']} contratos menores")
    cpv_info = bloque4_cpv(adjudicaciones, catalogo, menores_emp["filas"])

    if args.cpv.strip():
        candidatos, expansion = expande_cpv([c for c in args.cpv.split(",") if c.strip()])
    else:
        candidatos = [c["cpv"] for c in cpv_info["cpv_deducidos"]]
        expansion = [{"entrada": "(deducidos de sus contratos)", "tipo": "deducido",
                      "expandido_a": len(candidatos), "codigos": candidatos}]
    total_catalogo = sb.cuenta("licitaciones", "")
    criba = criba_cpv(sb, candidatos, total_catalogo)
    cpvs = criba["usados"]
    print(f"· {len(cpvs)} CPV de mercado" +
          (f" ({len(criba['descartados_genericos'])} genéricos fuera)"
           if criba["descartados_genericos"] else ""))

    menores_nicho = bloque3b_menores_nicho(
        sb, cpvs, trocea_terminos(args.nicho_incluye), trocea_terminos(args.nicho_excluye))
    mercado = bloque5_mercado(sb, cpvs, desde_iso)
    quien_gana = bloque6_quien_gana(sb, mercado.pop("ids", []))
    oportunidades = bloque7_oportunidades(sb, cpvs)
    print(f"· mercado {mercado['n_licitaciones']} licitaciones · "
          f"{len(quien_gana)} adjudicatarios · {len(oportunidades)} oportunidades vivas")

    competidores = bloque_competidores(sb, trocea_terminos(args.competidores))
    if competidores:
        print(f"· {len(competidores)} competidores comparados")

    filas_por_bloque = {
        "1_identidad": 1 if identidad else 0,
        "2_adjudicaciones": len(adjudicaciones),
        "3_menores_empresa": menores_emp["n"],
        "3b_menores_nicho": menores_nicho["n"],
        "4_cpv_deducidos": len(cpv_info["cpv_deducidos"]),
        "5_mercado_licitaciones": mercado["n_licitaciones"],
        "6_quien_gana": len(quien_gana),
        "7_oportunidades": len(oportunidades),
        "competidores": sum(c["n_adjudicaciones"] for c in competidores),
    }
    metadatos = bloque8_metadatos(sb, consulta_iso, args.meses, criba, expansion,
                                  filas_por_bloque, total_catalogo)

    informe = {
        "cif": cif, "identidad": identidad, "adjudicaciones": adjudicaciones,
        "menores_empresa": menores_emp, "menores_nicho": menores_nicho,
        "cpv": cpv_info, "cpv_mercado": cpvs, "mercado": mercado,
        "quien_gana": quien_gana, "oportunidades": oportunidades,
        "competidores": competidores, "metadatos": metadatos,
    }

    destino = Path(args.salida) if args.salida else (
        Path(dir_env) if dir_env else
        (ONEDRIVE_INFORMES if ONEDRIVE_INFORMES.parent.exists() else RAIZ / "informes"))
    destino.mkdir(parents=True, exist_ok=True)
    sello = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ruta_json = destino / f"{cif}_{sello}.json"
    ruta_md = destino / f"{cif}_{sello}.md"
    ruta_json.write_text(json.dumps(informe, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    ruta_md.write_text(escribe_md(informe), encoding="utf-8")

    print(f"\nOK · {ruta_json}\n   · {ruta_md}")
    print(f"   {sb.peticiones} peticiones · {round(time.time() - arranque, 1)} s en total"
          f" · la más lenta {sb.mas_lenta[1]} s")


if __name__ == "__main__":
    main()
