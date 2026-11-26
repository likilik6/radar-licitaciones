# -*- coding: utf-8 -*-
"""Tests de ya_lanzado_hoy.py (26/11/2026): la programada no repite lo que ya lanzó el PC a las 5:00.

Sin red. Se ejecuta con:  python test_ya_lanzado_hoy.py
"""
import contextlib
import io
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

import ya_lanzado_hoy as y

sys.stdout.reconfigure(encoding="utf-8")
HOY = date(2026, 11, 27)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def run(titulo=y.TITULO, creado="2026-11-27T04:00:05Z", status="completed", conclusion="success"):
    return {"display_title": titulo, "created_at": creado, "status": status, "conclusion": conclusion,
            "html_url": "https://github.com/x/y/actions/runs/1"}


def test_la_del_pc_acabo_bien():
    saltar, motivo = y.decide([run()], HOY)
    check(saltar and "acabó bien" in motivo, motivo)
    print("  OK test_la_del_pc_acabo_bien")


def test_la_del_pc_sigue_en_marcha():
    check(y.decide([run(status="in_progress", conclusion=None)], HOY)[0], "en marcha: salta")
    check(y.decide([run(status="queued", conclusion=None)], HOY)[0], "en cola: salta")
    print("  OK test_la_del_pc_sigue_en_marcha")


def test_la_del_pc_fallo_o_se_cancelo():
    for c in ("failure", "cancelled", "timed_out"):
        check(not y.decide([run(conclusion=c)], HOY)[0], f"{c}: corre la programada")
    # una fallida y después otra bien: salta
    check(y.decide([run(conclusion="success"), run(conclusion="failure")], HOY)[0], "la buena cuenta")
    print("  OK test_la_del_pc_fallo_o_se_cancelo")


def test_otro_dia_u_otro_titulo():
    check(not y.decide([run(creado="2026-11-26T04:00:05Z")], HOY)[0], "la de ayer no cuenta")
    check(not y.decide([run(titulo="Radar de licitaciones")], HOY)[0], "una a mano sin «diaria» no cuenta")
    check(not y.decide([], HOY)[0], "sin ejecuciones: corre")
    print("  OK test_otro_dia_u_otro_titulo")


def test_dia_en_hora_de_espana():
    # 23:30 UTC del 26/11 = 00:30 del 27/11 en España (invierno): ya es «hoy»
    check(y.decide([run(creado="2026-11-26T23:30:00Z")], HOY)[0], "el día se cuenta en hora de España")
    check(not y.decide([run(creado="2026-11-26T22:59:00Z")], HOY)[0], "23:59 del 26 en España: ayer")
    check(y.dia_madrid("2026-07-01T22:30:00Z") == date(2026, 7, 2), "en verano, UTC+2")
    print("  OK test_dia_en_hora_de_espana")


def test_main_escribe_la_salida():
    antes = {k: os.environ.get(k) for k in ("GITHUB_EVENT_NAME", "GITHUB_OUTPUT", "GITHUB_REPOSITORY", "GH_TOKEN")}
    abrir = y.urllib.request.urlopen
    try:
        with tempfile.TemporaryDirectory() as d:
            salida = Path(d) / "out"
            os.environ.update(GITHUB_OUTPUT=str(salida), GITHUB_REPOSITORY="x/y", GH_TOKEN="t")
            # no programada: nunca salta y no consulta nada
            os.environ["GITHUB_EVENT_NAME"] = "workflow_dispatch"
            y.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no debe consultar"))
            with contextlib.redirect_stdout(io.StringIO()):
                y.main()
            check(salida.read_text(encoding="utf-8") == "saltar=false\n", "a mano: saltar=false")
            # programada sin API: corre
            os.environ["GITHUB_EVENT_NAME"] = "schedule"

            def sin_red(*a, **k):
                raise OSError("sin red")
            y.urllib.request.urlopen = sin_red
            log = io.StringIO()
            with contextlib.redirect_stdout(log):
                y.main()
            check(salida.read_text(encoding="utf-8").splitlines()[-1] == "saltar=false", "sin API: corre")
            check("no pude consultar" in log.getvalue(), "y lo dice")
    finally:
        y.urllib.request.urlopen = abrir
        for k, v in antes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("  OK test_main_escribe_la_salida")


def test_el_titulo_es_el_del_workflow():
    yml = (Path(__file__).resolve().parent / ".github" / "workflows" / "radar.yml").read_text(encoding="utf-8")
    check(f"'{y.TITULO}'" in yml, "radar.yml pone el mismo título que busca ya_lanzado_hoy.py")
    print("  OK test_el_titulo_es_el_del_workflow")


TESTS = [test_la_del_pc_acabo_bien, test_la_del_pc_sigue_en_marcha, test_la_del_pc_fallo_o_se_cancelo,
         test_otro_dia_u_otro_titulo, test_dia_en_hora_de_espana, test_main_escribe_la_salida,
         test_el_titulo_es_el_del_workflow]


def main():
    print("¿Ya lo lanzó hoy el PC? (ya_lanzado_hoy.py):")
    for t in TESTS:
        t()
    print("\nTODO OK ✔")


if __name__ == "__main__":
    main()
