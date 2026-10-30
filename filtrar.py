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
import argparse
import requests
from pathlib import Path
from datetime import datetime
from collections import Counter

# normaliza() vive en utiles.py para compartirla con generar_web.py sin duplicarla.
from utiles import (normaliza, credencial_config, en_actions, get_con_reintentos,
                    busca_coincidencia, ordena_categorias, reclasifica)
# La lista de feeds, el extractor (descarga + paginación) y la extracción de
# campos CODICE de cada entrada (extrae_entrada) viven en feeds.py, para
# compartirlos con fetch.py y con el backfill del buscador sin duplicar nada.
from feeds import FEEDS, ATOM_NS, descarga_entradas, extrae_entrada

# Hacemos que la consola muestre los acentos y la "ñ" correctamente.
sys.stdout.reconfigure(encoding="utf-8")

# Los espacios de nombres CODICE y la extracción de campos de cada <entry> viven
# en feeds.py (extrae_entrada), compartidos con el backfill del buscador.

# --- Configuración del radar guardada en Supabase ---------------------------
# El panel web (con tu login) escribe la config; el robot la LEE aquí.
#
# CAMBIÓ EN SEPT. 2026: antes se leía con la clave "publishable" (pública), porque
# radar_config tenía lectura para todo el mundo. Al preparar el segundo perfil esa
# lectura se cierra —cada perfil solo ve su fila— y el robot pasa a identificarse
# con SUPABASE_SERVICE_ROLE (Actions) o SUPABASE_SECRET_KEY (.env local).
# La clave publishable se queda abajo solo como respaldo mientras dure la transición.
SUPABASE_URL = "https://uzktrhpgkyctlnqgdsys.supabase.co"
SUPABASE_KEY = "sb_publishable_3J3pFbMlNzu-NUDs1-740g_lu8YsRv_"


# a_texto() y a_numero() (limpieza de textos/importes del feed) viven ahora en
# feeds.py, junto al extractor que las usa.


def _lista(config, clave):
    """Devuelve config[clave] si es una lista; si no (falta, o tipo raro), [].
    Así el resto del código puede asumir siempre una lista sin comprobar."""
    valor = config.get(clave)
    return valor if isinstance(valor, list) else []


def lee_config_radar():
    """Lee la configuración del radar desde Supabase (tabla radar_config, fila id=1).

    CREDENCIAL: ya no se lee con la clave pública. Al abrir el segundo perfil, cada
    perfil solo puede ver su propia fila, así que el robot se identifica con
    SUPABASE_SERVICE_ROLE (Actions) o SUPABASE_SECRET_KEY (.env local).

    SI FALLA:
      · En GitHub Actions -> se PARA con exit 1. Antes seguía con intereses.yaml, y
        eso publicaba el radar con los criterios equivocados y el workflow en verde:
        un fallo mudo. Más vale no publicar que publicar mal.
      · En tu portátil -> avisa y sigue con intereses.yaml, para poder trabajar sin red.

    Que la fila exista pero traiga la config VACÍA ({}) no es un fallo: es como
    arranca un perfil nuevo antes de tocar el panel. En ese caso se usa intereses.yaml.
    """
    clave, origen = credencial_config(SUPABASE_KEY)
    try:
        respuesta = get_con_reintentos(
            f"{SUPABASE_URL}/rest/v1/radar_config",
            params={"id": "eq.1", "select": "config"},
            headers={"apikey": clave, "Authorization": f"Bearer {clave}"},
            timeout=30,
        )
        filas = respuesta.json()
        if not filas:
            raise RuntimeError(
                "radar_config no devolvió ninguna fila: o la credencial no tiene "
                "permiso, o la fila no existe"
            )
        config = filas[0].get("config")
        if config is None:
            # Columna vacía: significa lo mismo que {} (perfil aún sin configurar).
            # No es motivo para tirar la publicación del día.
            print("AVISO: radar_config existe pero no tiene configuración; uso intereses.yaml.")
            return {}
        if not isinstance(config, dict):
            raise RuntimeError("radar_config devolvió una fila sin 'config' utilizable")
        print(f"Config del radar leída de Supabase (credencial: {origen}).")
        return config
    except Exception as error:
        detalle = f"no se pudo leer radar_config de Supabase ({error}); credencial: {origen}."
        if en_actions():
            print(f"ERROR: {detalle}")
            print("Esto NO se ignora en GitHub Actions: el radar se publicaría con los")
            print("criterios de intereses.yaml en lugar de los del panel. Se aborta.")
            sys.exit(1)
        print(f"AVISO: {detalle} Uso intereses.yaml.")
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


# --- 0. Cómo se ha pedido esta ejecución -----------------------------------
# Sin argumentos: el trabajo de siempre (los dos feeds en vivo). Con --zip: RECUPERACIÓN
# desde el ZIP mensual de Datos Abiertos de las fuentes que se digan, y NADA más (no se
# tocan las otras fuentes). Por defecto simula: hay que pedir --escribir para guardar.
_ap = argparse.ArgumentParser(
    description="Filtra las licitaciones de la PCSP con los criterios del panel. "
                "Con --zip, recupera desde el ZIP de Datos Abiertos en vez del feed en vivo.")
_ap.add_argument("--zip", default="", metavar="FUENTES",
                 help="RECUPERACIÓN: fuentes a leer del ZIP mensual, separadas por comas "
                      "(p. ej. 'estatal'). Solo se procesan esas.")
_ap.add_argument("--mes", default=None, metavar="AAAAMM",
                 help="Mes del ZIP (por defecto, el del día de hoy).")
_ap.add_argument("--desde", default=None, metavar="AAAA-MM-DD",
                 help="Recuperar solo entradas actualizadas desde esta fecha (incluida).")
_ap.add_argument("--hasta", default=None, metavar="AAAA-MM-DD",
                 help="Recuperar solo entradas actualizadas hasta esta fecha (incluida).")
_ap.add_argument("--escribir", action="store_true",
                 help="Guardar de verdad. Sin esto, la recuperación SIMULA y solo cuenta.")
ARGS = _ap.parse_args()
FUENTES_ZIP = [x.strip() for x in ARGS.zip.split(",") if x.strip()]
MODO_RECUPERACION = bool(FUENTES_ZIP)
# En el trabajo diario se escribe siempre (es lo de siempre). En recuperación, solo si se pide.
ESCRIBIR = (not MODO_RECUPERACION) or ARGS.escribir

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
# ordena_categorias: la PRIORIDAD (criticas > a_revisar > ... > pruebas) se declara en
# utiles.py y no puede salir de la config, porque radar_config.config es jsonb y jsonb
# reordena las claves por longitud. Ver el comentario largo allí. Afecta a los dos
# recorridos de abajo -- el de clasificar y el de la poda -- porque los dos iteran este
# mismo diccionario y se queda la PRIMERA categoría que casa.
intereses_efectivos = ordena_categorias(categorias_panel or intereses)

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
# --- RECUPERACIÓN desde el ZIP de Datos Abiertos ----------------------------
def _entradas_del_zip(fuente, mes=None, desde=None, hasta=None):
    """Entradas ATOM del ZIP mensual de esa fuente, filtradas por fecha si se pide.

    Reutiliza lo que ya usa el catálogo del Buscador (backfill_catalogo): misma URL, misma
    descarga reanudable (si el ZIP ya está en cache_backfill/, no se vuelve a bajar) y el
    mismo recorrido de los .atom de dentro. Devuelve (entradas, cuentas_por_dia)."""
    from backfill_catalogo import url_zip, descarga_zip, itera_atoms, CACHE_DIR
    from lxml import etree
    from collections import Counter as _Counter

    periodo = mes or datetime.now().strftime("%Y%m")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    destino = CACHE_DIR / f"{fuente}_{periodo}.zip"
    print(f"  Recuperación «{fuente}»: ZIP de {periodo} (Datos Abiertos)")
    descarga_zip(url_zip(fuente, periodo), destino)

    entradas, por_dia, total_zip = [], _Counter(), 0
    for _nombre, blob in itera_atoms(destino):
        try:
            raiz = etree.fromstring(blob)
        except etree.XMLSyntaxError:
            continue
        for entrada_zip in raiz.findall("atom:entry", ATOM_NS):
            total_zip += 1
            marca = entrada_zip.find("atom:updated", ATOM_NS)
            dia = (marca.text or "")[:10] if marca is not None and marca.text else ""
            if desde and dia < desde:
                continue
            if hasta and dia > hasta:
                continue
            por_dia[dia] += 1
            entradas.append(entrada_zip)
    rango = f" entre {desde or 'el principio'} y {hasta or 'el final'}" if (desde or hasta) else ""
    print(f"  Recuperación «{fuente}»: {total_zip:,} entradas en el ZIP, "
          f"{len(entradas):,} en el rango{rango}.")
    for dia, n in sorted(por_dia.items()):
        print(f"      {dia}: {n:,}")
    return entradas, por_dia


# Cada feed se descarga y pagina con el MISMO extractor (feeds.descarga_entradas)
# y cada licitación queda etiquetada con su "fuente" para poder distinguirla.
total_entradas = 0           # cuántas entradas hemos leído en total (todos los feeds)
cuentas_zip = {}             # en recuperación: entradas del ZIP por día y fuente (control)
leidas_por_fuente = {}       # cuántas entradas trajo cada feed (para el log)
# Fecha de la entrada MÁS RECIENTE que trae cada feed. Con esto se detecta una fuente MUDA:
# un feed que sigue contestando pero lleva días sin publicar nada nuevo. Pasó con el estatal
# del 22 al 28/09/2026 y no se notó hasta que alguien preguntó: en el log, un feed congelado
# se ve igual que un día sin novedades.
ultima_entrada_por_fuente = {}
# Y la MÁS ANTIGUA que hemos leído: con ella se sabe si esta pasada enlaza con la anterior.
primera_entrada_por_fuente = {}
tope_por_fuente = {}

for feed in FEEDS:
    fuente = feed["fuente"]

    # En recuperación solo se procesan las fuentes pedidas: las demás ni se tocan.
    if MODO_RECUPERACION and fuente not in FUENTES_ZIP:
        print(f"Feed «{fuente}»: omitido (recuperación de {', '.join(FUENTES_ZIP)}).")
        leidas_por_fuente[fuente] = 0
        continue

    # Si la config limita las fuentes y esta no está, nos saltamos el feed entero.
    if fuentes_config and fuente not in fuentes_config:
        print(f"Feed «{fuente}»: omitido (no está en la config del radar).")
        leidas_por_fuente[fuente] = 0
        continue

    if MODO_RECUPERACION:
        entradas, cuentas_zip[fuente] = _entradas_del_zip(fuente, ARGS.mes, ARGS.desde, ARGS.hasta)
        paginas, tope = 0, False
    else:
        # Descarga + paginación rel="next" (hasta agotarla o hasta el tope de páginas).
        entradas, paginas, tope = descarga_entradas(feed["url"])
    leidas_por_fuente[fuente] = len(entradas)
    total_entradas += len(entradas)

    aviso_tope = "  [TOPE de páginas alcanzado: puede faltar histórico]" if tope else ""
    origen = "el ZIP de Datos Abiertos" if MODO_RECUPERACION else f"{paginas} página(s)"
    print(f"Feed «{fuente}»: {len(entradas)} entradas en {origen}{aviso_tope}")

    # La más reciente de TODAS las entradas leídas (hayan pasado el filtro o no): es la
    # señal de si la fuente sigue publicando.
    fechas_feed = []
    for entrada_cruda in entradas:
        marca = entrada_cruda.find("atom:updated", ATOM_NS)
        if marca is not None and marca.text:
            fechas_feed.append(marca.text.strip())
    if fechas_feed:
        ultima_entrada_por_fuente[fuente] = max(fechas_feed)[:19]
        primera_entrada_por_fuente[fuente] = min(fechas_feed)[:19]
    tope_por_fuente[fuente] = bool(tope)

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

# --- 4 bis. ¿HAY ALGUNA FUENTE MUDA? ---------------------------------------
# Se avisa a partir de 2 días sin entradas nuevas: un fin de semana no cuenta como avería,
# pero tres días sin publicar nada ya es raro y hay que mirarlo. El estado se guarda para
# que la web lo diga también (la generación la hace generar_web.py).
DIAS_PARA_AVISAR = 2
estado_fuentes = {}
# El estado de la pasada ANTERIOR (lo escribió la ejecución pasada): hace falta para saber
# hasta dónde llegamos la última vez y poder comprobar que esta pasada enlaza.
estado_previo = {}
_ruta_estado = Path("data") / "estado_fuentes.json"
if _ruta_estado.exists():
    try:
        estado_previo = json.loads(_ruta_estado.read_text(encoding="utf-8")) or {}
    except (ValueError, OSError):
        estado_previo = {}
# En una recuperación no se mira el feed en vivo, así que no hay nada que juzgar sobre si
# está mudo: avisar aquí sería con datos del ZIP y filtrados por rango. Se calla.
_avisar_mudas = not MODO_RECUPERACION
hoy_fecha = datetime.now().date()
for feed_info in FEEDS:
    nombre_fuente = feed_info["fuente"]
    ultima = ultima_entrada_por_fuente.get(nombre_fuente)
    dias = None
    if ultima:
        try:
            dias = (hoy_fecha - datetime.fromisoformat(ultima).date()).days
        except ValueError:
            dias = None
    # ¿ENLAZA con la pasada anterior? Se compara la entrada más ANTIGUA que hemos leído
    # ahora con la más RECIENTE que leímos la vez pasada. Si la más antigua de hoy es
    # POSTERIOR, en medio quedó un tramo que nadie ha visto: eso es un hueco. Es la
    # comprobación que de verdad importa, porque el aviso del tope de páginas está
    # encendido siempre (el feed se remonta años) y por eso no informa de nada.
    mas_antigua = primera_entrada_por_fuente.get(nombre_fuente)
    ultima_anterior = (estado_previo.get(nombre_fuente) or {}).get("ultima_entrada")
    hueco = bool(mas_antigua and ultima_anterior and mas_antigua > ultima_anterior)
    horas_hueco = None
    if hueco:
        try:
            horas_hueco = round(
                (datetime.fromisoformat(mas_antigua) - datetime.fromisoformat(ultima_anterior))
                .total_seconds() / 3600, 1)
        except ValueError:
            horas_hueco = None
    estado_fuentes[nombre_fuente] = {
        "ultima_entrada": ultima,
        "mas_antigua_leida": mas_antigua,
        "dias_sin_novedad": dias,
        "muda": bool(dias is not None and dias > DIAS_PARA_AVISAR),
        "tope_paginas": tope_por_fuente.get(nombre_fuente, False),
        "enlaza": (None if not (mas_antigua and ultima_anterior) else not hueco),
        "hueco_horas": horas_hueco,
        "leidas": leidas_por_fuente.get(nombre_fuente, 0),
        "comprobado": datetime.now().isoformat(timespec="seconds"),
    }
    if hueco and _avisar_mudas:
        print(f"AVISO GRAVE: la fuente «{nombre_fuente}» NO enlaza con la pasada anterior. "
              f"Lo más antiguo que hemos leído es del {mas_antigua} y la vez pasada llegamos "
              f"hasta el {ultima_anterior}: faltan unas {horas_hueco} horas de licitaciones "
              f"que nadie ha visto. Hay que recuperarlas con "
              f"«python filtrar.py --zip {nombre_fuente} --desde ... --hasta ...».")
    if not _avisar_mudas:
        continue
    if estado_fuentes[nombre_fuente]["muda"]:
        print(f"AVISO: la fuente «{nombre_fuente}» lleva {dias} días sin publicar nada nuevo "
              f"(su última entrada es del {ultima}). El feed contesta, pero no trae novedades: "
              f"míralo antes de dar por hecho que no hay licitaciones.")
    elif dias is not None:
        print(f"Fuente «{nombre_fuente}»: al día (última entrada del {ultima}).")

# En una recuperación NO se toca: ese fichero cuenta cómo van los feeds EN VIVO, y aquí no
# se han mirado (o se ha mirado solo una fuente). Pisarlo apagaría la alarma de fuente muda.
if not MODO_RECUPERACION:
    Path("data").mkdir(parents=True, exist_ok=True)
    with open(Path("data") / "estado_fuentes.json", "w", encoding="utf-8") as f:
        json.dump(estado_fuentes, f, ensure_ascii=False, indent=1)

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

# Cuántas había ANTES de tocar nada: es el control de que una recuperación solo puede
# sumar. Si al final hay menos, algo ha ido mal y no se escribe.
TOTAL_ANTES = len(datos)

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
            "num_expediente": lic["num_expediente"],
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
        # Nº de expediente: lo refrescamos también (y rellena las entradas antiguas
        # que aún no lo tuvieran, cuando se las vuelve a ver).
        datos[clave]["num_expediente"] = lic["num_expediente"]
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
    registro_guardado.setdefault("num_expediente", None)
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
recoincidencias = 0
for clave in list(datos.keys()):
    reg = datos[clave]
    # ¿sigue casando con alguna categoría efectiva (por CPV o palabra clave)?
    categoria_reev, motivo_reev = reclasifica(reg, intereses_efectivos)
    fuente_ok = (not fuentes_config) or (reg.get("fuente", "estatal") in fuentes_config)
    territorio_ok = pasa_territorio(reg.get("plataforma"), reg.get("region_codigo"))
    if categoria_reev is None or not fuente_ok or not territorio_ok:
        del datos[clave]
        podadas += 1
    else:
        # Se refrescan los DOS: la categoría y el motivo. Antes el motivo se escribía
        # una sola vez, el día que la licitación era nueva, y nunca más: ni al volver a
        # verla en el feed (esa rama solo toca fechas e importes) ni aquí. Resultado
        # medido el 09/10/2026 sobre las 714 guardadas: 13 tarjetas decían «coincide con
        # 9073» o «CPV 90920000» -- criterios que ya son de a_revisar -- estando en
        # críticas por OTRO criterio. La categoría era correcta; el motivo, de julio.
        # El motivo ya lo calcula busca_coincidencia aquí mismo: antes se tiraba.
        if motivo_reev != reg.get("coincidencia"):
            recoincidencias += 1
        reg["categoria"] = categoria_reev
        reg["coincidencia"] = motivo_reev

# --- CONTROL antes de escribir ----------------------------------------------
# Una recuperación solo puede SUMAR: si el total baja, es que la poda ha quitado cosas
# (criterios cambiados) y eso no debe pasar de tapadillo mientras se recupera.
_baja = len(datos) < TOTAL_ANTES
if MODO_RECUPERACION and _baja:
    print("=" * 70)
    print(f"CONTROL: el total BAJARÍA de {TOTAL_ANTES} a {len(datos)} ({podadas} podadas).")
    print("Una recuperación no puede quitar licitaciones. NO se escribe nada.")
    print("Si de verdad quieres podar, hazlo con una ejecución normal del radar.")
    print("=" * 70)
    sys.exit(2)

# Guardamos el diccionario completo.
# ensure_ascii=False -> conserva tildes y "ñ"; indent=2 -> deja el diff de Git legible.
if ESCRIBIR:
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
else:
    print("=" * 70)
    print("SIMULACIÓN: no se ha escrito nada. Añade --escribir para guardar de verdad.")

# --- 6. Resumen de la persistencia ------------------------------------------
print("=" * 70)
print(f"Total de licitaciones en el archivo: {len(datos)}")
# Conteo por fuente de TODO lo guardado (cumple "nº de licitaciones por fuente").
conteo_fuente = Counter(registro["fuente"] for registro in datos.values())
detalle_guardadas = ", ".join(f"{n} {fuente}" for fuente, n in sorted(conteo_fuente.items()))
print(f"Por fuente en el archivo: {detalle_guardadas}.")
print(f"Podadas (ya no encajan con la config actual): {podadas}")
if recoincidencias:
    # Se dice en voz alta porque es el síntoma de que los criterios han cambiado: una
    # tarjeta que ahora entra por otro criterio distinto del que la trajo.
    print(f"Motivo actualizado (entraban por otro criterio): {recoincidencias}")
print(f"Nuevas en esta ejecución: {len(nuevas)}")
for lic in nuevas:
    print(f"  - [{lic['fuente']}/{lic['categoria']}] {lic['titulo']}")

# --- CONTROL de la recuperación ---------------------------------------------
# Tres comprobaciones, con los números a la vista: que el total no baja, que lo leído
# cuadra con lo que dice el ZIP, y en qué día cae cada licitación nueva.
if MODO_RECUPERACION:
    from datetime import date as _date
    print("=" * 70)
    print("CONTROL DE LA RECUPERACIÓN")
    print(f"  Total en el archivo: {TOTAL_ANTES} antes -> {len(datos)} después "
          f"({len(datos) - TOTAL_ANTES:+d}) · podadas {podadas}")
    for _fuente, _cuentas in cuentas_zip.items():
        _leidas = sum(_cuentas.values())
        print(f"  ZIP «{_fuente}»: {_leidas:,} entradas en el rango, "
              f"{leidas_por_fuente.get(_fuente, 0):,} procesadas "
              f"({'cuadra' if _leidas == leidas_por_fuente.get(_fuente, 0) else 'NO CUADRA'})")
    _por_dia_nuevas = Counter((lic.get("fecha_publicacion") or "?")[:10] for lic in nuevas)
    if _por_dia_nuevas:
        print("  Nuevas por fecha de publicación: "
              + " · ".join(f"{d} {n}" for d, n in sorted(_por_dia_nuevas.items())))
    _hoy = _date.today().isoformat()
    _en_plazo = [lic for lic in nuevas if (lic.get("fecha_fin_plazo") or "") >= _hoy]
    print(f"  De las nuevas, EN PLAZO hoy: {len(_en_plazo)}")
    for lic in sorted(_en_plazo, key=lambda x: x.get("fecha_fin_plazo") or ""):
        print(f"    hasta {lic.get('fecha_fin_plazo')} · [{lic.get('categoria')}] "
              f"{(lic.get('titulo') or '')[:80]}")
    print("=" * 70)
