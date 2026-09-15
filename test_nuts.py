# -*- coding: utf-8 -*-
"""Pruebas del mapeo territorial · deterministas y SIN RED (como test_adjudicaciones.py).

    python test_nuts.py

Los casos NO son inventados: los códigos y sus proporciones salen de medir el feed real
y el corpus local de 12.219 entradas el 15/09/2026. Lo que se protege aquí es la regla
de oro del mapeo: ANTES UN NULL HONESTO QUE UN DATO INVENTADO. Si alguna de estas
pruebas se cae al tocar nuts.py, lo más probable es que se esté colando geografía falsa
en 624.204 filas.
"""

import sys

import nuts

FALLOS = []


def comprueba(nombre, obtenido, esperado):
    if obtenido == esperado:
        print(f"  OK {nombre}")
    else:
        FALLOS.append(nombre)
        print(f"  FALLO {nombre}\n     esperado: {esperado!r}\n     obtenido: {obtenido!r}")


print("\n== el vocabulario está y es el oficial ==")
comprueba("data/nuts_nombres.json existe", nuts.RUTA_VOCABULARIO.exists(), True)
comprueba("19 comunidades y ciudades autónomas", len(nuts.ccaa_por_codigo()), 19)
comprueba("ESZZ (Extra-Regio) NO cuenta como comunidad", "ESZZ" in nuts.ccaa_por_codigo(), False)

print("\n== NUTS-1: solo se resuelven los que contienen UNA comunidad ==")
# No está escrito a mano: se deriva contando hijos en el vocabulario.
comprueba("ES3 y ES7 son los únicos 1:1", sorted(nuts.nuts1_resolubles()), ["ES3", "ES7"])
comprueba("ES3 -> Madrid", nuts.a_ccaa("ES3")[1], "Comunidad de Madrid")
comprueba("ES7 -> Canarias", nuts.a_ccaa("ES7")[1], "Canarias")
for ambiguo in ("ES1", "ES2", "ES4", "ES5", "ES6"):
    comprueba(f"{ambiguo} agrupa varias -> NULL", nuts.a_ccaa(ambiguo)[1], None)

print("\n== NUTS-3 (provincia) -> su comunidad ==")
for codigo, ccaa, lugar in [
    ("ES618", "Andalucía", "Sevilla"),
    ("ES300", "Comunidad de Madrid", "Madrid"),
    ("ES111", "Galicia", "A Coruña"),
    ("ES511", "Cataluña", "Barcelona"),
    ("ES523", "Comunitat Valenciana", "Valencia/València"),
    ("ES620", "Región de Murcia", "Murcia"),
    ("ES709", "Canarias", "Tenerife"),
    ("ES531", "Illes Balears", "Eivissa y Formentera"),
    ("ES630", "Ciudad de Ceuta", "Ceuta"),
]:
    comprueba(f"{codigo} -> {ccaa}", nuts.a_ccaa(codigo)[1], ccaa)
    comprueba(f"{codigo} -> lugar {lugar}", nuts.a_lugar(codigo), lugar)

print("\n== NUTS-2: el lugar NO se afina más de lo que dice la fuente ==")
comprueba("ES51 -> Cataluña", nuts.a_ccaa("ES51")[1], "Cataluña")
comprueba("ES51 -> lugar = la propia comunidad", nuts.a_lugar("ES51"), "Cataluña")
comprueba("ES70 -> lugar = Canarias, NO una isla", nuts.a_lugar("ES70"), "Canarias")

print("\n== lo que NO se resuelve se queda a NULL (nada de inventar) ==")
for codigo, motivo in [
    ("ES", "país: ámbito nacional, no una comunidad (1,24% de las entradas)"),
    ("ESZ", "Extra-Regio NUTS-1"),
    ("ESZZ", "Extra-Regio NUTS-2: existe en la lista pero no es una comunidad"),
    ("ESZZZ", "Extra-Regio NUTS-3: truncado a ciegas daría ESZZ"),
    ("DK0", "extranjero (Dinamarca), medido en el feed real"),
    ("SK010", "extranjero (Eslovaquia), medido en el feed real"),
    ("PT1A", "extranjero (Portugal)"),
    ("ES99", "código con forma válida que NO está en la lista oficial"),
    ("", "vacío"),
    (None, "ausente"),
]:
    comprueba(f"{codigo!r} -> NULL ({motivo[:38]})", nuts.a_ccaa(codigo)[1], None)
    comprueba(f"{codigo!r} -> lugar NULL", nuts.a_lugar(codigo), None)

print("\n== normalización de la entrada ==")
comprueba("minúsculas", nuts.a_ccaa("es618")[1], "Andalucía")
comprueba("espacios alrededor", nuts.a_ccaa("  ES618  ")[1], "Andalucía")

print("\n== traduce(): lo que consume el pipeline ==")
comprueba("devuelve la terna", nuts.traduce("ES618"), ("Andalucía", "Sevilla", "ES618"))
comprueba("guarda el código crudo aunque no resuelva", nuts.traduce("ES"), (None, None, "ES"))
comprueba("sin código, todo a None", nuts.traduce(None), (None, None, None))

print("\n== el texto libre del feed NO se usa en ningún sitio ==")
# En las agregadas (157.752 filas) ese texto no viene NUNCA, y donde viene está sucio.
fuente = (nuts.RUTA_VOCABULARIO.parent.parent / "nuts.py").read_text(encoding="utf-8")
comprueba("nuts.py no lee reg['region']", "region']" in fuente or 'region"]' in fuente, False)

print("\n== ningún código español resuelve a una comunidad que no exista ==")
malos = []
for codigo in nuts.vocabulario():
    if not codigo.startswith("ES"):
        continue
    ccaa_cod, ccaa_nom = nuts.a_ccaa(codigo)
    if ccaa_cod is not None and ccaa_cod not in nuts.ccaa_por_codigo():
        malos.append(codigo)
comprueba("todos los códigos ES resuelven dentro de la tabla", malos, [])

# Barrido completo: los 60 NUTS-3 españoles tienen que resolver a una comunidad.
nuts3 = [c for c in nuts.vocabulario() if c.startswith("ES") and len(c) == 5 and c not in nuts.NO_SON_CCAA]
sin_resolver = [c for c in nuts3 if nuts.a_ccaa(c)[1] is None]
comprueba(f"los {len(nuts3)} NUTS-3 de España resuelven todos", sin_resolver, [])

print("\n" + ("TODO OK ✔" if not FALLOS else f"{len(FALLOS)} FALLOS: {FALLOS}"))
sys.exit(1 if FALLOS else 0)
