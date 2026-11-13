# -*- coding: utf-8 -*-
"""test_reparacion.py — pruebas de reparar_versiones.py (la reparación dirigida).

Usa la MISMA base falsa que test_versiones.py (BaseFalsa: un espejo de la RPC
carga_versiones de versiones_schema.sql), con las lecturas GET añadidas como las sirve
PostgREST (sellos en UTC y un tope de filas por página), y ZIP de juguete con la forma
real del feed. Sin red ni Supabase.

Qué se fija:
  · el índice se queda con la versión GANADORA de cada expediente sea cual sea el orden
    de los ZIP, de los .atom o de las entradas (también con un empate total), con uno o
    con varios procesos; entra en ZIP anidados (aunque se llamen igual); guarda la baja
    más reciente y el corte de cada fuente; apunta las versiones sin fecha legible;
  · el ensayo no escribe NADA (ni POST, ni DELETE, ni PATCH);
  · escribir repara las filas de la base sin pisar una versión más nueva (incluidos los
    empates de marca), deja las adjudicaciones de la ganadora, inserta los expedientes que
    faltan si la purga no los quitaría (con la frontera exacta de purga_catalogo) y marca
    las bajas solo al terminar;
  · NO toca una fila sin sello que el código viejo escribió después del corte de los ZIP
    ni una con alguna versión sin fecha legible;
  · para en seco si el índice está incompleto, si hay .atom ilegibles, si ninguna fila
    tiene sello (el código nuevo no ha corrido), si es franja del robot o si se le pide la
    ventana a trozos; y se para solo antes de la siguiente franja;
  · el listado de la base no se queda corto aunque el servidor sirva menos filas;
  · troceado (--max-filas) o de una vez, el resultado es el mismo y no repite filas.

Uso:
  python test_reparacion.py        # sale con código 1 si algo falla
"""

import io
import sys
import json
import zlib
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import backfill_catalogo as bc
import reparar_versiones as rv
import test_versiones as tv
from test_versiones import comprueba, entrada, idu, pagina, zip_de, baja, LODEPA, BASE_ATOM

SAB = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)     # sábado: fuera de las franjas


def _pgrst(texto):
    """Una fecha como la devuelve PostgREST (timestamptz en UTC), o None."""
    momento = tv._inst(texto)
    return momento.astimezone(timezone.utc).isoformat() if momento else None


class BaseConLectura(tv.BaseFalsa):
    """BaseFalsa + lo que lee el script: el listado de licitaciones por páginas (keyset),
    con un 'tope' de filas por página como el «max rows» del servidor, los count(*), las
    adjudicaciones de LODEPA y las decisiones. Apunta TODAS las escrituras."""

    def __init__(self, *a, ganadas=(), decisiones=(), contratos=(), sin_permiso=(), tope=None, **k):
        super().__init__(*a, **k)
        self.ganadas = list(ganadas)
        self.decisiones = sorted(set(ganadas) | set(decisiones))   # todas, de cualquier perfil
        self.contratos = sorted(contratos)
        self.sin_permiso = set(sin_permiso)
        self.tope = tope
        self.escrituras = []    # todo lo que no es un GET (salvo el sondeo vacío de la RPC)
        self.enviadas = []      # ids de las licitaciones que llegan a la RPC

    def post(self, url, headers=None, data=None, json=None, timeout=None):  # noqa: A002
        p = (json or {}).get("p_lote", {}) if isinstance(json, dict) else {}
        if p.get("licitaciones") or p.get("bajas") or "carga_versiones" not in url:
            self.escrituras.append(("POST", url))
        self.enviadas += [x["licitacion_id"] for x in p.get("licitaciones", [])]
        return super().post(url, headers=headers, data=data, json=json, timeout=timeout)

    def delete(self, url, *a, **k):
        self.escrituras.append(("DELETE", url))
        return super().delete(url, *a, **k)

    def patch(self, url, *a, **k):
        self.escrituras.append(("PATCH", url))
        return tv._Resp(204)

    def get(self, url, headers=None, params=None, timeout=None):
        ruta = url.split("/rest/v1/", 1)[1]
        params = params or {}
        if ruta == "licitaciones":
            ids = sorted(self.lic)
            if "licitacion_id" in params:
                desde = params["licitacion_id"][3:]
                ids = [i for i in ids if i > desde]
            n = int(params["limit"]) if self.tope is None else min(self.tope, int(params["limit"]))
            filas = [{"licitacion_id": i, "fuente": self.lic[i].get("fuente"),
                      "fecha_publicacion": self.lic[i].get("fecha_publicacion"),
                      "atom_updated": _pgrst(self.lic[i].get("atom_updated")),
                      "version_snapshot": _pgrst(self.lic[i].get("version_snapshot")),
                      "version_huella": self.lic[i].get("version_huella"),
                      "ultima_vez": _pgrst(self.lic[i].get("ultima_vez"))} for i in ids[:n]]
            return tv._Resp(200, filas)
        if ruta.startswith("adjudicaciones") and "cif_adjudicatario" in params:
            cifs = params["cif_adjudicatario"][4:-1].split(",")
            return tv._Resp(200, [{"licitacion_id": c[0]} for c in self.adj if c[2] in cifs])
        if ruta.startswith("adjudicaciones") or ruta.startswith("licitaciones?"):
            r = tv._Resp(200, [])
            r.headers = {"Content-Range": f"0-0/{len(self.adj if ruta.startswith('adj') else self.lic)}"}
            return r
        tabla = ruta.split("?", 1)[0]
        if tabla in self.sin_permiso:
            return tv._Resp(403, None, text='{"code":"42501","message":"permission denied"}')
        if tabla == "decisiones" and "estado" in params:
            return tv._Resp(200, [{"licitacion_id": i} for i in self.ganadas])
        if tabla in ("decisiones", "contratos"):
            ids = self.decisiones if tabla == "decisiones" else self.contratos
            if "licitacion_id" in params:
                ids = [i for i in ids if i > params["licitacion_id"][3:]]
            n = int(params["limit"]) if self.tope is None else min(self.tope, int(params["limit"]))
            return tv._Resp(200, [{"licitacion_id": i} for i in ids[:n]])
        return tv._Resp(200, [])


def legado(base, n, titulo="viejo", adjs=(), au=None, snap=None, huella=None, uv="2026-01-01T00:00:00+00:00"):
    """Una fila de HOY (sin sello; 'uv' = ultima_vez, la última vez que la escribió el
    código viejo) o, con au, una ya escrita por el código nuevo."""
    i = idu(n)
    base.lic[i] = {"licitacion_id": i, "titulo": titulo, "fuente": "estatal",
                   "fecha_publicacion": "2025-03-01T00:00:00+00:00", "estado_expediente": None,
                   "atom_updated": au, "version_snapshot": snap, "version_huella": huella, "ultima_vez": uv}
    for lote, cif in adjs:
        base.adj[(i, lote, cif)] = {"licitacion_id": i, "lote": lote, "cif_adjudicatario": cif,
                                    "importe_sin_iva": 1.0, "resultado_code": "8"}


def escribe_zips(tmp, unidades):
    for fuente, periodo, miembros in unidades:
        with zipfile.ZipFile(tmp / f"{fuente}_{periodo}.zip", "w") as z:
            for nombre, blob in miembros:
                z.writestr(nombre, blob)


@contextmanager
def entorno(base=None, pagina_get=2):
    """Carpeta temporal de ZIP + parches: sin red, sin esperas y la base falsa como Supabase.
    pagina_get pequeño para que el listado tenga que paginar."""
    tmp = Path(tempfile.mkdtemp(prefix="test_reparacion_"))
    viejo = rv.PAGINA
    rv.PAGINA = pagina_get
    try:
        with tv._sin_esperas(), tv.parches(bc__CACHE_DIR=tmp, **tv._parches_comunes(base or BaseConLectura())):
            yield tmp
    finally:
        rv.PAGINA = viejo
        shutil.rmtree(tmp, ignore_errors=True)


def indexa(tmp, unidades, nombre="indice.sqlite", orden=None, procesos=1):
    escribe_zips(tmp, unidades)
    lista = [(f, p) for f, p, _ in unidades]
    rv.indexar(orden or lista, tmp / nombre, procesos=procesos, limpiar=False)
    return tmp / nombre


def _consulta(ruta, sql, *args):
    bd = sqlite3.connect(str(ruta))
    try:
        return bd.execute(sql, args).fetchall()
    finally:
        bd.close()


def ganadoras(ruta):
    return sorted(_consulta(ruta, "select id, marca, snap, huella from version"))


def objeto_ganador(ruta, n):
    filas = _consulta(ruta, "select objeto from version where id = ?", idu(n))
    return json.loads(zlib.decompress(filas[0][0])) if filas else None


def estado_ganador(ruta, n):
    o = objeto_ganador(ruta, n)
    return o["estado_expediente"] if o else None


def para(f, *a, **k):
    """Llama a f y devuelve el mensaje del SystemExit con el que para (None si no para)."""
    try:
        f(*a, **k)
    except SystemExit as e:
        return str(e)
    return None


def unidades_de(unidades):
    return [(f, p) for f, p, _ in unidades]


# Tres unidades con versiones del MISMO expediente repartidas: la buena (RES, marca del
# 10/01 vista en el snapshot más nuevo) está en el ZIP de febrero.
N = 19000001
UNIDADES = [
    ("estatal", "2025", zip_de("2025", ("20251201_200000", "2025-12-01T20:00:00.000+01:00",
                                        [entrada(N, "2025-12-01T10:00:00.000+01:00", estado="PUB"),
                                         entrada(N + 1, "2025-12-01T11:00:00.000+01:00", estado="PUB")]),
                               base_snapshot="2026-01-01T03:00:00.000+01:00")),
    ("estatal", "202601", zip_de("202601", ("20260110_200000", "2026-01-10T20:00:00.000+01:00",
                                            [entrada(N, "2026-01-10T09:00:00.000+01:00", estado="ADJ",
                                                     adjs=[("", "B11111111", "X", "10.0", "8")])]),
                                 base_snapshot="2026-02-01T03:00:00.000+01:00")),
    ("estatal", "202602", zip_de("202602", ("20260203_200000", "2026-02-03T20:00:00.000+01:00",
                                            [entrada(N, "2026-01-10T09:00:00.000+01:00", estado="RES",
                                                     adjs=[("", "B11111111", "X", "10.0", "9"),
                                                           ("", LODEPA, "LODEPA", "20.0", "9")])]),
                                 base_snapshot="2026-03-01T03:00:00.000+01:00")),
]


# =============================================================================
# El índice
# =============================================================================
def test_indice_independiente_del_orden():
    with entorno() as tmp:
        a = indexa(tmp, UNIDADES, "a.sqlite")
        b = indexa(tmp, UNIDADES, "b.sqlite", orden=[(f, p) for f, p, _ in reversed(UNIDADES)])
        comprueba("índice: misma ganadora en los dos órdenes", ganadoras(a), ganadoras(b))
        comprueba("índice: gana RES (misma marca, snapshot más nuevo)", estado_ganador(a, N), "RES")
        comprueba("índice: el expediente que solo sale en 2025 también está", estado_ganador(a, N + 1), "PUB")
        comprueba("índice: el corte es el snapshot más nuevo leído (base de febrero, en UTC)",
                  _consulta(a, "select max(corte) from unidad")[0][0], "2026-03-01T02:00:00.000000")


def test_indice_con_varios_procesos():
    with entorno() as tmp:
        uno = indexa(tmp, UNIDADES, "uno.sqlite")
        dos = indexa(tmp, UNIDADES, "dos.sqlite", procesos=2)
        comprueba("índice: con 2 procesos, lo mismo que con 1", ganadoras(dos), ganadoras(uno))


def test_indice_empate_total_siempre_el_mismo():
    """Misma marca y MISMO snapshot con contenido distinto, en dos .atom con el mismo
    <updated> y también DOS VECES en el mismo .atom: gana siempre la misma."""
    m = "2026-03-05T09:00:00.000+01:00"
    s = "2026-03-05T20:00:00.000+01:00"
    variantes = [
        zip_de("202603", ("20260305_200000", s, [entrada(N, m, presu="1.0")]),
               ("20260305_200000_1", s, [entrada(N, m, presu="2.0")])),
        zip_de("202603", ("20260305_200000_1", s, [entrada(N, m, presu="2.0")]),
               ("20260305_200000", s, [entrada(N, m, presu="1.0")])),
        zip_de("202603", ("20260305_200000", s, [entrada(N, m, presu="1.0"), entrada(N, m, presu="2.0")])),
        zip_de("202603", ("20260305_200000", s, [entrada(N, m, presu="2.0"), entrada(N, m, presu="1.0")])),
    ]
    resultados = []
    with entorno() as tmp:
        for k, miembros in enumerate(variantes):
            resultados.append(ganadoras(indexa(tmp, [("estatal", "202603", miembros)], f"{k}.sqlite")))
    comprueba("índice: empate total -> la misma ganadora en los 4 órdenes", len({repr(r) for r in resultados}), 1)


def test_indice_zip_anidado_y_bajas():
    """Dos ZIP anidados que se llaman IGUAL (en carpetas distintas), uno detrás de otro: se
    leen los dos. Y de las bajas, la más reciente (la sin fecha, fuera)."""
    def anidado(fecha, estado):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for nombre, blob in zip_de(fecha[:6], (f"{fecha}_200000", f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:]}T20:00:00.000+01:00",
                                                   [entrada(N, f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:]}T09:00:00.000+01:00",
                                                            estado=estado)]),
                                       base_snapshot=f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:]}T21:00:00.000+01:00"):
                z.writestr(nombre, blob)
        return b.getvalue()
    anual = [(f"{BASE_ATOM}_20250301_200000.atom", pagina("2025-03-01T20:00:00.000+01:00", [
                 baja(N, "2025-03-01T10:00:00.000+01:00"), entrada(N, "2025-02-01T09:00:00.000+01:00")])),
             ("a/mensual.zip", anidado("20251220", "EV")),
             ("b/mensual.zip", anidado("20251110", "PUB")),
             (f"{BASE_ATOM}_20250401_200000.atom", pagina("2025-04-01T20:00:00.000+01:00", [
                 baja(N, "2025-12-22T10:00:00.000+01:00", tipo=None), baja(N + 5, "sin fecha")]))]
    with entorno() as tmp:
        ruta = indexa(tmp, [("estatal", "2025", anual)])
        comprueba("índice: la versión del primer ZIP anidado (EV, diciembre) gana", estado_ganador(ruta, N), "EV")
        comprueba("índice: se han leído los dos anidados (4 .atom de dentro + 2 de fuera)",
                  _consulta(ruta, "select versiones from unidad")[0][0], 3)
        comprueba("índice: la baja más reciente de cada expediente (y la sin fecha, fuera)",
                  sorted(_consulta(ruta, "select id, crudo, tipo from baja")),
                  [(idu(N), "2025-12-22T10:00:00.000+01:00", None)])


def test_indice_apunta_las_versiones_sin_fecha():
    """La versión más nueva viene sin zona horaria: no se puede comparar. El índice se queda
    la vieja como ganadora, pero apunta el expediente para que no se escriba."""
    u = ("estatal", "202603", zip_de("202603",
        ("20260301_200000", "2026-03-01T20:00:00.000+01:00", [entrada(N, "2026-03-01T09:00:00.000+01:00", estado="PUB")]),
        ("20260302_200000", "2026-03-02T20:00:00.000+01:00", [entrada(N, "2026-03-02T09:00:00.000", estado="RES")])))
    with entorno() as tmp:
        ruta = indexa(tmp, [u])
        comprueba("índice: apunta el expediente con una versión sin fecha legible",
                  (_consulta(ruta, "select id from dudosa"), _consulta(ruta, "select dudosas from unidad")[0][0]),
                  ([(idu(N),)], 1))


# =============================================================================
# El ensayo
# =============================================================================
def _escenario(**k):
    """La base de HOY y su índice:
      X (N)    fila sin sello y contenido viejo; su ganadora es RES con LODEPA, y tiene una
               baja POSTERIOR a esa versión -> se escribe y se marca la baja al terminar;
      Y (N+1)  ya escrita por el código nuevo con una versión MÁS NUEVA que la del índice -> no se toca;
      W (N+2)  en la base sin versión en los ZIP -> no se toca;
      V (N+3)  sin sello, con LODEPA en la base, y su ganadora sin LODEPA -> «desaparece»;
      Z (N+4)  en los ZIP pero no en la base, sin fecha de publicación -> se inserta (la purga
               no lo quitaría) y queda con su baja, posterior a su versión;
      O (N+6)  falta, publicado en 2022 y con el plazo cerrado -> la purga lo quitaría: no se inserta;
      P (N+7)  falta, publicado en 2022 pero con el plazo ABIERTO y ganado por LODEPA -> se
               inserta (la purga no quita lo abierto) y el ensayo avisa de que aparece LODEPA;
      Q (N+10) falta, publicado en 2022 y cerrado, pero tiene una DECISIÓN -> la purga no borra
               lo que está en decisiones o contratos, así que se inserta;
      R (N+5)  sin sello, con una baja ANTERIOR a su ganadora (republicada después) -> nunca
               debe quedar marcada como baja, ni siquiera a mitad de una reparación troceada."""
    k.setdefault("decisiones", [idu(N + 10), idu(N + 30), idu(N + 31), idu(N + 32)])
    base = BaseConLectura(ganadas=[idu(N + 3)], **k)
    legado(base, N, adjs=[("", "B99999999")])
    legado(base, N + 1, titulo="nuevo del diario", au="2026-02-15T09:00:00.000+01:00",
           snap="2026-02-15T20:00:00.000+01:00", huella="h-diario")
    legado(base, N + 2, titulo="sin versión en los ZIP")
    legado(base, N + 3, adjs=[("", LODEPA)])
    legado(base, N + 5)
    extra = ("estatal", "202603", zip_de("202603", ("20260310_200000", "2026-03-10T20:00:00.000+01:00", [
        entrada(N + 3, "2026-03-10T09:00:00.000+01:00", estado="RES", adjs=[("", "B22222222", "Y", "5.0", "9")]),
        entrada(N + 4, "2026-03-10T09:00:00.000+01:00", estado="PUB"),
        entrada(N + 5, "2026-03-10T09:00:00.000+01:00", estado="PUB"),
        entrada(N + 6, "2026-03-10T09:00:00.000+01:00", estado="RES", publicada="2022-05-01", fin="2022-06-01"),
        entrada(N + 7, "2026-03-10T09:00:00.000+01:00", estado="ADJ", publicada="2022-05-01", fin="2027-01-01",
                adjs=[("", LODEPA, "LODEPA", "7.0", "8")]),
        entrada(N + 10, "2026-03-10T09:00:00.000+01:00", estado="RES", publicada="2022-05-01", fin="2022-06-01"),
        baja(N, "2026-03-09T10:00:00.000+01:00"), baja(N + 4, "2026-03-11T10:00:00.000+01:00"),
        baja(N + 5, "2026-03-01T10:00:00.000+01:00")]),
        base_snapshot="2026-04-01T03:00:00.000+02:00"))
    return base, UNIDADES + [extra]


def test_ensayo_no_escribe():
    base, unidades = _escenario()
    with entorno(base) as tmp:
        ruta = indexa(tmp, unidades)
        c = rv.ensayo(unidades_de(unidades), ruta, reloj=lambda: SAB)
    comprueba("ensayo: ni una escritura", base.escrituras, [])
    comprueba("ensayo: cifras (base, con sello, a escribir, sin versión, fuera de la base)",
              (c["base"], c["con_sello"], c["a_escribir"], c["sin_version_en_zip"], c["zip_no_en_base"]),
              (5, 1, 3, 1, 4))
    comprueba("ensayo: de los que faltan, se insertan Z, P y Q (viejo con decisión) y la purga quitaría O",
              (c["a_insertar"], c["viejos_protegidos"], c["purga_los_quitaria"], c["bajas_de_insertables"]),
              (3, 1, 1, 1))
    comprueba("ensayo: LODEPA aparece en X y en P (que se inserta) y desaparece de V",
              (c["lodepa_aparecen"], c["lodepa_desaparecen"]), ([idu(N), idu(N + 7)], [idu(N + 3)]))


def test_listado_con_tope_del_servidor():
    """El servidor sirve 1 fila por página aunque se pidan 2: los listados (licitaciones y
    decisiones) siguen hasta una página vacía y no se quedan cortos."""
    base, unidades = _escenario(tope=1)
    with entorno(base) as tmp:
        c = rv.ensayo(unidades_de(unidades), indexa(tmp, unidades), reloj=lambda: SAB)
    comprueba("listado: con el tope del servidor lee las 5 filas", c["base"], 5)
    comprueba("listado: y todas las decisiones (Q, protegido por la suya, se insertaría)",
              (c["a_insertar"], c["viejos_protegidos"]), (3, 1))


# =============================================================================
# Escribir
# =============================================================================
def test_escribir_solo_lo_que_hace_falta():
    base, unidades = _escenario()
    y_antes = dict(base.fila(idu(N + 1)))
    with entorno(base) as tmp:
        rv.escribir(unidades_de(unidades), indexa(tmp, unidades), pausa=0, reloj=lambda: SAB)
    x = base.fila(idu(N))
    comprueba("escribir: X con la versión ganadora (RES) y sus adjudicaciones",
              (x.get("estado_expediente"), base.adj_de(idu(N))),
              ("RES", [("", "B11111111", 10.0, "9"), ("", LODEPA, 20.0, "9")]))
    comprueba("escribir: la baja de X (posterior a su versión) queda marcada",
              x.get("baja_en"), "2026-03-09T10:00:00.000+01:00")
    comprueba("escribir: R (baja anterior a su ganadora) no queda como baja", base.fila(idu(N + 5)).get("baja_en"), None)
    comprueba("escribir: Y (más nueva en la base) ni se manda ni cambia",
              (idu(N + 1) in base.enviadas, base.fila(idu(N + 1)) == y_antes), (False, True))
    comprueba("escribir: W (sin versión en los ZIP) sigue igual", base.fila(idu(N + 2)).get("titulo"),
              "sin versión en los ZIP")
    comprueba("escribir: Z se inserta con su versión y su baja",
              (base.fila(idu(N + 4)).get("estado_expediente"), base.fila(idu(N + 4)).get("baja_en")),
              ("PUB", "2026-03-11T10:00:00.000+01:00"))
    comprueba("escribir: P (viejo pero abierto) se inserta con su adjudicación; O (la purga lo quitaría), no",
              (base.adj_de(idu(N + 7)), idu(N + 6) in base.lic), ([("", LODEPA, 7.0, "8")], False))
    comprueba("escribir: Q (viejo y cerrado, pero con decisión) se inserta", idu(N + 10) in base.lic, True)
    comprueba("escribir: ninguna baja va a un expediente que no está", sum(r.get("bajas_sin_fila", 0) for r in base.respuestas), 0)
    comprueba("escribir: V queda con la adjudicación de su ganadora (sin LODEPA)",
              base.adj_de(idu(N + 3)), [("", "B22222222", 5.0, "9")])


def test_escribir_troceado_igual_que_de_una():
    base1, unidades = _escenario()
    with entorno(base1) as tmp:
        rv.escribir(unidades_de(unidades), indexa(tmp, unidades), pausa=0, reloj=lambda: SAB)
    base2, _ = _escenario()
    bajas_a_medias = []
    with entorno(base2) as tmp:
        ruta = indexa(tmp, unidades)
        for _pasada in range(10):
            antes = len(base2.enviadas)
            rv.escribir(unidades_de(unidades), ruta, max_filas=1, pausa=0, reloj=lambda: SAB)
            if len(base2.enviadas) == antes:
                break
            bajas_a_medias.append(sorted(i for i, f in base2.lic.items() if f.get("baja_en")))
    comprueba("troceado: mismo resultado que de una vez", (base2.lic, base2.adj), (base1.lic, base1.adj))
    comprueba("troceado: ninguna fila se manda dos veces", sorted(base2.enviadas), sorted(set(base2.enviadas)))
    # 6 filas (3 de la base y 3 que faltan) con tope 1: cinco pasadas paran a medias (sin
    # bajas) y la sexta escribe la última y TERMINA, así que manda las bajas (X y Z).
    comprueba("troceado: sin bajas a medias; las manda la pasada que termina", bajas_a_medias,
              [[]] * 5 + [sorted([idu(N), idu(N + 4)])])


def test_escribir_empates_de_marca_como_la_guarda():
    """Filas ya selladas con la MISMA marca que su ganadora: se manda solo si el snapshot de
    la ganadora es igual o posterior y el contenido distinto (la regla de la guarda)."""
    base, unidades = _escenario()
    marca, snap = "2026-01-10T09:00:00.000+01:00", "2026-02-03T20:00:00.000+01:00"
    with entorno(base) as tmp:
        ruta = indexa(tmp, unidades)
        huella = objeto_ganador(ruta, N)["version_huella"]
        casos = {"snapshot anterior y otra huella -> se manda": ("2026-01-10T20:00:00.000+01:00", "otra", True),
                 "mismo snapshot y otra huella -> se manda": (snap, "otra", True),
                 "mismo snapshot y misma huella -> no": (snap, huella, False),
                 "snapshot guardado posterior -> no": ("2026-02-10T20:00:00.000+01:00", "otra", False)}
        for nombre, (snap_guardado, huella_guardada, esperado) in casos.items():
            base.enviadas.clear()
            legado(base, N, au=marca, snap=snap_guardado, huella=huella_guardada)
            rv.escribir(unidades_de(unidades), ruta, pausa=0, reloj=lambda: SAB)
            comprueba(f"empate de marca: {nombre}", idu(N) in base.enviadas, esperado)


def test_escribir_no_toca_lo_posterior_al_corte_ni_lo_dudoso():
    """X la reescribió el código viejo DESPUÉS del corte de los ZIP (puede tener una versión
    más nueva que cualquiera de ellos); V tiene una versión sin fecha legible. Ninguna de
    las dos se toca; las demás, sí. Y D, que falta en la base, tiene una versión con fecha y
    otra sin ella: tampoco se inserta (la más nueva podría ser la que no se puede leer)."""
    base, unidades = _escenario()
    legado(base, N + 12, titulo="solo versiones sin fecha")   # U: en la base, sin ninguna versión legible
    # El corte (base del ZIP de abril) es el 03/04 a la 01:00 UTC; el límite, 3 h antes.
    base.lic[idu(N)]["ultima_vez"] = "2026-04-03T00:30:00+00:00"
    dudosa = ("estatal", "202604", zip_de("202604", ("20260402_200000", "2026-04-02T20:00:00.000+02:00",
                                                      [entrada(N + 3, "2026-04-02T09:00:00.000"),
                                                       entrada(N + 8, "2026-04-02T09:00:00.000"),
                                                       entrada(N + 11, "2026-04-02T09:00:00.000"),
                                                       entrada(N + 12, "2026-04-02T09:00:00.000")]),
                                           base_snapshot="2026-04-03T03:00:00.000+02:00",
                                           base_items=[entrada(N + 8, "2026-04-01T09:00:00.000+02:00")]))
    with entorno(base) as tmp:
        ruta = indexa(tmp, unidades + [dudosa])
        c = rv.ensayo(unidades_de(unidades + [dudosa]), ruta, reloj=lambda: SAB)
        rv.escribir(unidades_de(unidades + [dudosa]), ruta, pausa=0, reloj=lambda: SAB)
    comprueba("corte: el ensayo las cuenta aparte", (c["posteriores_al_corte"], c["dudosas_en_base"]), (1, 1))
    comprueba("corte: ni X (posterior al corte) ni V (dudosa) se mandan",
              (idu(N) in base.enviadas, idu(N + 3) in base.enviadas), (False, False))
    comprueba("corte: R sí se escribe", base.fila(idu(N + 5)).get("estado_expediente"), "PUB")
    comprueba("dudosa: D, que falta y tiene una versión sin fecha, no se inserta (y el ensayo lo cuenta)",
              (idu(N + 8) in base.lic, c["faltan_dudosos"]), (False, 1))
    comprueba("dudosa: las cifras de lo que falta cuadran (O para la purga; N+11, solo sin fecha, aparte)",
              (c["zip_no_en_base"], c["a_insertar"], c["purga_los_quitaria"], c["faltan_dudosos"],
               c["solo_sin_fecha"], idu(N + 11) in base.lic), (5, 3, 1, 1, 1, False))
    comprueba("dudosa: U (en la base y solo con versiones sin fecha) no se cuenta como «con versión»",
              c["dudosas_en_base"], 1)


def _fija_hecha_en(ruta, valores):
    bd = sqlite3.connect(str(ruta))
    try:
        for clave, hecha in valores.items():
            bd.execute("update unidad set hecha_en = ? where clave = ?", (hecha, clave))
        bd.commit()
    finally:
        bd.close()


def test_corte_con_un_mensual_abierto():
    """El ZIP de octubre se leyó el 30/10 (mes abierto, corte 29/10 19:00 UTC) y luego se
    añadió noviembre (corte 02/11). Al de octubre le falta lo publicado entre el 29/10 y el
    31/10, así que el corte de la fuente sigue siendo el 29/10: una fila que el código viejo
    escribió el 30/10 NO se toca. Con octubre ya leído entero, sí."""
    base = BaseConLectura()
    legado(base, N + 1, au="2026-10-20T09:00:00.000+02:00", snap="2026-10-20T20:00:00.000+02:00", huella="h")
    legado(base, N, uv="2026-10-30T11:00:00+00:00")
    unidades = [("estatal", "202610", zip_de("202610", ("20261010_200000", "2026-10-10T20:00:00.000+02:00",
                                                        [entrada(N, "2026-10-10T09:00:00.000+02:00", estado="PUB")]),
                                             base_snapshot="2026-10-29T20:00:00.000+01:00")),
                ("estatal", "202611", zip_de("202611", ("20261101_200000", "2026-11-01T20:00:00.000+01:00",
                                                        [entrada(N + 9, "2026-11-01T09:00:00.000+01:00")]),
                                             base_snapshot="2026-11-02T03:00:00.000+01:00"))]
    with entorno(base) as tmp:
        ruta = indexa(tmp, unidades)
        _fija_hecha_en(ruta, {"estatal_202610": "2026-10-30T12:00:00+00:00",
                              "estatal_202611": "2026-11-02T12:00:00+00:00"})
        rv.escribir(unidades_de(unidades), ruta, pausa=0, reloj=lambda: SAB)
        comprueba("corte: con octubre abierto, la fila escrita el 30/10 no se toca", idu(N) in base.enviadas, False)
        _fija_hecha_en(ruta, {"estatal_202610": "2026-11-05T00:00:00+00:00"})
        rv.escribir(unidades_de(unidades), ruta, pausa=0, reloj=lambda: SAB)
        comprueba("corte: con octubre leído entero, sí", idu(N) in base.enviadas, True)


def test_reindexa_un_mensual_abierto_caducado():
    with entorno() as tmp:
        ruta = indexa(tmp, UNIDADES)
        _fija_hecha_en(ruta, {"estatal_202601": "2026-01-15T00:00:00+00:00",    # abierto y viejo
                              "estatal_202602": "2026-03-10T00:00:00+00:00"})   # leído con el mes cerrado
        rv.indexar(unidades_de(UNIDADES), ruta, procesos=1, limpiar=False)
        hechas = dict(_consulta(ruta, "select clave, hecha_en from unidad"))
        comprueba("índice: relee el mensual abierto de hace días y no el cerrado",
                  (hechas["estatal_202601"] != "2026-01-15T00:00:00+00:00",
                   hechas["estatal_202602"] == "2026-03-10T00:00:00+00:00"), (True, True))


def test_bajas_no_se_mandan_para_filas_apartadas():
    """R se deja sin tocar (el código viejo la escribió después del corte). Su baja es
    anterior a su ganadora: si se mandara, la base la marcaría (fila sin sello)."""
    base, unidades = _escenario()
    base.lic[idu(N + 5)]["ultima_vez"] = "2026-03-31T23:00:00+00:00"     # límite: 31/03 22:00 UTC
    with entorno(base) as tmp:
        rv.escribir(unidades_de(unidades), indexa(tmp, unidades), pausa=0, reloj=lambda: SAB)
    comprueba("bajas: la fila apartada ni se escribe ni queda como baja",
              (idu(N + 5) in base.enviadas, base.fila(idu(N + 5)).get("baja_en")), (False, None))
    comprueba("bajas: las demás sí (X)", base.fila(idu(N)).get("baja_en"), "2026-03-09T10:00:00.000+01:00")


def test_tope_exacto_termina_y_manda_las_bajas():
    base, unidades = _escenario()
    with entorno(base) as tmp:
        rv.escribir(unidades_de(unidades), indexa(tmp, unidades), max_filas=6, pausa=0, reloj=lambda: SAB)
    comprueba("tope exacto (6 de 6): termina y marca la baja de X", base.fila(idu(N)).get("baja_en"),
              "2026-03-09T10:00:00.000+01:00")


def test_sin_permiso_en_decisiones_se_inserta_de_menos():
    """Si decisiones no se puede leer, se avisa y se sigue: Q (viejo, solo protegido por su
    decisión) no se inserta; los demás, sí."""
    base, unidades = _escenario(sin_permiso=("decisiones",))
    with entorno(base) as tmp:
        rv.escribir(unidades_de(unidades), indexa(tmp, unidades), pausa=0, reloj=lambda: SAB)
    comprueba("sin permiso en decisiones: Q no se inserta y Z sí",
              (idu(N + 10) in base.lic, idu(N + 4) in base.lic), (False, True))


def test_bajas_no_salen_pasada_la_hora():
    """La última pasada no tiene nada que escribir, pero el listado la ha llevado más allá de
    la hora límite: las bajas se dejan para la siguiente."""
    base, unidades = _escenario()
    with entorno(base) as tmp:
        ruta = indexa(tmp, unidades)
        rv.escribir(unidades_de(unidades), ruta, pausa=0, reloj=lambda: SAB)
        base.lic[idu(N)]["baja_en"] = base.lic[idu(N)]["baja_tipo"] = None
        horas = iter([datetime(2026, 11, 2, 4, 20, tzinfo=timezone.utc)]
                     + [datetime(2026, 11, 2, 4, 50, tzinfo=timezone.utc)] * 10)
        rv.escribir(unidades_de(unidades), ruta, pausa=0, reloj=lambda: next(horas))
        comprueba("bajas: pasada la hora límite no se mandan", base.fila(idu(N)).get("baja_en"), None)
        rv.escribir(unidades_de(unidades), ruta, pausa=0, reloj=lambda: SAB)
        comprueba("bajas: la pasada siguiente las manda", base.fila(idu(N)).get("baja_en"),
                  "2026-03-09T10:00:00.000+01:00")


def test_frontera_de_la_purga():
    """Igual que purga_catalogo: publicado hace MÁS de 3 años y con el plazo cerrado (o sin
    plazo). Por días, y la frontera cuenta como borrado."""
    hoy = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)
    casos = {
        "publicado justo hace 3 años, plazo cerrado -> la quitaría": ("2023-11-03", "2023-12-01", True),
        "un día después -> no": ("2023-11-04", "2023-12-01", False),
        "viejo sin plazo -> la quitaría": ("2022-01-01", None, True),
        "viejo con el plazo acabando hoy -> la quitaría": ("2022-01-01", "2026-11-03", True),
        "viejo con el plazo acabando mañana -> no": ("2022-01-01", "2026-11-04", False),
        "sin fecha de publicación -> no": (None, "2022-01-01", False),
        "con zona horaria en la fecha": ("2023-11-03+01:00", "2023-12-01+01:00", True),
    }
    for nombre, (pub, fin, esperado) in casos.items():
        comprueba(f"purga: {nombre}",
                  rv.la_purga_lo_quitaria({"fecha_publicacion": pub, "fecha_fin_plazo": fin}, hoy), esperado)
    bisiesto = datetime(2028, 2, 29, 10, 0, tzinfo=timezone.utc)
    comprueba("purga: un 29 de febrero el umbral es el 28/02 de hace 3 años",
              (rv.la_purga_lo_quitaria({"fecha_publicacion": "2025-02-28"}, bisiesto),
               rv.la_purga_lo_quitaria({"fecha_publicacion": "2025-03-01"}, bisiesto)), (True, False))


def test_escribir_usa_la_ventana_del_indice():
    """Índice hecho el 10/02 (ventana: 202602, 202601, 2025 y 2024); se escribe el 07/03,
    con la ventana de hoy ya en marzo. Vale el índice del 10/02; exigir la de hoy pararía."""
    hecho = datetime(2026, 2, 10, 12, 0, tzinfo=timezone.utc)
    base = BaseConLectura()
    legado(base, N + 1, au="2026-02-09T09:00:00.000+01:00", snap="2026-02-09T20:00:00.000+01:00", huella="h")
    legado(base, N)
    unidades = [(f, p, zip_de(p, base_snapshot="2026-02-10T03:00:00.000+01:00",
                              base_items=[entrada(N, "2026-02-05T09:00:00.000+01:00")] if (f, p) == ("estatal", "202602") else []))
                for f, p in bc.construir_unidades(hoy=hecho)]
    sabado_marzo = datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc)
    with entorno(base) as tmp:
        ruta = indexa(tmp, unidades)
        _fija_hecha_en(ruta, {f"{f}_{p}": hecho.isoformat() for f, p, _ in unidades})
        hoy = para(rv.escribir, bc.construir_unidades(hoy=sabado_marzo), ruta, pausa=0, reloj=lambda: sabado_marzo)
        comprueba("ventana: con la de hoy pararía (falta 202603)", "202603" in (hoy or ""), True)
        rv.escribir(None, ruta, pausa=0, reloj=lambda: sabado_marzo)
    comprueba("ventana: con la del índice escribe", idu(N) in base.enviadas, True)


def test_un_fallo_no_espera_a_las_descargas():
    """Si la lectura de un ZIP falla, indexar sale sin esperar a las descargas que van por
    delante (en la vida real, un anual tarda más de una hora)."""
    import time as _t
    viejos = rv._descarga, rv._indexa_unidad

    def descarga(u):
        if u[1] != "202602":
            _t.sleep(5)
        return Path("no-existe.zip")

    def falla(*a, **k):
        raise RuntimeError("lectura rota")

    rv._descarga, rv._indexa_unidad = descarga, falla
    try:
        with entorno() as tmp:
            t0 = _t.time()
            salio = None
            try:
                rv.indexar([("estatal", "202602"), ("estatal", "202601"), ("estatal", "2025")], tmp / "i.sqlite",
                           procesos=1, limpiar=False)
            except RuntimeError as e:
                salio = str(e)
            tardo = _t.time() - t0
    finally:
        rv._descarga, rv._indexa_unidad = viejos
    comprueba("fallo: sale enseguida, sin esperar a las descargas adelantadas", (salio, tardo < 3), ("lectura rota", True))
    # Y el PROCESO termina sin esperar a que acaben (en Actions, minutos de job; en local,
    # un Ctrl+C que parece colgado).
    codigo = (
        "import time, sys\n"
        "from pathlib import Path\n"
        "import reparar_versiones as rv\n"
        "def descarga(u):\n"
        "    if u[1] != '202602':\n"
        "        time.sleep(10)\n"
        "    return Path('no-existe.zip')\n"
        "def falla(*a, **k):\n"
        "    raise RuntimeError('lectura rota')\n"
        "rv._descarga, rv._indexa_unidad = descarga, falla\n"
        "rv.indexar([('estatal', '202602'), ('estatal', '202601')], sys.argv[1], procesos=1, limpiar=False)\n")
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        t0 = _t.time()
        r = subprocess.run([sys.executable, "-c", codigo, str(Path(tmp) / "i.sqlite")], cwd=Path(__file__).parent,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        tardo = _t.time() - t0
    comprueba("fallo: el proceso termina sin esperar a la descarga de 10 s", (r.returncode != 0, tardo < 8), (True, True))


def test_escribir_para_si_el_indice_esta_incompleto():
    base, unidades = _escenario()
    with entorno(base) as tmp:
        salio = para(rv.escribir, unidades_de(unidades), indexa(tmp, unidades[1:]), pausa=0, reloj=lambda: SAB)
    comprueba("índice incompleto: para y nombra lo que falta", "estatal_2025" in (salio or ""), True)
    comprueba("índice incompleto: ni una escritura", base.escrituras, [])


def test_escribir_exige_la_ventana_entera():
    viejo = sys.argv
    sys.argv = ["reparar_versiones.py", "--escribir", "--periodos", "2025"]
    try:
        salio = None
        try:
            rv.main()
        except SystemExit as e:
            salio = e.code
    finally:
        sys.argv = viejo
    comprueba("--escribir con --periodos: argparse lo rechaza (código 2)", salio, 2)


def test_escribir_para_si_hay_atom_ilegibles():
    base, unidades = _escenario()
    roto = [("estatal", "202604", [(f"{BASE_ATOM}_20260401_200000.atom", b"<feed><no-cierra>")])]
    with entorno(base) as tmp:
        ruta = indexa(tmp, unidades + roto)
        salio = para(rv.escribir, unidades_de(unidades + roto), ruta, pausa=0, reloj=lambda: SAB)
    comprueba("ilegibles: para antes de escribir", ("ilegibles" in (salio or ""), base.escrituras), (True, []))


def test_escribir_para_si_el_codigo_nuevo_no_ha_corrido():
    base, unidades = _escenario()
    base.lic[idu(N + 1)]["atom_updated"] = None          # ninguna fila con sello
    with entorno(base) as tmp:
        salio = para(rv.escribir, unidades_de(unidades), indexa(tmp, unidades), pausa=0, reloj=lambda: SAB)
    comprueba("sin sello: para con el aviso del paso 3", "radar.yml" in (salio or ""), True)
    comprueba("sin sello: ni una escritura", base.escrituras, [])


def test_franjas_del_robot():
    base, unidades = _escenario()
    lunes_10 = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
    with entorno(base) as tmp:
        ruta = indexa(tmp, unidades)
        salio = para(rv.escribir, unidades_de(unidades), ruta, pausa=0, reloj=lambda: lunes_10)
        comprueba("franja: el lunes a las 10:00 UTC no arranca",
                  ("franja del robot" in (salio or ""), base.escrituras), (True, []))
        # Lunes 04:20: la franja empieza a las 05:00, así que para a las 04:45. El reloj avanza
        # 20 min en cada consulta: escribe un lote y se para.
        horas = iter([datetime(2026, 11, 2, 4, 20, tzinfo=timezone.utc) + timedelta(minutes=20 * k) for k in range(50)])
        viejo = bc.LOTE_RPC
        bc.LOTE_RPC = 1
        try:
            rv.escribir(unidades_de(unidades), ruta, pausa=0, reloj=lambda: next(horas))
        finally:
            bc.LOTE_RPC = viejo
        comprueba("franja: se para antes de las 05:00 tras un lote, sin mandar bajas",
                  (len(base.enviadas), sum(r.get("bajas_recibidas", 0) for r in base.respuestas)), (1, 0))
        rv.escribir(unidades_de(unidades), ruta, pausa=0, reloj=lambda: SAB)
        comprueba("franja: el sábado termina lo que faltaba", base.fila(idu(N + 3)).get("estado_expediente"), "RES")


def test_franja_calculo():
    comprueba("franja: domingo 06:30 dentro (mantenimiento)",
              rv._franja(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))[0], True)
    comprueba("franja: viernes 15:00 fuera; la próxima, el mantenimiento del domingo",
              rv._franja(datetime(2026, 10, 30, 15, 0, tzinfo=timezone.utc)),
              (False, datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)))


def main():
    pruebas = (test_indice_independiente_del_orden, test_indice_con_varios_procesos,
               test_indice_empate_total_siempre_el_mismo, test_indice_zip_anidado_y_bajas,
               test_indice_apunta_las_versiones_sin_fecha,
               test_ensayo_no_escribe, test_listado_con_tope_del_servidor,
               test_escribir_solo_lo_que_hace_falta, test_escribir_troceado_igual_que_de_una,
               test_escribir_empates_de_marca_como_la_guarda,
               test_escribir_no_toca_lo_posterior_al_corte_ni_lo_dudoso,
               test_corte_con_un_mensual_abierto, test_reindexa_un_mensual_abierto_caducado,
               test_bajas_no_se_mandan_para_filas_apartadas, test_tope_exacto_termina_y_manda_las_bajas,
               test_frontera_de_la_purga, test_sin_permiso_en_decisiones_se_inserta_de_menos,
               test_bajas_no_salen_pasada_la_hora,
               test_escribir_usa_la_ventana_del_indice, test_un_fallo_no_espera_a_las_descargas,
               test_escribir_para_si_el_indice_esta_incompleto, test_escribir_exige_la_ventana_entera,
               test_escribir_para_si_hay_atom_ilegibles, test_escribir_para_si_el_codigo_nuevo_no_ha_corrido,
               test_franjas_del_robot, test_franja_calculo)
    for t in pruebas:
        tv.prueba(t)
    print(f"\n{len(tv.BIEN)} bien, {len(tv.FALLOS)} mal")
    if tv.FALLOS:
        print("FALLAN:", ", ".join(tv.FALLOS))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
