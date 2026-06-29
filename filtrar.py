# Filtra las licitaciones de los feeds de la Plataforma de Contratación
# según los criterios que tenemos guardados en intereses.yaml.
#
# Procesa una LISTA de feeds (estatal 643 + agregadas 1044) con el MISMO extractor:
# la descarga y la paginación viven en feeds.py; aquí extraemos los datos de cada
# entrada, clasificamos por categoría y guardamos en data/licitaciones.json.

# Librerías que usamos:
# - yaml (pyyaml): para leer nuestro archivo intereses.yaml.
# - requests: para leer la configuración del radar desde Supabase (por HTTP).
# - utiles.normaliza: para comparar texto ignorando mayúsculas y tildes
#   (la misma función la usa generar_web.py; por eso vive en utiles.py).
# - feeds: la LISTA de feeds y el extractor común (descarga + paginación rel="next").
# - sys: solo para que los acentos se vean bien al imprimir en Windows.
# - json: para guardar las licitaciones en un archivo .json (librería estándar).
# - pathlib (Path): para manejar rutas y crear la carpeta data/ si no existe.
# - datetime: para apuntar cuándo vemos cada licitación por primera y última vez.
# - collections.Counter: para contar cuántas licitaciones hay por fuente.
import sys
import json
import yaml
import requests
from pathlib import Path
from datetime import datetime
from collections import Counter

# normaliza() vive en utiles.py para compartirla con generar_web.py sin duplicarla.
from utiles import normaliza
# La lista de feeds, el extractor (descarga + paginación) y la extracción de
# campos CODICE de cada entrada (extrae_entrada) viven en feeds.py, para
# compartirlos con fetch.py y con el backfill del buscador sin duplicar nada.
from feeds import FEEDS, descarga_entradas, extrae_entrada

# Hacemos que la consola muestre los acentos y la "ñ" correctamente.
sys.stdout.reconfigure(encoding="utf-8")

# Los espacios de nombres CODICE y la extracción de campos de cada <entry> viven
# en feeds.py (extrae_entrada), compartidos con el backfill del buscador.

# --- Configuración del radar guardada en Supabase ---------------------------
# El panel web (con tu login) escribe la config; el robot la LEE aquí con la clave
# "publishable" (pública, la MISMA que ya usa generar_web.py en su JS). Leerla solo
# necesita permiso de lectura, que es público por diseño (la config no es secreta:
# equivale a que intereses.yaml, que ya es público, defina qué busca el radar).
SUPABASE_URL = "https://uzktrhpgkyctlnqgdsys.supabase.co"
SUPABASE_KEY = "sb_publishable_3J3pFbMlNzu-NUDs1-740g_lu8YsRv_"


# a_texto() y a_numero() (limpieza de textos/importes del feed) viven ahora en
# feeds.py, junto al extractor que las usa.


def busca_coincidencia(cpvs, titulo_normalizado, criterios):
    """Comprueba si una licitación encaja con un grupo de criterios.
    'criterios' es un bloque del YAML con dos listas: 'cpv' y 'palabras_clave'.
    Devuelve un texto explicando QUÉ criterio coincidió, o None si no coincide nada."""

    # 1) ¿Algún CPV de la licitación EMPIEZA por alguno de los CPV buscados?
    #    (usar "empieza por" permite, por ejemplo, que "9073" cace a "90731100").
    for prefijo in criterios.get("cpv", []) or []:
        for cpv in cpvs:
            if cpv.startswith(prefijo):
                return f"CPV {cpv} (coincide con {prefijo})"

    # 2) ¿El título contiene alguna de las palabras clave? (ignorando mayúsculas/tildes)
    for palabra in criterios.get("palabras_clave", []) or []:
        if normaliza(palabra) in titulo_normalizado:
            return f"palabra clave «{palabra}»"

    # Si no coincidió ni por CPV ni por palabra, devolvemos None.
    return None


def _lista(config, clave):
    """Devuelve config[clave] si es una lista; si no (falta, o tipo raro), [].
    Así el resto del código puede asumir siempre una lista sin comprobar."""
    valor = config.get(clave)
    return valor if isinstance(valor, list) else []


def lee_config_radar():
    """Lee la configuración del radar desde Supabase (tabla radar_config, fila id=1).
    Devuelve el dict de config, o {} si no se puede leer (Supabase caído, tabla aún
    no creada, sin conexión...). En ese caso el resto del programa cae a intereses.yaml
    y a todas las fuentes, así que el robot NUNCA se rompe por esto."""
    try:
        respuesta = requests.get(
            f"{SUPABASE_URL}/rest/v1/radar_config",
            params={"id": "eq.1", "select": "config"},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=30,
        )
        respuesta.raise_for_status()
        filas = respuesta.json()
        if filas and isinstance(filas[0].get("config"), dict):
            return filas[0]["config"]
    except Exception as error:
        print(f"AVISO: no se pudo leer radar_config de Supabase ({error}); uso intereses.yaml.")
    return {}


def _terminos_activos(lista):
    """Normaliza una lista de términos del panel a una lista de textos ACTIVOS.
    Acepta términos como texto suelto ("limpieza") o como objeto {"v": texto,
    "on": true/false} (lo que guarda el panel para poder seleccionar/deseleccionar
    sin borrar). Descarta los desactivados (on=false) y los vacíos."""
    activos = []
    for item in lista or []:
        if isinstance(item, dict):
            if item.get("on", True) and item.get("v"):
                activos.append(item["v"])
        elif isinstance(item, str) and item.strip():
            activos.append(item.strip())
    return activos


def categorias_desde_config(config):
    """Convierte config['categorias'] (lo que edita el panel) al MISMO formato que
    intereses.yaml: {nombre: {'cpv': [...], 'palabras_clave': [...]}} con solo los
    términos activos. Devuelve None si la config no trae categorías usables (para
    caer entonces a intereses.yaml)."""
    categorias = config.get("categorias")
    if not isinstance(categorias, dict) or not categorias:
        return None
    efectivas = {}
    for nombre, crit in categorias.items():
        if not isinstance(crit, dict):
            continue
        efectivas[nombre] = {
            "cpv": _terminos_activos(crit.get("cpv")),
            "palabras_clave": _terminos_activos(crit.get("palabras_clave")),
        }
    return efectivas or None


# --- 1. Cargamos los criterios desde intereses.yaml -------------------------
# Lo abrimos con encoding utf-8 porque tiene acentos y "ñ".
with open("intereses.yaml", encoding="utf-8") as f:
    intereses = yaml.safe_load(f)

# "intereses" es un diccionario con TODAS las categorías (criticas, a_revisar,
# pruebas, y las que añadas en el futuro). El ORDEN en que aparecen en el YAML
# marca la prioridad: una licitación se queda en la PRIMERA categoría con la que
# coincide. Así no hay nombres de categoría fijos en el código.

# --- 1.bis Configuración del radar (Supabase) -------------------------------
# La config la pone el panel web (con login) y vive en Supabase. Aquí la leemos y
# decidimos QUÉ caza el radar. Si no hay config (o Supabase no responde), todo cae
# a intereses.yaml y a todas las fuentes: el comportamiento de siempre.
config_radar = lee_config_radar()
fuentes_config = _lista(config_radar, "fuentes")        # qué feeds leer
plataformas_config = _lista(config_radar, "plataformas")  # "Estado" = estatal
regiones_config = _lista(config_radar, "regiones")      # códigos NUTS (ES220...)

# Criterios EFECTIVOS de "qué cazar": si la config trae "categorias" (editadas
# desde el panel: mismos grupos criticas/a_revisar/pruebas, con sus CPV y palabras),
# SUSTITUYEN por completo a las de intereses.yaml. Si no, usamos el YAML tal cual.
categorias_panel = categorias_desde_config(config_radar)
intereses_efectivos = categorias_panel or intereses

if config_radar:
    n_cpv = sum(len(crit["cpv"]) for crit in intereses_efectivos.values())
    n_kw = sum(len(crit["palabras_clave"]) for crit in intereses_efectivos.values())
    origen = "panel" if categorias_panel else "intereses.yaml"
    print("Config del radar (Supabase) aplicada:")
    print(f"  Criterios ({origen}): {n_cpv} CPV + {n_kw} palabras en {len(intereses_efectivos)} grupos")
    print(f"  Fuentes: {fuentes_config or 'todas'}")
    print(f"  Plataformas: {plataformas_config or 'todas'}")
    print(f"  Regiones (NUTS): {regiones_config or 'todas'}")
else:
    print("Sin config en Supabase (o no disponible): uso intereses.yaml y todas las fuentes.")


def pasa_territorio(plataforma_lic, region_codigo_lic):
    """¿La licitación encaja con el territorio elegido en la config? Si una lista
    de la config está vacía, no filtra por ese criterio (pasa todo)."""
    # Plataforma efectiva: la agregadora (p.ej. "Gobierno de Navarra"), o "Estado"
    # cuando es del feed estatal (que no trae AgentParty, plataforma=None).
    plat = plataforma_lic or "Estado"
    if plataformas_config and plat not in plataformas_config:
        return False
    if regiones_config and region_codigo_lic not in regiones_config:
        return False
    return True


# Una lista de resultados por CADA categoría efectiva, en el mismo orden.
# (un diccionario: nombre_de_categoria -> lista de licitaciones de esa categoría)
resultados = {nombre: [] for nombre in intereses_efectivos}

# --- 2. Recorremos la LISTA de feeds (estatal + agregadas) ------------------
# Cada feed se descarga y pagina con el MISMO extractor (feeds.descarga_entradas)
# y cada licitación queda etiquetada con su "fuente" para poder distinguirla.
total_entradas = 0           # cuántas entradas hemos leído en total (todos los feeds)
leidas_por_fuente = {}       # cuántas entradas trajo cada feed (para el log)

for feed in FEEDS:
    fuente = feed["fuente"]

    # Si la config limita las fuentes y esta no está, nos saltamos el feed entero.
    if fuentes_config and fuente not in fuentes_config:
        print(f"Feed «{fuente}»: omitido (no está en la config del radar).")
        leidas_por_fuente[fuente] = 0
        continue

    # Descarga + paginación rel="next" (hasta agotarla o hasta el tope de páginas).
    entradas, paginas, tope = descarga_entradas(feed["url"])
    leidas_por_fuente[fuente] = len(entradas)
    total_entradas += len(entradas)

    aviso_tope = "  [TOPE de páginas alcanzado: puede faltar histórico]" if tope else ""
    print(f"Feed «{fuente}»: {len(entradas)} entradas en {paginas} página(s){aviso_tope}")

    # --- Recorremos las licitaciones de ESTE feed una a una -----------------
    for entrada in entradas:
        # Extracción de TODOS los campos CODICE: la hace el extractor compartido
        # (feeds.extrae_entrada), el MISMO que usa el backfill del buscador, para no
        # duplicar la lógica. Devuelve un dict con id, titulo, objeto, enlace, cpv[],
        # fuente, organismo, plataforma, region(_codigo), importes y fechas. La
        # "categoria" y la "coincidencia" se añaden abajo, cuando sepamos su grupo.
        registro = extrae_entrada(entrada, fuente)
        cpvs = registro["cpv"]
        # Normalizamos el título una sola vez para comparar palabras clave.
        titulo_normalizado = normaliza(registro["titulo"])

        # Clasificamos recorriendo las categorías EFECTIVAS EN ORDEN. La PRIMERA con
        # la que coincida se queda con la licitación; el orden marca la prioridad
        # (criticas > a_revisar > pruebas, o lo que defina el panel).
        for nombre, criterios in intereses_efectivos.items():
            motivo = busca_coincidencia(cpvs, titulo_normalizado, criterios)
            if motivo:
                # Está cazada por interés; ahora debe pasar el filtro de TERRITORIO
                # (plataforma/región) elegido en la config. Si no, NO se guarda.
                if pasa_territorio(registro["plataforma"], registro["region_codigo"]):
                    registro["categoria"] = nombre        # categoría efectiva
                    registro["coincidencia"] = motivo
                    resultados[nombre].append(registro)
                break  # ya decidida (guardada o descartada por territorio)
        # Si ninguna categoría coincide, la licitación se ignora (no la guardamos).

# --- 3. Mostramos los resultados agrupados, una sección por categoría --------
for nombre, lista in resultados.items():
    # Título de la sección a partir del nombre: "a_revisar" -> "A REVISAR".
    titulo_seccion = nombre.upper().replace("_", " ")
    print("=" * 70)
    print(f"{titulo_seccion} ({len(lista)})")
    print("=" * 70)
    for lic in lista:
        print(f"- {lic['titulo']}")
        print(f"  Enlace: {lic['enlace']}")
        print(f"  Motivo: {lic['coincidencia']}")
        print()

# --- 4. Resumen de esta ejecución -------------------------------------------
print("=" * 70)
# Un trocito de texto por categoría: "0 en criticas, 0 en a_revisar, 5 en pruebas".
detalle = ", ".join(f"{len(lista)} en {nombre}" for nombre, lista in resultados.items())
print(f"Resumen: {detalle}; sobre {total_entradas} licitaciones leídas en total.")
# Cuántas entradas trajo cada feed (antes de filtrar).
detalle_feeds = ", ".join(f"{n} de {fuente}" for fuente, n in leidas_por_fuente.items())
print(f"Entradas leídas por fuente: {detalle_feeds}.")

# --- 5. Guardamos las licitaciones en data/licitaciones.json ----------------
# Juntamos en una sola lista todas las que han pasado el filtro (todas las categorías,
# en orden de prioridad: primero las de "criticas", luego "a_revisar", etc.).
licitaciones_filtradas = [lic for lista in resultados.values() for lic in lista]

# Ruta del archivo. Path nos permite crear la carpeta "data" si todavía no existe.
ruta_json = Path("data") / "licitaciones.json"
ruta_json.parent.mkdir(parents=True, exist_ok=True)

# Cargamos lo que ya hubiera guardado de ejecuciones anteriores.
# Si es la PRIMERA vez (el archivo aún no existe), empezamos con un diccionario vacío.
if ruta_json.exists():
    with open(ruta_json, encoding="utf-8") as f:
        datos = json.load(f)
else:
    datos = {}

# Momento de esta ejecución, como texto en formato ISO (ej: "2026-06-22T18:30:00.123").
ahora = datetime.now().isoformat()

# Recorremos las licitaciones filtradas y actualizamos el diccionario "datos".
# Usamos el id como clave: así no se duplican y reconocemos las que ya conocíamos.
nuevas = []
for lic in licitaciones_filtradas:
    clave = lic["id"]
    if clave not in datos:
        # No estaba: es NUEVA. La añadimos con primera_vez = ultima_vez = ahora.
        datos[clave] = {
            "id": lic["id"],
            "titulo": lic["titulo"],
            "enlace": lic["enlace"],
            "cpv": lic["cpv"],
            "fuente": lic["fuente"],
            "organismo": lic["organismo"],
            "plataforma": lic["plataforma"],
            "region": lic["region"],
            "region_codigo": lic["region_codigo"],
            "categoria": lic["categoria"],
            "coincidencia": lic["coincidencia"],
            "presupuesto_con_iva": lic["presupuesto_con_iva"],
            "presupuesto_sin_iva": lic["presupuesto_sin_iva"],
            "valor_estimado": lic["valor_estimado"],
            "fecha_fin_plazo": lic["fecha_fin_plazo"],
            "fecha_publicacion": lic["fecha_publicacion"],
            "fecha_actualizacion": lic["fecha_actualizacion"],
            "primera_vez": ahora,
            "ultima_vez": ahora,
        }
        nuevas.append(datos[clave])
    else:
        # Ya la conocíamos: refrescamos cuándo la hemos visto por última vez, su
        # fecha de actualización y los datos económicos/fechas (pueden cambiar:
        # correcciones de presupuesto, ampliaciones de plazo...). NO tocamos
        # "primera_vez" ni "fuente" (el origen de una licitación no cambia; si
        # apareciera en los dos feeds, se queda con el primero que la vio).
        datos[clave]["ultima_vez"] = ahora
        datos[clave]["fecha_actualizacion"] = lic["fecha_actualizacion"]
        datos[clave]["presupuesto_con_iva"] = lic["presupuesto_con_iva"]
        datos[clave]["presupuesto_sin_iva"] = lic["presupuesto_sin_iva"]
        datos[clave]["valor_estimado"] = lic["valor_estimado"]
        datos[clave]["fecha_fin_plazo"] = lic["fecha_fin_plazo"]
        datos[clave]["fecha_publicacion"] = lic["fecha_publicacion"]
        # Territorio: lo refrescamos también (y de paso rellena las entradas
        # antiguas que aún no lo tuvieran, cuando se las vuelve a ver).
        datos[clave]["organismo"] = lic["organismo"]
        datos[clave]["plataforma"] = lic["plataforma"]
        datos[clave]["region"] = lic["region"]
        datos[clave]["region_codigo"] = lic["region_codigo"]

# Migración: las entradas guardadas ANTES de existir estos campos no los tienen.
# El "fuente" lo dejamos en "estatal" (hasta ahora el único feed era ese); los de
# territorio quedan en None si nunca se vuelve a ver la licitación. setdefault solo
# pone el valor si falta; no pisa los que ya estén.
for registro_guardado in datos.values():
    registro_guardado.setdefault("fuente", "estatal")
    registro_guardado.setdefault("organismo", None)
    registro_guardado.setdefault("plataforma", None)
    registro_guardado.setdefault("region", None)
    registro_guardado.setdefault("region_codigo", None)

# --- Poda: quitar lo que YA NO encaja con la config ACTUAL -------------------
# El archivo ACUMULA histórico (para conservar primera_vez), así que solo añadir no
# basta: si cambias la config del radar (o intereses.yaml), las licitaciones que
# dejan de cumplir los criterios tienen que DESAPARECER del radar. Re-evaluamos cada
# entrada guardada con sus PROPIOS datos (cpv/título/territorio) contra los criterios
# efectivos de ahora, y borramos las que ya no casan (por criterio, fuente o territorio).
podadas = 0
for clave in list(datos.keys()):
    reg = datos[clave]
    titulo_norm = normaliza(reg.get("titulo", "") or "")
    cpvs_reg = reg.get("cpv", []) or []
    # ¿sigue casando con alguna categoría efectiva (por CPV o palabra clave)?
    categoria_reev = None
    for nombre, criterios in intereses_efectivos.items():
        if busca_coincidencia(cpvs_reg, titulo_norm, criterios):
            categoria_reev = nombre
            break
    fuente_ok = (not fuentes_config) or (reg.get("fuente", "estatal") in fuentes_config)
    territorio_ok = pasa_territorio(reg.get("plataforma"), reg.get("region_codigo"))
    if categoria_reev is None or not fuente_ok or not territorio_ok:
        del datos[clave]
        podadas += 1
    else:
        reg["categoria"] = categoria_reev   # refresca el grupo por si cambió

# Guardamos el diccionario completo.
# ensure_ascii=False -> conserva tildes y "ñ"; indent=2 -> deja el diff de Git legible.
with open(ruta_json, "w", encoding="utf-8") as f:
    json.dump(datos, f, ensure_ascii=False, indent=2)

# --- 6. Resumen de la persistencia ------------------------------------------
print("=" * 70)
print(f"Total de licitaciones en el archivo: {len(datos)}")
# Conteo por fuente de TODO lo guardado (cumple "nº de licitaciones por fuente").
conteo_fuente = Counter(registro["fuente"] for registro in datos.values())
detalle_guardadas = ", ".join(f"{n} {fuente}" for fuente, n in sorted(conteo_fuente.items()))
print(f"Por fuente en el archivo: {detalle_guardadas}.")
print(f"Podadas (ya no encajan con la config actual): {podadas}")
print(f"Nuevas en esta ejecución: {len(nuevas)}")
for lic in nuevas:
    print(f"  - [{lic['fuente']}/{lic['categoria']}] {lic['titulo']}")
