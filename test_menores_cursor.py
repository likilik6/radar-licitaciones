# -*- coding: utf-8 -*-
"""Pruebas de la diaria de menores con CURSOR (22/11/2026).

Sin red y sin base: el feed, los ZIP mensuales y Supabase son de mentira.
  · corte_de, _marca_menor (cambio de hora) y la regla de la versión más nueva;
  · recorre_menores: para en el cursor, tope, fallo de red, sin cursor;
  · PaginasZip: busca la página en el ZIP correcto (el corte del día 1 va en el del mes
    anterior) y sigue la cadena hasta el cursor;
  · menores_diario de punta a punta: verde, hueco recuperado, hueco sin recuperar,
    primera pasada, cursor ilegible y el fallo de red del 02/11;
  · menores_recuperar: simula sin escribir y, escribiendo, SOLO inserta lo que falta;
  · carga_menores: la versión más nueva gana sea cual sea el orden de los periodos;
  · diario() ya no carga menores.

Uso:  python test_menores_cursor.py
"""
import sys
import inspect
import tempfile
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import backfill_catalogo as bc

FICHERO = "contratosMenoresPerfilesContratantes"
BASE = "https://contrataciondelestado.es/sindicacion/sindicacion_1143/"
CABECERA = bc.MENORES_ATOM          # la cabecera real (otro dominio que las páginas: da igual)
ID = "https://contrataciondelestado.es/sindicacion/datosAbiertosMenores/"

FALLOS = []


def comprueba(cond, texto):
    print(("  OK   " if cond else "  FALLO ") + texto)
    if not cond:
        FALLOS.append(texto)


def url_pagina(corte, n=0):
    return BASE + f"{FICHERO}_{corte}" + (f"_{n}" if n else "") + ".atom"


def entrada(num, updated, objeto):
    return (f'<entry><id>{ID}{num}</id><title>t{num}</title><updated>{updated}</updated>'
            f'<cac-place-ext:ContractFolderStatus><cac:ProcurementProject><cbc:Name>{objeto}'
            f'</cbc:Name></cac:ProcurementProject></cac-place-ext:ContractFolderStatus></entry>')


def pagina(siguiente, entradas):
    nexto = f'<link rel="next" href="{siguiente}"/>' if siguiente else ""
    return ('<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:cbc="urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2" '
            'xmlns:cac="urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2" '
            'xmlns:cac-place-ext="urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2">'
            + nexto + "".join(entradas) + "</feed>").encode("utf-8")


def cadena(paginas):
    """paginas: lista de (url, [entradas]) de la más nueva a la más vieja -> {url: bytes}."""
    salida = {}
    for i, (url, ents) in enumerate(paginas):
        sig = paginas[i + 1][0] if i + 1 < len(paginas) else None
        salida[url] = pagina(sig, ents)
    return salida


def trae_de(paginas, fallan=()):
    pedidas = []

    def trae(url):
        pedidas.append(url)
        if url in fallan:
            raise ConnectionError("timeout de mentira")
        return paginas[url]
    trae.pedidas = pedidas
    return trae


# --------------------------------------------------------------------------
print("\n[1] corte_de, marcas y versión más nueva")
comprueba(bc.corte_de(url_pagina("20261121_031003", 2)) == ("20261121_031003", 2), "corte con número de página")
comprueba(bc.corte_de(url_pagina("20261121_031003")) == ("20261121_031003", 0), "primera página del corte = 0")
comprueba(bc.corte_de(CABECERA) is None, "la cabecera no lleva corte")
a = bc._marca_menor("2026-10-25T02:30:00.000+02:00")   # 00:30 UTC
b = bc._marca_menor("2026-10-25T02:10:00.000+01:00")   # 01:10 UTC
comprueba(a is not None and b is not None and b > a,
          "cambio de hora: 02:10+01:00 es MÁS NUEVA que 02:30+02:00 (el texto diría lo contrario)")
comprueba(bc._marca_menor("2026-10-25T02:10:00") is None, "fecha sin zona -> sin marca")
comprueba(bc._marca_menor("") is None and bc._marca_menor(None) is None, "vacía -> sin marca")
comprueba(bc._gana(1.0, bc._SIN_VER) and not bc._gana(None, 1.0) and bc._gana(1.0, None)
          and not bc._gana(1.0, 1.0) and bc._gana(2.0, 1.0), "regla _gana")

el = bc.EligeMenores()
el.mete(bc.entradas_de(pagina(None, [entrada(1, "2026-11-20T10:00:00+01:00", "v2"),
                                     entrada(2, "2026-11-20T10:00:00+01:00", "primera")])), "p1")
el.mete(bc.entradas_de(pagina(None, [entrada(1, "2026-11-01T10:00:00+01:00", "v1"),
                                     entrada(2, "2026-11-20T10:00:00+01:00", "segunda"),
                                     entrada(3, "", "sin marca")])), "p2")
el.mete(bc.entradas_de(pagina(None, [entrada(3, "2026-11-02T10:00:00+01:00", "con marca")])), "p3")
comprueba(el.filas[ID + "1"]["objeto"] == "v2", "gana la versión más nueva aunque salga antes la vieja... o después")
comprueba(el.filas[ID + "2"]["objeto"] == "primera" and el.origen[ID + "2"] == "p1",
          "a igual marca se queda la primera (la página más nueva)")
comprueba(el.filas[ID + "3"]["objeto"] == "con marca", "una versión con marca gana a una sin ella")

# --------------------------------------------------------------------------
print("\n[2] recorre_menores")
C3, C2, C1 = "20261122_030526", "20261121_031003", "20261120_030657"
feed = cadena([
    (CABECERA, [entrada(10, "2026-11-22T09:00:00+01:00", "h")]),
    (url_pagina(C3), [entrada(11, "2026-11-22T02:00:00+01:00", "a")]),
    (url_pagina(C3, 1), [entrada(12, "2026-11-21T20:00:00+01:00", "b")]),
    (url_pagina(C2), [entrada(13, "2026-11-21T02:00:00+01:00", "c")]),
    (url_pagina(C2, 1), [entrada(14, "2026-11-20T20:00:00+01:00", "d")]),
    (url_pagina(C1), [entrada(15, "2026-11-20T02:00:00+01:00", "e")]),
])
paginas_vistas = []
trae = trae_de(feed)
r = bc.recorre_menores(CABECERA, C2, trae, lambda e, n: paginas_vistas.append(n), tope=20)
comprueba(r["llego"] and r["paginas"] == 3 and r["entradas"] == 3, "para ANTES del corte del cursor (lee cabecera + 2)")
comprueba(url_pagina(C2) not in trae.pedidas, "ni siquiera pide la página del cursor")
comprueba(r["primer_corte"] == C3 and r["corte_entero"] == C3, "corte más nuevo leído = C3")
comprueba(paginas_vistas[0] == FICHERO + ".atom", "con_pagina recibe el nombre de cada página, en orden")

r = bc.recorre_menores(CABECERA, C1, trae_de(feed), lambda e, n: None, tope=3)
comprueba(not r["llego"] and r["motivo"] == "tope" and r["pendiente"] == url_pagina(C2),
          "tope: no llega y deja apuntada la primera página sin leer")
comprueba(r["corte_entero"] == C3, "con tope, C3 está entero (ya se pasó a C2)")

r = bc.recorre_menores(CABECERA, C1, trae_de(feed), lambda e, n: None, tope=4)
comprueba(r["corte_entero"] == C3 and r["primer_corte"] == C3, "parado a mitad de C2: C2 no cuenta como entero")
r = bc.recorre_menores(CABECERA, None, trae_de(feed), lambda e, n: None, tope=2)
comprueba(r["primer_corte"] == C3 and r["corte_entero"] is None,
          "parado a mitad del PRIMER corte: ningún corte entero (no vale como cursor)")

r = bc.recorre_menores(CABECERA, C1, trae_de(feed, fallan={url_pagina(C2)}), lambda e, n: None, tope=20)
comprueba(not r["llego"] and r["motivo"].startswith("error en") and r["pendiente"] == url_pagina(C2),
          "fallo de red: para, lo dice y apunta la página que falló")

r = bc.recorre_menores(CABECERA, None, trae_de(feed), lambda e, n: None, tope=20)
comprueba(r["llego"] and r["paginas"] == 6, "sin cursor: lee hasta el final de la cadena")
r = bc.recorre_menores(CABECERA, None, trae_de(feed), lambda e, n: None, tope=4)
comprueba(r["motivo"] == "tope" and r["corte_entero"] == C3, "sin cursor y con tope: el cursor nuevo sería C3")

# --------------------------------------------------------------------------
print("\n[3] PaginasZip y rangos")
comprueba(bc.PaginasZip.meses_candidatos("20261001_030812")[0] == "202609", "corte del día 1: primero el ZIP del mes anterior")
comprueba(bc.PaginasZip.meses_candidatos("20261015_030812")[0] == "202610", "corte normal: primero su mes")
comprueba(bc.PaginasZip.meses_candidatos("20260101_030000") == ["202512", "202601", "202602"], "1 de enero -> diciembre anterior")
comprueba(bc.normaliza_rango("20260901", "20260930") == ("20260901_000000", "20260930_235959"), "rango por días")
for malo in [("2026-09-01", "20260930"), ("20260931", "20261001"), ("20261001", "20260901")]:
    try:
        bc.normaliza_rango(*malo)
        comprueba(False, f"rango malo {malo} rechazado")
    except ValueError:
        comprueba(True, f"rango malo {malo} rechazado")
comprueba(bc.periodos_de_rango("20260901_000000", "20260930_235959") == ["202609", "202608"],
          "septiembre entero: ZIP 202609 y 202608 (por el corte del 1)")
comprueba(bc.periodos_de_rango("20260926_000000", "20261103_235959") == ["202611", "202610", "202609"],
          "rango que cruza meses")

TMP = Path(tempfile.mkdtemp(prefix="test_menores_cursor_"))


def zip_de(periodo, paginas_bytes):
    ruta = TMP / f"menores_{periodo}.zip"
    with zipfile.ZipFile(ruta, "w") as z:
        for url, blob in paginas_bytes.items():
            z.writestr(bc._nombre(url), blob)
    return ruta


# --------------------------------------------------------------------------
print("\n[4] menores_diario de punta a punta (Supabase de mentira)")


class Escena:
    """Sustituye en el módulo todo lo que toca la red o la base y lo apunta."""

    def __init__(self, cursor, consultado=True):
        self.subidas, self.marcas, self.cursor = [], [], (cursor, consultado)
        self._orig = {}
        for nombre, valor in {
            "_preparar_upsert": lambda: (None, {"Prefer": "x"}, None, "https://falsa"),
            "_tabla_menores_lista": lambda *a, **k: True,
            "lee_cursor_menores": lambda *a, **k: self.cursor,
            "_sube_lote": lambda s, url, h, filas, *a, **k: self.subidas.append((h.get("Prefer"), list(filas))),
            "marca_refresco": lambda s, u, h, clave, ok, filas=None, detalle=None, error=None:
                self.marcas.append({"clave": clave, "ok": ok, "detalle": detalle, "error": error}),
            "refrescar_cobertura_menores": lambda *a, **k: True,
            "PAUSA_ENTRE_PAGINAS": 0,
        }.items():
            self._orig[nombre] = getattr(bc, nombre)
            setattr(bc, nombre, valor)

    def filas(self):
        return {f["licitacion_id"]: f for _p, lote in self.subidas for f in lote}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for nombre, valor in self._orig.items():
            setattr(bc, nombre, valor)


# Feed de hoy: cabecera + C3 (2 pág). El viernes se leyó hasta C0 (cursor). Entre medias,
# C2 y C1 (3 páginas) que el tope de mentira (3) no alcanza: hay que tirar del ZIP.
C0 = "20261119_030625"
completa = [
    (CABECERA, [entrada(20, "2026-11-22T09:00:00+01:00", "cabecera")]),
    (url_pagina(C3), [entrada(21, "2026-11-22T02:00:00+01:00", "nuevo 21"),
                      entrada(30, "2026-11-22T01:00:00+01:00", "v3 de 30")]),
    (url_pagina(C3, 1), [entrada(22, "2026-11-21T20:00:00+01:00", "nuevo 22")]),
    (url_pagina(C2), [entrada(23, "2026-11-21T02:00:00+01:00", "hueco 23"),
                      entrada(30, "2026-11-21T01:00:00+01:00", "v2 de 30")]),
    (url_pagina(C2, 1), [entrada(24, "2026-11-20T20:00:00+01:00", "hueco 24")]),
    (url_pagina(C1), [entrada(25, "2026-11-20T02:00:00+01:00", "hueco 25")]),
    (url_pagina(C0), [entrada(26, "2026-11-19T02:00:00+01:00", "ya leído")]),
]
feed = cadena(completa)
zips = {"202611": zip_de("202611", {u: b for u, b in feed.items() if u != CABECERA})}
descargar = lambda p: zips.get(p)

bc.MENORES_TOPE_PAGINAS, tope_orig = 20, bc.MENORES_TOPE_PAGINAS
with Escena(C0) as e:
    codigo = bc.menores_diario(trae_vivo=trae_de(feed), descargar=descargar)
    comprueba(codigo == 0, "en vivo llega al cursor -> código 0 (verde)")
    comprueba(e.marcas and e.marcas[-1]["ok"] and e.marcas[-1]["detalle"]["corte"] == C3,
              "el cursor avanza al corte más nuevo (C3)")
    comprueba(ID + "26" not in e.filas(), "no vuelve a subir lo de antes del cursor")
    comprueba(e.filas()[ID + "30"]["objeto"] == "v3 de 30", "de cada menor sube la versión más nueva")
    comprueba(all(p == "x" for p, _l in e.subidas), "la diaria hace upsert normal (merge)")

bc.MENORES_TOPE_PAGINAS = 3
with Escena(C0) as e:
    codigo = bc.menores_diario(trae_vivo=trae_de(feed), descargar=descargar)
    comprueba(codigo == 1, "hueco recuperado del ZIP -> código 1 (rojo, para avisar)")
    m = e.marcas[-1]
    comprueba(m["ok"] and m["detalle"]["corte"] == C3 and m["detalle"]["hueco"]
              and m["detalle"]["zip"] == ["202611"] and m["detalle"]["paginas_zip"] == 3,
              "recuperado: cursor a C3 y queda apuntado el hueco y el ZIP")
    comprueba({ID + n for n in ("23", "24", "25")} <= set(e.filas()), "suben los menores del hueco")
    comprueba(e.filas()[ID + "30"]["objeto"] == "v3 de 30", "la versión del ZIP no pisa la más nueva del vivo")

with Escena(C0) as e:
    sin_c1 = {"202611": zip_de("202611b", {u: b for u, b in feed.items()
                                          if u not in (CABECERA, url_pagina(C1))})}
    codigo = bc.menores_diario(trae_vivo=trae_de(feed), descargar=lambda p: sin_c1.get(p))
    comprueba(codigo == 1, "hueco SIN recuperar -> código 1")
    m = e.marcas[-1]
    comprueba(not m["ok"] and m["detalle"] is None and "falta" in m["error"],
              "sin recuperar: marca el fallo y NO manda cursor (se queda el de antes)")
    comprueba(ID + "21" in e.filas(), "lo leído en vivo se sube igual")
bc.MENORES_TOPE_PAGINAS = tope_orig

with Escena(None) as e:
    bc.MENORES_TOPE_PAGINAS = 4
    codigo = bc.menores_diario(trae_vivo=trae_de(feed), descargar=descargar)
    bc.MENORES_TOPE_PAGINAS = tope_orig
    comprueba(codigo == 0 and e.marcas[-1]["ok"] and e.marcas[-1]["detalle"]["corte"] == C3,
              "primera pasada (sin cursor): lee hasta el tope y deja el cursor puesto")

with Escena(None) as e:
    bc.MENORES_TOPE_PAGINAS = 2
    codigo = bc.menores_diario(trae_vivo=trae_de(feed), descargar=descargar)
    bc.MENORES_TOPE_PAGINAS = tope_orig
    comprueba(codigo == 1 and not e.marcas[-1]["ok"] and e.marcas[-1]["detalle"] is None,
              "primera pasada parada a mitad del primer corte: no deja cursor y sale en rojo")

with Escena(None, consultado=False) as e:
    codigo = bc.menores_diario(trae_vivo=trae_de(feed), descargar=descargar)
    comprueba(codigo == 1 and not e.marcas[-1]["ok"] and e.marcas[-1]["detalle"] is None,
              "cursor ilegible: lee como antes, no toca el cursor y sale en rojo")
    comprueba(len(e.filas()) > 0, "y sube lo leído")

with Escena(C0) as e:
    codigo = bc.menores_diario(trae_vivo=trae_de(feed, fallan={url_pagina(C2)}), descargar=descargar)
    comprueba(codigo == 1 and e.marcas[-1]["ok"] and e.marcas[-1]["detalle"]["corte"] == C3,
              "fallo de red a mitad (lo del 02/11): lo rescata el ZIP y avisa")
    comprueba(ID + "23" in e.filas(), "y la página que falló llega por el ZIP")

# --------------------------------------------------------------------------
print("\n[5] menores_recuperar: simula y solo inserta lo que falta")
S = "20260926_035058"
sept = cadena([
    (url_pagina(S), [entrada(40, "2026-09-09T10:00:00+02:00", "presente")]),
    (url_pagina(S, 13), [entrada(41, "2026-09-09T10:00:00+02:00", "perdido 41"),
                         entrada(42, "2026-09-10T10:00:00+02:00", "perdido 42")]),
    (url_pagina("20260925_032309"), [entrada(43, "2026-09-08T10:00:00+02:00", "fuera de rango")]),
])
zip_sep = zip_de("202609", sept)
abrir = lambda p: zip_sep if p == "202609" else None
with Escena(None) as e:
    orig_ids = bc._ids_en_base
    base = {ID + "40"}
    consultas = []
    def ids_en_base_falso(s, u, h, ids, lote=80):
        # La base de mentira ve también lo que la escena ha subido.
        consultas.append(len(ids))
        vista = base | {f["licitacion_id"] for _p, lote_ in e.subidas for f in lote_}
        return {i for i in ids if i in vista}
    bc._ids_en_base = ids_en_base_falso
    try:
        n = bc.menores_recuperar("20260926", "20260926", escribir=False, descargar=abrir,
                                 en_base=lambda ids: {i for i in ids if i in base})
        comprueba(n == 2 and not e.subidas, "simulación: cuenta 2 que faltan y no escribe nada")
        n = bc.menores_recuperar("20260926", "20260926", escribir=True, descargar=abrir,
                                 en_base=lambda ids: {i for i in ids if i in base})
        subidas = [f["licitacion_id"] for _p, lote in e.subidas for f in lote]
        comprueba(sorted(subidas) == [ID + "41", ID + "42"], "escribiendo: solo los que faltan (ni el presente ni el de fuera de rango)")
        comprueba(all(p.startswith("resolution=ignore-duplicates") for p, _l in e.subidas),
                  "y con ignore-duplicates: nunca pisa una fila que ya esté")
        comprueba(n == 0, "tras insertar, «siguen faltando: 0»")
        comprueba(consultas == [2], "la comprobación final es UNA consulta para todos, no una por menor")
    finally:
        bc._ids_en_base = orig_ids

# --------------------------------------------------------------------------
print("\n[6] carga_menores: la versión más nueva, sea cual sea el orden de los periodos")
z_nov = zip_de("nov", cadena([(url_pagina(C3), [entrada(50, "2026-11-22T02:00:00+01:00", "nueva")])]))
z_sep = zip_de("sep", cadena([(url_pagina(S), [entrada(50, "2026-09-10T02:00:00+02:00", "vieja")])]))
fuentes = {"202611": z_nov, "202609": z_sep}
orig_desc, orig_cache = bc.descarga_zip, bc.CACHE_DIR
bc.CACHE_DIR = TMP / "cache"
bc.CACHE_DIR.mkdir()
# carga_menores lee el ZIP de CACHE_DIR/menores_<periodo>.zip: la descarga de mentira lo copia ahí.
bc.descarga_zip = lambda url, destino: Path(destino).write_bytes(
    fuentes[url.rsplit("_", 1)[-1][:-4]].read_bytes())
try:
    for orden in (["202611", "202609"], ["202609", "202611"]):
        with Escena(None) as e:
            bc.carga_menores(orden, limpiar=False)
            final = {}
            for _p, lote in e.subidas:
                for f in lote:
                    final[f["licitacion_id"]] = f["objeto"]
            comprueba(final.get(ID + "50") == "nueva", f"orden {orden}: en la base acaba la nueva")
finally:
    bc.descarga_zip, bc.CACHE_DIR = orig_desc, orig_cache

# --------------------------------------------------------------------------
print("\n[7] diario() ya no carga menores")
fuente_diario = inspect.getsource(bc.diario)
comprueba("menores_diario(" not in fuente_diario and "menores_incremental" not in fuente_diario,
          "diario() no llama a la carga de menores")
comprueba(not hasattr(bc, "menores_incremental"), "menores_incremental ya no existe")

print()
if FALLOS:
    print(f"{len(FALLOS)} FALLO(S):")
    for f in FALLOS:
        print("  -", f)
    sys.exit(1)
print("TODO OK")
