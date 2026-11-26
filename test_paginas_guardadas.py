# -*- coding: utf-8 -*-
"""Tests de las páginas guardadas (26/11/2026): el job «descargar» baja cada feed una vez
(descarga_paginas.py) y «generar» y «catalogo» las leen con RADAR_PAGINAS.

Sin red: requests.get se sustituye por un servidor falso que sirve tres páginas encadenadas
con rel="next". Se ejecuta con:  python test_paginas_guardadas.py
"""
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import feeds
import descarga_paginas

sys.stdout.reconfigure(encoding="utf-8")

BASE = "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_1044/"
CABECERA = BASE + "PlataformasAgregadasSinMenores.atom"
P1 = BASE + "PlataformasAgregadasSinMenores_20261124_030042.atom"
P2 = BASE + "PlataformasAgregadasSinMenores_20261124_030042_1.atom"
RECHAZO = (b"<html><head><title>Request Rejected</title></head><body>The requested URL was rejected. "
           b"Please consult with your administrator.<br><br>Your support ID is: <4515090675055901215><br><br>"
           b"<a href='javascript:history.back();'>[Go Back]</a></body></html>")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def pagina(ids, siguiente=None):
    enlace = f'<link rel="next" href="{siguiente}"/>' if siguiente else ""
    entradas = "".join(f"<entry><id>{i}</id><updated>2026-11-24T01:00:13.000+01:00</updated>"
                       f"<title>t{i}</title></entry>" for i in ids)
    return (f'<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            f'<link rel="self" href="x"/>{enlace}<updated>2026-11-24T03:00:42Z</updated>{entradas}</feed>'
            ).encode("utf-8")


PAGINAS = {CABECERA: pagina(["a", "b"], P1), P1: pagina(["c", "d"], P2), P2: pagina(["e"])}


class _Resp:
    def __init__(self, status_code=200, content=b"", content_type="application/atom+xml"):
        self.status_code, self.content, self.headers = status_code, content, {"Content-Type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise feeds.requests.HTTPError(f"{self.status_code} Error")


class Servidor:
    """Sustituye a requests.get: sirve 'paginas' (url -> bytes o _Resp) y anota cada petición."""

    def __init__(self, paginas):
        self.paginas, self.pedidas, self.agentes = paginas, [], []

    def __call__(self, url, headers=None, timeout=None):
        self.pedidas.append(url)
        self.agentes.append((headers or {}).get("User-Agent"))
        r = self.paginas[url]
        return r if isinstance(r, _Resp) else _Resp(200, r)


@contextlib.contextmanager
def entorno(servidor, paginas_guardadas=None):
    """requests.get falso, sin pausas y con RADAR_PAGINAS puesta (o quitada)."""
    get, dormir, antes = feeds.requests.get, feeds.time.sleep, os.environ.get("RADAR_PAGINAS")
    feeds.requests.get = servidor
    feeds.time.sleep = lambda s: None
    if paginas_guardadas is None:
        os.environ.pop("RADAR_PAGINAS", None)
    else:
        os.environ["RADAR_PAGINAS"] = str(paginas_guardadas)
    try:
        yield
    finally:
        feeds.requests.get = get
        feeds.time.sleep = dormir
        if antes is None:
            os.environ.pop("RADAR_PAGINAS", None)
        else:
            os.environ["RADAR_PAGINAS"] = antes


def ids(entradas):
    return [e.findtext("atom:id", namespaces=feeds.ATOM_NS) for e in entradas]


def guarda(salida, paginas=PAGINAS, max_paginas=20):
    srv = Servidor(paginas)
    with entorno(srv), contextlib.redirect_stdout(io.StringIO()):
        codigo = descarga_paginas.main(["--salida", str(salida), "--max-paginas", str(max_paginas)],
                                       feeds=[{"fuente": "agregadas", "url": CABECERA}])
    return codigo, srv, json.loads((Path(salida) / feeds.MANIFIESTO).read_text(encoding="utf-8"))


def test_guardadas_igual_que_en_vivo():
    with entorno(Servidor(PAGINAS)):
        vivo = feeds.descarga_entradas(CABECERA)
    with tempfile.TemporaryDirectory() as d:
        codigo, srv, man = guarda(d)
        check(codigo == 0, "descarga_paginas termina con 0")
        check(srv.pedidas == [CABECERA, P1, P2], f"una petición por página: {srv.pedidas}")
        feed = man["feeds"][CABECERA]
        check([p["nombre"] for p in feed["paginas"]] ==
              ["PlataformasAgregadasSinMenores.atom", "PlataformasAgregadasSinMenores_20261124_030042.atom",
               "PlataformasAgregadasSinMenores_20261124_030042_1.atom"], "nombres de las páginas, en orden")
        for p in feed["paginas"]:
            datos = (Path(d) / p["fichero"]).read_bytes()
            check(datos == PAGINAS[p["url"]], f"{p['fichero']}: los bytes exactos")
            check(hashlib.sha256(datos).hexdigest() == p["sha256"], f"{p['fichero']}: SHA-256")
        check(feed["fallo"] is None and feed["tope"] is False, "sin fallo ni tope")
        nadie = Servidor({})
        with entorno(nadie, d):
            guardado = feeds.descarga_entradas(CABECERA)
        check(nadie.pedidas == [], "con RADAR_PAGINAS no se pide nada")
    check(ids(vivo[0]) == ids(guardado[0]) == ["a", "b", "c", "d", "e"], "mismas entradas en vivo y guardadas")
    check(vivo[1:] == guardado[1:] == (3, False), f"mismas páginas y tope: {vivo[1:]} {guardado[1:]}")
    print("  OK test_guardadas_igual_que_en_vivo")


def test_tope_se_guarda():
    with tempfile.TemporaryDirectory() as d:
        _, srv, man = guarda(d, max_paginas=2)
        check(srv.pedidas == [CABECERA, P1], "para en el tope")
        check(man["feeds"][CABECERA]["tope"] is True, "tope en el manifiesto")
        with entorno(Servidor({}), d):
            r = feeds.descarga_entradas(CABECERA)
    check(r[1:] == (2, True) and ids(r[0]) == ["a", "b", "c", "d"], f"tope leído de las guardadas: {r[1:]}")
    print("  OK test_tope_se_guarda")


def test_rechazo_se_para_sin_reintentar():
    paginas = {CABECERA: _Resp(200, RECHAZO, "text/html")}
    with tempfile.TemporaryDirectory() as d:
        codigo, srv, man = guarda(d, paginas)
        check(codigo == 0, "un rechazo no hace fallar el job")
        check(srv.pedidas == [CABECERA], f"un solo intento: {srv.pedidas}")
        f = man["feeds"][CABECERA]["fallo"]
        check(f["tipo"] == "rechazo" and f["support_id"] == "4515090675055901215" and f["pagina"] == 0,
              f"rechazo anotado: {f}")
        check(f["codigo"] == 200 and f["content_type"] == "text/html" and "Request Rejected" in f["inicio"],
              "código, tipo y primeros caracteres")
        check(man["feeds"][CABECERA]["paginas"] == [], "ninguna página guardada")
        salida = io.StringIO()
        with entorno(Servidor({}), d), contextlib.redirect_stdout(salida):
            r = feeds.descarga_entradas(CABECERA)
        check(r == ([], 0, False), "las guardadas: vacío, como en vivo")
        check("AVISO" in salida.getvalue(), "y avisa")
    print("  OK test_rechazo_se_para_sin_reintentar")


def test_rechazo_a_media_cadena_guarda_lo_anterior():
    paginas = {CABECERA: PAGINAS[CABECERA], P1: _Resp(503, b"Service Unavailable", "text/plain")}
    with tempfile.TemporaryDirectory() as d:
        _, srv, man = guarda(d, paginas)
        check(srv.pedidas == [CABECERA, P1], "para en la página que falla")
        f = man["feeds"][CABECERA]
        check(len(f["paginas"]) == 1 and f["fallo"]["tipo"] == "error_http" and f["fallo"]["codigo"] == 503,
              f"guarda la cabecera y anota el 503: {f['fallo']}")
        with entorno(Servidor({}), d), contextlib.redirect_stdout(io.StringIO()):
            r = feeds.descarga_entradas(CABECERA)
    check(ids(r[0]) == ["a", "b"] and r[1:] == (1, False), "lee lo que se guardó")
    print("  OK test_rechazo_a_media_cadena_guarda_lo_anterior")


def test_sin_respuesta():
    def corta(url, headers=None, timeout=None):
        raise feeds.requests.ConnectionError("Connection aborted.")
    with tempfile.TemporaryDirectory() as d:
        with entorno(corta), contextlib.redirect_stdout(io.StringIO()):
            descarga_paginas.main(["--salida", d], feeds=[{"fuente": "agregadas", "url": CABECERA}])
        man = json.loads((Path(d) / feeds.MANIFIESTO).read_text(encoding="utf-8"))
    f = man["feeds"][CABECERA]["fallo"]
    check(f["tipo"] == "sin_respuesta" and f["codigo"] is None and "ConnectionError" in f["error"], f"{f}")
    print("  OK test_sin_respuesta")


def test_sha_que_no_cuadra_para():
    with tempfile.TemporaryDirectory() as d:
        _, _, man = guarda(d)
        segunda = Path(d) / man["feeds"][CABECERA]["paginas"][1]["fichero"]
        segunda.write_bytes(segunda.read_bytes().replace(b"<id>c</id>", b"<id>X</id>"))
        salida = io.StringIO()
        with entorno(Servidor({}), d), contextlib.redirect_stdout(salida):
            r = feeds.descarga_entradas(CABECERA)
    check(ids(r[0]) == ["a", "b"] and r[1] == 1, "para en la página alterada")
    check("SHA-256" in salida.getvalue(), "y dice por qué")
    print("  OK test_sha_que_no_cuadra_para")


def test_sin_manifiesto_lee_en_vivo():
    srv = Servidor(PAGINAS)
    with tempfile.TemporaryDirectory() as d:
        salida = io.StringIO()
        with entorno(srv, d), contextlib.redirect_stdout(salida):
            r = feeds.descarga_entradas(CABECERA)
    check(srv.pedidas == [CABECERA, P1, P2] and r[1] == 3, "sin manifiesto: en vivo, como antes")
    check("en vivo" in salida.getvalue(), "y lo avisa")
    print("  OK test_sin_manifiesto_lee_en_vivo")


def test_feed_que_no_esta_en_el_manifiesto():
    with tempfile.TemporaryDirectory() as d:
        guarda(d)
        srv = Servidor(PAGINAS)
        with entorno(srv, d), contextlib.redirect_stdout(io.StringIO()):
            r = feeds.descarga_entradas(BASE + "otro.atom")
    check(r == ([], 0, False) and srv.pedidas == [], "un feed que no se bajó: vacío, sin pedirlo")
    print("  OK test_feed_que_no_esta_en_el_manifiesto")


def test_feeds_a_guardar():
    fuentes = [f["fuente"] for f in descarga_paginas.FEEDS_A_GUARDAR]
    check(fuentes == ["estatal", "agregadas", "menores"], f"643, 1044 y 1143: {fuentes}")
    check(descarga_paginas.FEEDS_A_GUARDAR[2]["url"].endswith(
        "/sindicacion_1143/contratosMenoresPerfilesContratantes.atom"), "la de menores es la 1143")
    print("  OK test_feeds_a_guardar")


def test_agente_honesto():
    antes = os.environ.get("RADAR_CONTACTO")
    try:
        os.environ.pop("RADAR_CONTACTO", None)
        sin = feeds.agente_de_usuario()
        os.environ["RADAR_CONTACTO"] = "contacto@ejemplo.test"
        con = feeds.agente_de_usuario()
    finally:
        if antes is None:
            os.environ.pop("RADAR_CONTACTO", None)
        else:
            os.environ["RADAR_CONTACTO"] = antes
    for ua in (sin, con, feeds.CABECERAS["User-Agent"]):
        check(ua.startswith("RadarLicitaciones/") and "Mozilla" not in ua and "Chrome" not in ua,
              f"no se hace pasar por un navegador: {ua}")
    check("contacto" not in sin and "contacto: contacto@ejemplo.test" in con, "el contacto, solo si está")
    srv = Servidor(PAGINAS)
    with entorno(srv):
        feeds.descarga_entradas(CABECERA)
    check(set(srv.agentes) == {feeds.CABECERAS["User-Agent"]}, "cada petición lleva el agente honesto")
    print("  OK test_agente_honesto")


TESTS = [test_guardadas_igual_que_en_vivo, test_tope_se_guarda, test_rechazo_se_para_sin_reintentar,
         test_rechazo_a_media_cadena_guarda_lo_anterior, test_sin_respuesta, test_sha_que_no_cuadra_para,
         test_sin_manifiesto_lee_en_vivo, test_feed_que_no_esta_en_el_manifiesto, test_feeds_a_guardar,
         test_agente_honesto]


def main():
    print("Páginas guardadas (descarga_paginas.py + feeds.descarga_entradas):")
    for t in TESTS:
        t()
    print("\nTODO OK ✔")


if __name__ == "__main__":
    main()
