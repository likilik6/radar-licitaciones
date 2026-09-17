"""Pruebas deterministas de menores_nicho.py (sin red). Ejecutar: python test_menores_nicho.py"""
import json
import sys
import tempfile
from pathlib import Path

import yaml

import menores_nicho as mn

sys.stdout.reconfigure(encoding="utf-8")
OK, FALLOS = 0, []


def comprueba(nombre, condicion, detalle=""):
    global OK
    if condicion:
        OK += 1
    else:
        FALLOS.append(f"{nombre} {detalle}")
        print(f"FALLO: {nombre} {detalle}")


CRITERIOS = {
    "criticas": {"cpv": ["39811200", "90731100"], "palabras_clave": ["ionización", "ionización bipolar", "formaldehído"]},
    "a_revisar": {"cpv": ["9073", "39811200"], "palabras_clave": ["ventilación", "Purificador", "ionización"]},
    "pruebas": {"cpv": ["9091"], "palabras_clave": ["limpieza"]},
}

# --- nicho_de_criterios: criticas + a_revisar, sin tildes, sin repetir, nunca «pruebas»
cpv, palabras = mn.nicho_de_criterios(CRITERIOS)
comprueba("cpv sin repetir y sin pruebas", cpv == ["39811200", "90731100", "9073"], cpv)
comprueba("palabras sin tildes y sin repetir", palabras == ["ionizacion", "ionizacion bipolar", "formaldehido", "ventilacion", "purificador"], palabras)
comprueba("criterios vacíos", mn.nicho_de_criterios(None) == ([], []))

# --- cadena_websearch: frases entre comillas, lexemas sueltos, sin tildes ni comillas sueltas
comprueba("todos entre comillas", mn.cadena_websearch(["cámara de ionización", "dosimetría"]) == '"camara de ionizacion" or "dosimetria"')
comprueba("un '-' no funciona como negación", mn.cadena_websearch(["-cpap", "or"]) == '"-cpap" or "or"')
comprueba("comillas internas fuera", mn.cadena_websearch(['equipo "de" anestesia']) == '"equipo de anestesia"')
comprueba("vacíos fuera", mn.cadena_websearch(["", "  ", "cpap"]) == '"cpap"')

# --- aplica_exclusiones: ACOTADA por palabra; grupos ajenos al nicho se ignoran
grupos = [
    {"nombre": "a", "aplica_a": ["ionizacion"], "terminos": ["dosimetria", "camara de ionizacion"]},
    {"nombre": "b", "aplica_a": ["purificador"], "terminos": ["purificador de agua"]},
    {"nombre": "c", "aplica_a": ["purificador"], "terminos": ["adn"]},
    {"nombre": "d", "aplica_a": ["no_esta_en_el_nicho"], "terminos": ["x"]},
    {"nombre": "e", "aplica_a": ["ventilacion"], "terminos": []},
]
libres, excl = mn.aplica_exclusiones(palabras, grupos)
comprueba("libres: las que no tienen grupo (ionizacion bipolar sigue libre)",
          libres == ["ionizacion bipolar", "formaldehido", "ventilacion"], libres)
comprueba("excl en el orden del nicho", [e["kw"] for e in excl] == ["ionizacion", "purificador"], excl)
comprueba("dos grupos sobre la misma palabra", excl[1]["excluye"] == ['"purificador de agua"', '"adn"'], excl[1])
comprueba("grupo sin términos no convierte la palabra", "ventilacion" in libres)

# --- nicho(): lee el JSON (con BOM), ignora inactivos; si falla, avisa y sale sin exclusiones
with tempfile.TemporaryDirectory() as tmp:
    ruta = Path(tmp) / "excl.json"
    ruta.write_text(json.dumps({"grupos": [
        {"nombre": "a", "activo": True, "aplica_a": ["ventilación"], "terminos": ["ventilación no invasiva", "cpap"]},
        {"nombre": "b", "activo": False, "aplica_a": ["formaldehido"], "terminos": ["frascos"]},
    ]}, ensure_ascii=False), encoding="utf-8-sig")
    n = mn.nicho(CRITERIOS, ruta)
    comprueba("nicho con JSON con BOM", n["aviso"] is None and n["excl"] == [{"kw": "ventilacion", "excluye": ['"ventilacion no invasiva" or "cpap"']}], n)
    comprueba("inactivo ignorado", "formaldehido" in n["palabras"], n["palabras"])
    roto = Path(tmp) / "roto.json"
    roto.write_text("{no es json", encoding="utf-8")
    n2 = mn.nicho(CRITERIOS, roto)
    comprueba("JSON roto: aviso y sin exclusiones", n2["aviso"] and n2["excl"] == [] and "ventilacion" in n2["palabras"], n2)
    n3 = mn.nicho(CRITERIOS, Path(tmp) / "no_existe.json")
    comprueba("JSON ausente: aviso y sin exclusiones", n3["aviso"] and n3["excl"] == [], n3)

    # Formas equivocadas (revisión): nunca tumban la publicación ni cambian el nicho en silencio
    for contenido, nombre in (("[]", "raíz lista"), ("null", "raíz null"), ('{"grupos": 5}', "grupos número")):
        malo = Path(tmp) / "malo.json"
        malo.write_text(contenido, encoding="utf-8")
        nm = mn.nicho(CRITERIOS, malo)
        comprueba(f"JSON {nombre}: aviso y sin exclusiones", nm["aviso"] and nm["excl"] == [], nm)
    raro = Path(tmp) / "raro.json"
    raro.write_text(json.dumps({"grupos": [
        {"nombre": "cadena", "activo": True, "aplica_a": ["ventilacion"], "terminos": "respirador"},
        {"nombre": "activo_texto", "activo": "true", "aplica_a": ["ventilacion"], "terminos": ["cpap"]},
        {"nombre": "aplica_texto", "activo": True, "aplica_a": "ventilacion", "terminos": ["cpap"]},
        {"nombre": "bueno", "activo": True, "aplica_a": ["formaldehido"], "terminos": ["frascos"]},
    ]}), encoding="utf-8")
    nr = mn.nicho(CRITERIOS, raro)
    comprueba("grupos mal escritos: se ignoran con aviso (3)", nr["aviso"] and nr["aviso"].count("mal escrito") == 3, nr["aviso"])
    comprueba("grupos mal escritos: el bueno sí se aplica y no hay letras sueltas",
              nr["excl"] == [{"kw": "formaldehido", "excluye": ['"frascos"']}], nr["excl"])

# --- sql_condicion: misma forma que el or() de menores_api.js
n = {"cpv": ["9073"], "palabras": "calidad del aire", "excl": [{"kw": "ventilacion", "excluye": ["cpap", '"ventilacion no invasiva"']}]}
cond, params = mn.sql_condicion(n)
comprueba("sql: una rama por pieza", cond.count(" or ") == 2 and cond.count("and not tsv") == 2, cond)
comprueba("sql: parámetros en orden", params == ["% 9073%", "calidad del aire", "ventilacion", "cpap", '"ventilacion no invasiva"'], params)

# --- El fichero REAL del repo con el intereses.yaml REAL (lo que se publica)
real = mn.nicho(yaml.safe_load(open(mn.RAIZ / "intereses.yaml", encoding="utf-8")))
comprueba("fichero real se lee", real["aviso"] is None, real["aviso"])
kws_con_excl = {e["kw"] for e in real["excl"]}
comprueba("palabras con exclusión en el real", kws_con_excl == {"ventilacion", "formaldehido", "ionizacion", "purificador", "filtracion del aire"}, kws_con_excl)
comprueba("ionizacion bipolar sigue libre en el real", "ionizacion bipolar" in real["palabras"].split(" or "))
comprueba("real: ninguna exclusión vacía", all(e["excluye"] and all(x for x in e["excluye"]) for e in real["excl"]))
comprueba("real: 'cardiorespiratoria' excluye la ventilación clínica",
          any("cardiorespiratoria" in x for e in real["excl"] if e["kw"] == "ventilacion" for x in e["excluye"]))
comprueba("real: 'tubuladura' ya no está", not any("tubuladura" in x for e in real["excl"] for x in e["excluye"]))

print(f"\n{'TODO OK ✔' if not FALLOS else 'HAY FALLOS ✘'} ({OK} de {OK + len(FALLOS)})")
sys.exit(1 if FALLOS else 0)
