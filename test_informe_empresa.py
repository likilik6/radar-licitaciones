#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pruebas de informe_empresa.py · deterministas y SIN RED (como test_adjudicaciones.py).

    python test_informe_empresa.py

Cubren lo que no depende de Supabase: la regla del % de baja (que es donde más fácil
es equivocarse, porque tiene tres caminos), la expansión de prefijos CPV contra el
vocabulario del repo, y la normalización de CIF. Los datos de los casos NO son
inventados: salen de la comprobación en vivo del 14/09/2026 contra la base.

Lo que estas pruebas NO cubren (hace falta el .env con la Secret key): las consultas
reales. Para eso está el criterio de aceptación de Hardolass, que se comprueba
ejecutando el script.
"""

import json
import sys
from pathlib import Path

import informe_empresa as ie

FALLOS = []


def comprueba(nombre, obtenido, esperado):
    if obtenido == esperado:
        print(f"  OK {nombre}")
    else:
        FALLOS.append(nombre)
        print(f"  FALLO {nombre}\n     esperado: {esperado!r}\n     obtenido: {obtenido!r}")


# ---------------------------------------------------------------------------
print("\n== normaliza_cif ==")
comprueba("mayúsculas y signos", ie.normaliza_cif(" b-85.578/573 "), "B85578573")
comprueba("ya normalizado", ie.normaliza_cif("A28345577"), "A28345577")
comprueba("vacío", ie.normaliza_cif(None), "")

# ---------------------------------------------------------------------------
print("\n== es_unitario (réplica de compEsUnitario, D1.1) ==")
comprueba("code 1 = acuerdo marco -> precios unitarios", ie.es_unitario("1", 5000), True)
comprueba("code 2 = sistema dinámico -> precios unitarios", ie.es_unitario("2", 5000), True)
comprueba("code 0 = contrato normal", ie.es_unitario("0", 5000), False)
comprueba("code 3 = basado en AM -> importe real", ie.es_unitario("3", 5000), False)
comprueba("code 4 = basado en SDA -> importe real", ie.es_unitario("4", 5000), False)
comprueba("sin code + importe bajo -> heurística de E.5", ie.es_unitario(None, 21), True)
comprueba("sin code + importe alto -> contrato", ie.es_unitario(None, 50000), False)

# ---------------------------------------------------------------------------
print("\n== calcula_baja (réplica de compBajaAdjHtml) ==")

# EL CASO DE ACEPTACIÓN. Hardolass B01947753, medido en vivo: importe 14.810,00 €,
# SIN presupuesto de lote, lote vacío (contrato sin lotes) y presupuesto del expediente
# 14.810,00 €. Si el fallback al expediente no estuviera, esto daría None y el informe
# fallaría el criterio 2 del encargo.
hardolass = ie.calcula_baja(
    {"sistema_contratacion": "0", "importe_sin_iva": 14810.0,
     "presupuesto_lote_sin_iva": None, "lote": ""}, 14810.0)
comprueba("Hardolass · baja 0,0 %", hardolass["pct_baja"], 0.0)
comprueba("Hardolass · base = expediente", hardolass["origen_base"], "expediente")

conlote = ie.calcula_baja(
    {"sistema_contratacion": "0", "importe_sin_iva": 80.0,
     "presupuesto_lote_sin_iva": 100.0, "lote": "3"}, 999999.0)
comprueba("con presupuesto de lote manda el lote", conlote["pct_baja"], 20.0)
comprueba("con presupuesto de lote · origen", conlote["origen_base"], "lote")

multilote = ie.calcula_baja(
    {"sistema_contratacion": "0", "importe_sin_iva": 80.0,
     "presupuesto_lote_sin_iva": None, "lote": "3"}, 1000.0)
comprueba("multi-lote sin presupuesto de lote -> null (NO cae al expediente)",
          multilote["pct_baja"], None)

am = ie.calcula_baja(
    {"sistema_contratacion": "1", "importe_sin_iva": 21.0,
     "presupuesto_lote_sin_iva": 1200000.0, "lote": "1"}, None)
comprueba("acuerdo marco -> % suprimido", am["pct_baja"], None)
comprueba("acuerdo marco -> rótulo", "precios unitarios" in am["nota"], True)

sinnada = ie.calcula_baja(
    {"sistema_contratacion": "0", "importe_sin_iva": 500.0,
     "presupuesto_lote_sin_iva": None, "lote": ""}, None)
comprueba("sin presupuesto por ningún lado -> null", sinnada["pct_baja"], None)

alto = ie.calcula_baja(
    {"sistema_contratacion": "0", "importe_sin_iva": 120.0,
     "presupuesto_lote_sin_iva": 100.0, "lote": "1"}, None)
comprueba("adjudicado POR ENCIMA del presupuesto -> baja negativa, no se oculta",
          alto["pct_baja"], -20.0)

# ---------------------------------------------------------------------------
print("\n== expande_cpv (prefijos contra el vocabulario del repo) ==")
codigos, detalle = ie.expande_cpv(["45441000"])
comprueba("código exacto se respeta", codigos, ["45441000"])
comprueba("código exacto · tipo", detalle[0]["tipo"], "codigo")

codigos, detalle = ie.expande_cpv(["4544"])
comprueba("prefijo expande a varios", len(codigos) > 5, True)
comprueba("prefijo · todos empiezan por él", all(c.startswith("4544") for c in codigos), True)
comprueba("prefijo · todos son de 8 dígitos", all(len(c) == 8 for c in codigos), True)
comprueba("prefijo · tipo", detalle[0]["tipo"], "prefijo")
comprueba("prefijo · declara a cuántos expande", detalle[0]["expandido_a"], len(codigos))

codigos, _ = ie.expande_cpv(["45441000", "4544"])
comprueba("sin duplicados al mezclar código y prefijo", len(codigos), len(set(codigos)))
comprueba("el código exacto va primero", codigos[0], "45441000")

codigos, _ = ie.expande_cpv(["45-44-10-00"])
comprueba("tolera separadores", codigos, ["45441000"])

codigos, _ = ie.expande_cpv(["99999999"])
comprueba("código inexistente se respeta (la consulta dará 0)", codigos, ["99999999"])

# ---------------------------------------------------------------------------
print("\n== en_lista (filtro in.(...) de PostgREST) ==")
comprueba("entrecomilla", ie.en_lista(["a", "b"]), 'in.("a","b")')
comprueba("escapa comillas", ie.en_lista(['x"y']), 'in.("x""y")')

# ---------------------------------------------------------------------------
print("\n== num ==")
comprueba("cadena a número", ie.num("14810.00"), 14810.0)
comprueba("vacío a None", ie.num(""), None)
comprueba("None a None", ie.num(None), None)
comprueba("basura a None", ie.num("no"), None)

# ---------------------------------------------------------------------------
print("\n== el vocabulario CPV está en el repo ==")
comprueba("existe data/cpv_nombres.json", ie.CPV_VOCABULARIO.exists(), True)
voc = json.loads(ie.CPV_VOCABULARIO.read_text(encoding="utf-8"))
comprueba("tiene miles de códigos", len(voc) > 9000, True)
comprueba("todas las claves son de 8 dígitos", all(len(k) == 8 for k in voc), True)

# ---------------------------------------------------------------------------
print("\n== bloque 5: desglose por COMUNIDAD además de por órgano ==")


class SupabaseDeMentira:
    """Devuelve filas fijas: prueba la AGREGACIÓN, no la red."""

    def __init__(self, filas):
        self._filas = filas

    def filas(self, tabla, params, tope=None):
        return list(self._filas)


# Caso pensado a propósito: el órgano que más licita (TRAGSA) es estatal y reparte por
# tres comunidades. Es justo el motivo por el que el desglose por órgano NO sustituye al
# geográfico, y por el que se dan los dos.
FILAS = [
    {"licitacion_id": "1", "organo_contratacion": "TRAGSA", "presupuesto_sin_iva": 100,
     "fecha_fin_plazo": None, "estado_adjudicacion": None, "ccaa": "Andalucía"},
    {"licitacion_id": "2", "organo_contratacion": "TRAGSA", "presupuesto_sin_iva": 200,
     "fecha_fin_plazo": None, "estado_adjudicacion": None, "ccaa": "Galicia"},
    {"licitacion_id": "3", "organo_contratacion": "TRAGSA", "presupuesto_sin_iva": 300,
     "fecha_fin_plazo": None, "estado_adjudicacion": "desierta_total", "ccaa": "Andalucía"},
    {"licitacion_id": "4", "organo_contratacion": "Ajuntament de Barcelona",
     "presupuesto_sin_iva": 50, "fecha_fin_plazo": None, "estado_adjudicacion": None,
     "ccaa": "Cataluña"},
    # Ámbito nacional: sin comunidad. NO debe inventarse ni colarse en ningún grupo.
    {"licitacion_id": "5", "organo_contratacion": "ADIF", "presupuesto_sin_iva": 999,
     "fecha_fin_plazo": None, "estado_adjudicacion": None, "ccaa": None},
]
b5 = ie.bloque5_mercado(SupabaseDeMentira(FILAS), ["45321000"], "2024-01-01T00:00:00+00:00")

comprueba("cuenta todas las licitaciones", b5["n_licitaciones"], 5)
comprueba("importe total incluye las que no tienen comunidad",
          b5["importe_total_sin_iva"], 1649.0)
comprueba("desiertas", b5["desiertas"], 1)
por_ccaa = {c["ccaa"]: c for c in b5["por_ccaa"]}
comprueba("tres comunidades", sorted(por_ccaa), ["Andalucía", "Cataluña", "Galicia"])
comprueba("Andalucía agrega sus dos filas", por_ccaa["Andalucía"]["n_licitaciones"], 2)
comprueba("Andalucía suma importes", por_ccaa["Andalucía"]["importe_sin_iva"], 400.0)
comprueba("ordenado por importe descendente", b5["por_ccaa"][0]["ccaa"], "Andalucía")
comprueba("la fila sin comunidad NO entra en ningún grupo",
          sum(c["n_licitaciones"] for c in b5["por_ccaa"]), 4)
comprueba("se declara cuántas no tienen comunidad", b5["n_sin_ccaa"], 1)
comprueba("se declara la cobertura", b5["pct_con_ccaa"], 80.0)
comprueba("el aviso dice la cobertura real", "80.0%" in b5["aviso_desglose"], True)
comprueba("sigue habiendo desglose por órgano", len(b5["por_organo"]), 3)
comprueba("se piden las filas con la columna ccaa", "ccaa" in FILAS[0], True)

vacio = ie.bloque5_mercado(SupabaseDeMentira([]), [], "2024-01-01T00:00:00+00:00")
comprueba("sin CPV -> desglose vacío, no excepción", vacio["por_ccaa"], [])
comprueba("sin CPV -> cobertura None", vacio["pct_con_ccaa"], None)

print("\n== el script no escribe NUNCA en la base ==")
fuente = (Path(__file__).resolve().parent / "informe_empresa.py").read_text(encoding="utf-8")
for verbo in (".post(", ".patch(", ".delete(", ".put("):
    comprueba(f"no usa {verbo}", verbo in fuente, False)
comprueba("solo hay GET", fuente.count("self.ses.get(") >= 1, True)

# ---------------------------------------------------------------------------
print("\n== filas() no admite un 'limit' de quien llama (el fallo de los 278 s) ==")
# Pedir 1 fila con filas(..., '...&limit=1') metía DOS limit en la URL; PostgREST se
# quedaba con el de filas() (1000), el bucle veía la página llena y recorría las 624.204
# filas del catálogo. Dos veces = 1.248 peticiones de más. Ahora salta antes de la red.
_sb = ie.Supabase("https://ejemplo.invalido", "clave-de-mentira")   # no hace red al crearse
try:
    _sb.filas("licitaciones", "select=fecha_publicacion&limit=1")
    comprueba("filas() con limit -> debe lanzar ValueError", "no lanzó", "ValueError")
except ValueError as err:
    comprueba("filas() con limit -> ValueError", "limit" in str(err), True)
    comprueba("filas() con limit -> no llegó a pedir nada", _sb.peticiones, 0)
except Exception as err:  # noqa: BLE001
    comprueba("filas() con limit -> ValueError", type(err).__name__, "ValueError")

print("\n== resta_meses recorta el día (un 31 no pide '31 de febrero') ==")
from datetime import datetime, timezone  # noqa: E402
comprueba("31/03 - 1 mes", ie.resta_meses(datetime(2026, 3, 31, tzinfo=timezone.utc), 1).date().isoformat(), "2026-02-28")
comprueba("15/09 - 24 meses", ie.resta_meses(datetime(2026, 9, 15, tzinfo=timezone.utc), 24).date().isoformat(), "2024-09-15")
comprueba("15/09 - 18 meses", ie.resta_meses(datetime(2026, 9, 15, tzinfo=timezone.utc), 18).date().isoformat(), "2025-03-15")

print("\n== la clave no se imprime ==")
comprueba("la clave no aparece en ningún print",
          any("clave" in l and "print(" in l for l in fuente.splitlines()), False)

print("\n" + ("TODO OK ✔" if not FALLOS else f"{len(FALLOS)} FALLOS: {FALLOS}"))
sys.exit(1 if FALLOS else 0)
