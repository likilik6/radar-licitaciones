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

print("\n== variantes acentuadas (mejora 1: ilike NO ignora tildes) ==")
# Medido sobre los menores del nicho: «purificación» sale CON tilde en 19 de 20 casos y
# «desinfección» en 60 de 62. Un ilike con el término sin tilde perdería el 95% SIN AVISAR,
# que es la peor clase de fallo: el informe saldría, con un número menor y creíble.
v = ie.variantes_acentuadas("purificacion")
comprueba("incluye el término tal cual", "purificacion" in v, True)
comprueba("incluye la forma real, con tilde", "purificación" in v, True)
comprueba("una vocal acentuada por variante, sin explosión combinatoria", len(v), 7)
comprueba("fotocatalit -> fotocatalít (la que casa «fotocatalítico»)",
          "fotocatalít" in ie.variantes_acentuadas("fotocatalit"), True)
comprueba("formaldehido -> formaldehído",
          "formaldehído" in ie.variantes_acentuadas("formaldehido"), True)
comprueba("respeta los espacios de las frases",
          "calidad del aire" in ie.variantes_acentuadas("calidad del aire"), True)
comprueba("vacío -> []", ie.variantes_acentuadas(""), [])
comprueba("None -> []", ie.variantes_acentuadas(None), [])
comprueba("sin vocales -> solo él mismo", ie.variantes_acentuadas("xyz"), ["xyz"])

print("\n== trocea_terminos ==")
comprueba("separa por comas y limpia", ie.trocea_terminos(" a , b ,, c "), ["a", "b", "c"])
comprueba("vacío -> []", ie.trocea_terminos(""), [])

print("\n== criba de CPV por NIVEL (mejora 2) ==")
# Con LODEPA se colaron 50000000 y 51000000 —divisiones enteras del árbol CPV— y
# arrastraron 5.472 licitaciones por 3.408 M€ que son el mercado de mantenimiento
# integral de edificios, no el suyo. El criterio de FRECUENCIA no los cazaba: ninguno
# llegaba al 2% del catálogo. Son genéricos por lo que SIGNIFICAN, no por lo que aparecen.
comprueba("50000000 -> 7 ceros", ie.ceros_finales("50000000"), 7)
comprueba("51000000 -> 6 ceros", ie.ceros_finales("51000000"), 6)
comprueba("71700000 -> 5 ceros", ie.ceros_finales("71700000"), 5)
comprueba("50730000 -> 4 ceros", ie.ceros_finales("50730000"), 4)
comprueba("90731100 -> 2 ceros", ie.ceros_finales("90731100"), 2)
comprueba("el umbral deja fuera las divisiones",
          ie.ceros_finales("50000000") >= ie.CEROS_GENERICO, True)
comprueba("y NO toca las clases del nicho",
          ie.ceros_finales("90731100") >= ie.CEROS_GENERICO, False)
comprueba("71700000, con 5 ceros, se queda",
          ie.ceros_finales("71700000") >= ie.CEROS_GENERICO, False)


class SupabaseCriba:
    """Cuenta las consultas: sirve para probar que el criterio de nivel no gasta ninguna."""

    def __init__(self):
        self.consultas = 0

    def cuenta(self, tabla, params):
        self.consultas += 1
        return 10          # muy por debajo del umbral de frecuencia


sb_c = SupabaseCriba()
cr = ie.criba_cpv(sb_c, ["50000000", "51000000", "90731100", "71700000"], 624204)
comprueba("las divisiones quedan fuera",
          sorted(g["cpv"] for g in cr["descartados_genericos"]), ["50000000", "51000000"])
comprueba("el motivo se declara (nada de recortes silenciosos)",
          {g["motivo"] for g in cr["descartados_genericos"]}, {"nivel"})
comprueba("los específicos se usan", cr["usados"], ["90731100", "71700000"])
comprueba("el criterio de nivel NO gasta una consulta", sb_c.consultas, 2)

print("\n== bloque_competidores (mejora 3) ==")
comprueba("lista vacía -> []", ie.bloque_competidores(None, []), [])
comprueba("un CIF vacío se ignora sin romper", ie.bloque_competidores(None, ["", "  "]), [])

# ---------------------------------------------------------------------------
print("\n== fuentes de menores: lectura de data/menores_fuentes.json ==")
# F1 de menores autonómicos: la tabla deja de ser solo estatal. Los controles de siempre
# (LODEPA 6 menores / 56.208,00 €; Hardolass 2 / 78.580,00 € = 38.800 + 39.780, medidos
# en vivo) son de la parte ESTATAL, así que el desglose tiene que dejarlos a la vista.
import tempfile  # noqa: E402

real = ie.lee_fuentes_menores()
comprueba("el fichero del repo se lee", "estatal" in real, True)
comprueba("la estatal figura como cargada", real.get("estatal", {}).get("cargada"), True)
comprueba("las notas («_nota») no son fuentes", any(k.startswith("_") for k in real), False)

with tempfile.TemporaryDirectory() as tmp:
    roto = Path(tmp) / "roto.json"
    roto.write_text("{esto no es json", encoding="utf-8")
    lista = Path(tmp) / "lista.json"
    lista.write_text('["estatal"]', encoding="utf-8")
    comprueba("fichero ausente -> {} (el informe sale igual)",
              ie.lee_fuentes_menores(Path(tmp) / "no_existe.json"), {})
    comprueba("JSON roto -> {}", ie.lee_fuentes_menores(roto), {})
    comprueba("JSON que no es un objeto -> {}", ie.lee_fuentes_menores(lista), {})
    # La ruta por defecto se resuelve AL LLAMAR, no al importar: así se puede parchear.
    _original = ie.FUENTES_MENORES
    ie.FUENTES_MENORES = Path(tmp) / "no_existe.json"
    try:
        comprueba("parchear FUENTES_MENORES cambia lo que se lee", ie.lee_fuentes_menores(), {})
    finally:
        ie.FUENTES_MENORES = _original

ESTATAL = {"etiqueta": "Estatal",
           "nombre": "Plataforma de Contratación del Sector Público (sindicación 1143)",
           "cargada": True}
ANDALUCIA = {"etiqueta": "Andalucía",
             "nombre": "Junta de Andalucía (datos abiertos de contratación menor)",
             "cargada": True}
F_UNA = {"estatal": ESTATAL, "andalucia": {**ANDALUCIA, "cargada": False}}   # la de hoy
F_VARIAS = {"estatal": ESTATAL, "andalucia": ANDALUCIA}                      # tras F1

print("\n== desglose_por_fuente ==")
comprueba("etiqueta conocida", ie.etiqueta_fuente("andalucia", F_VARIAS), "Andalucía")
comprueba("fuente fuera del fichero -> su código tal cual",
          ie.etiqueta_fuente("galicia", F_VARIAS), "galicia")
comprueba("sin fichero -> su código tal cual", ie.etiqueta_fuente("estatal", {}), "estatal")
comprueba("sin fuente -> rótulo explícito, no vacío", ie.etiqueta_fuente(None, F_VARIAS),
          "(sin fuente)")

MEZCLA = [
    {"fuente": "andalucia", "importe_sin_iva": 1000},
    {"fuente": "estatal", "importe_sin_iva": 38800.0},
    {"fuente": "andalucia", "importe_sin_iva": None},      # sin importe: no suma, sí cuenta
    {"fuente": "larioja", "importe_sin_iva": 0},           # 0 € NO es «no se sabe»
    {"fuente": "estatal", "importe_sin_iva": "39780.00"},  # PostgREST puede dar cadena
    {"fuente": "galicia", "importe_sin_iva": None},        # ninguna trae importe -> null
    {"fuente": "andalucia", "importe_sin_iva": 250.5},
]
d = ie.desglose_por_fuente(MEZCLA, F_VARIAS)
comprueba("de más a menos contratos; empate por código (como el SQL)",
          [x["fuente"] for x in d], ["andalucia", "estatal", "galicia", "larioja"])
comprueba("la parte estatal queda a la vista (control Hardolass)", d[1],
          {"fuente": "estatal", "etiqueta": "Estatal", "n": 2, "importe_total_sin_iva": 78580.0})
comprueba("una fila sin importe cuenta pero no suma", (d[0]["n"], d[0]["importe_total_sin_iva"]),
          (3, 1250.5))
comprueba("ninguna fila con importe -> null", d[2]["importe_total_sin_iva"], None)
comprueba("importe 0 -> 0.0, no null", d[3]["importe_total_sin_iva"], 0.0)
comprueba("fuente sin etiqueta -> código", d[2]["etiqueta"], "galicia")
comprueba("la suma del desglose cuadra con las filas", sum(x["n"] for x in d), len(MEZCLA))
comprueba("sin filas -> []", ie.desglose_por_fuente([], F_VARIAS), [])

print("\n== textos de fuentes: una y varias cargadas ==")
aviso_una = ie.aviso_fuente_menores(F_UNA)
comprueba("una cargada · nombra la estatal",
          "«Estatal»: Plataforma de Contratación del Sector Público (sindicación 1143)" in aviso_una,
          True)
comprueba("una cargada · NO nombra la declarada sin cargar", "Andalucía" in aviso_una, False)
comprueba("una cargada · mantiene que no se suman", "NO se suman con los del bloque 2" in aviso_una,
          True)
aviso_varias = ie.aviso_fuente_menores(F_VARIAS)
comprueba("varias cargadas · nombra las dos, en el orden del fichero",
          aviso_varias.index("«Estatal»") < aviso_varias.index("«Andalucía»: Junta de Andalucía"),
          True)
comprueba("varias cargadas · mantiene que no se suman",
          "NO se suman con los del bloque 2" in aviso_varias, True)
aviso_sin = ie.aviso_fuente_menores({})
comprueba("sin fichero · lo dice", "No se pudo leer data/menores_fuentes.json" in aviso_sin, True)
comprueba("sin fichero · mantiene que no se suman", "NO se suman" in aviso_sin, True)
comprueba("fichero sin ninguna cargada · no dice que no se pudo leer",
          ie.aviso_fuente_menores({"estatal": {**ESTATAL, "cargada": False}})
          .startswith("Ninguna fuente figura como cargada"), True)
comprueba("cargada tiene que ser true de verdad, no una cadena",
          ie.nombra_cargadas({"estatal": {**ESTATAL, "cargada": "false"}}), None)
t8_una, t8_varias = ie.texto_fuentes_menores(F_UNA), ie.texto_fuentes_menores(F_VARIAS)
comprueba("bloque 8 · una cargada", ("sindicación 1143)" in t8_una, "Andalucía" in t8_una),
          (True, False))
comprueba("bloque 8 · varias cargadas", "«Andalucía»: Junta de Andalucía" in t8_varias, True)
comprueba("bloque 8 · sigue avisando de las agregadas",
          "NO incluye las plataformas agregadas" in t8_varias, True)
comprueba("bloque 8 · sin fichero no revienta",
          ie.texto_fuentes_menores({}).startswith("Fuentes sin nombrar."), True)


class SupabaseMenores:
    """Devuelve filas fijas y APUNTA lo que se pidió, para ver que el select trae fuente."""

    def __init__(self, filas, conteo=0):
        self._filas = filas
        self._conteo = conteo
        self.params = []
        self.peticiones, self.por_tabla, self.segundos, self.mas_lenta = 0, {}, 0.0, ("", 0.0)

    def filas(self, tabla, params, tope=None):
        self.params.append(params)
        return list(self._filas)

    def cuenta(self, tabla, params):
        return self._conteo

    def una(self, tabla, params):
        return {"fecha_publicacion": "2024-09-15T00:00:00+00:00"}


print("\n== bloque 3: menores de la empresa con desglose por fuente ==")
HARDOLASS = [
    {"licitacion_id": "m1", "objeto": "Limpieza A", "organo_contratacion": "Órgano 1",
     "cif_adjudicatario": "B01947753", "n_adjudicatarios": 1, "importe_sin_iva": 38800.0,
     "fecha_adjudicacion": "2025-06-01", "cpv": ["90911200"], "fuente": "estatal"},
    {"licitacion_id": "m2", "objeto": "Limpieza B", "organo_contratacion": "Órgano 2",
     "cif_adjudicatario": "B01947753", "n_adjudicatarios": 1, "importe_sin_iva": 39780.0,
     "fecha_adjudicacion": "2024-03-01", "cpv": [], "fuente": "estatal"},
]
sb_h = SupabaseMenores(HARDOLASS)
b3 = ie.bloque3_menores_cif(sb_h, "B01947753", F_UNA)
comprueba("el select pide la columna fuente", ",fuente" in sb_h.params[0].split("&")[0], True)
comprueba("Hardolass · total de siempre", (b3["n"], b3["importe_total_sin_iva"]), (2, 78580.0))
comprueba("Hardolass · por_fuente = solo estatal", b3["por_fuente"],
          [{"fuente": "estatal", "etiqueta": "Estatal", "n": 2, "importe_total_sin_iva": 78580.0}])
comprueba("se conservan TODAS las claves de antes",
          {"filas", "n", "importe_total_sin_iva", "ultimo", "n_sin_fecha", "n_compartidos",
           "importe_compartido", "aviso_fuente", "aviso_repartidos"} <= set(b3), True)

ANDALUZAS = [
    {"licitacion_id": "and:766139", "objeto": "Limpieza C", "organo_contratacion": "SAS",
     "cif_adjudicatario": "B01947753", "n_adjudicatarios": 1, "importe_sin_iva": 1200.0,
     "fecha_adjudicacion": "2025-07-01", "cpv": [], "fuente": "andalucia"},
    {"licitacion_id": "and:766140", "objeto": "Limpieza D", "organo_contratacion": "SAS",
     "cif_adjudicatario": "B01947753", "n_adjudicatarios": 1, "importe_sin_iva": None,
     "fecha_adjudicacion": "2025-08-01", "cpv": [], "fuente": "andalucia"},
    {"licitacion_id": "gal:1", "objeto": "Limpieza E", "organo_contratacion": "Xunta",
     "cif_adjudicatario": "B01947753", "n_adjudicatarios": 1, "importe_sin_iva": None,
     "fecha_adjudicacion": "2025-09-01", "cpv": [], "fuente": "galicia"},
]
b3v = ie.bloque3_menores_cif(SupabaseMenores(HARDOLASS + ANDALUZAS), "B01947753", F_VARIAS)
comprueba("varias fuentes · el total suma todas", (b3v["n"], b3v["importe_total_sin_iva"]),
          (5, 79780.0))
comprueba("varias fuentes · el control estatal sigue leyéndose en su línea",
          [x for x in b3v["por_fuente"] if x["fuente"] == "estatal"][0]["importe_total_sin_iva"],
          78580.0)
comprueba("varias fuentes · orden y etiquetas",
          [(x["etiqueta"], x["n"], x["importe_total_sin_iva"]) for x in b3v["por_fuente"]],
          [("Andalucía", 2, 1200.0), ("Estatal", 2, 78580.0), ("galicia", 1, None)])

# Sin pasar `fuentes`, el bloque lee el fichero él solo: se parchea la ruta.
_original = ie.FUENTES_MENORES
ie.FUENTES_MENORES = Path(tempfile.gettempdir()) / "menores_fuentes_que_no_existe.json"
try:
    b3s = ie.bloque3_menores_cif(SupabaseMenores(HARDOLASS), "B01947753")
finally:
    ie.FUENTES_MENORES = _original
comprueba("JSON ausente · no revienta y usa el código", b3s["por_fuente"][0]["etiqueta"], "estatal")
comprueba("JSON ausente · el aviso lo dice", "No se pudo leer" in b3s["aviso_fuente"], True)
comprueba("JSON ausente · las cifras no cambian", b3s["importe_total_sin_iva"], 78580.0)

print("\n== bloque 3b: desglose del nicho, tope y aviso del CPV ==")
sb_n = SupabaseMenores(HARDOLASS + ANDALUZAS, conteo=5)
b3b = ie.bloque3b_menores_nicho(sb_n, ["90911200"], fuentes=F_VARIAS)
comprueba("3b · el select pide la columna fuente", "cpv,enlace,fuente&" in sb_n.params[0], True)
comprueba("3b · por_fuente sobre las filas traídas", [x["n"] for x in b3b["por_fuente"]], [2, 2, 1])
comprueba("3b · sin tope, la nota lo dice", "todas, sin tope" in b3b["por_fuente_nota"], True)
comprueba("3b · aviso del CPV en el JSON",
          ("CÓDIGO CPV" in b3b["aviso_cpv"], "Andalucía" in b3b["aviso_cpv"]), (True, True))
_tope = ie.TOPE_MENORES_NICHO
ie.TOPE_MENORES_NICHO = 5
try:
    b3b_t = ie.bloque3b_menores_nicho(SupabaseMenores(HARDOLASS + ANDALUZAS, 99), ["90911200"],
                                      fuentes=F_VARIAS)
finally:
    ie.TOPE_MENORES_NICHO = _tope
comprueba("3b · topado", b3b_t["topado"], True)
comprueba("3b · topado, la nota dice que es una muestra",
          ("TOPADAS" in b3b_t["por_fuente_nota"], "no el del universo" in b3b_t["por_fuente_nota"]),
          (True, True))
MIL = [{"fuente": "estatal", "importe_sin_iva": 1, "organo_contratacion": "X"}] * ie.TOPE_MENORES_NICHO
b3b_mil = ie.bloque3b_menores_nicho(SupabaseMenores(MIL, 22854), ["90911200"], fuentes=F_VARIAS)
comprueba("3b · topado con el tope real, miles con punto (y la coma del texto intacta)",
          b3b_mil["por_fuente_nota"].split(" (")[0],
          "Desglose sobre las 3.000 filas traídas, TOPADAS en 3.000")
b3b_v = ie.bloque3b_menores_nicho(None, [], fuentes=F_VARIAS)
comprueba("3b sin CPV · desglose vacío y aviso igual",
          (b3b_v["por_fuente"], b3b_v["aviso_cpv"] == ie.aviso_cpv_nicho(F_VARIAS)), ([], True))

print("\n== bloque 8: la nota de fuentes nombra las cargadas (fichero parcheado) ==")
with tempfile.TemporaryDirectory() as tmp:
    parcheado = Path(tmp) / "menores_fuentes.json"
    parcheado.write_text(json.dumps({"_nota": "x", **F_VARIAS}, ensure_ascii=False),
                         encoding="utf-8")
    _original = ie.FUENTES_MENORES
    ie.FUENTES_MENORES = parcheado
    try:
        b8 = ie.bloque8_metadatos(SupabaseMenores([]), "2026-09-17T00:00:00+00:00", 24,
                                  {}, [], {}, 624204)
    finally:
        ie.FUENTES_MENORES = _original
comprueba("bloque 8 · menores nombra Estatal y Andalucía",
          ("«Estatal»" in b8["fuentes"]["menores"], "«Andalucía»" in b8["fuentes"]["menores"]),
          (True, True))
comprueba("bloque 8 · el resto de la nota no cambia", b8["fuentes"]["aviso"],
          "Los importes de menores y de adjudicaciones NO se suman entre sí.")


print("\n== escribe_md: columna Fuente, línea de desglose y aviso del CPV ==")


def informe_minimo(menores_empresa, menores_nicho):
    """Lo justo para que escribe_md pinte; lo que se prueba son las secciones 3 y 3b."""
    return {
        "cif": "B01947753", "identidad": None, "adjudicaciones": [],
        "menores_empresa": menores_empresa, "menores_nicho": menores_nicho,
        "cpv": {"cpv_deducidos": []}, "quien_gana": [], "oportunidades": [], "competidores": [],
        "mercado": {"n_licitaciones": 0, "importe_total_sin_iva": None, "abiertas_hoy": 0,
                    "desiertas": 0, "por_organo": [], "por_ccaa": [], "n_sin_ccaa": 0},
        "metadatos": {"consultado_en": "2026-09-17T00:00:00+00:00", "ventana_informe_meses": 24,
                      "catalogo_total_filas": 624204,
                      "catalogo_ventana": {"desde": None, "hasta": None},
                      "catalogo_por_anno": [], "peticiones_http": 0,
                      "segundos_en_consultas": 0, "filas_por_bloque": {},
                      "cpv_criba": {}, "cpv_expansion": []},
    }


def seccion(md, desde, hasta):
    return md[md.index(desde):md.index(hasta)]


md_una = ie.escribe_md(informe_minimo(b3, b3b_v))
s3 = seccion(md_una, "## 3 · Contratos menores", "### 3b")
comprueba("MD una · cabecera con columna Fuente",
          "| Fecha | Objeto | Órgano | Fuente | Importe s/IVA |" in s3, True)
comprueba("MD una · filas con la etiqueta", "| Estatal | 38.800,00 € |" in s3, True)
comprueba("MD una · solo estatal -> sin línea de desglose (el total YA es la parte estatal)",
          "**Por fuente:**" in s3, False)
comprueba("MD una · total de siempre", "**Total menores:** 78.580,00 € · **nº:** 2" in s3, True)

md_varias = ie.escribe_md(informe_minimo(b3v, b3b))
s3v = seccion(md_varias, "## 3 · Contratos menores", "### 3b")
comprueba("MD varias · línea de desglose con la parte estatal a la vista",
          "**Por fuente:** Andalucía: 2 menores, 1.200,00 € · Estatal: 2 menores, 78.580,00 € "
          "· galicia: 1 menor, —" in s3v, True)
comprueba("MD varias · fila de una fuente sin etiqueta sale con su código",
          "| galicia | — |" in s3v, True)
comprueba("MD varias · el aviso nombra las dos cargadas",
          ("«Estatal»" in s3v, "«Andalucía»" in s3v), (True, True))

solo_and = ie.bloque3_menores_cif(SupabaseMenores(ANDALUZAS[:2]), "B01947753", F_VARIAS)
s3a = seccion(ie.escribe_md(informe_minimo(solo_and, b3b_v)), "## 3 · Contratos menores", "### 3b")
comprueba("MD una sola fuente NO estatal -> sí hay línea de desglose",
          "**Por fuente:** Andalucía: 2 menores, 1.200,00 €" in s3a, True)

s3b = seccion(md_varias, "### 3b", "## 4 ·")
comprueba("MD 3b · aviso del CPV", f"> ⚠️ {b3b['aviso_cpv']}" in s3b, True)
comprueba("MD 3b · línea de desglose", "**Por fuente:** Andalucía: 2 menores" in s3b, True)
comprueba("MD 3b · nota de sobre qué filas es", "_Desglose sobre las 5 filas del nicho" in s3b,
          True)
s3b_v = seccion(md_una, "### 3b", "## 4 ·")
comprueba("MD 3b sin filas · el aviso del CPV sale igual y no hay desglose",
          (b3b_v["aviso_cpv"] in s3b_v, "**Por fuente:**" in s3b_v), (True, False))
comprueba("MD · miles con punto en el desglose",
          ie.linea_por_fuente([{"etiqueta": "Estatal", "n": 22854, "importe_total_sin_iva": 1.5}]),
          "Estatal: 22.854 menores, 1,50 €")

print("\n== revisión: el hueco entre cargar una fuente y marcarla como cargada ==")
# menores_autonomicos.py acaba la carga con «RECUERDA: 'andalucia' sigue con
# cargada=false»: durante un rato hay filas andaluzas y el fichero dice que no. El aviso
# no puede nombrar solo la estatal encima de una tabla con filas de Andalucía.
b3_tr = ie.bloque3_menores_cif(SupabaseMenores(HARDOLASS + ANDALUZAS[:2]), "B01947753", F_UNA)
av_tr = b3_tr["aviso_fuente"]
comprueba("hueco · el aviso dice que hay filas de una fuente sin marcar",
          "Hay filas de «Andalucía», que aún NO figura como cargada" in av_tr, True)
comprueba("hueco · lo dice ANTES de «que no figure aquí» (lo de la tabla figura en el aviso)",
          av_tr.index("«Andalucía»") < av_tr.index("que no figure aquí"), True)
comprueba("hueco · sigue nombrando la estatal y avisando de que no se suman",
          ("«Estatal»: Plataforma" in av_tr, "NO se suman" in av_tr), (True, True))
s3_tr = seccion(ie.escribe_md(informe_minimo(b3_tr, b3b_v)), "## 3 · Contratos menores", "### 3b")
comprueba("hueco · el MD lo lleva en el aviso de la sección 3",
          "> ⚠️ Fuentes de menores cargadas: «Estatal»" in s3_tr
          and "Hay filas de «Andalucía»" in s3_tr, True)
comprueba("hueco · plural con dos fuentes sin marcar (una ni siquiera está en el fichero)",
          "Hay filas de «Andalucía», «galicia», que aún NO figuran como cargadas" in
          ie.aviso_fuente_menores(F_UNA, ["andalucia", "estatal", "galicia", "andalucia"]), True)
comprueba("hueco · con Andalucía ya marcada no hay frase",
          "NO figura" in ie.aviso_fuente_menores(F_VARIAS, ["estatal", "andalucia"]), False)
comprueba("hueco · solo estatal (hoy) no hay frase",
          "NO figura" in ie.aviso_fuente_menores(F_UNA, ["estatal", "estatal"]), False)
comprueba("hueco · sin fichero no se sabe qué está cargado: no se afirma nada",
          "NO figura" in ie.aviso_fuente_menores({}, ["andalucia"]), False)
av_ninguna = ie.aviso_fuente_menores({"estatal": {**ESTATAL, "cargada": False}}, ["estatal"])
comprueba("hueco · fichero sin cargadas y filas estatales: lo dice",
          (av_ninguna.startswith("Ninguna fuente figura como cargada"),
           "Hay filas de «Estatal», que aún NO figura" in av_ninguna), (True, True))
comprueba("bloque 8 · la nota de fuentes dice lo mismo en el hueco",
          "Hay filas de «Andalucía»" in ie.texto_fuentes_menores(F_UNA, ["estatal", "andalucia"]),
          True)
comprueba("bloque 8 · sin filas de fuentes sin marcar no hay frase",
          "NO figura" in ie.texto_fuentes_menores(F_UNA, ["estatal"]), False)
b8_tr = ie.bloque8_metadatos(SupabaseMenores([]), "2026-09-17T00:00:00+00:00", 24, {}, [], {},
                             624204, F_UNA, ["estatal", "andalucia"])
comprueba("bloque 8 · recibe las fuentes presentes y las lleva a metadatos",
          "Hay filas de «Andalucía»" in b8_tr["fuentes"]["menores"], True)

print("\n== revisión: el aviso del CPV depende de si Andalucía está cargada ==")
# Hoy Andalucía tiene 0 filas: decir que «sus menores pueden NO aparecer aquí por el CPV»
# haría creer que están en la base y el CPV los esconde.
cpv_hoy = ie.aviso_cpv_nicho(F_UNA)
comprueba("sin cargar · dice que aún no está cargada",
          ("Andalucía aún no está cargada" in cpv_hoy, "pueden NO aparecer aquí" in cpv_hoy),
          (True, False))
cpv_f1 = ie.aviso_cpv_nicho(F_VARIAS)
comprueba("cargada · avisa de que sus menores pueden NO aparecer",
          ("pueden NO aparecer aquí aunque sean del nicho" in cpv_f1, "aún no está" in cpv_f1),
          (True, False))
comprueba("en el hueco · filas andaluzas en el nicho = se trata como cargada",
          ie.aviso_cpv_nicho(F_UNA, ["estatal", "andalucia"]), cpv_f1)
comprueba("sin fichero · no afirma ni una cosa ni la otra",
          "no se sabe si Andalucía está cargada" in ie.aviso_cpv_nicho({}), True)
for nombre_caso, texto in (("sin cargar", cpv_hoy), ("cargada", cpv_f1),
                           ("sin fichero", ie.aviso_cpv_nicho({}))):
    comprueba(f"{nombre_caso} · siempre: filtro por código y estatales sin CPV",
              ("CÓDIGO CPV" in texto, "el 47,8% no trae CPV" in texto,
               texto.endswith("Que no aparezcan no quiere decir que no existan.")),
              (True, True, True))
b3b_hoy = ie.bloque3b_menores_nicho(SupabaseMenores(HARDOLASS, 2), ["90911200"], fuentes=F_UNA)
comprueba("3b solo estatal y Andalucía sin cargar · JSON con el aviso de hoy",
          b3b_hoy["aviso_cpv"], cpv_hoy)
comprueba("3b sin CPV y Andalucía sin cargar · también",
          ie.bloque3b_menores_nicho(None, [], fuentes=F_UNA)["aviso_cpv"], cpv_hoy)
comprueba("3b con filas andaluzas en el hueco · JSON con el aviso de cargada",
          ie.bloque3b_menores_nicho(SupabaseMenores(HARDOLASS + ANDALUZAS, 5), ["90911200"],
                                    fuentes=F_UNA)["aviso_cpv"], cpv_f1)
comprueba("MD 3b · pinta el aviso del JSON, no uno fijo",
          f"> ⚠️ {cpv_hoy}" in seccion(ie.escribe_md(informe_minimo(b3, b3b_hoy)), "### 3b",
                                       "## 4 ·"), True)

print("\n== revisión: sin fichero de fuentes no se manda a un desglose que no está ==")
s3_sin = seccion(ie.escribe_md(informe_minimo(b3s, b3b_v)), "## 3 · Contratos menores", "### 3b")
comprueba("sin fichero, solo estatal · no hay línea «Por fuente»", "**Por fuente:**" in s3_sin,
          False)
comprueba("sin fichero, solo estatal · el aviso no remite al desglose",
          "desglose" in b3s["aviso_fuente"], False)
comprueba("sin fichero · el aviso explica lo que sí se ve (el código en la columna)",
          ("código de su fuente" in b3s["aviso_fuente"], "| estatal | 38.800,00 € |" in s3_sin),
          (True, True))

print("\n== revisión: textos del 3b (miles y singular) y nombre ausente ==")
FILA_NICHO = {"fuente": "estatal", "importe_sin_iva": 10.0, "organo_contratacion": "O"}
b3b_criba = ie.bloque3b_menores_nicho(SupabaseMenores([FILA_NICHO] * 1079, 22854), ["90911200"],
                                      ["limpieza"], [], fuentes=F_UNA)
s3b_criba = seccion(ie.escribe_md(informe_minimo(b3, b3b_criba)), "### 3b", "## 4 ·")
comprueba("criba · miles con punto, como la línea «Por fuente»",
          ("De 22.854 menores que casan por código CPV, 22.854 contienen" in s3b_criba,
           "y 1.079 quedan tras descartar" in s3b_criba, "22,854" in s3b_criba,
           "**Por fuente:** Estatal: 1.079 menores" in s3b_criba),
          (True, True, False, True))
comprueba("una sola fila · singular",
          ie.bloque3b_menores_nicho(SupabaseMenores([FILA_NICHO], 1), ["90911200"],
                                    fuentes=F_UNA)["por_fuente_nota"],
          "Desglose sobre la única fila del nicho.")
comprueba("dos filas · plural de siempre",
          ie.bloque3b_menores_nicho(SupabaseMenores([FILA_NICHO] * 2, 2), ["90911200"],
                                    fuentes=F_UNA)["por_fuente_nota"],
          "Desglose sobre las 2 filas del nicho: todas, sin tope.")
comprueba("sin nombre · la etiqueta, como generar_web._fuentes_menores",
          ie.nombra_cargadas({"estatal": {"etiqueta": "Estatal", "cargada": True}}),
          "«Estatal»: Estatal")
comprueba("sin nombre ni etiqueta · el código",
          ie.nombra_cargadas({"estatal": {"cargada": True}}), "«estatal»: estatal")

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
