"""Pruebas de la PRIORIDAD de las categorías (utiles.ordena_categorias y busca_coincidencia).

Sin red y sin tocar nada: ejecutar «python test_orden_categorias.py».

POR QUÉ EXISTE ESTE FICHERO: una licitación se queda en la PRIMERA categoría que casa, así
que el orden del diccionario ES la prioridad. Hasta el 02/10/2026 ese orden salía de
radar_config, y eso era imposible de controlar: la columna es jsonb y jsonb guarda las
claves por (longitud, bytes), así que «pruebas» (7 letras) le ganaba a «criticas» (8) y
«a_revisar» (9) iba última. Resultado: 11 tarjetas buenas enterradas en el cajón de
pruebas, entre ellas 2 M€ de Bioseguridad de Murcia Oeste y los 779 monitores de calidad
del aire del CIBER. Si alguien vuelve a confiar en el orden de la config, estas pruebas
tienen que ponerse rojas.
"""
import json
import sys
from pathlib import Path

from utiles import (ORDEN_CATEGORIAS, CATEGORIA_ULTIMA, busca_coincidencia,
                    normaliza, ordena_categorias)

sys.stdout.reconfigure(encoding="utf-8")
OK, FALLOS = 0, []


def comprueba(nombre, condicion, detalle=""):
    global OK
    if condicion:
        OK += 1
    else:
        FALLOS.append(f"{nombre} {detalle}")
        print(f"FALLO: {nombre} {detalle}")


CRIT = {"cpv": ["90731100"], "palabras_clave": ["bioseguridad"]}
REV = {"cpv": ["9073"], "palabras_clave": ["purificador"]}
PRU = {"cpv": [], "palabras_clave": ["monitorización", "bioseguridad"]}

# --- ordena_categorias: el orden que importa -------------------------------------------
# Tal y como lo devuelve la base HOY (jsonb, ordenado por longitud de la clave).
COMO_LO_DA_JSONB = {"pruebas": PRU, "criticas": CRIT, "a_revisar": REV}
comprueba("el orden del jsonb se corrige",
          list(ordena_categorias(COMO_LO_DA_JSONB)) == ["criticas", "a_revisar", "pruebas"],
          list(ordena_categorias(COMO_LO_DA_JSONB)))
comprueba("no se pierde ni se inventa ningún grupo",
          set(ordena_categorias(COMO_LO_DA_JSONB)) == set(COMO_LO_DA_JSONB))
comprueba("el contenido de cada grupo llega intacto",
          ordena_categorias(COMO_LO_DA_JSONB)["criticas"] is CRIT)
comprueba("es idempotente",
          list(ordena_categorias(ordena_categorias(COMO_LO_DA_JSONB)))
          == list(ordena_categorias(COMO_LO_DA_JSONB)))

# Faltan grupos: no se inventan.
comprueba("si falta «pruebas», no aparece",
          list(ordena_categorias({"a_revisar": REV, "criticas": CRIT})) == ["criticas", "a_revisar"])
comprueba("si solo hay «pruebas», sale sola",
          list(ordena_categorias({"pruebas": PRU})) == ["pruebas"])

# Grupo nuevo inventado en el panel: entra EN MEDIO, nunca delante de críticas ni
# detrás de pruebas. Así añadir un grupo no cambia en silencio lo que ya funcionaba.
CON_NUEVO = {"pruebas": PRU, "zzz_nuevo": CRIT, "criticas": CRIT, "a_revisar": REV}
comprueba("un grupo nuevo va en medio",
          list(ordena_categorias(CON_NUEVO)) == ["criticas", "a_revisar", "zzz_nuevo", "pruebas"],
          list(ordena_categorias(CON_NUEVO)))
comprueba("dos grupos nuevos conservan su orden relativo (determinista)",
          list(ordena_categorias({"b": REV, "a": REV, "criticas": CRIT}))
          == ["criticas", "b", "a"])

# Entradas raras: ni explota ni devuelve None.
for valor in (None, {}, [], "criticas", 7):
    comprueba(f"entrada rara {valor!r} -> {{}}", ordena_categorias(valor) == {})

comprueba("«pruebas» es la última declarada", CATEGORIA_ULTIMA == "pruebas")
comprueba("críticas es la primera declarada", ORDEN_CATEGORIAS[0] == "criticas")

# --- el efecto real: quién se queda la licitación --------------------------------------
def clasifica(categorias, titulo, cpvs=()):
    """Lo mismo que hacen el robot y la poda: la PRIMERA que casa."""
    titulo_norm = normaliza(titulo)
    for nombre, criterios in categorias.items():
        if busca_coincidencia(list(cpvs), titulo_norm, criterios):
            return nombre
    return None


CASO = "Servicio de Bioseguridad en los centros del Área de Salud I"
comprueba("REGRESIÓN: con el orden del jsonb la pillaba «pruebas»",
          clasifica(COMO_LO_DA_JSONB, CASO) == "pruebas")
comprueba("con el orden arreglado la pilla «criticas»",
          clasifica(ordena_categorias(COMO_LO_DA_JSONB), CASO) == "criticas")
comprueba("lo que solo casa con pruebas sigue en pruebas",
          clasifica(ordena_categorias(COMO_LO_DA_JSONB),
                    "Monitorización de la red de presas") == "pruebas")
comprueba("lo que no casa con nada queda fuera",
          clasifica(ordena_categorias(COMO_LO_DA_JSONB), "Suministro de sillas") is None)
comprueba("por CPV manda también el orden (9073 en a_revisar, 90731100 en criticas)",
          clasifica(ordena_categorias(COMO_LO_DA_JSONB), "Lo que sea",
                    cpvs=["90731100"]) == "criticas")
comprueba("un CPV que solo casa con el prefijo corto cae en a_revisar",
          clasifica(ordena_categorias(COMO_LO_DA_JSONB), "Lo que sea",
                    cpvs=["90731700"]) == "a_revisar")

# --- busca_coincidencia: lo que ya hacía, que siga haciéndolo --------------------------
comprueba("casa por prefijo de CPV",
          busca_coincidencia(["90731100"], "", {"cpv": ["9073"]}) is not None)
comprueba("el prefijo no casa al revés",
          busca_coincidencia(["9073"], "", {"cpv": ["90731100"]}) is None)
comprueba("la palabra casa sin tildes y sin mayúsculas",
          busca_coincidencia([], normaliza("Purificación DEL Aire"),
                             {"palabras_clave": ["purificacion del aire"]}) is not None)
comprueba("el motivo dice por qué casó",
          "CPV" in (busca_coincidencia(["90731100"], "", {"cpv": ["9073"]}) or ""))
comprueba("criterios vacíos no casan con nada",
          busca_coincidencia(["90731100"], "lo que sea", {}) is None)

# --- por qué aquí NO se comprueba el fichero de datos real -----------------------
# Tentador, pero no vale: data/licitaciones.json guarda la categoría con la que se
# clasificó cada tarjeta, y los criterios de ENTONCES no están en ninguna parte (el
# panel se edita y radar_config no guarda histórico). Compararlo contra intereses.yaml
# da falsos fallos cada vez que el panel y el YAML difieran, que es lo normal. Esa
# comprobación se hace tras cada pasada leyendo el panel en vivo, no aquí.

print(f"\n{'TODO OK ✔' if not FALLOS else 'HAY FALLOS ✘'} ({OK} de {OK + len(FALLOS)})")
sys.exit(1 if FALLOS else 0)
