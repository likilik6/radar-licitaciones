#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pruebas de menores_autonomicos.py · deterministas y SIN RED (como test_informe_empresa.py).

    python test_menores_autonomicos.py

Cubren lo que decide qué se carga y cómo: fechas (la trampa de la zona horaria de
2023-2024), importes con coma o punto, el CSV con '|' y comillas dentro de los campos,
los NIF (máscaras AEPD, multi-NIF, nombres metidos en el campo), el desempate de ID
repetidos, la ventana, el CPV del portal, los lotes de la RPC y la regla de gemelas con
casos inyectados (hoy no hay ningún caso real: 0 órganos en común con la estatal).

Los CSV de prueba se escriben en una carpeta temporal con la forma medida en F0
(cabecera de 23 columnas, CRLF, valores rellenos con espacios, cp1252 o UTF-8).
Lo que NO cubren: la descarga real, el portal y la RPC en Supabase (eso se comprueba con
la simulación sobre los ficheros reales y con la primera carga).
"""

import json
import sys
import tempfile
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import menores_autonomicos as ma

sys.stdout.reconfigure(encoding="utf-8")
FALLOS = []
BIEN = []


def comprueba(nombre, obtenido, esperado):
    if obtenido == esperado:
        BIEN.append(nombre)
        print(f"  OK {nombre}")
    else:
        FALLOS.append(nombre)
        print(f"  FALLO {nombre}\n     esperado: {esperado!r}\n     obtenido: {obtenido!r}")


def lanza(nombre, funcion, excepcion):
    try:
        funcion()
    except excepcion:
        comprueba(nombre, True, True)
    except Exception as err:  # noqa: BLE001
        comprueba(nombre, type(err).__name__, excepcion.__name__)
    else:
        comprueba(nombre, "no lanzó", excepcion.__name__)


# ---------------------------------------------------------------------------
# Utilidades para fabricar CSV con la forma real
# ---------------------------------------------------------------------------
def linea_csv(**valores):
    """Una línea del CSV de la Junta: 23 campos rellenos con espacios, '|' de separador y
    comillas SOLO donde hacen falta (como el fichero real)."""
    campos = []
    for columna in ma.CABECERA_ANDALUCIA:
        v = str(valores.get(columna, ""))
        if "|" in v or '"' in v:
            v = '"' + v.replace('"', '""') + '"'
        campos.append(v.ljust(12))
    return "|".join(campos) + "\r\n"


def fila_base(id_, **extra):
    base = {"ID_EXPEDIENTE": str(id_).rjust(12, " "), "ORGANO_CONTRATACION": "Servicio Andaluz de Salud",
            "NUM_EXPEDIENTE": f"CONTR 2025 {id_:010d}", "TITULO": "Suministro de guantes",
            "DESCRIPCION": "Suministro de guantes de nitrilo", "IMPORTE_ADJUDICACION_SIN_IVA": "100,00",
            "IMPORTE_ADJUDICACION_CON_IVA": "121,00", "NIF_ADJUDICATARIO": "B12345678",
            "ADJUDICATARIO_DENOMINACION": "Guantes del Sur SL", "FECHA_ADJUDICACION": "10/03/2025",
            "ESTADO": "Resuelto"}
    base.update(extra)
    return base


def escribe_csv(carpeta, nombre, filas, codificacion="cp1252", como_zip=False):
    texto = "|".join(ma.CABECERA_ANDALUCIA) + "\r\n" + "".join(linea_csv(**f) for f in filas)
    datos = texto.encode(codificacion)
    ruta = Path(carpeta) / nombre
    if como_zip:
        with zipfile.ZipFile(ruta, "w") as z:
            z.writestr(nombre.replace(".zip", ""), datos)
    else:
        ruta.write_bytes(datos)
    return ruta


DESDE = "2023-09-17"

# ---------------------------------------------------------------------------
print("\n== fechas ==")
comprueba("ISO +0100 a las 23:00 -> literal, sin cambiar de día",
          ma.normaliza_fecha("2023-03-14T23:00:00+0100"), ("2023-03-14", "iso"))
comprueba("ISO +0200 a las 00:00 -> literal (en UTC sería el día anterior)",
          ma.normaliza_fecha("2023-07-01T00:00:00+0200"), ("2023-07-01", "iso"))
comprueba("dd/mm/aaaa", ma.normaliza_fecha("05/02/2025"), ("2025-02-05", "dmy"))
comprueba("d/m/aaaa sin ceros", ma.normaliza_fecha("5/2/2025"), ("2025-02-05", "dmy"))
comprueba("29/02 en año bisiesto", ma.normaliza_fecha("29/02/2024"), ("2024-02-29", "dmy"))
comprueba("29/02 en año NO bisiesto -> inválida", ma.normaliza_fecha("29/02/2025"), (None, "invalida"))
comprueba("31/04 -> inválida", ma.normaliza_fecha("31/04/2025"), (None, "invalida"))
comprueba("vacía", ma.normaliza_fecha("   "), (None, "vacia"))
comprueba("formato raro", ma.normaliza_fecha("14.03.2023"), (None, "invalida"))
comprueba("año implausible (feeds.a_fecha) -> inválida", ma.normaliza_fecha("01/01/1999"), (None, "invalida"))

print("\n== ventana (3 años, con 29 de febrero) ==")
comprueba("hoy 17/09/2026 -> desde 17/09/2023", ma.resta_anios(date(2026, 9, 17), 3), date(2023, 9, 17))
comprueba("hoy 29/02/2028 -> desde 28/02/2025", ma.resta_anios(date(2028, 2, 29), 3), date(2025, 2, 28))
comprueba("hoy 28/02/2027 -> desde 28/02/2024", ma.resta_anios(date(2027, 2, 28), 3), date(2024, 2, 28))
comprueba("29/02/2024 dentro de la ventana que empieza el 28/02/2024",
          ma.en_ventana("2024-02-29", "2024-02-28"), True)
comprueba("el primer día de la ventana entra", ma.en_ventana("2023-09-17", DESDE), True)
comprueba("el día anterior no entra", ma.en_ventana("2023-09-16", DESDE), False)
comprueba("sin fecha entra (se cuenta aparte)", ma.en_ventana(None, DESDE), True)
comprueba("años por defecto: los que toca la ventana",
          ma.anios_por_defecto(date(2023, 9, 17), date(2026, 9, 17)), [2023, 2024, 2025, 2026])

print("\n== importes ==")
comprueba("coma decimal (2025)", ma.normaliza_importe("1234,56"), (Decimal("1234.56"), "ok"))
comprueba("punto decimal (2023/2024/2026)", ma.normaliza_importe("1234.56"), (Decimal("1234.56"), "ok"))
comprueba("sin decimales", ma.normaliza_importe("49838"), (Decimal("49838"), "ok"))
comprueba("',50' sin cero delante", ma.normaliza_importe(",50"), (Decimal("0.50"), "ok"))
comprueba("'.9' sin cero delante", ma.normaliza_importe(".9"), (Decimal("0.9"), "ok"))
comprueba("miles con punto y decimal con coma", ma.normaliza_importe("1.234,56"), (Decimal("1234.56"), "ok"))
comprueba("miles con coma y decimal con punto", ma.normaliza_importe("1,234.56"), (Decimal("1234.56"), "ok"))
comprueba("una coma con 3 decimales es DECIMAL (no hay miles sin decimales)",
          ma.normaliza_importe("1,234"), (Decimal("1.234"), "ok"))
comprueba("miles repetidos bien agrupados", ma.normaliza_importe("1.234.567"), (Decimal("1234567"), "ok"))
comprueba("separadores mal agrupados -> inválido", ma.normaliza_importe("12.34.5"), (None, "invalido"))
comprueba("vacío", ma.normaliza_importe("  "), (None, "vacio"))
comprueba("texto -> inválido", ma.normaliza_importe("n/d"), (None, "invalido"))
comprueba("rellenado con espacios", ma.normaliza_importe("  27843.91   "), (Decimal("27843.91"), "ok"))

# ---------------------------------------------------------------------------
print("\n== NIF ==")
comprueba("';' final (2023-2024)", ma.normaliza_nifs("B12345678;")[0], ["B12345678"])
nifs, orig, marcas = ma.normaliza_nifs("B12345678;A87654321;")
comprueba("multi-NIF: orden del fichero", nifs, ["B12345678", "A87654321"])
comprueba("nombre metido en el campo se descarta",
          ma.normaliza_nifs("GUANTES DEL SUR SL;B12345678")[0], ["B12345678"])
comprueba("nombre metido: se cuenta", "sin_digitos" in ma.normaliza_nifs("GUANTES DEL SUR SL;B12345678")[2], True)
comprueba("'*********' no identifica a nadie: fuera", ma.normaliza_nifs("*********")[0], [])
comprueba("ES + CIF -> sin ES", ma.normaliza_nifs("ESB12345678")[0], ["B12345678"])
comprueba("ES + CIF: se cuenta", "prefijo_es" in ma.normaliza_nifs("ESB12345678")[2], True)
comprueba("ES delante de algo que no es NIF español se deja", ma.normaliza_nifs("ES12345")[0], ["ES12345"])
comprueba("signos y minúsculas", ma.normaliza_nifs(" b-12.345/678 ")[0], ["B12345678"])
comprueba("DNI 8 dígitos -> ***4567**", ma.normaliza_nifs("12345678Z")[0], ["***4567**"])
comprueba("DNI 8: el original solo en memoria", ma.normaliza_nifs("12345678Z")[1], ["12345678Z"])
comprueba("DNI 7 dígitos -> relleno y ***3456**", ma.normaliza_nifs("1234567Z")[0], ["***3456**"])
comprueba("ES + DNI -> sin ES y enmascarado", ma.normaliza_nifs("ES12345678Z")[0], ["***4567**"])
comprueba("NIE -> ****4567*", ma.normaliza_nifs("X1234567L")[0], ["****4567*"])
comprueba("NIF K/L/M -> ****4567*", ma.normaliza_nifs("K1234567L")[0], ["****4567*"])
comprueba("ya enmascarado se deja (normalizado)", ma.normaliza_nifs(" ***4567** ")[0], ["***4567**"])
comprueba("ya enmascarado: tipo", ma.normaliza_nifs("***4567**")[2], ["ya_enmascarado"])
comprueba("extranjero tal cual", ma.normaliza_nifs("PT123456789")[0], ["PT123456789"])
comprueba("IVA holandés tal cual", ma.normaliza_nifs("NL123456789B01")[0], ["NL123456789B01"])
comprueba("CIF de persona jurídica: tipo", ma.normaliza_nifs("B12345678")[2], ["juridica"])
comprueba("vacío", ma.normaliza_nifs("  ;  ")[0], [])
comprueba("dos DNI con la misma máscara no se duplican",
          ma.normaliza_nifs("12345678Z;99945678A")[0], ["***4567**"])
comprueba("...pero los dos DNI completos quedan en memoria para la regla exacta",
          ma.normaliza_nifs("12345678Z;99945678A")[1], ["12345678Z", "99945678A"])
comprueba("DNI sin letra (8 dígitos) -> la misma máscara que con letra", ma.normaliza_nifs("12345678")[0], ["***4567**"])
comprueba("DNI sin letra: tipo y original en memoria",
          ma.normaliza_nifs("12345678")[1:], (["12345678"], ["dni_sin_letra"]))
comprueba("'0' + 8 dígitos -> DNI sin letra", ma.normaliza_nifs("012345678")[0], ["***4567**"])
comprueba("ES + DNI sin letra -> sin ES y enmascarado", ma.normaliza_nifs("ES12345678")[0], ["***4567**"])
comprueba("9 dígitos sin 0 delante (NIF portugués) tal cual", ma.normaliza_nifs("123456789")[0], ["123456789"])
comprueba("CIF jurídico: nada en originales", ma.normaliza_nifs("B12345678")[1], [])

print("\n== máscaras que ya trae la Junta -> forma AEPD ==")
comprueba("'***4567**' se queda", ma.mascara_canonica("***4567**"), "***4567**")
comprueba("'****4567*' (NIE) se queda", ma.mascara_canonica("****4567*"), "****4567*")
comprueba("'X***4567*' -> '****4567*'", ma.mascara_canonica("X***4567*"), "****4567*")
comprueba("'12****78Z' (enseña 1-2, 7-8 y letra) -> nada", ma.mascara_canonica("12****78Z"), "*********")
comprueba("'123****8Q' -> nada", ma.mascara_canonica("123****8Q"), "*********")
comprueba("'***999***' (ventana incompleta) -> nada", ma.mascara_canonica("***999***"), "*********")
comprueba("'**4567***' (otra ventana) -> nada", ma.mascara_canonica("**4567***"), "*********")
comprueba("máscara de 10 posiciones -> nada", ma.mascara_canonica("****4567**"), "*********")
comprueba("'***45678*' -> solo la ventana del DNI", ma.mascara_canonica("***45678*"), "***4567**")
nifs_m, orig_m, marcas_m = ma.normaliza_nifs("12****78Z")
comprueba("máscara que no deja la ventana: fuera y contada", (nifs_m, orig_m, marcas_m),
          ([], [], ["ya_enmascarado", "mascara_descartada"]))
comprueba("máscara reducida: contada", ma.normaliza_nifs("X***4567*")[0:3:2],
          (["****4567*"], ["ya_enmascarado", "mascara_reducida"]))


def posiciones_visibles(mascaras):
    return {i for m in mascaras for i, c in enumerate(m) if c != "*"}


# La reconstrucción de la revisión de F1: '***dddd**' en un contrato y 'dd****ddL' en otro
# de la misma persona. Tras la canónica, juntas no enseñan más que la ventana.
salida_persona = ma.normaliza_nifs("12345678Z")[0] + ma.normaliza_nifs("12****78Z")[0] + \
    ma.normaliza_nifs("123****8Z")[0] + ma.normaliza_nifs("***456***")[0]
comprueba("juntando todas las máscaras de una persona solo se ven los dígitos 4-7",
          posiciones_visibles(salida_persona), {3, 4, 5, 6})

print("\n== DNI en los textos (adjudicatario y objeto) ==")
comprueba("el nombre ES el DNI -> máscara", ma.protege_texto("12345678Z", por_partes=True), ("***4567**", 1))
comprueba("el nombre es un DNI con letra mala -> máscara igual (entero)",
          ma.protege_texto("12345678A", por_partes=True), ("***4567**", 1))
comprueba("el nombre es un DNI sin letra -> máscara", ma.protege_texto("12345678", por_partes=True), ("***4567**", 1))
comprueba("el nombre es un NIE -> máscara", ma.protege_texto("X1234567L", por_partes=True), ("****4567*", 1))
comprueba("varios adjudicatarios: solo la parte que es DNI",
          ma.protege_texto("GUANTES DEL SUR SL; 12345678Z", por_partes=True), ("GUANTES DEL SUR SL; ***4567**", 1))
comprueba("un nombre normal no se toca", ma.protege_texto("GARCIA PEREZ, JUAN", por_partes=True), ("GARCIA PEREZ, JUAN", 0))
comprueba("un CIF jurídico como nombre no se toca", ma.protege_texto("B12345678", por_partes=True), ("B12345678", 0))
comprueba("DNI con letra buena dentro del objeto",
          ma.protege_texto("Honorarios de D. Juan García, DNI 12345678-Z, por la ponencia"),
          ("Honorarios de D. Juan García, DNI ***4567**, por la ponencia", 1))
comprueba("NIE con letra buena dentro del objeto", ma.protege_texto("Pago a x1234567l por curso"),
          ("Pago a ****4567* por curso", 1))
comprueba("8 cifras y letra MALA dentro del objeto (referencia) no se tocan",
          ma.protege_texto("Repuesto ref. 12345678A para bomba"), ("Repuesto ref. 12345678A para bomba", 0))
comprueba("vacío", ma.protege_texto(None), (None, 0))

print("\n== objeto ==")
comprueba("descripción vacía (2023) -> título", ma.elige_objeto("Guantes", ""), "Guantes")
comprueba("descripción más corta -> título", ma.elige_objeto("Guantes de nitrilo", "Guantes"), "Guantes de nitrilo")
comprueba("descripción más larga -> descripción", ma.elige_objeto("Guantes", "Guantes de nitrilo"), "Guantes de nitrilo")
comprueba("igual de largas -> descripción", ma.elige_objeto("AAAA", "BBBB"), "BBBB")
comprueba("las dos vacías -> None", ma.elige_objeto("", " "), None)

print("\n== normorg y nombres ==")
comprueba("tildes, mayúsculas, signos y '(En Transición)'",
          ma.normorg("Consejería de Salud y Consumo (En Transición)."), "CONSEJERIA DE SALUD Y CONSUMO")
comprueba("espacios colapsados", ma.normorg("  Servicio   Andaluz\tde Salud "), "SERVICIO ANDALUZ DE SALUD")
comprueba("formas jurídicas fuera", ma.tokens_nombre("Guantes del Sur, S.L.U."), {"GUANTES", "SUR"})
comprueba("mismo nombre en otro orden = 1,0", ma.similitud_nombres("GARCIA PEREZ JUAN", "Juan García Pérez"), 1.0)
comprueba("nombre vacío = 0", ma.similitud_nombres("", "JUAN"), 0.0)

# ---------------------------------------------------------------------------
print("\n== lectura del CSV (cp1252, comillas, '|' dentro, repetidos, estado, ventana) ==")
with tempfile.TemporaryDirectory() as tmp:
    filas_csv = [
        fila_base(1001, TITULO='Reparación "urgente" | bomba de calor', DESCRIPCION=""),
        fila_base(1002, NIF_ADJUDICATARIO="12345678Z;", ADJUDICATARIO_DENOMINACION="GARCIA PEREZ JUAN;"),
        fila_base(1003, ESTADO="Evaluación"),
        fila_base(1004, FECHA_ADJUDICACION="16/09/2023"),                 # fuera de ventana
        fila_base(1005, FECHA_ADJUDICACION="17/09/2023"),                 # primer día: dentro
        fila_base(1006, FECHA_ADJUDICACION=""),                           # sin fecha: dentro
        fila_base(1007, FECHA_ADJUDICACION="31/02/2025"),                 # inválida: dentro, null
        fila_base(1008, IMPORTE_ADJUDICACION_SIN_IVA="", IMPORTE_ADJUDICACION_CON_IVA="abc"),
        # ID repetido que solo cambia el órgano, como los 35 de 2025.
        fila_base(1009, ORGANO_CONTRATACION="Instituto Andaluz de Investigación y Formación Agraria"),
        fila_base(1009, ORGANO_CONTRATACION="IFAPA Centro Alameda del Obispo"),
        # Persona física con el DNI también como «nombre» y otra con la máscara de la Junta.
        fila_base(1011, NIF_ADJUDICATARIO="12345678Z", ADJUDICATARIO_DENOMINACION="12345678Z",
                  DESCRIPCION="Ponencia de D. Juan García con DNI 12345678Z en unas jornadas"),
        fila_base(1012, NIF_ADJUDICATARIO="12****78Z;", ADJUDICATARIO_DENOMINACION="GARCIA PEREZ JUAN;"),
    ]
    ruta = escribe_csv(tmp, "menores_2025.csv", filas_csv)
    filas, originales, info = ma.lee_fichero_andalucia(ruta, DESDE)
    comprueba("codificación cp1252 detectada", info["codificacion"], "cp1252")
    comprueba("líneas leídas", info["lineas_leidas"], 12)
    comprueba("reparada por comillas (el '|' del título)", info["reparadas_por_comillas"], 1)
    comprueba("comillas duplicadas y '|' dentro del campo",
              filas["and:1001"]["objeto"], 'Reparación "urgente" | bomba de calor')
    comprueba("ID relleno con espacios -> clave sin relleno", "and:1001" in filas, True)
    comprueba("enlace con el ID", filas["and:1001"]["enlace"].endswith("idExpediente=1001"), True)
    comprueba("fuente", filas["and:1001"]["fuente"], "andalucia")
    comprueba("cpv null (no tocar)", filas["and:1001"]["cpv"], None)
    comprueba("importe con coma -> Decimal", filas["and:1001"]["importe_sin_iva"], Decimal("100.00"))
    comprueba("DNI enmascarado en la fila", filas["and:1002"]["cif_adjudicatario"], "***4567**")
    comprueba("nombre sin ';' final", filas["and:1002"]["adjudicatario"], "GARCIA PEREZ JUAN")
    comprueba("el DNI completo NO va en la fila", "12345678Z" in json.dumps(filas["and:1002"], default=str), False)
    comprueba("el DNI completo solo en originales", originales.get("and:1002"), ["12345678Z"])
    comprueba("ESTADO no resuelto fuera", "and:1003" in filas, False)
    comprueba("ESTADO no resuelto contado", info["fuera_por_estado"], {"Evaluación": 1})
    comprueba("fuera de ventana", ("and:1004" in filas, info["fuera_de_ventana"]), (False, 1))
    comprueba("primer día de la ventana dentro", "and:1005" in filas, True)
    comprueba("sin fecha: se carga y se cuenta", (filas["and:1006"]["fecha_adjudicacion"], info["sin_fecha"]), (None, 1))
    comprueba("fecha inválida: null y contada", (filas["and:1007"]["fecha_adjudicacion"], info["fecha_invalida"]), (None, 1))
    comprueba("importes vacíos/no numéricos -> null contados",
              (info["importe_sin_iva_null"], info["importe_con_iva_null"], info["importe_con_iva_no_numerico"]), (1, 1, 1))
    comprueba("ID repetido: gana el órgano que ordena primero (IFAPA)",
              filas["and:1009"]["organo_contratacion"], "IFAPA Centro Alameda del Obispo")
    comprueba("ID repetido contado", (info["ids_repetidos"]["ids"], info["ids_repetidos"]["filas_descartadas"]), (1, 1))
    comprueba("en ventana = válidas + fuera por estado", info["en_ventana"], info["filas_validas"] + 1)
    comprueba("descripción vacía -> objeto del título contado", info["objeto_del_titulo"], 1)
    comprueba("nombre que es un DNI: sale enmascarado",
              (filas["and:1011"]["adjudicatario"], filas["and:1011"]["cif_adjudicatario"]), ("***4567**", "***4567**"))
    comprueba("DNI dentro del objeto: sale enmascarado", "12345678Z" in filas["and:1011"]["objeto"], False)
    comprueba("ningún DNI completo en ninguna fila que se cargaría",
              [k for k, f in filas.items() if "12345678Z" in json.dumps(f, default=str)], [])
    comprueba("máscara de la Junta que no deja la ventana: fila sin NIF",
              (filas["and:1012"]["cif_adjudicatario"], filas["and:1012"]["n_adjudicatarios"]), (None, None))
    comprueba("recuentos de privacidad",
              {k: info["nif"].get(k) for k in ("adjudicatario_con_dni_tapado", "objeto_con_dni_tapado",
                                                "mascaras_descartadas", "ya_enmascarados")},
              {"adjudicatario_con_dni_tapado": 1, "objeto_con_dni_tapado": 1, "mascaras_descartadas": 1,
               "ya_enmascarados": 1})

    # El mismo par de filas repetidas en el ORDEN CONTRARIO: mismo resultado.
    ruta2 = escribe_csv(tmp, "menores_2025b.csv", [filas_csv[9], filas_csv[8]])
    filas2, _, _ = ma.lee_fichero_andalucia(ruta2, DESDE)
    comprueba("desempate determinista en el otro orden",
              filas2["and:1009"]["organo_contratacion"], "IFAPA Centro Alameda del Obispo")
    iguales = [fila_base(1010, ADJUDICATARIO_DENOMINACION="B"), fila_base(1010, ADJUDICATARIO_DENOMINACION="A")]
    fa, _, _ = ma.lee_fichero_andalucia(escribe_csv(tmp, "r1.csv", iguales), DESDE)
    fb, _, _ = ma.lee_fichero_andalucia(escribe_csv(tmp, "r2.csv", iguales[::-1]), DESDE)
    comprueba("mismo órgano: desempate por el texto, igual en los dos órdenes",
              (fa["and:1010"]["adjudicatario"], fb["and:1010"]["adjudicatario"]), ("A", "A"))

    # 2023-2024: fechas ISO con zona, punto decimal, ';' final y .csv.zip.
    ruta_zip = escribe_csv(tmp, "menores_2023.csv.zip", [
        fila_base(540001, FECHA_ADJUDICACION="2023-09-17T00:00:00+0200", IMPORTE_ADJUDICACION_SIN_IVA="27843.91",
                  IMPORTE_ADJUDICACION_CON_IVA="", NIF_ADJUDICATARIO="A82850611;"),
        fila_base(540002, FECHA_ADJUDICACION="2023-09-16T23:00:00+0100"),
    ], como_zip=True)
    fz, _, iz = ma.lee_fichero_andalucia(ruta_zip, DESDE)
    comprueba("zip leído sin descomprimir a disco", iz["zip"], True)
    comprueba("ISO +0200 a las 00:00 del primer día: dentro (literal)", "and:540001" in fz, True)
    comprueba("ISO 23:00 del día anterior: fuera (literal, no se pasa a UTC)", "and:540002" in fz, False)
    comprueba("importe con punto", fz["and:540001"]["importe_sin_iva"], Decimal("27843.91"))
    comprueba("con IVA vacío en 2023 -> null (no se deriva del sin IVA)", fz["and:540001"]["importe_con_iva"], None)
    comprueba("NIF con ';' final", fz["and:540001"]["cifs_adjudicatarios"], ["A82850611"])

    # 2026: UTF-8 sin BOM.
    ruta_utf8 = escribe_csv(tmp, "menores_2026.csv", [fila_base(950001, ORGANO_CONTRATACION="Consejería de Educación")],
                            codificacion="utf-8")
    fu, _, iu = ma.lee_fichero_andalucia(ruta_utf8, DESDE)
    comprueba("UTF-8 detectado", iu["codificacion"], "utf-8")
    comprueba("tildes bien leídas en UTF-8", fu["and:950001"]["organo_contratacion"], "Consejería de Educación")

    # Multi-NIF en la fila.
    fm, _, im = ma.lee_fichero_andalucia(escribe_csv(tmp, "multi.csv", [
        fila_base(1020, NIF_ADJUDICATARIO="B12345678;A87654321")]), DESDE)
    comprueba("multi-NIF: cif_adjudicatario = el primero", fm["and:1020"]["cif_adjudicatario"], "B12345678")
    comprueba("multi-NIF: cifs ordenados", fm["and:1020"]["cifs_adjudicatarios"], ["A87654321", "B12345678"])
    comprueba("multi-NIF: n_adjudicatarios", fm["and:1020"]["n_adjudicatarios"], 2)
    comprueba("multi-NIF: contado", im["nif"]["filas_multi"], 1)
    fv, _, _ = ma.lee_fichero_andalucia(escribe_csv(tmp, "vacio.csv", [fila_base(1021, NIF_ADJUDICATARIO="")]), DESDE)
    comprueba("sin NIF: n_adjudicatarios null y cifs []",
              (fv["and:1021"]["n_adjudicatarios"], fv["and:1021"]["cifs_adjudicatarios"]), (None, []))

    # Cabecera distinta: se para ese fichero.
    malo = Path(tmp) / "malo.csv"
    malo.write_bytes(b"ID|OTRA\r\n1|2\r\n")
    lanza("cabecera distinta -> ErrorFormato", lambda: ma.lee_fichero_andalucia(malo, DESDE), ma.ErrorFormato)

    # El mismo ID en dos ficheros de la misma ejecución: gana el primero y se cuenta.
    vistos = set()
    ma.lee_fichero_andalucia(escribe_csv(tmp, "a.csv", [fila_base(2001)]), DESDE, vistos)
    fb2, _, ib2 = ma.lee_fichero_andalucia(escribe_csv(tmp, "b.csv", [fila_base(2001)]), DESDE, vistos)
    comprueba("ID ya visto en otro fichero: fuera y contado", ("and:2001" in fb2, ib2["ids_ya_en_otro_fichero"]), (False, 1))

# ---------------------------------------------------------------------------
print("\n== CPV del portal ==")
comprueba("8 dígitos sin repetir, en su orden",
          ma.cpv_de_hit({"codigosCpv": [{"codigo": "33600000-6"}, {"codigo": "33600000-6"},
                                        {"codigo": "33141000-0", "denominacion": "x"}]}),
          ["33600000", "33141000"])
comprueba("sin codigosCpv -> []", ma.cpv_de_hit({"idExpediente": 1}), [])
comprueba("código corto se ignora", ma.cpv_de_hit({"codigosCpv": [{"codigo": "3360"}]}), [])


class RespuestaFalsa:
    def __init__(self, status=200, datos=None, cabeceras=None):
        self.status_code = status
        self._datos = datos
        self.headers = cabeceras or {}
        self.text = json.dumps(datos, default=str)

    def json(self):
        return self._datos


class PortalFalso:
    """Primer lote bien (con un contrato sin CPV), segundo lote siempre 500."""

    def __init__(self):
        self.peticiones = 0

    def post(self, url, headers=None, data=None, timeout=None):
        self.peticiones += 1
        ids = json.loads(data)["query"]["ids"]["values"]
        if "1" in ids:
            hits = [{"_id": i, "_source": {"codigosCpv": [{"codigo": "50000000-5"}] if i != "2" else []}} for i in ids]
            return RespuestaFalsa(200, {"hits": {"hits": hits}})
        return RespuestaFalsa(500, {"error": "x"})


filas_cpv = {f"and:{i}": {"licitacion_id": f"and:{i}", "cpv": None} for i in list(range(1, 1001)) + [5000]}
portal = PortalFalso()
viejo_lote, ma.TAM_LOTE_CPV = ma.TAM_LOTE_CPV, 1000
res_cpv = ma.enriquece_cpv(filas_cpv, sesion=portal, pausa=0, intentos=1)
ma.TAM_LOTE_CPV = viejo_lote
comprueba("CPV: lote bueno rellena", filas_cpv["and:1"]["cpv"], ["50000000"])
comprueba("CPV: contrato sin CPV en el portal queda null (no borra)", filas_cpv["and:2"]["cpv"], None)
comprueba("CPV: lote fallido deja null y no tumba nada", filas_cpv["and:5000"]["cpv"], None)
comprueba("CPV: recuentos", (res_cpv["pedidos"], res_cpv["con_cpv"], res_cpv["lotes_fallidos"]), (1001, 999, 1))


class PortalQueIgnora:
    """Lo que pasó de verdad en F1: HTTP 200 con 1.000 contratos cualquiera (ninguno pedido)."""

    def __init__(self):
        self.peticiones = 0

    def post(self, url, headers=None, data=None, timeout=None):
        self.peticiones += 1
        hits = [{"_id": str(534331 + i), "_source": {"codigosCpv": [{"codigo": "92000000-1"}]}} for i in range(1000)]
        return RespuestaFalsa(200, {"hits": {"total": {"value": 948458}, "hits": hits}})


filas_ign = {f"and:{i}": {"licitacion_id": f"and:{i}", "cpv": None} for i in range(1, 3001)}
portal_ign = PortalQueIgnora()
res_ign = ma.enriquece_cpv(filas_ign, sesion=portal_ign, pausa=0, intentos=1)
comprueba("portal que ignora la consulta: ningún CPV ajeno asignado",
          sum(1 for f in filas_ign.values() if f["cpv"] is not None), 0)
comprueba("portal que ignora la consulta: se corta tras la PRIMERA petición", portal_ign.peticiones, 1)
comprueba("portal que ignora la consulta: se dice en el informe",
          (res_ign.get("consulta_ignorada"), res_ign.get("hits_no_pedidos"), res_ign.get("lotes_no_pedidos")), (1, 1000, 2))
comprueba("portal que ignora la consulta: 'pedidos' cuenta solo lo pedido",
          (res_ign.get("pedidos"), res_ign.get("filas_no_pedidas")), (1000, 2000))

# ---------------------------------------------------------------------------
print("\n== lotes para la RPC ==")
muchas = [{"licitacion_id": f"and:{i}", "objeto": "x", "fuente": "andalucia",
           "importe_sin_iva": Decimal("27843.91"), "cifs_adjudicatarios": []} for i in range(1203)]
lotes = ma.lotes_rpc(muchas)
comprueba("1.203 filas -> lotes de 500, 500 y 203", [len(l) for l in lotes], [500, 500, 203])
comprueba("ningún lote con claves repetidas",
          all(len({f["licitacion_id"] for f in l}) == len(l) for l in lotes), True)
comprueba("las mismas claves json en TODAS las filas",
          {tuple(f.keys()) for l in lotes for f in l}, {ma.CAMPOS_RPC})
comprueba("Decimal -> número exacto en json", json.dumps(lotes[0][0]["importe_sin_iva"]), "27843.91")
lanza("claves repetidas -> no se generan lotes",
      lambda: ma.lotes_rpc(muchas[:3] + muchas[:1]), ValueError)
lanza("una fila estatal no sale nunca hacia la RPC",
      lambda: ma.fila_para_rpc({"licitacion_id": "and:1", "fuente": "estatal"}), ValueError)


class SesionCarga:
    """menores_carga_lote de mentira: cuenta lotes y devuelve lo que devolvería la RPC."""

    def __init__(self, rechaza=False):
        self.lotes = []
        self.rechaza = rechaza

    def post(self, url, headers=None, params=None, data=None, timeout=None):
        filas = json.loads(data)["p_filas"]
        self.lotes.append(len(filas))
        if self.rechaza:
            return RespuestaFalsa(400, {"code": "P0001", "message": "menores_carga_lote: el lote trae claves repetidas"})
        return RespuestaFalsa(200, {"recibidas": len(filas), "insertadas": len(filas) - 1,
                                    "actualizadas": 1, "iguales": 0, "conflictos": 0})


sesion_carga = SesionCarga()
suma = ma.carga_filas(ma.ClienteSupabase("https://ejemplo.invalido", "x", sesion_carga),
                      {f["licitacion_id"]: f for f in muchas}, "prueba")
comprueba("carga: 3 llamadas a la RPC (500, 500, 203)", sesion_carga.lotes, [500, 500, 203])
comprueba("carga: suma lo que devuelve la RPC",
          (suma["recibidas"], suma["insertadas"], suma["actualizadas"], suma["lotes"]), (1203, 1200, 3, 3))
sesion_rechazo = SesionCarga(rechaza=True)
lanza("carga: un 4xx de datos para la carga",
      lambda: ma.carga_filas(ma.ClienteSupabase("https://ejemplo.invalido", "x", sesion_rechazo),
                             {f["licitacion_id"]: f for f in muchas}, "prueba"), ma.ErrorCarga)
comprueba("carga: tras un 4xx no se reintenta ni se sigue con otros lotes", sesion_rechazo.lotes, [500])

# ---------------------------------------------------------------------------
print("\n== estado de los recursos ==")
recurso = {"resource_id": "r1", "last_modified": "2026-07-07T11:31:44", "size": 224440205}
estado = {"andalucia": {"r1": {"last_modified": "2026-07-07T11:31:44", "size": 224440205, "procesado": "x"}}}
comprueba("mismo last_modified y size -> sin cambios", ma.recurso_sin_cambios(estado, "andalucia", recurso), True)
comprueba("otro size -> hay que procesar",
          ma.recurso_sin_cambios(estado, "andalucia", {**recurso, "size": 1}), False)
comprueba("otro last_modified -> hay que procesar",
          ma.recurso_sin_cambios(estado, "andalucia", {**recurso, "last_modified": "2026-09-01"}), False)
comprueba("recurso nuevo -> hay que procesar", ma.recurso_sin_cambios({}, "andalucia", recurso), False)
comprueba("URL por el host público (el interno rechaza conexiones)",
          ma.url_descarga("pkg", "res", "https://gdc-pdpopendata-ckan.paas.junta-andalucia.es/datosabiertos/"
                                        "portal/dataset/pkg/resource/res/download/menores_2025_v1.csv"),
          "https://www.juntadeandalucia.es/datosabiertos/portal/dataset/pkg/resource/res/download/menores_2025_v1.csv")

# ---------------------------------------------------------------------------
print("\n== regla de gemelas (casos inyectados) ==")
VALIDOS = {"SERVICIO ANDALUZ DE SALUD", "CONSEJERIA DE SALUD Y CONSUMO"}


def a_(id_, **extra):
    base = {"licitacion_id": f"and:{id_}", "cifs_adjudicatarios": ["B12345678"], "adjudicatario": "Guantes del Sur SL",
            "importe_sin_iva": Decimal("100.00"), "importe_con_iva": Decimal("121.00"),
            "fecha_adjudicacion": "2025-03-10", "organo_contratacion": "Servicio Andaluz de Salud"}
    base.update(extra)
    return base


def e_(id_, **extra):
    base = {"licitacion_id": f"https://contrataciondelestado.es/{id_}", "cif_adjudicatario": "B12345678",
            "adjudicatario": "GUANTES DEL SUR, S.L.", "importe_sin_iva": 100.0, "fecha_adjudicacion": "2025-03-12",
            "organo_contratacion": "Servicio Andaluz de Salud."}
    base.update(extra)
    return base


comprueba("positivo claro", ma.es_gemela(a_(1), e_(1), VALIDOS), True)
comprueba("admite ES+NIF en la estatal", ma.es_gemela(a_(1), e_(1, cif_adjudicatario="ESB12345678"), VALIDOS), True)
comprueba("órgano estatal que no está en la lista", ma.es_gemela(a_(1), e_(1, organo_contratacion="Ayuntamiento de Écija"), VALIDOS), False)
comprueba("IVA cruzado (sin IVA estatal = con IVA autonómico)", ma.es_gemela(a_(1), e_(1, importe_sin_iva=121.0), VALIDOS), False)
comprueba("importe a 1 céntimo sí", ma.es_gemela(a_(1), e_(1, importe_sin_iva=100.01), VALIDOS), True)
comprueba("importe a 2 céntimos no", ma.es_gemela(a_(1), e_(1, importe_sin_iva=100.02), VALIDOS), False)
comprueba("sin importe estatal no", ma.es_gemela(a_(1), e_(1, importe_sin_iva=None), VALIDOS), False)
comprueba("15 días sí", ma.es_gemela(a_(1), e_(1, fecha_adjudicacion="2025-03-25"), VALIDOS), True)
comprueba("16 días no", ma.es_gemela(a_(1), e_(1, fecha_adjudicacion="2025-03-26"), VALIDOS), False)
comprueba("fecha estatal nula no", ma.es_gemela(a_(1), e_(1, fecha_adjudicacion=None), VALIDOS), False)
comprueba("otro CIF no", ma.es_gemela(a_(1), e_(1, cif_adjudicatario="B99999999"), VALIDOS), False)
persona = dict(cifs_adjudicatarios=["***4567**"], adjudicatario="GARCIA PEREZ JUAN")
comprueba("máscara igual (estatal con DNI completo) y nombre parecido: sí",
          ma.es_gemela(a_(2, **persona), e_(2, cif_adjudicatario="12345678Z", adjudicatario="Juan García Pérez"), VALIDOS), True)
comprueba("máscara igual en las dos y nombre parecido: sí",
          ma.es_gemela(a_(2, **persona), e_(2, cif_adjudicatario="***4567**", adjudicatario="JUAN GARCIA PEREZ"), VALIDOS), True)
comprueba("máscara igual pero nombre NO parecido: no",
          ma.es_gemela(a_(2, **persona), e_(2, cif_adjudicatario="***4567**", adjudicatario="MARIA LOPEZ RUIZ"), VALIDOS), False)
comprueba("máscara distinta: no",
          ma.es_gemela(a_(2, **persona), e_(2, cif_adjudicatario="***9999**", adjudicatario="JUAN GARCIA PEREZ"), VALIDOS), False)
# DNI completo en las dos (el andaluz, en memoria): igualdad EXACTA, sin máscara ni nombre.
comprueba("DNI completo DISTINTO con los mismos dígitos 4-7 y el mismo nombre: no (antes se borraba)",
          ma.es_gemela(a_(4, cifs_adjudicatarios=["***4567**"], adjudicatario="GARCIA PEREZ, JUAN ANTONIO"),
                       e_(4, cif_adjudicatario="99945678A", adjudicatario="GARCIA PEREZ JUAN ANTONIO"),
                       VALIDOS, ["12345678Z"]), False)
comprueba("mismo DNI completo con un nombre de pila de más (Jaccard 0,75): sí",
          ma.es_gemela(a_(4, cifs_adjudicatarios=["***4567**"], adjudicatario="GARCIA PEREZ, JUAN ANTONIO"),
                       e_(4, cif_adjudicatario="12345678Z", adjudicatario="JUAN GARCIA PEREZ"),
                       VALIDOS, ["12345678Z"]), True)
comprueba("DNI completo andaluz frente a máscara estatal: máscara + nombre",
          ma.es_gemela(a_(4, cifs_adjudicatarios=["***4567**"], adjudicatario="GARCIA PEREZ JUAN"),
                       e_(4, cif_adjudicatario="***4567**", adjudicatario="JUAN GARCIA PEREZ"),
                       VALIDOS, ["12345678Z"]), True)
comprueba("máscara sin la ventana en la estatal ('12****78Z') nunca identifica",
          ma.es_gemela(a_(4, **persona), e_(4, cif_adjudicatario="12****78Z", adjudicatario="JUAN GARCIA PEREZ"), VALIDOS), False)
comprueba("dos '*********' con el mismo nombre no son la misma persona",
          ma.identidad({"cifs_adjudicatarios": ["*********"], "adjudicatario": "JUAN GARCIA PEREZ"},
                       {"cif_adjudicatario": "*********", "adjudicatario": "JUAN GARCIA PEREZ"}), False)
gem, _ = ma.empareja([a_(5, cifs_adjudicatarios=["***4567**"], adjudicatario="GARCIA PEREZ JUAN")],
                     [e_(5, cif_adjudicatario="99945678A", adjudicatario="GARCIA PEREZ JUAN")],
                     VALIDOS, {"and:5": ["12345678Z"]})
comprueba("empareja pasa los originales a la regla (DNI distinto, misma máscara: nada)", gem, [])

VALIDOS_ALIAS = VALIDOS | {"SAS SERVICIOS CENTRALES"}
comprueba("órgano estatal por ALIAS: sí",
          ma.es_gemela(a_(3), e_(3, organo_contratacion="SAS - Servicios Centrales"), VALIDOS_ALIAS), True)

gem, rev = ma.empareja([a_(10)], [e_(10)], VALIDOS)
comprueba("emparejamiento 1:1 -> gemela", (gem, rev), ([("and:10", e_(10)["licitacion_id"])], []))
gem, rev = ma.empareja([a_(11)], [e_(11), e_(12, fecha_adjudicacion="2025-03-05")], VALIDOS)
comprueba("1:N (una autonómica, dos estatales) -> revisión, nada se toca", (len(gem), len(rev)), (0, 2))
gem, rev = ma.empareja([a_(13), a_(14)], [e_(13)], VALIDOS)
comprueba("N:1 (dos autonómicas, una estatal) -> revisión, nada se toca", (len(gem), len(rev)), (0, 2))
gem, _ = ma.empareja([a_(15, **persona)], [e_(15, cif_adjudicatario="12345678Z", adjudicatario="JUAN GARCIA PEREZ")],
                     VALIDOS, {"and:15": ["12345678Z"]})
comprueba("la estatal con DNI completo se encuentra (índice por máscara)", len(gem), 1)
comprueba("claves de búsqueda: NIF, ES+NIF y máscara",
          ma.claves_busqueda(a_(16, cifs_adjudicatarios=["***4567**"]), ["12345678Z"]),
          {"***4567**", "12345678Z", "ES12345678Z"})


# ---------------------------------------------------------------------------
class SesionFalsa:
    """Imita requests.Session contra PostgREST y cuenta lo que se pide."""

    def __init__(self, organos_estatales, organos_fuente, candidatas=(), en_base=(), conteos=None):
        self.organos = {"estatal": list(organos_estatales), "andalucia": list(organos_fuente)}
        self.candidatas = list(candidatas)
        self.en_base = list(en_base)
        self.conteos = conteos or {}               # {órgano tal cual: filas} para Prefer count=exact
        self.llamadas = []

    def post(self, url, headers=None, params=None, data=None, timeout=None):
        self.llamadas.append(("POST", url, params))
        if url.endswith("rpc/menores_organos"):
            fuente = json.loads(data)["p_fuente"]
            desde = (params or {}).get("offset", 0)
            return RespuestaFalsa(200, self.organos[fuente][desde:desde + ma.PAGINA])
        return RespuestaFalsa(404, {"code": "PGRST202"})

    def get(self, url, headers=None, params=None, data=None, timeout=None):
        self.llamadas.append(("GET", url, params))
        p = dict(params)
        filas = self.candidatas if p.get("fuente") == "eq.estatal" else self.en_base
        if "count=exact" in (headers or {}).get("Prefer", ""):
            organo = p.get("organo_contratacion", "")[5:-2]          # in.("X") -> X
            return RespuestaFalsa(200, filas[:1], {"Content-Range": f"0-0/{self.conteos.get(organo, len(filas))}"})
        return RespuestaFalsa(200, filas)          # menos de una página: no hay segunda

    def delete(self, url, headers=None, params=None, data=None, timeout=None):
        self.llamadas.append(("DELETE", url, params))
        return RespuestaFalsa(204, None, {"Content-Range": "*/1"})

    def cuenta(self, metodo, trozo=""):
        return sum(1 for m, u, p in self.llamadas if m == metodo and trozo in u)


print("\n== puerta de órganos ==")
sesion = SesionFalsa(["MINISTERIO DE DEFENSA", "Ayuntamiento de Écija"], [])
cliente = ma.ClienteSupabase("https://ejemplo.invalido", "clave-de-mentira", sesion)
puerta = ma.PuertaOrganos(cliente, "andalucia", {})
filas_p = {"and:1": a_(1)}
info_p = ma.resuelve_gemelas(cliente, puerta, filas_p, {}, cargar=True)
comprueba("puerta vacía: 0 órganos en común", info_p["organos_en_comun"], 0)
comprueba("puerta vacía: NINGUNA consulta por CIF", sesion.cuenta("GET"), 0)
comprueba("puerta vacía: ningún borrado", sesion.cuenta("DELETE"), 0)
comprueba("puerta vacía: solo las RPC de órganos (estatal: página + página vacía; fuente: vacía)",
          sesion.cuenta("POST", "menores_organos"), 3)
comprueba("puerta vacía: la fila sigue para cargar", "and:1" in filas_p, True)
info_b = ma.gemelas_en_base(cliente, puerta, set(), cargar=True)
comprueba("sentido contrario con puerta vacía: tampoco consulta", (info_b["filas_revisadas"], sesion.cuenta("GET")), (0, 0))
comprueba("sin credenciales la puerta se salta y se dice",
          "sin credenciales" in ma.resuelve_gemelas(None, None, {}, {}, cargar=False)["puerta"], True)

print("\n== gemelas con la puerta abierta (sesión falsa) ==")
for cargar in (False, True):
    sesion = SesionFalsa(["Servicio Andaluz de Salud", "MINISTERIO DE DEFENSA"], [],
                         candidatas=[e_(20), e_(99, cif_adjudicatario="B99999999")])
    cliente = ma.ClienteSupabase("https://ejemplo.invalido", "clave-de-mentira", sesion)
    puerta = ma.PuertaOrganos(cliente, "andalucia", {})
    filas_g = {"and:20": a_(20), "and:21": a_(21, organo_contratacion="Consejería de Cultura")}
    info_g = ma.resuelve_gemelas(cliente, puerta, filas_g, {}, cargar=cargar)
    modo = "carga" if cargar else "simulación"
    comprueba(f"{modo}: 1 órgano en común", info_g["organos_en_comun"], 1)
    comprueba(f"{modo}: solo se revisa la fila del órgano en común", info_g["filas_revisadas"], 1)
    comprueba(f"{modo}: la gemela no se carga", ("and:20" in filas_g, "and:21" in filas_g), (False, True))
    borrados = [p for m, u, p in sesion.llamadas if m == "DELETE"]
    if cargar:
        comprueba("carga: borra la gemela CON el filtro de fuente",
                  borrados, [[("licitacion_id", 'in.("and:20")'), ("fuente", "eq.andalucia")]])
    else:
        comprueba("simulación: no borra nada", borrados, [])
    consulta = next(p for m, u, p in sesion.llamadas if m == "GET" and ("fuente", "eq.estatal") in p)
    comprueba(f"{modo}: la consulta por CIF lleva fuente estatal y fecha ±15 días",
              [v for k, v in consulta if k == "fecha_adjudicacion"], ["gte.2025-02-23", "lte.2025-03-25"])
lanza("borra_autonomicas no borra nunca la estatal",
      lambda: ma.borra_autonomicas(cliente, "estatal", {"x"}), ValueError)

print("\n== alias en la puerta ==")
sesion = SesionFalsa(["SAS - Servicios Centrales"], ["Servicio Andaluz de Salud"])
cliente = ma.ClienteSupabase("https://ejemplo.invalido", "clave-de-mentira", sesion)
puerta = ma.PuertaOrganos(cliente, "andalucia", {"SAS SERVICIOS CENTRALES": "SERVICIO ANDALUZ DE SALUD"})
interseccion, validos, objetivo = puerta.calcula()
comprueba("alias: el órgano estatal entra en la intersección", interseccion, {"SAS SERVICIOS CENTRALES"})
comprueba("alias: se revisan las filas del órgano de la fuente", objetivo, {"SERVICIO ANDALUZ DE SALUD"})
comprueba("valor_rpc admite texto o {nombre: texto}",
          (ma.valor_rpc("A"), ma.valor_rpc({"menores_organos": "A"})), ("A", "A"))

print("\n== cliente PostgREST ==")
lanza("filas() no admite un 'limit' de quien llama",
      lambda: cliente.filas("menores", [("select", "x"), ("limit", 1)], "x"), ValueError)
comprueba("en_lista entrecomilla (':' y ',' son sintaxis de PostgREST)",
          ma.en_lista(["and:1", 'Órgano "raro", S.A.']), 'in.("and:1","Órgano \\"raro\\", S.A.")')
comprueba("error sin mensaje cuando la petición llevaba NIF",
          ma._motivo(RespuestaFalsa(400, {"code": "22P02", "message": "valor 12345678Z"}), False), "[22P02]")

print("\n== consultas de candidatas: sin DNI completos en la URL ==")
comprueba("patrones de una máscara de DNI", ma.patrones_persona("***4567**"), ["___4567__", "ES___4567__"])
comprueba("patrones de una máscara de NIE", ma.patrones_persona("****4567*"), ["____4567_", "ES____4567_"])
comprueba("el patrón casa con el DNI completo y con su máscara (LIKE: '_' = un carácter)",
          [bool(__import__("re").fullmatch(ma.patrones_persona("***4567**")[0].replace("_", "."), x))
           for x in ("12345678Z", "***4567**", "B12345678")], [True, True, False])
comprueba("sin máscara completa no hay patrón", ma.patrones_persona("*********"), [])
sesion = SesionFalsa(["Servicio Andaluz de Salud"], [])
cliente = ma.ClienteSupabase("https://ejemplo.invalido", "clave-de-mentira", sesion)
filas_q = [a_(30),
           a_(31, cifs_adjudicatarios=["***4567**"], adjudicatario="GARCIA PEREZ JUAN"),
           a_(33, cifs_adjudicatarios=["***9999**"], adjudicatario="LOPEZ RUIZ ANA", fecha_adjudicacion="2025-03-20"),
           a_(32, cifs_adjudicatarios=["****4567*"], adjudicatario="SMITH JOHN", fecha_adjudicacion="2025-06-10")]
_, n_q = ma.busca_candidatas(cliente, filas_q, {"and:31": ["12345678Z"]})
gets = [p for m, u, p in sesion.llamadas if m == "GET"]
comprueba("ninguna consulta lleva el DNI completo que solo está en memoria",
          [p for p in gets if "12345678Z" in json.dumps(p)], [])
comprueba("3 consultas: 1 por CIF exacto y 2 por patrón (fechas a más de 15 días)", (n_q, len(gets)), (3, 3))
comprueba("CIF jurídico por igualdad, con y sin 'ES'",
          [v for p in gets for k, v in p if k == "cif_adjudicatario"], ['in.("B12345678","ESB12345678")'])
comprueba("personas por patrón, agrupadas por fecha y con ±15 días",
          [([v for k, v in p if k == "or"], [v for k, v in p if k == "fecha_adjudicacion"]) for p in gets
           if any(k == "or" for k, _ in p)],
          [(["(cif_adjudicatario.like.ES___4567__,cif_adjudicatario.like.ES___9999__,"
             "cif_adjudicatario.like.___4567__,cif_adjudicatario.like.___9999__)"], ["gte.2025-02-23", "lte.2025-04-04"]),
           (["(cif_adjudicatario.like.ES____4567_,cif_adjudicatario.like.____4567_)"], ["gte.2025-05-26", "lte.2025-06-25"])])
comprueba("todas las consultas de candidatas con fuente estatal y un solo limit",
          all(("fuente", "eq.estatal") in p and sum(1 for k, _ in p if k == "limit") == 1 for p in gets), True)


class SesionPaginas:
    """PostgREST que devuelve 'total' claves ordenadas, de 1.000 en 1.000, respetando licitacion_id=gt."""

    def __init__(self, total):
        self.claves = [f"and:{i:05d}" for i in range(total)]
        self.llamadas = []

    def get(self, url, headers=None, params=None, data=None, timeout=None):
        self.llamadas.append(params)
        desde = [v[3:] for k, v in params if k == "licitacion_id" and v.startswith("gt.")]
        resto = [c for c in self.claves if not desde or c > desde[0]]
        limite = next(int(v) for k, v in params if k == "limit")
        return RespuestaFalsa(200, [{"licitacion_id": c} for c in resto[:limite]], {"Content-Range": "0-0/2003"})


print("\n== paginación por clave (sin offset) ==")
sp = SesionPaginas(2003)
cp = ma.ClienteSupabase("https://ejemplo.invalido", "x", sp)
todas = cp.filas("menores", [("select", "licitacion_id"), ("fuente", "eq.andalucia")], "prueba")
comprueba("recoge todas las filas, sin repetir", (len(todas), len({f["licitacion_id"] for f in todas})), (2003, 2003))
comprueba("3 páginas", len(sp.llamadas), 3)
comprueba("ninguna página usa offset", any(k == "offset" for p in sp.llamadas for k, _ in p), False)
comprueba("la 2ª y 3ª página siguen desde la última clave",
          [[v for k, v in p if k == "licitacion_id"] for p in sp.llamadas], [[], ["gt.and:00999"], ["gt.and:01999"]])
comprueba("cada página: un limit y order por clave",
          all(sum(1 for k, _ in p if k == "limit") == 1 and ("order", "licitacion_id") in p for p in sp.llamadas), True)
lanza("filas() no admite 'offset' ni 'order' de quien llama",
      lambda: cp.filas("menores", [("select", "x"), ("order", "fecha")], "x"), ValueError)
comprueba("cuenta() lee el total de Content-Range", cp.cuenta("menores", [("select", "licitacion_id")], "x"), 2003)

print("\n== sentido contrario: tope por órgano y años ya leídos ==")
fila_cultura = a_(40, organo_contratacion="Consejería de Cultura")
sesion = SesionFalsa(["Servicio Andaluz de Salud", "Consejería de Cultura"],
                     ["Servicio Andaluz de Salud", "Consejería de Cultura"],
                     candidatas=[e_(40, organo_contratacion="Consejería de Cultura")], en_base=[fila_cultura],
                     conteos={"Servicio Andaluz de Salud": 300000, "Consejería de Cultura": 1})
cliente = ma.ClienteSupabase("https://ejemplo.invalido", "clave-de-mentira", sesion)
puerta = ma.PuertaOrganos(cliente, "andalucia", {})
info_t = ma.gemelas_en_base(cliente, puerta, set(), cargar=True)
lecturas = [p for m, u, p in sesion.llamadas if m == "GET" and ("fuente", "eq.andalucia") in p
            and ("limit", 1) not in p]
comprueba("el órgano por encima del tope no se lee, se avisa",
          (info_t["organos_sin_recorrer"], info_t["detalle_organos_sin_recorrer"]),
          (1, [{"organo": "SERVICIO ANDALUZ DE SALUD", "filas": 300000}]))
comprueba("solo se leen las filas del órgano pequeño",
          [v for p in lecturas for k, v in p if k == "organo_contratacion"], ['in.("Consejería de Cultura")'])
comprueba("la gemela del órgano pequeño se encuentra y se borra", (info_t["gemelas"], info_t["gemelas_borradas"]), (1, 1))
sesion = SesionFalsa(["Consejería de Cultura"], ["Consejería de Cultura"], en_base=[fila_cultura])
cliente = ma.ClienteSupabase("https://ejemplo.invalido", "clave-de-mentira", sesion)
info_l = ma.gemelas_en_base(cliente, ma.PuertaOrganos(cliente, "andalucia", {}), set(), cargar=True, todos_leidos=True)
comprueba("todos los años leídos en la ejecución: no se lee nada de la base",
          (sesion.cuenta("GET"), "omitido" in info_l), (0, True))


class RespuestaDescarga:
    def __init__(self, status, datos, cabeceras, corte):
        self.status_code, self._datos, self.headers, self._corte = status, datos, cabeceras, corte

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk_size=1):
        enviado = 0
        for i in range(0, len(self._datos), 1000):
            if enviado >= self._corte:
                raise ma.requests.ConnectionError("corte 10054")
            enviado += len(self._datos[i:i + 1000])
            yield self._datos[i:i + 1000]


class ServidorDescarga:
    """Corta cada respuesta a los 'corte' bytes; con o sin respetar Range."""

    def __init__(self, datos, respeta_range, corte=3000):
        self.datos, self.respeta_range, self.corte, self.peticiones = datos, respeta_range, corte, 0

    def get(self, url, headers=None, stream=None, timeout=None):
        self.peticiones += 1
        rango = (headers or {}).get("Range")
        if rango and self.respeta_range:
            inicio = int(rango.split("=")[1].rstrip("-"))
            return RespuestaDescarga(206, self.datos[inicio:], {
                "Content-Range": f"bytes {inicio}-{len(self.datos) - 1}/{len(self.datos)}"}, self.corte)
        return RespuestaDescarga(200, self.datos, {"Content-Length": str(len(self.datos))}, self.corte)


print("\n== descarga reanudable (servidor falso, sin red) ==")
datos_d = bytes(range(256)) * 39 + b"x" * 16          # 10.000 bytes
get_real, sleep_real = ma.requests.get, ma.time.sleep
ma.time.sleep = lambda s: None
try:
    with tempfile.TemporaryDirectory() as tmp:
        srv = ServidorDescarga(datos_d, respeta_range=True)
        ma.requests.get = srv.get
        destino = ma.descarga_reanudable("https://x/f.csv", Path(tmp) / "a.csv", len(datos_d), intentos=3)
        comprueba("con Range y cortes: fichero idéntico en 4 peticiones",
                  (destino.read_bytes() == datos_d, srv.peticiones), (True, 4))
        srv = ServidorDescarga(datos_d, respeta_range=False)
        ma.requests.get = srv.get
        try:
            ma.descarga_reanudable("https://x/f.csv", Path(tmp) / "b.csv", len(datos_d), intentos=3)
            comprueba("servidor que ignora Range y corta: termina con error", "no lanzó", "RuntimeError")
        except RuntimeError as err:
            comprueba("servidor que ignora Range y corta: para tras 3 vueltas sin pasar del máximo",
                      (srv.peticiones, "ignora Range" in str(err)), (4, True))
finally:
    ma.requests.get, ma.time.sleep = get_real, sleep_real

print("\n== CKAN: size como texto ==")
get_ckan = ma._get_con_reintentos
ma._get_con_reintentos = lambda url, params=None: RespuestaFalsa(200, {"success": True, "result": {
    "id": "pkg", "resources": [{"id": ma.RECURSOS_VERIFICADOS[2025][1], "format": "CSV", "size": "224440205",
                                "last_modified": "2026-07-07T11:31:44", "url": "https://h/download/m.csv"}]}})
try:
    comprueba("size en texto -> entero", ma.recurso_andalucia(2025)["size"], 224440205)
finally:
    ma._get_con_reintentos = get_ckan

print("\n== workflow ==")
yml = (Path(__file__).resolve().parent / ".github" / "workflows" / "menores_autonomicos.yml").read_text(encoding="utf-8")
dias_cron = {d for c in __import__("re").findall(r'cron:\s*"[^"]*\s(\S+)"', yml) for d in c.split(",")}
comprueba("programado solo en fin de semana (el Radar va de lunes a viernes)", dias_cron <= {"0", "6", "7"}, True)
comprueba("espera al Radar y a los backfill antes de cargar",
          all(x in yml for x in ("radar.yml", "backfill.yml", "actions: read")), True)

print("\n== el script no imprime NIF ==")
fuente = (Path(__file__).resolve().parent / "menores_autonomicos.py").read_text(encoding="utf-8")
comprueba("ningún print de cif/nif/adjudicatario",
          [l.strip() for l in fuente.splitlines() if "print(" in l and any(
              x in l for x in ("cif_adjudicatario", "originales", "adjudicatario']", 'adjudicatario"]'))], [])

print("\n" + (f"TODO OK ✔ ({len(BIEN)} de {len(BIEN)})" if not FALLOS
              else f"{len(BIEN)} bien y {len(FALLOS)} FALLOS: {FALLOS}"))
sys.exit(1 if FALLOS else 0)
