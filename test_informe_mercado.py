#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pruebas de informe_mercado.py · deterministas y SIN RED (como test_informe_empresa.py).

    python test_informe_mercado.py

Cubren lo que no depende de Supabase: la normalización de términos y su rechazo, la construcción
de la tsquery (prefiltro) y de las dos regex (servidor ARE y cliente Python) y que las dos digan lo
mismo, la fusión CPV ∪ texto con la etiqueta `criterio`, la regla de --excluye, el invariante de
filas_por_criterio, la regla de baja heredada, la separación de precios unitarios y el conteo por
lote distinto, los tramos mensuales y el nombre de fichero. Los casos de datos salen de la
comprobación en vivo del 12/10/2026 (Alcorcón, Guadarrama, Laredo).

Lo que NO cubren (hace falta el .env): las consultas reales. Para eso está el criterio de
aceptación de camas hospitalarias, que se comprueba ejecutando el script.
"""

import re
import sys
from datetime import datetime, timezone

import informe_mercado as im

FALLOS = []


def comprueba(nombre, obtenido, esperado):
    if obtenido == esperado:
        print(f"  OK {nombre}")
    else:
        FALLOS.append(nombre)
        print(f"  FALLO {nombre}\n     esperado: {esperado!r}\n     obtenido: {obtenido!r}")


# ---------------------------------------------------------------------------
print("\n== normalización de términos ==")
comprueba("minúsculas y tildes fuera", im.normaliza_termino(" Hospitalización "), "hospitalizacion")
comprueba("ñ se conserva", im.normaliza_termino("Baño"), "baño")
comprueba("guion como espacio", im.normaliza_termino("anti-escaras"), "anti escaras")
comprueba("válido", im.termino_valido("camas de hospital"), True)
comprueba("inválido: puntos", im.termino_valido("c.p.a.p"), False)
comprueba("inválido: paréntesis", im.termino_valido("uci (adultos)"), False)
comprueba("inválido: vacío", im.termino_valido(""), False)
validos, rechazados = im.prepara_terminos("cama, camas, c.p.a.p")
comprueba("prepara_terminos separa válidos", [t["base"] for t in validos], ["cama", "camas"])
comprueba("prepara_terminos separa rechazados", rechazados, ["c.p.a.p"])

print("\n== patrón por palabra (clases de caracteres por vocal) ==")
comprueba("camas", im.patron_palabra("camas"), "c[aá]m[aá]s")
comprueba("u admite ú y ü", im.patron_palabra("pinguino"), "p[ií]ng[uúü][ií]n[oó]")
comprueba("ñ también como n", im.patron_palabra("baño"), "b[aá][ñn][oó]")
comprueba("dígitos tal cual", im.patron_palabra("uci2"), "[uúü]c[ií]2")

print("\n== tsquery (prefiltro) ==")
terms, _ = im.prepara_terminos("cama,camas")
comprueba("palabras con peso", im.construye_tsquery(terms, "AB"), "cama:AB | camas:AB")
frase, _ = im.prepara_terminos("camas de hospitalización")
comprueba("frase con <-> y unaccent (stopword dentro se deja)",
          im.construye_tsquery(frase, "A"), "camas:A <-> de:A <-> hospitalizacion:A")
enie, _ = im.prepara_terminos("baño")
comprueba("ñ -> n en la tsquery", im.construye_tsquery(enie, "AB"), "bano:AB")

print("\n== regex servidor (ARE) ==")
patron = im.construye_regex_servidor(terms)
comprueba("sin paréntesis, comas, comillas ni espacios", any(c in patron for c in "(),\" "), False)
comprueba("alternancia de nivel superior", patron, "\\mc[aá]m[aá]\\M|\\mc[aá]m[aá]s\\M")
pf = im.construye_regex_servidor(frase)
comprueba("frase con separador [\\s-]+ y clases", pf, "\\mc[aá]m[aá]s[\\s-]+d[eé][\\s-]+h[oó]sp[ií]t[aá]l[ií]z[aá]c[ií][oó]n\\M")
larga, _ = im.prepara_terminos("resonancia magnetica nuclear de alto campo")
comprueba("una frase larga sigue siendo corta (sin producto cartesiano)", len(im.construye_regex_servidor(larga)) < 150, True)

print("\n== regex cliente (Python) coincide con la de servidor ==")
rx = im.construye_regex_cliente(terms)
casos = [("Suministro de Camas hospitalarias", True), ("cámaras de seguridad", False),
         ("escamas de pescado", False), ("CAMAS ELÉCTRICAS", True), ("soportes/camas (pórticos)", True),
         ("camas de hospital", True), ("recamas", False), ("cama", True), ("camastro", False),
         ("Ayuntamiento de Camas", True), ("camá", True)]
for texto, esperado in casos:
    comprueba(f"cliente «{texto}»", bool(rx.search(texto)), esperado)
rxf = im.construye_regex_cliente(frase)
comprueba("frase cliente casa con guion", bool(rxf.search("Camas-de-hospitalización nuevas")), True)
comprueba("frase cliente no casa sin la frase", bool(rxf.search("camas y hospitalización")), False)
dea = im.construye_regex_cliente(im.prepara_terminos("desfibrilador externo automatico")[0])
comprueba("frase con tilde en la última palabra", bool(dea.search("Suministro de desfibrilador externo automático")), True)
comprueba("sin términos -> None", im.construye_regex_cliente([]), None)
comprueba("criterio columna a columna: la frase no cruza titulo/objeto",
          im.casa_en_columnas(im.construye_regex_cliente(im.prepara_terminos("camas articuladas")[0]),
                              {"titulo": "Suministro de camas", "objeto": "articuladas para UCI"}, ("titulo", "objeto")), False)

print("\n== fusión CPV ∪ texto y criterio ==")
A = {"licitacion_id": "a", "titulo": "Camas hospitalarias", "objeto": ""}
B = {"licitacion_id": "b", "titulo": "Colchones", "objeto": "sin la palabra"}
C = {"licitacion_id": "c", "titulo": "Adquisición de soportes/camas", "objeto": ""}   # token pegado, solo CPV
D = {"licitacion_id": "d", "titulo": "Camas eléctricas CPV mobiliario", "objeto": ""}
fusion, repetidos = im.fusiona([A, B, C, A], [A, D], rx, ("titulo", "objeto"))
crit = {f["licitacion_id"]: f["criterio"] for f in fusion}
comprueba("ambos (en las dos consultas)", crit["a"], "ambos")
comprueba("solo cpv", crit["b"], "cpv")
comprueba("cpv + regex en cliente -> ambos (token pegado)", crit["c"], "ambos")
comprueba("solo texto", crit["d"], "texto")
comprueba("ids repetidos entre páginas contados", repetidos, {"cpv": 1, "texto": 0})
comprueba("una fila por id", len(fusion), 4)

print("\n== --excluye ==")
rx_exc = im.construye_regex_cliente(im.prepara_terminos("mobiliario")[0])
finales, excluidas = im.aplica_excluye(fusion, rx_exc, True)
ids = {f["licitacion_id"] for f in finales}
comprueba("solo texto + excluye -> fuera", "d" in ids, False)
comprueba("contadas", excluidas, 1)
comprueba("las de CPV se quedan", ids, {"a", "b", "c"})
comprueba("excluye_casa False cuando no casa", [f["excluye_casa"] for f in finales if f["licitacion_id"] == "a"], [False])
sin_exc, n = im.aplica_excluye(fusion, None, False)
comprueba("sin --excluye: nada fuera y excluye_casa null", (n, sin_exc[0]["excluye_casa"]), (0, None))
comprueba("_campos_texto no se escribe", any("_campos_texto" in f for f in finales), False)

print("\n== invariante filas_por_criterio ==")
fc = im.filas_por_criterio(finales)
comprueba("solo_cpv + solo_texto + ambos = total", fc["solo_cpv"] + fc["solo_texto"] + fc["ambos"], fc["total"])
comprueba("reparto", (fc["solo_cpv"], fc["solo_texto"], fc["ambos"], fc["total"]), (1, 0, 2, 3))
comprueba("vacío", im.filas_por_criterio([]), {"solo_cpv": 0, "solo_texto": 0, "ambos": 0, "total": 0})

print("\n== adjudicación enriquecida (casos vivos del 12/10/2026) ==")
lic_alc = {"presupuesto_sin_iva": 31262.0, "fecha_publicacion": "2026-06-04T00:00:00+00:00",
           "fecha_fin_plazo": None, "fuente": "agregadas", "ccaa": "Comunidad de Madrid",
           "organo_contratacion": "Hospital Universitario Fundación Alcorcón", "criterio": "ambos"}
adj_alc = {"licitacion_id": "x", "lote": "1", "resultado": "Formalizado", "resultado_code": "9",
           "importe_sin_iva": 31262.0, "presupuesto_lote_sin_iva": None, "n_ofertas": 2,
           "adjudicatario": "EMPRESA ADJUDICATARIA CAMAS EJEMPLO IBERIA, S.L.", "cif_adjudicatario": "B12345674",
           "id_scheme": "NIF", "es_pyme": None, "fecha_adjudicacion": None, "estado_expediente": "ADJ",
           "sistema_contratacion": "3", "tipo_contrato": "1"}
e = im.enriquece_adjudicacion(adj_alc, lic_alc, "2026-10-12")
comprueba("baja null por lote numerado sin presupuesto de lote (regla heredada)", e["pct_baja"], None)
comprueba("baja_aviso lo marca", bool(e["baja_aviso"]), True)
comprueba("fecha_referencia cae a publicación", (e["fecha_referencia"], e["fecha_referencia_origen"]), ("2026-06-04", "publicacion"))
comprueba("basado en AM no es unitario", e["es_unitario"], False)
comprueba("rótulo sistema", e["sistema_contratacion_rotulo"], "Contrato basado en Acuerdo Marco")
comprueba("rótulo tipo", e["tipo_contrato_rotulo"], "Suministros")
comprueba("no desierto", e["es_desierto"], False)
comprueba("fuente y ccaa desnormalizadas", (e["fuente"], e["ccaa"]), ("agregadas", "Comunidad de Madrid"))
lic_lar = {"presupuesto_sin_iva": 243000.0, "fecha_publicacion": "2024-08-30T00:00:00+00:00"}
adj_lar = {"licitacion_id": "y", "lote": "1", "resultado": "Adjudicado", "resultado_code": "8",
           "importe_sin_iva": 148170.0, "presupuesto_lote_sin_iva": 148500.0, "n_ofertas": 4,
           "adjudicatario": "CAMAS EJEMPLO IBERIA S.L.", "cif_adjudicatario": "B12345674",
           "fecha_adjudicacion": "2024-12-23", "sistema_contratacion": "0", "tipo_contrato": "1"}
e2 = im.enriquece_adjudicacion(adj_lar, lic_lar, "2026-10-12")
comprueba("baja sobre el lote", (e2["pct_baja"], e2["baja_origen"]), (0.22, "lote"))
comprueba("fecha_referencia = adjudicación", (e2["fecha_referencia"], e2["fecha_referencia_origen"]), ("2024-12-23", "adjudicacion"))
comprueba("sin aviso", e2["baja_aviso"], None)
desierto = im.enriquece_adjudicacion({"licitacion_id": "z", "lote": "2", "resultado_code": "3", "cif_adjudicatario": "",
                                      "importe_sin_iva": None, "sistema_contratacion": "0"}, {}, "2026-10-12")
comprueba("desierto (cif vacío y código 3)", desierto["es_desierto"], True)
retirado = im.enriquece_adjudicacion({"licitacion_id": "z", "lote": "", "resultado_code": "4", "cif_adjudicatario": "",
                                      "sistema_contratacion": "0"}, {}, "2026-10-12")
comprueba("retirado (código 4)", (retirado["es_desierto"], retirado["es_retirado"]), (False, True))
unit = im.enriquece_adjudicacion({"licitacion_id": "w", "lote": "", "resultado_code": "8", "cif_adjudicatario": "B1",
                                  "importe_sin_iva": 12.5, "sistema_contratacion": "1"}, {"presupuesto_sin_iva": 1000.0}, "2026-10-12")
comprueba("AM establecimiento = unitario, baja null", (unit["es_unitario"], unit["pct_baja"]), (True, None))
futuro = im.enriquece_adjudicacion({"licitacion_id": "w", "lote": "", "resultado_code": "8", "cif_adjudicatario": "B1",
                                    "importe_sin_iva": 5000.0, "sistema_contratacion": "0", "fecha_adjudicacion": "2027-01-01"}, {}, "2026-10-12")
comprueba("fecha futura marcada", futuro["fecha_adjudicacion_futura"], True)

print("\n== derivados del bloque 1 ==")
lics = [{"licitacion_id": "x", "tiene_resultado": False}, {"licitacion_id": "q", "tiene_resultado": False}]
im.deriva_bloque1(lics, [e, dict(e, lote="2", cif_adjudicatario="B2"), dict(e, lote="2", cif_adjudicatario="B3")])
comprueba("lotes distintos (no filas)", (lics[0]["n_lotes_con_resultado"], lics[0]["n_filas_adjudicacion"]), (2, 3))
comprueba("tipo/sistema de expediente", (lics[0]["tipo_contrato_expediente"], lics[0]["sistema_contratacion_expediente"]), ("1", "3"))
comprueba("sin lotes -> null y tiene_resultado False", (lics[1].get("n_lotes_con_resultado"), lics[1]["tiene_resultado"]), (None, False))

print("\n== agregados por lote distinto y tarifas ==")
L = [{"licitacion_id": "x", "organo_contratacion": "O1", "ccaa": "Madrid", "presupuesto_sin_iva": 100.0, "tiene_resultado": True},
     {"licitacion_id": "y", "organo_contratacion": "O1", "ccaa": "Madrid", "presupuesto_sin_iva": None, "tiene_resultado": True}]
AD = [dict(e, licitacion_id="x", organo_contratacion="O1", ccaa="Madrid", lote="1", importe_sin_iva=10.0, cif_adjudicatario="B1"),
      dict(e, licitacion_id="x", organo_contratacion="O1", ccaa="Madrid", lote="1", importe_sin_iva=10.0, cif_adjudicatario="B2"),  # mismo lote, 2 ganadores
      dict(e, licitacion_id="y", organo_contratacion="O1", ccaa="Madrid", lote="1", importe_sin_iva=7.0, cif_adjudicatario="B1", es_unitario=True),
      dict(desierto, licitacion_id="y", organo_contratacion="O1", ccaa="Madrid", lote="2")]
org = im.agrega_por_clave(L, AD, "organo_contratacion")[0]
comprueba("lote con 2 ganadores cuenta una vez (y el de tarifa también es un lote adjudicado)",
          (org["n_lotes_adjudicados"], org["importe_adjudicado_sin_iva"]), (2, 10.0))
comprueba("tarifas aparte", (org["n_lotes_precios_unitarios"], org["importe_tarifas_unitarias"]), (1, 7.0))
comprueba("desiertos", org["n_lotes_desiertos"], 1)
comprueba("presupuesto solo de las que lo traen", org["presupuesto_sin_iva"], 100.0)
ag = im.agrega_adjudicatarios(AD, [{"cif_adjudicatario": "B1", "importe_sin_iva": 3.0, "cifs_adjudicatarios": ["B1", "B9"]}])
comprueba("por CIF cada ganador se lleva el lote", (ag["B1"]["lotes"], ag["B2"]["lotes"]), (2, 1))
comprueba("importe real excluye tarifas", (ag["B1"]["importe"], ag["B1"]["tarifas"]), (10.0, 7.0))
comprueba("menores: principal y secundario", (ag["B1"]["menores"], ag["B9"]["menores_secundario"], ag["B9"]["principal"]), (1, 1, False))
res = im.resumen_adjudicaciones(AD, [dict(l, estado_adjudicacion="adjudicada") for l in L])
comprueba("lotes distintos y multiadjudicatario", (res["n_lotes_distintos"], res["n_lotes_multiadjudicatario"]), (3, 1))
comprueba("desiertos por lote", res["n_lotes_desiertos"], 1)
comprueba("discrepancia estado vs lotes (y es desierta_parcial)", res["n_discrepancias_estado_vs_lotes"], 1)
comprueba("cif con forma válida", (im.cif_con_forma_valida("B12345674"), im.cif_con_forma_valida("UTEPENDIENTE"), im.cif_con_forma_valida("312836")), (True, False, False))

print("\n== paginación con un Supabase simulado (sin red) ==")


class _Resp:
    def __init__(self, filas):
        self._filas = filas
        self.headers = {"Content-Range": f"0-0/{len(filas)}"}

    def json(self):
        return self._filas


class _SbFalso:
    """Devuelve `total` filas en páginas de PAGINA y registra las URLs pedidas."""
    def __init__(self, total):
        self.total, self.urls = total, []

    def _pide(self, tabla, params, cabeceras=None):
        self.urls.append(params)
        m = re.search(r"limit=(\d+)&offset=(\d+)", params)
        lim, off = int(m.group(1)), int(m.group(2))
        return _Resp([{"licitacion_id": str(i)} for i in range(off, min(off + lim, self.total))])


sb = _SbFalso(2350)
filas, info = im.pagina(sb, "licitaciones", "select=licitacion_id&order=licitacion_id.asc")
comprueba("trae todas las filas en páginas", (len(filas), info["paginas"], info["topado"], info["lenta"]), (2350, 3, False, False))
comprueba("limit/offset en cada página", [re.search(r"offset=(\d+)", u).group(1) for u in sb.urls], ["0", "1000", "2000"])
sb = _SbFalso(5000)
filas, info = im.pagina(sb, "licitaciones", "select=licitacion_id&order=licitacion_id.asc", tope=2000)
comprueba("tope: corta y lo declara", (len(filas), info["topado"]), (2000, True))
sb = _SbFalso(1000)
filas, info = im.pagina(sb, "licitaciones", "select=licitacion_id&order=licitacion_id.asc", tope=2000)
comprueba("justo 1000 filas: segunda página vacía, no topado", (len(filas), info["paginas"], info["topado"]), (1000, 2, False))
try:
    im.pagina(sb, "licitaciones", "select=licitacion_id&limit=5")
    comprueba("limit en params -> error", False, True)
except ValueError:
    comprueba("limit en params -> error", True, True)
comprueba("resumen_topado con estimación", im.resumen_topado({"consulta_cpv": {"topado": True, "filas_estimadas_si_topado": 31000},
                                                              "consulta_texto": None}),
          {"topado": True, "filas_estimadas_cpv": 31000, "filas_estimadas_texto": None})

print("\n== duplicados, tramos, slug, url ==")
dup = im.posibles_duplicados([{"licitacion_id": "1", "num_expediente": "AM-SUM 043/26", "organo_contratacion": "Hospital Alcorcón", "fuente": "agregadas"},
                              {"licitacion_id": "2", "num_expediente": "AMSUM04326", "organo_contratacion": "hospital alcorcon", "fuente": "estatal"},
                              {"licitacion_id": "3", "num_expediente": "OTRO", "organo_contratacion": "x", "fuente": "estatal"}])
comprueba("mismo expediente normalizado y órgano -> grupo", (len(dup), sorted(dup[0]["ids"])), (1, ["1", "2"]))
tr = im.tramos_mensuales(datetime(2024, 10, 12, tzinfo=timezone.utc), datetime(2025, 1, 5, tzinfo=timezone.utc))
comprueba("tramos mensuales", [(a.isoformat()[:10], b.isoformat()[:10]) for a, b in tr],
          [("2024-10-12", "2024-11-12"), ("2024-11-12", "2024-12-12"), ("2024-12-12", "2025-01-05")])
comprueba("slug translitera", im.slug("Camas Eléctricas / UCI"), "camas-electricas-uci")
comprueba("slug vacío", im.slug("¿?"), "mercado")
comprueba("valor_url codifica + \\ | y espacios", im.valor_url("\\s+ |"), "%5Cs%2B%20%7C")
comprueba("en_lista + valor_url", im.valor_url(im.en_lista(["a", "b"])), "in.%28%22a%22%2C%22b%22%29")

# ---------------------------------------------------------------------------
print()
if FALLOS:
    print(f"FALLAN {len(FALLOS)} pruebas: " + ", ".join(FALLOS))
    sys.exit(1)
print("Todas las pruebas en verde.")
