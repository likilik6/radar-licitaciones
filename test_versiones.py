# -*- coding: utf-8 -*-
"""Pruebas de la REGLA DE VERSIÓN y de las BAJAS del feed (PASO 0 del 15/10/2026).

Qué protegen: que de cada expediente (atom:id) se guarde SIEMPRE la versión más nueva
y que las bajas (<at:deleted-entry>) queden marcadas. El PASO 0 midió que hoy no es así:
  · el re-backfill (carga) se queda con la versión del día MÁS VIEJO de cada ZIP;
  · la ingesta diaria (diario) y el Radar (filtrar.py) con la MÁS VIEJA de las 20 páginas;
  · una ejecución lanzada después pisa a otra aunque traiga una versión más vieja;
  · las bajas se ignoran.

LA REGLA (especificación de sindicación de la PCSP v1.3, §3.3.2, más un desempate):
  1. Gana la versión de MAYOR atom:updated, comparado como FECHA CON ZONA, nunca como
     texto (el 25/10 hay cambio de hora y el texto ordena al revés la hora repetida).
  2. Si el atom:updated es IGUAL, gana la leída en un SNAPSHOT IGUAL O MÁS RECIENTE (el
     <updated> del propio .atom) si su CONTENIDO es distinto. Hace falta: en septiembre de
     2026 la Plataforma republicó 6.491 expedientes con la marca vieja y el contenido nuevo
     (ADJ→RES, EV→ADJ...), y releer un .atom con un extractor mejorado debe poder reescribir.
     Acierta aunque traiga menos avisos o un estado «hacia atrás» (8 de 8 en la web, 30/10).
  3. Si son iguales las tres cosas, no se hace nada.

Los casos llevan los números de expedientes REALES del PASO 0 (con datos simplificados).

CÓMO SE PRUEBA SIN RED NI SUPABASE: una «base falsa» (BaseFalsa) hace de PostgREST. Entiende
el upsert de hoy (escribe a ciegas, como merge-duplicates) y la RPC nueva carga_versiones
(escribe solo si la versión gana; es un ESPEJO de versiones_schema.sql). Las pruebas miran el
ESTADO FINAL de esa base, así que valen igual antes y después del arreglo: hoy salen en ROJO
y, cuando esté hecho, tienen que salir en VERDE sin tocarlas. La guarda de verdad (el SQL) se
prueba aparte, con prueba_versiones.sql en el SQL Editor.

Uso:
  python test_versiones.py        # sale con código 1 si algo falla
"""
import json
import sys
import shutil
import tempfile
import zipfile
import itertools
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
import requests
from lxml import etree

import feeds
import backfill_catalogo as bc
import utiles

FALLOS = []
BIEN = []


def comprueba(nombre, obtenido, esperado):
    if obtenido == esperado:
        BIEN.append(nombre)
        print(f"  OK {nombre}")
    else:
        FALLOS.append(nombre)
        print(f"  FALLO {nombre}\n     esperado: {esperado!r}\n     obtenido: {obtenido!r}")


def prueba(funcion):
    """Ejecuta una prueba y, si revienta (p. ej. porque la función nueva aún no existe, o
    porque el código llama a sys.exit), lo cuenta como FALLO con el motivo, sin parar las
    demás."""
    try:
        funcion()
    except (Exception, SystemExit) as err:  # noqa: BLE001
        FALLOS.append(funcion.__name__)
        tb = traceback.extract_tb(err.__traceback__)
        donde = f" [{Path(tb[-1].filename).name}:{tb[-1].lineno}]" if tb else ""
        print(f"  FALLO {funcion.__name__}: {type(err).__name__}: {err}{donde}")


# =============================================================================
# Mini-XML con la forma real del feed
# =============================================================================
NSDECL = (
    'xmlns="http://www.w3.org/2005/Atom" '
    'xmlns:cbc="urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2" '
    'xmlns:cac="urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2" '
    'xmlns:place="urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2" '
    'xmlns:pce="urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2" '
    'xmlns:at="http://purl.org/atompub/tombstones/1.0"'
)
ESTATAL = "https://contrataciondelestado.es/sindicacion/licitacionesPerfilContratante"
AGREGADAS = "https://contrataciondelestado.es/sindicacion/PlataformasAgregadasSinMenores"
URL = "https://x.supabase.co"
LODEPA = "B86833753"
BASE_ATOM = "licitacionesPerfilesContratanteCompleto3"


def idu(n, raiz=ESTATAL):
    return f"{raiz}/{n}"


def entrada(n, updated, estado="PUB", fin="2026-05-21", presu="1000.0", adjs=(), raiz=ESTATAL,
            titulo="Contrato de prueba", cpv="90920000", publicada=None):
    """Una <entry>. adjs = [(lote, cif, nombre, importe, code), ...]; un TenderResult por fila.
    publicada: fecha del aviso (fecha_publicacion); sin ella, la entrada no la trae."""
    i = idu(n, raiz)
    tr = ""
    for lote, cif, nombre, importe, code in adjs:
        gan = (f'<cac:WinningParty><cac:PartyIdentification><cbc:ID schemeName="NIF">{cif}</cbc:ID>'
               f'</cac:PartyIdentification><cac:PartyName><cbc:Name>{nombre}</cbc:Name></cac:PartyName>'
               f'</cac:WinningParty>') if cif else ""
        lot = f"<cbc:ProcurementProjectLotID>{lote}</cbc:ProcurementProjectLotID>" if lote else ""
        tr += (f"<cac:TenderResult><cbc:ResultCode>{code}</cbc:ResultCode>{gan}"
               f"<cac:AwardedTenderedProject>{lot}<cac:LegalMonetaryTotal>"
               f'<cbc:TaxExclusiveAmount currencyID="EUR">{importe}</cbc:TaxExclusiveAmount>'
               f"</cac:LegalMonetaryTotal></cac:AwardedTenderedProject></cac:TenderResult>")
    aviso = (f"<place:ValidNoticeInfo><cbc:IssueDate>{publicada}</cbc:IssueDate></place:ValidNoticeInfo>"
             if publicada else "")
    return (f"<entry><id>{i}</id><title>{titulo}</title><link href=\"{i}\"/>"
            f"<updated>{updated}</updated>"
            f"<place:ContractFolderStatus>"
            f"<cbc:ContractFolderID>EXP-{n}</cbc:ContractFolderID>"
            f"<pce:ContractFolderStatusCode>{estado}</pce:ContractFolderStatusCode>"
            f"<cac:ProcurementProject><cbc:Name>{titulo}</cbc:Name>"
            f'<cac:BudgetAmount><cbc:TaxExclusiveAmount currencyID="EUR">{presu}</cbc:TaxExclusiveAmount>'
            f"</cac:BudgetAmount>"
            f"<cac:RequiredCommodityClassification><cbc:ItemClassificationCode>{cpv}</cbc:ItemClassificationCode>"
            f"</cac:RequiredCommodityClassification></cac:ProcurementProject>"
            f"<cac:TenderingProcess><cac:TenderSubmissionDeadlinePeriod><cbc:EndDate>{fin}</cbc:EndDate>"
            f"</cac:TenderSubmissionDeadlinePeriod></cac:TenderingProcess>"
            f"{tr}{aviso}</place:ContractFolderStatus></entry>")


def baja(n, cuando, tipo="ANULADA", raiz=ESTATAL):
    com = f'<at:comment type="{tipo}"/>' if tipo else ""
    return f'<at:deleted-entry ref="{idu(n, raiz)}" when="{cuando}">{com}</at:deleted-entry>'


def pagina(feed_updated, items, siguiente=None):
    """Un .atom: <updated> del propio fichero (el SNAPSHOT) + bajas y entradas. En los .atom
    reales las bajas van SIEMPRE delante de las entradas (0 intercaladas en 86 ficheros)."""
    nxt = f'<link rel="next" href="{siguiente}"/>' if siguiente else ""
    return (f'<feed {NSDECL}><id>feed</id><title>t</title><updated>{feed_updated}</updated>{nxt}'
            + "".join(items) + "</feed>").encode("utf-8")


# =============================================================================
# Base falsa: PostgREST de hoy (upsert ciego) + la RPC nueva (espejo del SQL)
# =============================================================================
def _inst(texto):
    """Fecha del feed -> datetime con zona; None si viene vacía, sin zona o mal formada.
    ORÁCULO propio del test (no usa feeds.instante)."""
    if texto is None:
        return None
    try:
        d = datetime.fromisoformat(str(texto).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else None


def _dia(x):
    return None if x is None else str(x)[:10]


# Columnas que escribe carga_versiones (la lista del INSERT de versiones_schema.sql).
COLS_LIC = ["licitacion_id", "titulo", "objeto", "num_expediente", "organo_contratacion", "cpv", "fuente",
            "presupuesto_con_iva", "presupuesto_sin_iva", "valor_estimado", "fecha_publicacion",
            "fecha_fin_plazo", "enlace", "ccaa", "lugar_ejecucion", "nuts_codigo",
            "atom_updated", "version_snapshot", "version_huella", "estado_expediente"]
OBLIGATORIAS = ("licitacion_id", "atom_updated", "version_snapshot", "version_huella")


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.headers = {}

    def json(self):
        return self._payload


class BaseFalsa:
    """Hace de Supabase: guarda licitaciones (por id) y adjudicaciones (por clave) en memoria.
    fallar_desde=k: a partir de la k-ésima llamada a la RPC responde 500 (para probar la
    reanudación), hasta que se llame a curar()."""

    def __init__(self, fallar_desde=None, sin_rpc=False):
        self.lic = {}       # licitacion_id -> dict de columnas
        self.adj = {}       # (licitacion_id, lote, cif) -> dict
        self.respuestas = []
        self.llamadas_rpc = 0
        self.fallar_desde = fallar_desde
        self.sin_rpc = sin_rpc            # como si versiones_schema.sql aún no se hubiera ejecutado

    def curar(self):
        self.fallar_desde = None

    # ---- lo que usa el código de HOY ----
    def post(self, url, headers=None, data=None, json=None, timeout=None):  # noqa: A002
        cuerpo = json if json is not None else (__import__("json").loads(data) if data else None)
        ruta = url.split("/rest/v1/", 1)[1]
        if ruta.startswith("licitaciones"):
            for fila in cuerpo:                      # merge-duplicates: escribe A CIEGAS
                self.lic.setdefault(fila["licitacion_id"], {}).update(fila)
            return _Resp(201)
        if ruta.startswith("adjudicaciones"):
            for fila in cuerpo:
                clave = (fila["licitacion_id"], fila["lote"], fila["cif_adjudicatario"])
                self.adj.setdefault(clave, {}).update(fila)
            return _Resp(201)
        if ruta.startswith("rpc/carga_versiones"):
            if self.sin_rpc:
                return _Resp(404, None, text='{"code":"PGRST202","message":"Could not find the function"}')
            p = cuerpo["p_lote"]
            if not p.get("licitaciones") and not p.get("bajas"):
                return _Resp(200, self._carga_versiones(p))   # el sondeo vacío no cuenta
            self.llamadas_rpc += 1
            if self.fallar_desde is not None and self.llamadas_rpc >= self.fallar_desde:
                return _Resp(500, None, text='{"code":"57014","message":"canceling statement due to statement timeout"}')
            r = self._carga_versiones(cuerpo["p_lote"])
            self.respuestas.append(r)
            return _Resp(200, r)
        return _Resp(200, None, text="0")            # cualquier otra RPC: no hace nada

    def delete(self, url, headers=None, params=None, timeout=None):
        expr = (params or {}).get("licitacion_id", "")
        if expr.startswith("in.("):
            ids = {x.strip('"') for x in expr[4:-1].split('","')}
            for clave in [c for c in self.adj if c[0] in ids]:
                del self.adj[clave]
        return _Resp(204)

    def get(self, url, headers=None, params=None, timeout=None):
        return _Resp(200, [])

    # ---- la RPC NUEVA: espejo de public.carga_versiones (versiones_schema.sql) ----
    def _carga_versiones(self, p):
        if "con_adjudicaciones" in p or "adjudicaciones" in p:
            raise AssertionError("carga_versiones: las adjudicaciones van DENTRO de cada licitación")
        lics = p.get("licitaciones", [])
        validas = [x for x in lics
                   if all(x.get(c) is not None for c in OBLIGATORIAS)
                   and _inst(x["atom_updated"]) and _inst(x["version_snapshot"])
                   and isinstance(x.get("adjudicaciones"), list)]
        unicas = {}
        for n, x in enumerate(validas):              # un id repetido: la más nueva (a igualdad, la última)
            clave = (_inst(x["atom_updated"]), _inst(x["version_snapshot"]), n)
            if x["licitacion_id"] not in unicas or clave > unicas[x["licitacion_id"]][0]:
                unicas[x["licitacion_id"]] = (clave, x)
        aplicadas = []
        for i, (_, x) in unicas.items():
            t = self.lic.get(i)
            au, vs = _inst(x["atom_updated"]), _inst(x["version_snapshot"])
            if t is None or t.get("atom_updated") is None:
                aplica = True
            else:
                tau = _inst(t["atom_updated"])
                tvs = _inst(t.get("version_snapshot")) or datetime.min.replace(tzinfo=timezone.utc)
                aplica = au > tau or (au == tau and vs >= tvs and x["version_huella"] != t.get("version_huella"))
            if not aplica:
                continue
            viejo = t or {}
            fila = {c: x.get(c) for c in COLS_LIC}  # el SQL pone NULL en lo que no venga
            for c in ("primera_vez", "estado_adjudicacion", "n_lotes_desiertos"):
                fila[c] = viejo.get(c)
            conserva = viejo.get("baja_en") and _inst(viejo["baja_en"]) > au
            fila["baja_en"] = viejo.get("baja_en") if conserva else None
            fila["baja_tipo"] = viejo.get("baja_tipo") if conserva else None
            self.lic[i] = fila
            aplicadas.append((i, x))
        borr = ins = recalc = 0
        for i, x in aplicadas:
            for clave in [c for c in self.adj if c[0] == i]:
                del self.adj[clave]
                borr += 1
            nuevas = {}
            for f in x["adjudicaciones"]:           # misma clave repetida: gana la última
                nuevas[(i, f.get("lote") or "", f.get("cif_adjudicatario") or "")] = {
                    **f, "licitacion_id": i, "lote": f.get("lote") or "",
                    "cif_adjudicatario": f.get("cif_adjudicatario") or "", "atom_updated": x["atom_updated"]}
            self.adj.update(nuevas)
            ins += len(nuevas)
            if not nuevas and (self.lic[i].get("estado_adjudicacion") is not None
                               or self.lic[i].get("n_lotes_desiertos") is not None):
                self.lic[i]["estado_adjudicacion"] = self.lic[i]["n_lotes_desiertos"] = None
                recalc += 1
        bajas = {}
        for b in p.get("bajas", []):
            if b.get("licitacion_id") and _inst(b.get("baja_en")):
                if b["licitacion_id"] not in bajas or _inst(b["baja_en"]) > _inst(bajas[b["licitacion_id"]]["baja_en"]):
                    bajas[b["licitacion_id"]] = b
        marc = sin_fila = 0
        for i, b in bajas.items():
            t = self.lic.get(i)
            if t is None:
                sin_fila += 1
                continue
            cuando = _inst(b["baja_en"])
            if (t.get("atom_updated") is None or cuando > _inst(t["atom_updated"])) and (
                    t.get("baja_en") is None or cuando > _inst(t["baja_en"])):
                t["baja_en"], t["baja_tipo"] = b["baja_en"], b.get("baja_tipo")
                marc += 1
        return {"recibidas": len(lics), "descartadas": len(lics) - len(validas),
                "duplicadas": len(validas) - len(unicas), "aplicadas": len(aplicadas),
                "rechazadas": len(unicas) - len(aplicadas), "adj_borradas": borr, "adj_insertadas": ins,
                "sin_adjudicacion_recalculadas": recalc, "bajas_recibidas": len(bajas),
                "bajas_marcadas": marc, "bajas_sin_fila": sin_fila}

    # ---- ayudas para las comprobaciones ----
    def adj_de(self, i):
        return sorted((f["lote"], f["cif_adjudicatario"], float(f["importe_sin_iva"] or 0),
                       f.get("resultado_code")) for c, f in self.adj.items() if c[0] == i)

    def fila(self, i):
        return self.lic.get(i) or {}


_NADA = object()


@contextmanager
def parches(**cambios):
    """Sustituye atributos de módulos ('modulo__atributo') y los restaura al salir. Si el
    atributo aún no existe (código nuevo), se crea y luego se borra."""
    modulos = {"bc": bc, "feeds": feeds, "requests": requests}
    guardados = []
    try:
        for clave, valor in cambios.items():
            mod_nombre, attr = clave.split("__", 1)
            mod = modulos[mod_nombre]
            guardados.append((mod, attr, getattr(mod, attr, _NADA)))
            setattr(mod, attr, valor)
        yield
    finally:
        for mod, attr, viejo in reversed(guardados):
            if viejo is _NADA:
                delattr(mod, attr)
            else:
                setattr(mod, attr, viejo)


def _sin_red(*a, **k):
    raise RuntimeError("test_versiones: el código ha intentado usar la RED")


def _parches_comunes(base):
    """Lo que no se prueba aquí: red, esperas, menores, refrescos y resúmenes."""
    nada = lambda *a, **k: None  # noqa: E731
    return dict(
        bc___preparar_upsert=lambda: (base, {"apikey": "K"}, f"{URL}/rest/v1/licitaciones?on_conflict=licitacion_id", URL),
        bc___tabla_adjudicaciones_lista=lambda *a, **k: True,
        bc__menores_incremental=nada, bc__refrescar_cobertura_menores=nada,
        bc___resumen_post_carga=nada, bc___resumen_adjudicaciones=nada,
        bc__automarcar_ganadas=nada, bc__refrescar_competidores=nada,
        bc__refrescar_desiertas_bucle=nada,
        requests__get=_sin_red, requests__post=_sin_red, requests__request=_sin_red,
    )


@contextmanager
def _sin_esperas():
    viejo = bc.time.sleep
    bc.time.sleep = lambda s: None
    try:
        yield
    finally:
        bc.time.sleep = viejo


def corre_diario(base, paginas, ahora=None):
    """Ejecuta bc.diario(['estatal']) con el feed vivo sustituido por 'paginas' (bytes de
    cada .atom en el orden de la cadena rel=next). Da el feed en los DOS formatos: el de
    hoy (descarga_entradas) y el del arreglo (descarga_feed: versiones con su snapshot y
    bajas)."""
    raices = [etree.fromstring(p) for p in paginas]
    A = feeds.ATOM_NS
    entradas = [e for r in raices for e in r.findall("atom:entry", A)]
    versiones = [(e, r.findtext("atom:updated", namespaces=A)) for r in raices for e in r.findall("atom:entry", A)]
    T = "{http://purl.org/atompub/tombstones/1.0}"
    bajas = []
    for r in raices:
        for d in r.findall(T + "deleted-entry"):
            c = d.find(T + "comment")
            bajas.append({"id": d.get("ref"), "cuando": d.get("when"), "tipo": c.get("type") if c is not None else None})

    def desc_entradas(url, *a, **k):
        return (entradas if "sindicacion_643" in url else []), len(raices), False

    def desc_feed(url, *a, **k):
        if "sindicacion_643" in url:
            return versiones, bajas, len(raices), False
        return [], [], 0, False

    extra = {"bc__AHORA_ISO": ahora} if ahora else {}
    with _sin_esperas(), parches(bc__descarga_entradas=desc_entradas, feeds__descarga_feed=desc_feed,
                                 bc__descarga_feed=desc_feed, **extra, **_parches_comunes(base)):
        bc.diario(["estatal"])


def corre_carga(base, unidades, solo_catalogo=False, tmp=None):
    """Ejecuta UNA llamada a bc.carga() sobre ZIP de juguete. unidades = [(fuente, periodo,
    miembros)], en el orden en que se le pasan a carga(). Los .atom de cada ZIP van en el
    ORDEN REAL de Datos Abiertos (fichero base primero y luego los días ascendentes).
    tmp: carpeta a reutilizar (checkpoint y ZIP) para probar una reanudación."""
    propia = tmp is None
    tmp = Path(tmp or tempfile.mkdtemp(prefix="test_versiones_"))
    try:
        for fuente, periodo, miembros in unidades:
            destino = tmp / f"{fuente}_{periodo}.zip"
            if not destino.exists():
                with zipfile.ZipFile(destino, "w") as z:
                    for nombre, blob in miembros:
                        z.writestr(nombre, blob)
        with _sin_esperas(), parches(bc__CACHE_DIR=tmp, bc__ESTADO_LOAD=tmp / "estado.json", **_parches_comunes(base)):
            bc.carga([(f, p) for f, p, _ in unidades], limpiar=False, solo_catalogo=solo_catalogo)
    finally:
        if propia:
            shutil.rmtree(tmp, ignore_errors=True)


def zip_de(periodo, *paginas_fechadas, base_snapshot="2026-10-31T03:00:00.000+01:00", base_items=()):
    """Miembros de un ZIP: el fichero base y luego los .atom fechados que se pasen, cada uno
    como (sufijo 'AAAAMMDD_HHMMSS[_n]', snapshot, items), EN EL ORDEN EN QUE SE PASAN."""
    m = [(f"{BASE_ATOM}.atom", pagina(base_snapshot, list(base_items)))]
    for sufijo, snap, items in paginas_fechadas:
        m.append((f"{BASE_ATOM}_{sufijo}.atom", pagina(snap, list(items))))
    return m


# =============================================================================
# BLOQUE 1 · La regla de versión (funciones puras nuevas de feeds.py)
# =============================================================================
def test_instante_con_zona():
    comprueba("instante: +02:00 se pasa a UTC",
              feeds.instante("2026-10-14T18:07:20.126+02:00"),
              datetime(2026, 10, 14, 16, 7, 20, 126000, tzinfo=timezone.utc))
    comprueba("instante: sin milisegundos",
              feeds.instante("2026-10-14T18:07:20+02:00"), datetime(2026, 10, 14, 16, 7, 20, tzinfo=timezone.utc))
    comprueba("instante: misma hora con otra zona es el mismo instante",
              feeds.instante("2026-03-29T01:30:00.000+01:00") == feeds.instante("2026-03-29T00:30:00.000Z"), True)
    comprueba("instante: vacío -> None", feeds.instante(""), None)
    comprueba("instante: None -> None", feeds.instante(None), None)
    comprueba("instante: sin zona -> None (no se adivina)", feeds.instante("2026-10-14T18:07:20"), None)
    comprueba("instante: basura -> None", feeds.instante("ayer"), None)


def test_cambio_de_hora_25_10_2026():
    a, b = "2026-10-25T02:30:00.000+02:00", "2026-10-25T02:10:00.000+01:00"
    comprueba("cambio de hora: como TEXTO ordena al revés (la trampa)", a > b, True)
    comprueba("cambio de hora: como instante, b es posterior", feeds.instante(b) > feeds.instante(a), True)
    comprueba("cambio de hora: es_mas_nueva(b, a)", feeds.es_mas_nueva((b, None), (a, None)), True)


def test_es_mas_nueva():
    v1 = ("2026-10-05T09:16:06.631+02:00", "2026-10-06T03:00:00.000+02:00")
    v2 = ("2026-10-07T09:16:24.294+02:00", "2026-10-08T03:00:00.000+02:00")
    comprueba("es_mas_nueva: estrictamente mayor", feeds.es_mas_nueva(v2, v1), True)
    comprueba("es_mas_nueva: la vieja no gana", feeds.es_mas_nueva(v1, v2), False)
    comprueba("es_mas_nueva: igual en todo -> no se hace nada", feeds.es_mas_nueva(v1, v1), False)
    comprueba("es_mas_nueva: sin nada guardado -> sí", feeds.es_mas_nueva(v1, None), True)
    # Desempate (republicación de septiembre): misma marca, snapshot posterior.
    pub = ("2026-08-31T19:19:52.539+02:00", "2026-09-01T20:13:32.000+02:00")
    anul = ("2026-08-31T19:19:52.539+02:00", "2026-09-23T09:27:41.000+02:00")
    comprueba("es_mas_nueva: empate de marca, snapshot más nuevo gana", feeds.es_mas_nueva(anul, pub), True)
    comprueba("es_mas_nueva: empate de marca, snapshot más viejo no", feeds.es_mas_nueva(pub, anul), False)
    # 20434791: snapshot MÁS NUEVO con atom:updated MÁS VIEJO -> manda la marca.
    pub2 = ("2026-09-16T14:40:48.962+02:00", "2026-09-16T20:23:00.000+02:00")
    pre2 = ("2026-09-16T14:39:22.186+02:00", "2026-09-26T21:08:17.000+02:00")
    comprueba("es_mas_nueva: manda la marca aunque el snapshot sea más viejo", feeds.es_mas_nueva(pub2, pre2), True)


def _e(xml, snapshot="2026-10-01T00:00:00+02:00"):
    return etree.fromstring(pagina(snapshot, [xml])).find("atom:entry", feeds.ATOM_NS)


def test_elige_versiones_independiente_del_orden():
    vs = [
        (_e(entrada(20595831, "2026-09-30T10:00:00.000+02:00", presu="1.0")), "2026-10-01T03:00:00.000+02:00"),
        (_e(entrada(20595831, "2026-10-05T10:00:00.000+02:00", presu="2.0")), "2026-10-06T03:00:00.000+02:00"),
        (_e(entrada(20595831, "2026-10-15T10:00:00.000+02:00", presu="3.0")), "2026-10-16T03:00:00.000+02:00"),
        (_e(entrada(20329062, "2026-08-31T19:19:52.539+02:00", estado="PUB")), "2026-09-01T20:13:32.000+02:00"),
        (_e(entrada(20329062, "2026-08-31T19:19:52.539+02:00", estado="ANUL")), "2026-09-23T09:27:41.000+02:00"),
        # cambio de hora: la de +01:00 es POSTERIOR aunque como texto parezca anterior
        (_e(entrada(19000025, "2026-10-25T02:30:00.000+02:00", presu="1.0")), "2026-10-25T02:45:00.000+02:00"),
        (_e(entrada(19000025, "2026-10-25T02:10:00.000+01:00", presu="2.0")), "2026-10-25T03:00:00.000+01:00"),
    ]
    etiqueta = {id(e): i for i, (e, _) in enumerate(vs)}   # qué versión es cada elemento
    resultados = set()
    for perm in itertools.permutations(vs):
        elegidas = feeds.elige_versiones(perm)
        resultados.add(tuple(etiqueta[id(elegidas[idu(n)][0])] for n in (20595831, 20329062, 19000025)))
    comprueba("elige_versiones: las 5.040 permutaciones dan lo mismo (la nueva, la ANUL y la de +01:00)",
              resultados, {(2, 4, 6)})


def test_elige_versiones_marca_antes_que_snapshot():
    """20434791 (estatal_202609.zip): el .atom MÁS NUEVO (26/09) trae una PRE con la marca más
    VIEJA; gana la PUB del 16/09 14:40, que está en un .atom anterior."""
    vs = [
        (_e(entrada(20434791, "2026-09-16T14:39:22.186+02:00", estado="PRE", presu="1.0")), "2026-09-26T21:08:17.000+02:00"),
        (_e(entrada(20434791, "2026-09-07T10:00:00.000+02:00", estado="PRE", presu="0.5")), "2026-09-23T09:27:41.000+02:00"),
        (_e(entrada(20434791, "2026-09-16T14:40:48.962+02:00", estado="PUB", presu="2.0")), "2026-09-16T20:23:00.000+02:00"),
    ]
    elegida = feeds.elige_versiones(vs)[idu(20434791)]
    comprueba("elige_versiones: gana la marca más nueva (PUB), no el snapshot más nuevo",
              (feeds.extrae_entrada(elegida[0], "estatal").get("estado_expediente"), elegida[1]),
              ("PUB", "2026-09-16T20:23:00.000+02:00"))


# =============================================================================
# BLOQUE 2 · El orden de los ZIP (re-backfill, carga)
# =============================================================================
def test_orden_atoms():
    P = "PlataformasAgregadasSinMenores"
    real = [  # orden REAL dentro de los ZIP (base, luego días ascendentes; páginas sueltas)
        f"{P}_20241029_080034_8.atom",           # agregadas_2024 empieza por esta página suelta
        f"{P}.atom",
        f"{P}_20210930_202609.atom",            # ficheros viejos metidos en el ZIP
        f"{P}_20261001_030040.atom",
        f"{P}_20261001_030040_1.atom",
        f"{P}_20261001_030040_2.atom",
        f"{P}_20261001_030040_10.atom",          # sufijos de dos cifras: _10 va después de _2
        f"{P}_20260923_092741.atom",             # dos snapshots el mismo día
        f"{P}_20260923_211008.atom",
        f"{P}_20260923_211008_24.atom",
        f"{P}_20261010_030108.atom",
        f"{P}_20261010_030108_1.atom",
    ]
    esperado = [
        f"{P}.atom",
        f"{P}_20261010_030108.atom",
        f"{P}_20261010_030108_1.atom",
        f"{P}_20261001_030040.atom",
        f"{P}_20261001_030040_1.atom",
        f"{P}_20261001_030040_2.atom",
        f"{P}_20261001_030040_10.atom",
        f"{P}_20260923_211008.atom",
        f"{P}_20260923_211008_24.atom",
        f"{P}_20260923_092741.atom",
        f"{P}_20241029_080034_8.atom",
        f"{P}_20210930_202609.atom",
    ]
    comprueba("orden_atoms: base, luego snapshots de más nuevo a más viejo (y _1 < _2 < _10)",
              bc.orden_atoms(list(reversed(real))), esperado)
    comprueba("orden_atoms: no depende del orden de entrada", bc.orden_atoms(real), esperado)


def test_carga_zip_mensual_fin_de_plazo():
    """19366614 (Piera): versiones del 04/05 (fin 08/05) y del 08/05 (fin 21/05). La base
    quedó con el 08/05 porque el .atom del 05/05 va antes que el del 09/05 en el ZIP."""
    base = BaseFalsa()
    n, R = 19366614, AGREGADAS
    miembros = [
        ("PlataformasAgregadasSinMenores.atom", pagina("2026-06-01T04:00:00.000+02:00", [])),
        ("PlataformasAgregadasSinMenores_20260505_040056.atom", pagina("2026-05-05T04:00:56.000+02:00",
            [entrada(n, "2026-05-04T14:09:58.000+02:00", fin="2026-05-08", raiz=R)])),
        ("PlataformasAgregadasSinMenores_20260509_040108_2.atom", pagina("2026-05-09T04:01:08.000+02:00",
            [entrada(n, "2026-05-08T10:05:19.000+02:00", fin="2026-05-21", raiz=R)])),
    ]
    corre_carga(base, [("agregadas", "202605", miembros)])
    comprueba("carga (ZIP mensual): fin de plazo de la ÚLTIMA versión",
              _dia(base.fila(idu(n, R)).get("fecha_fin_plazo")), "2026-05-21")


def test_carga_zip_anual_no_pierde_la_adjudicacion():
    """17260538 (La Fe, ganada por LODEPA): en el ZIP anual la primera versión que se ve es la
    PUB del 12/05/2025, sin resultado; hoy se saltan las 3 siguientes y la adjudicación no se
    sube nunca."""
    base = BaseFalsa()
    n = 17260538
    miembros = zip_de("2025",
        ("20250512_182559", "2025-05-12T18:25:59.000+02:00",
         [entrada(n, "2025-05-12T14:22:18.177+02:00", estado="PUB", fin="2025-05-26", presu="4812.0")]),
        ("20250530_182341_3", "2025-05-30T18:23:41.000+02:00",
         [entrada(n, "2025-05-30T09:21:37.000+02:00", estado="ADJ", fin="2025-05-26", presu="4812.0",
                  adjs=[("", LODEPA, "LODEPA", "4795.0", "8")])]),
        ("20250917_182726_1", "2025-09-17T18:27:26.000+02:00",
         [entrada(n, "2025-09-17T12:21:21.178+02:00", estado="RES", fin="2025-05-26", presu="4812.0",
                  adjs=[("", LODEPA, "LODEPA", "4795.0", "9")])]),
        base_snapshot="2025-12-31T18:00:00.000+01:00")
    corre_carga(base, [("estatal", "2025", miembros)])
    comprueba("carga (ZIP anual): está la adjudicación de LODEPA de la última versión (código 9)",
              base.adj_de(idu(n)), [("", LODEPA, 4795.0, "9")])
    comprueba("carga (ZIP anual): la licitación guarda el estado de la última (RES)",
              base.fila(idu(n)).get("estado_expediente"), "RES")


def test_carga_marca_antes_que_snapshot():
    """20434791 en un ZIP: el .atom más NUEVO trae la versión con la marca más VIEJA (PRE). Un
    cliente que se quede con «la primera que ve» en orden_atoms se equivoca."""
    base = BaseFalsa()
    n = 20434791
    miembros = zip_de("202609",
        ("20260916_202300", "2026-09-16T20:23:00.000+02:00",
         [entrada(n, "2026-09-16T14:40:48.962+02:00", estado="PUB", presu="2.0")]),
        ("20260923_092741", "2026-09-23T09:27:41.000+02:00",
         [entrada(n, "2026-09-07T10:00:00.000+02:00", estado="PRE", presu="0.5")]),
        ("20260926_210817_20", "2026-09-26T21:08:17.000+02:00",
         [entrada(n, "2026-09-16T14:39:22.186+02:00", estado="PRE", presu="1.0")]),
        base_snapshot="2026-09-30T03:00:00.000+02:00")
    corre_carga(base, [("estatal", "202609", miembros)])
    f = base.fila(idu(n))
    comprueba("carga: gana la marca más nueva (PUB, 2.0) aunque esté en un .atom más viejo",
              (f.get("estado_expediente"), float(f.get("presupuesto_sin_iva") or 0)), ("PUB", 2.0))


def test_carga_empate_estado_hacia_atras():
    """20363389: misma marca 01/09 09:28:55.250. El diario del 01/09 trae ADJ con su ganador y la
    republicación del 23/09, EV sin adjudicación (y menos avisos). Parece la copia «vieja», pero
    la web dice Evaluación: la adjudicación se cayó. Gana el snapshot más reciente, no la copia con
    más avisos o el estado más avanzado (medido el 30/10: así acierta en 8 de 8 casos comprobados)."""
    n = 20363389
    marca = "2026-09-01T09:28:55.250+02:00"
    adj = entrada(n, marca, estado="ADJ", adjs=[("", "B11111111", "X", "900.0", "8")])
    ev = entrada(n, marca, estado="EV")
    base = BaseFalsa()
    corre_carga(base, [("estatal", "202609", zip_de("202609",
        ("20260901_201332_3", "2026-09-01T20:13:32.289+02:00", [adj]),
        ("20260923_092741_3", "2026-09-23T09:27:41.388+02:00", [ev]),
        base_snapshot="2026-09-30T03:00:00.000+02:00"))])
    comprueba("carga: empate con estado hacia atrás -> EV y sin adjudicación",
              (base.fila(idu(n)).get("estado_expediente"), base.adj_de(idu(n))), ("EV", []))
    finales = set()
    for orden in (([adj], "2026-09-01T20:13:32.289+02:00", [ev], "2026-09-23T09:27:41.388+02:00"),
                  ([ev], "2026-09-23T09:27:41.388+02:00", [adj], "2026-09-01T20:13:32.289+02:00")):
        base = BaseFalsa()
        corre_diario(base, [pagina(orden[1], orden[0])])
        corre_diario(base, [pagina(orden[3], orden[2])])
        finales.add((base.fila(idu(n)).get("estado_expediente"), tuple(base.adj_de(idu(n)))))
    comprueba("entre ejecuciones: EV sin adjudicación en los dos órdenes", finales, {("EV", ())})


def test_carga_solo_catalogo_sigue_mandando_adjudicaciones():
    """Con la RPC, escribir una versión SUSTITUYE sus adjudicaciones: un --solo-catalogo que
    mandara listas vacías las borraría. Tiene que mandar las de verdad."""
    base = BaseFalsa()
    n = 18900010
    miembros = zip_de("202608", ("20260810_030000", "2026-08-10T03:00:00.000+02:00",
                                 [entrada(n, "2026-08-09T10:00:00.000+02:00", estado="RES",
                                          adjs=[("", "B12345678", "X", "300.0", "9")])]),
                      base_snapshot="2026-08-31T03:00:00.000+02:00")
    corre_carga(base, [("estatal", "202608", miembros)], solo_catalogo=True)
    comprueba("carga --solo-catalogo: la versión escrita lleva sus adjudicaciones",
              base.adj_de(idu(n)), [("", "B12345678", 300.0, "9")])


# =============================================================================
# BLOQUE 3 · El feed vivo con el mismo expediente en varias páginas (diario)
# =============================================================================
def test_diario_mismo_id_en_varias_paginas():
    """20603060 (AUCORSA): la del 01/10 trae 3 adjudicatarios; la del 05/10, 2 (y 443 € en vez
    de 418 €). El diario del 08/10 escribió la del 01/10."""
    base = BaseFalsa()
    n = 20603060
    v_nueva = entrada(n, "2026-10-05T08:31:56.704+02:00", estado="RES", fin="2026-10-01", presu="912.18",
                      adjs=[("1", "A00000001", "EMPRESA A", "21.0", "8"), ("1", "B00000006", "EMPRESA C", "443.0", "8")])
    v_vieja = entrada(n, "2026-10-01T13:54:51.274+02:00", estado="RES", fin="2026-10-01", presu="912.18",
                      adjs=[("1", "A00000002", "EMPRESA B", "10.96", "8"), ("1", "B00000006", "EMPRESA C", "418.0", "8"),
                            ("1", "A00000001", "EMPRESA A", "21.0", "8")])
    paginas = [
        pagina("2026-10-08T03:00:00.000+02:00", [entrada(1, "2026-10-08T01:00:00.000+02:00")]),
        pagina("2026-10-06T20:18:59.000+02:00", [v_nueva]),
        pagina("2026-10-02T20:17:04.000+02:00", [v_vieja]),
    ]
    corre_diario(base, paginas)
    comprueba("diario: adjudicaciones de la versión del 05/10 (2 ganadores, 443 €)",
              base.adj_de(idu(n)), [("1", "A00000001", 21.0, "8"), ("1", "B00000006", 443.0, "8")])


def test_diario_empate_republicacion():
    """20329062: misma marca 31/08 19:19:52.539. En el snapshot del 01/09 sale PUB y en el del
    23/09 sale ANUL. Gana la del snapshot más reciente."""
    base = BaseFalsa()
    n = 20329062
    paginas = [
        pagina("2026-09-23T09:27:41.000+02:00", [entrada(n, "2026-08-31T19:19:52.539+02:00", estado="ANUL", fin="2026-09-15")]),
        pagina("2026-09-01T20:13:32.000+02:00", [entrada(n, "2026-08-31T19:19:52.539+02:00", estado="PUB", fin="2026-09-15")]),
    ]
    corre_diario(base, paginas)
    comprueba("diario: empate de marca -> estado del snapshot más reciente (ANUL)",
              base.fila(idu(n)).get("estado_expediente"), "ANUL")


def test_diario_marca_antes_que_snapshot():
    """20434791 por el feed vivo: la página MÁS NUEVA trae la PRE con la marca más vieja."""
    base = BaseFalsa()
    n = 20434791
    corre_diario(base, [
        pagina("2026-09-26T21:08:17.000+02:00", [entrada(n, "2026-09-16T14:39:22.186+02:00", estado="PRE", presu="1.0")]),
        pagina("2026-09-23T09:27:41.000+02:00", [entrada(n, "2026-09-07T10:00:00.000+02:00", estado="PRE", presu="0.5")]),
        pagina("2026-09-16T20:23:00.000+02:00", [entrada(n, "2026-09-16T14:40:48.962+02:00", estado="PUB", presu="2.0")]),
    ])
    f = base.fila(idu(n))
    comprueba("diario: gana la marca más nueva (PUB, 2.0), no la primera que aparece",
              (f.get("estado_expediente"), float(f.get("presupuesto_sin_iva") or 0)), ("PUB", 2.0))


def test_diario_cambio_de_hora():
    """25/10/2026: la versión de las 02:10+01:00 es POSTERIOR a la de las 02:30+02:00, aunque como
    texto parezca anterior."""
    base = BaseFalsa()
    n = 19000025
    corre_diario(base, [
        pagina("2026-10-25T03:00:00.000+01:00", [entrada(n, "2026-10-25T02:10:00.000+01:00", presu="2.0")]),
        pagina("2026-10-25T02:45:00.000+02:00", [entrada(n, "2026-10-25T02:30:00.000+02:00", presu="1.0")]),
    ])
    comprueba("diario: con el cambio de hora gana la de +01:00",
              float(base.fila(idu(n)).get("presupuesto_sin_iva") or 0), 2.0)


def test_diario_licitacion_y_adjudicaciones_de_la_misma_version():
    """18734395: la del 13/10 está en EV sin resultado y la del 14/10 en ADJ. La licitación y las
    adjudicaciones tienen que salir de la MISMA versión (la del 14/10)."""
    base = BaseFalsa()
    n = 18734395
    paginas = [
        pagina("2026-10-15T03:00:00.000+02:00", [entrada(n, "2026-10-14T16:53:00.000+02:00", estado="ADJ", fin="2026-09-30",
                                                          adjs=[("3", "B00000005", "X", "100.0", "8")])]),
        pagina("2026-10-14T03:00:00.000+02:00", [entrada(n, "2026-10-13T12:59:00.000+02:00", estado="EV", fin="2026-09-30")]),
    ]
    corre_diario(base, paginas)
    fila = base.fila(idu(n))
    comprueba("diario: la licitación guarda la versión del 14/10",
              (fila.get("estado_expediente"), _inst(fila.get("atom_updated"))),
              ("ADJ", _inst("2026-10-14T16:53:00.000+02:00")))
    comprueba("diario: y sus adjudicaciones", base.adj_de(idu(n)), [("3", "B00000005", 100.0, "8")])


def test_diario_idempotente():
    """Dos pasadas con el MISMO feed (a otra hora) no reescriben nada: la huella no puede llevar
    la hora de carga (ultima_vez, updated_at)."""
    base = BaseFalsa()
    paginas = [pagina("2026-10-15T03:00:00.000+02:00", [
        entrada(18900020, "2026-10-14T10:00:00.000+02:00", estado="RES", adjs=[("1", "B11111111", "X", "10.0", "9")]),
        entrada(18900021, "2026-10-14T09:00:00.000+02:00"),
    ])]
    corre_diario(base, paginas, ahora="2026-10-15T10:00:00+00:00")
    corre_diario(base, paginas, ahora="2026-10-16T10:00:00+00:00")
    ultima = base.respuestas[-1] if base.respuestas else {}
    comprueba("diario: la segunda pasada idéntica no aplica nada ni borra adjudicaciones",
              (ultima.get("aplicadas"), ultima.get("adj_borradas"), ultima.get("rechazadas")), (0, 0, 2))


# =============================================================================
# BLOQUE 4 · Ejecuciones separadas en cualquier orden (la guarda de la base)
# =============================================================================
def test_troceado_orden_inverso():
    """Lo del 15/09: la tanda de 2026 primero y la de 2025 DESPUÉS. La de 2025 no puede pisar."""
    base = BaseFalsa()
    n = 18900001
    corre_carga(base, [("estatal", "202602", zip_de("202602", ("20260211_030000", "2026-02-11T03:00:00.000+01:00",
        [entrada(n, "2026-02-10T10:00:00.000+01:00", estado="RES", presu="2000.0")])))])
    corre_carga(base, [("estatal", "202511", zip_de("202511", ("20251104_030000", "2025-11-04T03:00:00.000+01:00",
        [entrada(n, "2025-11-03T10:00:00.000+01:00", estado="PUB", presu="1000.0")])))], solo_catalogo=True)
    f = base.fila(idu(n))
    comprueba("troceado: queda la versión de 2026 aunque la de 2025 se cargue después",
              (float(f.get("presupuesto_sin_iva") or 0), f.get("estado_expediente")), (2000.0, "RES"))


def test_troceado_seis_tandas_del_15_09():
    """Las tandas del 15/09 en su orden real (2026, luego 2025 y 2024) y al revés: mismo final."""
    finales = set()
    n = 18900002
    versiones = [  # (periodo, sufijo, snapshot, updated, presupuesto)
        ("202609", "20260911_030000", "2026-09-11T03:00:00.000+02:00", "2026-09-10T10:00:00.000+02:00", "3.0"),
        ("202503", "20250311_030000", "2025-03-11T03:00:00.000+01:00", "2025-03-10T10:00:00.000+01:00", "2.0"),
        ("202410", "20241011_030000", "2024-10-11T03:00:00.000+02:00", "2024-10-10T10:00:00.000+02:00", "1.0"),
    ]
    for orden in (versiones, list(reversed(versiones))):
        base = BaseFalsa()
        for periodo, suf, snap, upd, presu in orden:
            corre_carga(base, [("estatal", periodo, zip_de(periodo, (suf, snap, [entrada(n, upd, presu=presu)])))])
        finales.add(float(base.fila(idu(n)).get("presupuesto_sin_iva") or 0))
    comprueba("troceado: en cualquier orden queda la de 2026", finales, {3.0})


def test_empate_entre_dos_ejecuciones():
    """La republicación de septiembre llega en OTRA ejecución: misma marca, snapshot posterior y
    contenido distinto -> se escribe; y al revés no se deshace."""
    finales = set()
    n = 20329062
    pub = pagina("2026-09-01T20:13:32.000+02:00", [entrada(n, "2026-08-31T19:19:52.539+02:00", estado="PUB")])
    anul = pagina("2026-09-23T09:27:41.000+02:00", [entrada(n, "2026-08-31T19:19:52.539+02:00", estado="ANUL")])
    for orden in ((pub, anul), (anul, pub)):
        base = BaseFalsa()
        for p in orden:
            corre_diario(base, [p])
        finales.add(base.fila(idu(n)).get("estado_expediente"))
    comprueba("empate entre ejecuciones: queda la del snapshot más reciente en los dos órdenes", finales, {"ANUL"})


def test_empate_mismo_snapshot_otras_adjudicaciones():
    """Releer el MISMO .atom con otro contenido (p. ej. un extractor mejorado) tiene que escribir,
    también cuando solo cambian las ADJUDICACIONES: la huella las incluye."""
    base = BaseFalsa()
    n = 18900030
    snap = "2026-10-10T03:00:00.000+02:00"
    corre_diario(base, [pagina(snap, [entrada(n, "2026-10-09T10:00:00.000+02:00", estado="RES",
                                              adjs=[("1", "B11111111", "X", "10.0", "9")])])])
    corre_diario(base, [pagina(snap, [entrada(n, "2026-10-09T10:00:00.000+02:00", estado="RES",
                                              adjs=[("1", "B11111111", "X", "10.0", "9"), ("2", "B22222222", "Y", "20.0", "9")])])])
    comprueba("empate con el mismo snapshot: si cambian las adjudicaciones, se escriben",
              base.adj_de(idu(n)), [("1", "B11111111", 10.0, "9"), ("2", "B22222222", 20.0, "9")])


# =============================================================================
# BLOQUE 5 · Adjudicaciones
# =============================================================================
def test_adj_nueva_reemplaza_sin_huerfanas():
    """19554568: la del 03/06 (ADJ) trae los lotes 610363/610364; la del 25/06 (RES), los mismos
    lotes renumerados 618417/618418. Cargar después un ZIP con la vieja no puede dejar 4 filas."""
    # (el caso real es de agregadas; aquí va por el feed estatal de la prueba, da igual)
    base = BaseFalsa()
    n = 19554568
    nueva = entrada(n, "2026-06-25T10:00:00.000+02:00", estado="RES",
                    adjs=[("618417", "A00000003", "X", "100.0", "9"), ("618418", "W0000007H", "Y", "200.0", "9")])
    vieja = entrada(n, "2026-06-03T10:00:00.000+02:00", estado="ADJ",
                    adjs=[("610363", "A00000003", "X", "100.0", "8"), ("610364", "W0000007H", "Y", "200.0", "8")])
    corre_diario(base, [pagina("2026-06-26T03:00:00.000+02:00", [nueva])])     # el diario escribe la nueva
    corre_carga(base, [("estatal", "202606", zip_de("202606",                    # y luego un re-backfill trae la vieja
        ("20260604_040000", "2026-06-04T04:00:00.000+02:00", [vieja]), base_snapshot="2026-06-30T03:00:00.000+02:00"))])
    comprueba("adjudicaciones: solo los lotes de la versión nueva, sin huérfanas",
              base.adj_de(idu(n)), [("618417", "A00000003", 100.0, "9"), ("618418", "W0000007H", 200.0, "9")])


def test_adj_nueva_sin_resultado_borra():
    """19116501: RES con ganador el 01/09; ANUL sin TenderResult el 10/09. La base tiene que
    quedarse SIN filas (hoy conserva el ganador)."""
    base = BaseFalsa()
    n = 19116501
    corre_diario(base, [pagina("2026-09-02T03:00:00.000+02:00", [entrada(n, "2026-09-01T10:00:00.000+02:00", estado="RES",
                                                                          adjs=[("", "B00000004", "Z", "500.0", "9")])])])
    corre_diario(base, [pagina("2026-09-11T03:00:00.000+02:00", [entrada(n, "2026-09-10T10:00:00.000+02:00", estado="ANUL")])])
    comprueba("adjudicaciones: una versión nueva SIN resultado borra las filas viejas", base.adj_de(idu(n)), [])


def test_adj_vieja_no_toca():
    base = BaseFalsa()
    n = 18900003
    nueva = entrada(n, "2026-10-05T10:00:00.000+02:00", estado="RES", adjs=[("", "B11111111", "Nuevo", "443.0", "9")])
    vieja = entrada(n, "2026-10-01T10:00:00.000+02:00", estado="ADJ", adjs=[("", "B22222222", "Viejo", "418.0", "8")])
    corre_diario(base, [pagina("2026-10-06T03:00:00.000+02:00", [nueva])])
    corre_diario(base, [pagina("2026-10-02T03:00:00.000+02:00", [vieja])])
    comprueba("adjudicaciones: una versión vieja que llega después no toca nada",
              base.adj_de(idu(n)), [("", "B11111111", 443.0, "9")])


# =============================================================================
# BLOQUE 6 · Bajas (<at:deleted-entry>)
# =============================================================================
def _el(xml):
    return etree.fromstring(pagina("2026-10-15T03:00:00.000+02:00", [xml])).find(
        "{http://purl.org/atompub/tombstones/1.0}deleted-entry")


def test_parsea_baja():
    comprueba("parsea_baja: CERRADA (agregadas)",
              feeds.parsea_baja(_el(baja(8241841, "2026-03-17T00:00:25.645+01:00", "CERRADA", AGREGADAS))),
              {"id": idu(8241841, AGREGADAS), "cuando": "2026-03-17T00:00:25.645+01:00", "tipo": "CERRADA"})
    comprueba("parsea_baja: ANULADA (estatal), el tipo se guarda tal cual",
              feeds.parsea_baja(_el(baja(20670590, "2026-10-14T18:07:20.126+02:00", "ANULADA")))["tipo"], "ANULADA")
    comprueba("parsea_baja: sin comment -> tipo None",
              feeds.parsea_baja(_el(baja(1, "2026-10-14T18:07:20.126+02:00", None)))["tipo"], None)
    sin_ref = etree.fromstring(pagina("2026-10-15T03:00:00.000+02:00",
                                      ['<at:deleted-entry when="2026-10-14T18:07:20.126+02:00"/>'])).find(
        "{http://purl.org/atompub/tombstones/1.0}deleted-entry")
    comprueba("parsea_baja: sin ref -> None (se descarta)", feeds.parsea_baja(sin_ref), None)


def test_lee_pagina_separa_entradas_y_bajas():
    raiz = etree.fromstring(pagina("2026-10-15T03:00:55.827+02:00", [
        baja(2, "2026-10-15T00:00:47.517+02:00", "CERRADA"),
        entrada(1, "2026-10-15T02:00:00.000+02:00"),
        entrada(3, "2026-10-14T23:00:00.000+02:00"),
    ]))
    entradas, bajas, snapshot = feeds.lee_pagina(raiz)
    comprueba("lee_pagina: 2 entradas", len(entradas), 2)
    comprueba("lee_pagina: 1 baja", [b["id"] for b in bajas], [idu(2)])
    comprueba("lee_pagina: snapshot = <updated> del propio .atom", snapshot, "2026-10-15T03:00:55.827+02:00")


def _baja_de(base, n):
    f = base.fila(idu(n))
    return (_inst(f.get("baja_en")), f.get("baja_tipo"))


def test_diario_marca_la_baja():
    """16577712 (Univ. Córdoba, SDA): ANUL el 02/10 a las 11:24 y baja ANULADA a las 12:05. Hoy la
    base la sigue teniendo «abierta hasta 2035» sin ninguna marca."""
    base = BaseFalsa()
    n = 16577712
    corre_diario(base, [pagina("2026-10-03T03:00:00.000+02:00", [
        baja(n, "2026-10-02T12:05:00.000+02:00", "ANULADA"),
        entrada(n, "2026-10-02T11:24:55.000+02:00", estado="ANUL", fin="2035-03-04"),
    ])])
    comprueba("bajas: queda marcada con su fecha y su tipo", _baja_de(base, n),
              (_inst("2026-10-02T12:05:00.000+02:00"), "ANULADA"))


def test_baja_en_pagina_mas_nueva_que_su_version():
    """Lo normal en el feed real: la baja está en una página MÁS NUEVA que la última versión."""
    base = BaseFalsa()
    n = 16577713
    corre_diario(base, [
        pagina("2026-10-04T03:00:00.000+02:00", [baja(n, "2026-10-03T08:00:00.000+02:00", "ANULADA"),
                                                 entrada(1, "2026-10-04T01:00:00.000+02:00")]),
        pagina("2026-10-03T03:00:00.000+02:00", [entrada(n, "2026-10-02T11:24:55.000+02:00", estado="ANUL")]),
    ])
    comprueba("bajas: marcada aunque esté en una página más nueva que su versión", _baja_de(base, n),
              (_inst("2026-10-03T08:00:00.000+02:00"), "ANULADA"))


def test_baja_en_zip_mas_nuevo_que_su_version():
    """En una carga, los ZIP van de más nuevo a más viejo: la baja (202610) llega ANTES que la
    única versión (202609) de un expediente que la base aún no tiene."""
    base = BaseFalsa()
    n = 16577714
    corre_carga(base, [
        ("estatal", "202610", zip_de("202610", ("20261005_030000", "2026-10-05T03:00:00.000+02:00",
                                                [baja(n, "2026-10-04T12:00:00.000+02:00", "ANULADA"),
                                                 entrada(2, "2026-10-04T20:00:00.000+02:00")]))),
        ("estatal", "202609", zip_de("202609", ("20260920_030000", "2026-09-20T03:00:00.000+02:00",
                                                [entrada(n, "2026-09-19T10:00:00.000+02:00", estado="PUB")]),
                                     base_snapshot="2026-09-30T03:00:00.000+02:00")),
    ])
    comprueba("bajas (carga): marcada aunque llegue antes que su versión", _baja_de(base, n),
              (_inst("2026-10-04T12:00:00.000+02:00"), "ANULADA"))


def test_carga_reanudada_no_pierde_la_baja():
    """Una carga que se corta a mitad (500 de Supabase) y se reanuda con el checkpoint no puede
    perder la baja leída en la unidad que ya se completó."""
    base = BaseFalsa(fallar_desde=2)
    n = 16577715
    tmp = Path(tempfile.mkdtemp(prefix="test_versiones_reanuda_"))
    unidades = [
        ("estatal", "202610", zip_de("202610", ("20261005_030000", "2026-10-05T03:00:00.000+02:00",
                                                [baja(n, "2026-10-04T12:00:00.000+02:00", "ANULADA"),
                                                 entrada(3, "2026-10-04T20:00:00.000+02:00")]))),
        ("estatal", "202609", zip_de("202609", ("20260920_030000", "2026-09-20T03:00:00.000+02:00",
                                                [entrada(n, "2026-09-19T10:00:00.000+02:00", estado="PUB")]),
                                     base_snapshot="2026-09-30T03:00:00.000+02:00")),
    ]
    try:
        try:
            corre_carga(base, unidades, tmp=tmp)          # se corta a la 2.ª llamada
        except (Exception, SystemExit):  # noqa: BLE001
            pass
        base.curar()
        corre_carga(base, unidades, tmp=tmp)              # se reanuda
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    f = base.fila(idu(n))
    comprueba("reanudación: versión escrita y baja marcada",
              (f.get("estado_expediente"), _inst(f.get("baja_en"))), ("PUB", _inst("2026-10-04T12:00:00.000+02:00")))


def test_carga_reanudada_a_mitad_de_unidad():
    """El corte llega a MITAD de un ZIP: la baja estaba en un .atom ya subido (que al
    reanudar se salta) y la versión en el .atom que falló. Al reanudar, la baja tiene que
    volver a leerse y quedar marcada."""
    base = BaseFalsa(fallar_desde=2)
    n = 16577716
    tmp = Path(tempfile.mkdtemp(prefix="test_versiones_reanuda2_"))
    unidades = [("estatal", "202610", zip_de("202610",
        ("20261003_030000", "2026-10-03T03:00:00.000+02:00", [entrada(n, "2026-10-02T10:00:00.000+02:00", estado="PUB")]),
        ("20261005_030000", "2026-10-05T03:00:00.000+02:00", [baja(n, "2026-10-04T12:00:00.000+02:00", "ANULADA"),
                                                              entrada(4, "2026-10-04T20:00:00.000+02:00")])))]
    try:
        try:
            corre_carga(base, unidades, tmp=tmp)          # sube el del 05/10 y se corta en el del 03/10
        except (Exception, SystemExit):  # noqa: BLE001
            pass
        base.curar()
        corre_carga(base, unidades, tmp=tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    f = base.fila(idu(n))
    comprueba("reanudación a mitad de ZIP: versión escrita y baja marcada",
              (f.get("estado_expediente"), _inst(f.get("baja_en"))), ("PUB", _inst("2026-10-04T12:00:00.000+02:00")))


def test_sin_la_rpc_para_antes_de_escribir():
    """Si el código se despliega ANTES que versiones_schema.sql, el diario tiene que parar
    con un aviso claro y sin escribir nada (ni por la vía vieja)."""
    base = BaseFalsa(sin_rpc=True)
    mensaje = None
    try:
        corre_diario(base, [pagina("2026-10-15T03:00:00.000+02:00", [entrada(18900050, "2026-10-14T10:00:00.000+02:00")])])
    except SystemExit as e:
        mensaje = str(e)
    comprueba("sin la RPC: para avisando de versiones_schema.sql y no escribe nada",
              (mensaje is not None and "versiones_schema.sql" in mensaje, len(base.lic)), (True, 0))


def test_bajas_troceadas_y_partidas():
    """Las bajas van en llamadas de como mucho LOTE_BAJAS y, si una falla por tiempo, se
    parte en dos (antes solo se podían partir las versiones)."""
    class Sesion:
        def __init__(self):
            self.tamanos = []
        def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
            n = len(json["p_lote"]["bajas"])
            self.tamanos.append(n)
            if n > 3000:      # simula un 57014 con lotes grandes
                return _Resp(500, None, text='{"code":"57014"}')
            return _Resp(200, {"bajas_recibidas": n})
    bajas = [{"licitacion_id": f"x{i}", "baja_en": "2026-10-01T00:00:00+02:00", "baja_tipo": "CERRADA"} for i in range(12000)]
    s = Sesion()
    with _sin_esperas():
        r = bc.sube_versiones(s, URL, {"Prefer": "return=minimal"}, [], bajas)
    comprueba("bajas: se mandan todas, troceadas y partiendo las que fallan",
              (r.get("bajas_recibidas"), max(n for n in s.tamanos if n <= 3000) <= 3000), (12000, True))


def test_baja_seguida_de_republicacion():
    """19979771: baja ANULADA el 06/10 y una versión posterior el 07/10. No debe quedar marcada."""
    base = BaseFalsa()
    n = 19979771
    corre_diario(base, [
        pagina("2026-10-08T03:00:00.000+02:00", [entrada(n, "2026-10-07T09:00:00.000+02:00", estado="PUB", fin="2026-10-30")]),
        pagina("2026-10-07T03:00:00.000+02:00", [baja(n, "2026-10-06T10:00:00.000+02:00", "ANULADA"),
                                                 entrada(n, "2026-10-05T09:00:00.000+02:00", estado="PUB", fin="2026-10-20")]),
    ])
    f = base.fila(idu(n))
    comprueba("bajas: una versión posterior a la baja la deja sin marcar",
              (f.get("baja_en"), _dia(f.get("fecha_fin_plazo"))), (None, "2026-10-30"))


def test_baja_sin_tipo_y_varias_bajas():
    base = BaseFalsa()
    n = 19000040
    corre_diario(base, [
        pagina("2026-10-12T03:00:00.000+02:00", [baja(n, "2026-10-11T10:00:00.000+02:00", None)]),
        pagina("2026-10-10T03:00:00.000+02:00", [baja(n, "2026-10-09T10:00:00.000+02:00", "ANULADA"),
                                                 entrada(n, "2026-10-08T10:00:00.000+02:00")]),
    ])
    comprueba("bajas: de varias se queda la más reciente, y sin tipo vale (tipo None)",
              _baja_de(base, n), (_inst("2026-10-11T10:00:00.000+02:00"), None))


def test_version_intermedia_conserva_la_baja():
    """Llega una versión más nueva que la guardada pero ANTERIOR a la baja ya marcada: se
    escribe y la baja se conserva."""
    base = BaseFalsa()
    n = 19000041
    corre_diario(base, [pagina("2026-10-12T03:00:00.000+02:00", [baja(n, "2026-10-11T10:00:00.000+02:00", "ANULADA"),
                                                                  entrada(n, "2026-10-08T10:00:00.000+02:00", presu="1.0")])])
    corre_diario(base, [pagina("2026-10-10T03:00:00.000+02:00", [entrada(n, "2026-10-09T10:00:00.000+02:00", presu="2.0")])])
    f = base.fila(idu(n))
    comprueba("bajas: la versión intermedia se escribe y la baja sigue",
              (float(f.get("presupuesto_sin_iva") or 0), _inst(f.get("baja_en"))),
              (2.0, _inst("2026-10-11T10:00:00.000+02:00")))


def test_baja_de_un_id_que_no_esta():
    base = BaseFalsa()
    corre_diario(base, [pagina("2026-10-15T03:00:00.000+02:00", [baja(8488725, "2026-10-15T00:00:47.517+02:00", "CERRADA")])])
    comprueba("bajas: una baja de un id que no está en la base no crea nada", base.lic.get(idu(8488725)), None)


# =============================================================================
# BLOQUE 7 · El Radar (data/licitaciones.json): utiles.fusiona_radar y radar_pasada
# =============================================================================
def _lic_radar(updated, fin, presu=13327.0, snapshot=None, titulo="Renfe higiene"):
    return {"id": idu(20603377), "titulo": titulo, "enlace": "x", "cpv": ["90920000"], "fuente": "estatal",
            "num_expediente": "2026-04139", "organismo": "Renfe", "plataforma": None, "region": None,
            "region_codigo": "ES300", "categoria": "a_revisar", "coincidencia": "CPV 90920000",
            "presupuesto_con_iva": None, "presupuesto_sin_iva": presu, "valor_estimado": presu,
            "fecha_fin_plazo": fin, "fecha_publicacion": "2026-10-01", "fecha_actualizacion": updated,
            "version_snapshot": snapshot, "estado_expediente": "PUB"}


def test_radar_conserva_la_mas_nueva():
    """20603377 (Renfe): el Radar enseña «cerrado 14/10» y el plazo real acaba el 16/10."""
    nueva = _lic_radar("2026-10-05T09:53:59.000+02:00", "2026-10-16", snapshot="2026-10-05T20:18:59.000+02:00")
    vieja = _lic_radar("2026-10-01T10:48:51.000+02:00", "2026-10-14", snapshot="2026-10-01T20:17:04.000+02:00")
    datos = {}
    utiles.fusiona_radar(datos, nueva, "2026-10-08T12:30:00")   # orden del feed: primero la nueva
    utiles.fusiona_radar(datos, vieja, "2026-10-08T12:30:00")
    comprueba("radar: se queda el fin de plazo de la versión nueva", datos[idu(20603377)]["fecha_fin_plazo"], "2026-10-16")


def test_radar_independiente_del_orden():
    vs = [_lic_radar("2026-10-01T10:48:51.000+02:00", "2026-10-14", presu=1.0, snapshot="2026-10-01T20:00:00.000+02:00"),
          _lic_radar("2026-10-05T09:53:59.000+02:00", "2026-10-16", presu=2.0, snapshot="2026-10-05T20:00:00.000+02:00"),
          _lic_radar("2026-10-03T09:00:00.000+02:00", "2026-10-15", presu=3.0, snapshot="2026-10-03T20:00:00.000+02:00")]
    finales = set()
    for perm in itertools.permutations(vs):
        datos = {idu(20603377): {**_lic_radar("2026-09-20T10:00:00.000+02:00", "2026-10-10", presu=0.5),
                                 "primera_vez": "2026-09-20T12:00:00", "ultima_vez": "2026-09-20T12:00:00"}}
        for v in perm:
            utiles.fusiona_radar(datos, dict(v), "2026-10-08T12:30:00")
        d = datos[idu(20603377)]
        finales.add((d["fecha_fin_plazo"], d["presupuesto_sin_iva"], d["primera_vez"]))
    comprueba("radar: las 6 permutaciones dejan la del 05/10 y la primera_vez intacta",
              finales, {("2026-10-16", 2.0, "2026-09-20T12:00:00")})


def test_radar_cambio_de_hora_y_empate():
    datos = {}
    utiles.fusiona_radar(datos, _lic_radar("2026-10-25T02:30:00.000+02:00", "2026-11-01", presu=1.0,
                                           snapshot="2026-10-25T02:45:00.000+02:00"), "a")
    utiles.fusiona_radar(datos, _lic_radar("2026-10-25T02:10:00.000+01:00", "2026-11-02", presu=2.0,
                                           snapshot="2026-10-25T03:00:00.000+01:00"), "b")
    comprueba("radar: con el cambio de hora gana la de +01:00", datos[idu(20603377)]["presupuesto_sin_iva"], 2.0)
    datos = {}
    utiles.fusiona_radar(datos, _lic_radar("2026-08-31T19:19:52.539+02:00", "2026-09-15", presu=1.0,
                                           snapshot="2026-09-23T09:27:41.000+02:00"), "a")
    utiles.fusiona_radar(datos, _lic_radar("2026-08-31T19:19:52.539+02:00", "2026-09-15", presu=9.0,
                                           snapshot="2026-09-01T20:13:32.000+02:00"), "b")
    comprueba("radar: en un empate de marca no gana el snapshot más viejo", datos[idu(20603377)]["presupuesto_sin_iva"], 1.0)


def test_radar_legado_sin_fecha():
    datos = {idu(20603377): {**_lic_radar(None, "2026-10-10", presu=0.5), "primera_vez": "p", "ultima_vez": "u"}}
    utiles.fusiona_radar(datos, _lic_radar("2026-10-05T09:53:59.000+02:00", "2026-10-16"), "2026-10-08T12:30:00")
    comprueba("radar: una ficha antigua sin fecha_actualizacion se actualiza",
              datos[idu(20603377)]["fecha_fin_plazo"], "2026-10-16")


def test_radar_bajas():
    """El Radar también marca las bajas (sin borrar la ficha) y las desmarca si se republica."""
    datos = {}
    i = idu(20603377)
    utiles.fusiona_radar(datos, _lic_radar("2026-10-02T11:24:55.000+02:00", "2035-03-04"), "a")
    n = utiles.marca_bajas_radar(datos, [
        {"id": i, "cuando": "2026-10-02T12:05:00.000+02:00", "tipo": "ANULADA"},
        {"id": i, "cuando": "2026-10-02T11:00:00.000+02:00", "tipo": "CERRADA"},   # anterior: no cuenta
        {"id": idu(1), "cuando": "2026-10-02T12:05:00.000+02:00", "tipo": "CERRADA"},  # no está en el Radar
    ])
    comprueba("radar: la baja posterior queda marcada (y solo esa)",
              (n, datos[i].get("baja_en"), datos[i].get("baja_tipo")), (1, "2026-10-02T12:05:00.000+02:00", "ANULADA"))
    utiles.fusiona_radar(datos, _lic_radar("2026-10-02T11:30:00.000+02:00", "2035-03-04", presu=5.0), "b")
    comprueba("radar: una versión anterior a la baja no la desmarca",
              (datos[i]["presupuesto_sin_iva"], datos[i].get("baja_en")), (5.0, "2026-10-02T12:05:00.000+02:00"))
    utiles.fusiona_radar(datos, _lic_radar("2026-10-07T09:00:00.000+02:00", "2026-10-30"), "c")
    comprueba("radar: una versión posterior a la baja la desmarca", "baja_en" in datos[i], False)


def test_radar_pasada_clasifica_la_ultima_version():
    """filtrar.py clasifica TODAS las versiones y luego fusiona: si la vieja casa con los
    criterios y la nueva no, hoy entra la vieja. utiles.radar_pasada debe elegir primero la
    versión más nueva y clasificar ESA."""
    criterios = {"a_revisar": {"cpv": [], "palabras_clave": ["purificador"]}}
    vieja = {**_lic_radar("2026-10-01T10:00:00.000+02:00", "2026-10-14", snapshot="2026-10-01T20:00:00.000+02:00",
                          titulo="Suministro de purificador de aire"), "cpv": ["39000000"]}
    nueva = {**_lic_radar("2026-10-05T10:00:00.000+02:00", "2026-10-16", snapshot="2026-10-05T20:00:00.000+02:00",
                          titulo="Suministro de mobiliario"), "cpv": ["39000000"]}
    datos = {}
    nuevas = utiles.radar_pasada(datos, [nueva, vieja], criterios, lambda plat, reg: True, "2026-10-08T12:30:00")
    comprueba("radar_pasada: la versión vieja que casaba no entra si la nueva ya no casa",
              (sorted(datos), len(nuevas)), ([], 0))
    datos = {}
    nuevas = utiles.radar_pasada(datos, [vieja, {**nueva, "titulo": "Purificador y mobiliario"}], criterios,
                                 lambda plat, reg: True, "2026-10-08T12:30:00")
    comprueba("radar_pasada: entra UNA vez y con los datos de la versión nueva",
              (len(nuevas), datos.get(idu(20603377), {}).get("fecha_fin_plazo")), (1, "2026-10-16"))


def test_radar_pasada_titulo_roto_no_poda():
    """20324973 (Gobierno Vasco): la ficha entró por «monitorización» y la versión del 07/10
    trae el título en mojibake («MonitorizaciÃ³n»), que ya no casa. La ficha tiene que
    quedarse con su título bueno (para que la poda no la borre) y con el plazo nuevo."""
    criterios = {"pruebas": {"cpv": [], "palabras_clave": ["monitorización"]}}
    i = idu(20324973)
    datos = {i: {**_lic_radar("2026-10-01T10:00:00.000+02:00", "2026-10-14", titulo="Monitorización del aire"),
                 "id": i, "categoria": "pruebas", "coincidencia": "palabra clave «monitorización»",
                 "cpv": ["39000000"], "primera_vez": "p", "ultima_vez": "u"}}
    rota = {**_lic_radar("2026-10-07T10:00:00.000+02:00", "2026-10-20", snapshot="2026-10-08T03:00:00.000+02:00",
                         titulo="MonitorizaciÃ³n del aire"), "id": i, "cpv": ["39000000"]}
    utiles.radar_pasada(datos, [rota], criterios, lambda plat, reg: True, "2026-10-08T12:30:00")
    f = datos[i]
    comprueba("radar_pasada: la versión con el título roto refresca el plazo pero no el título",
              (f["titulo"], f["categoria"], f["fecha_fin_plazo"]), ("Monitorización del aire", "pruebas", "2026-10-20"))


# =============================================================================
def main():
    bloques = [
        ("Bloque 1 · regla de versión", (test_instante_con_zona, test_cambio_de_hora_25_10_2026, test_es_mas_nueva,
                                         test_elige_versiones_independiente_del_orden,
                                         test_elige_versiones_marca_antes_que_snapshot)),
        ("Bloque 2 · orden de los ZIP (carga)", (test_orden_atoms, test_carga_zip_mensual_fin_de_plazo,
                                                 test_carga_zip_anual_no_pierde_la_adjudicacion,
                                                 test_carga_marca_antes_que_snapshot,
                                                 test_carga_empate_estado_hacia_atras,
                                                 test_carga_solo_catalogo_sigue_mandando_adjudicaciones)),
        ("Bloque 3 · feed vivo (diario)", (test_diario_mismo_id_en_varias_paginas, test_diario_empate_republicacion,
                                           test_diario_marca_antes_que_snapshot, test_diario_cambio_de_hora,
                                           test_diario_licitacion_y_adjudicaciones_de_la_misma_version,
                                           test_diario_idempotente)),
        ("Bloque 4 · ejecuciones separadas (guarda)", (test_troceado_orden_inverso, test_troceado_seis_tandas_del_15_09,
                                                       test_empate_entre_dos_ejecuciones,
                                                       test_empate_mismo_snapshot_otras_adjudicaciones)),
        ("Bloque 5 · adjudicaciones", (test_adj_nueva_reemplaza_sin_huerfanas, test_adj_nueva_sin_resultado_borra,
                                       test_adj_vieja_no_toca)),
        ("Bloque 6 · bajas", (test_parsea_baja, test_lee_pagina_separa_entradas_y_bajas, test_diario_marca_la_baja,
                              test_baja_en_pagina_mas_nueva_que_su_version, test_baja_en_zip_mas_nuevo_que_su_version,
                              test_carga_reanudada_no_pierde_la_baja, test_carga_reanudada_a_mitad_de_unidad,
                              test_sin_la_rpc_para_antes_de_escribir, test_bajas_troceadas_y_partidas,
                              test_baja_seguida_de_republicacion,
                              test_baja_sin_tipo_y_varias_bajas, test_version_intermedia_conserva_la_baja,
                              test_baja_de_un_id_que_no_esta)),
        ("Bloque 7 · Radar (data/licitaciones.json)", (test_radar_conserva_la_mas_nueva, test_radar_independiente_del_orden,
                                                       test_radar_cambio_de_hora_y_empate, test_radar_legado_sin_fecha,
                                                       test_radar_bajas, test_radar_pasada_clasifica_la_ultima_version,
                                                       test_radar_pasada_titulo_roto_no_poda)),
    ]
    for titulo, pruebas in bloques:
        print(titulo)
        for t in pruebas:
            prueba(t)
    print(f"\n{len(BIEN)} bien, {len(FALLOS)} mal")
    if FALLOS:
        print("EN ROJO:", ", ".join(FALLOS))


if __name__ == "__main__":
    main()
    sys.exit(1 if FALLOS else 0)
