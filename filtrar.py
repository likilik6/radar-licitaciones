# Filtra las licitaciones del feed de la Plataforma de Contratación
# según los criterios que tenemos guardados en intereses.yaml.

# Librerías que usamos:
# - requests: para descargar el feed de internet.
# - yaml (pyyaml): para leer nuestro archivo intereses.yaml.
# - lxml: para leer el XML del feed y sacar el código CPV de cada licitación.
# - utiles.normaliza: para comparar texto ignorando mayúsculas y tildes
#   (la misma función la usa generar_web.py; por eso vive en utiles.py).
# - sys: solo para que los acentos se vean bien al imprimir en Windows.
# - json: para guardar las licitaciones en un archivo .json (librería estándar).
# - pathlib (Path): para manejar rutas y crear la carpeta data/ si no existe.
# - datetime: para apuntar cuándo vemos cada licitación por primera y última vez.
import sys
import json
import requests
import yaml
from pathlib import Path
from datetime import datetime
from lxml import etree

# normaliza() vive en utiles.py para compartirla con generar_web.py sin duplicarla.
from utiles import normaliza

# Hacemos que la consola muestre los acentos y la "ñ" correctamente.
sys.stdout.reconfigure(encoding="utf-8")

# La misma dirección y la misma cabecera de navegador que en fetch.py
# (el servidor bloquea las peticiones que no parezcan de un navegador).
URL = "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"
CABECERAS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

# El feed usa "espacios de nombres" (namespaces) para etiquetar el XML.
# Le ponemos un apodo corto a cada uno para poder buscar las etiquetas:
#   - "atom" es el formato del feed (donde están el título y el enlace).
#   - "cbc"  es donde vive el código CPV de cada licitación.
#   - "cac" agrupa bloques (proyecto, presupuesto, proceso de licitación...).
#   - "place" es la extensión de la Plataforma (donde está la fecha de publicación).
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "place": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
}


def a_texto(valor):
    """Limpia un texto del feed. Devuelve None si no venía (para guardar 'null'
    en el JSON) en vez de una cadena vacía."""
    if valor is None:
        return None
    valor = valor.strip()
    return valor or None


def a_numero(valor):
    """Convierte un importe del feed (texto como '722654.59') a número (float).
    Devuelve None si el campo no venía o no se puede convertir, para no romper
    la ejecución cuando una licitación no trae ese dato."""
    valor = a_texto(valor)
    if valor is None:
        return None
    try:
        return float(valor)
    except ValueError:
        return None


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


# --- 1. Cargamos los criterios desde intereses.yaml -------------------------
# Lo abrimos con encoding utf-8 porque tiene acentos y "ñ".
with open("intereses.yaml", encoding="utf-8") as f:
    intereses = yaml.safe_load(f)

# "intereses" es un diccionario con TODAS las categorías (criticas, a_revisar,
# pruebas, y las que añadas en el futuro). El ORDEN en que aparecen en el YAML
# marca la prioridad: una licitación se queda en la PRIMERA categoría con la que
# coincide. Así no hay nombres de categoría fijos en el código.

# --- 2. Descargamos el feed -------------------------------------------------
respuesta = requests.get(URL, headers=CABECERAS)

# --- 3. Lo leemos como XML con lxml -----------------------------------------
# Le pasamos los bytes (respuesta.content); lxml detecta solo la codificación.
raiz = etree.fromstring(respuesta.content)

# Cada licitación es una etiqueta <entry> dentro del feed.
entradas = raiz.findall("atom:entry", NS)

# Una lista de resultados por CADA categoría del YAML, en el mismo orden.
# (un diccionario: nombre_de_categoria -> lista de licitaciones de esa categoría)
resultados = {nombre: [] for nombre in intereses}

# --- 4. Recorremos las licitaciones una a una -------------------------------
for entrada in entradas:
    # Título de la licitación.
    titulo = entrada.findtext("atom:title", default="(sin título)", namespaces=NS).strip()

    # Enlace (está en el atributo "href" de la etiqueta <link>).
    link = entrada.find("atom:link", NS)
    enlace = link.get("href") if link is not None else "(sin enlace)"

    # Id único de la licitación. En el XML es la etiqueta <id> (equivale al
    # entry.id de feedparser). Si por lo que fuera no viniera, usamos el enlace.
    id_unico = entrada.findtext("atom:id", default="", namespaces=NS).strip() or enlace

    # Fecha de actualización: etiqueta <updated> (equivale a entry.get("updated", "")).
    fecha_actualizacion = entrada.findtext("atom:updated", default="", namespaces=NS).strip()

    # Códigos CPV: pueden ser varios, así que los recogemos todos en una lista.
    # Están en las etiquetas <cbc:ItemClassificationCode> dentro de la entrada.
    cpvs = [c.text for c in entrada.findall(".//cbc:ItemClassificationCode", NS) if c.text]

    # --- Datos económicos y fechas (del mismo bloque CODICE que el CPV) -------
    # El presupuesto y el valor estimado viven en cac:BudgetAmount, DENTRO del
    # proyecto principal (cac:ProcurementProject, hijo directo de ContractFolderStatus).
    # Fijamos la ruta exacta para NO coger por error el presupuesto de un lote
    # (los lotes cuelgan de cac:ProcurementProjectLot). Si algún campo no viene,
    # findtext devuelve None y nuestras funciones lo dejan en None (null en el JSON).
    base_presupuesto = ".//place:ContractFolderStatus/cac:ProcurementProject/cac:BudgetAmount/"
    presupuesto_con_iva = a_numero(entrada.findtext(base_presupuesto + "cbc:TotalAmount", namespaces=NS))
    presupuesto_sin_iva = a_numero(entrada.findtext(base_presupuesto + "cbc:TaxExclusiveAmount", namespaces=NS))
    valor_estimado = a_numero(entrada.findtext(base_presupuesto + "cbc:EstimatedOverallContractAmount", namespaces=NS))

    # Fecha de fin del plazo de presentación de ofertas.
    fecha_fin_plazo = a_texto(entrada.findtext(
        ".//place:ContractFolderStatus/cac:TenderingProcess"
        "/cac:TenderSubmissionDeadlinePeriod/cbc:EndDate", namespaces=NS))

    # Fecha de publicación del anuncio (la fecha de emisión del aviso).
    fecha_publicacion = a_texto(entrada.findtext(
        ".//place:ValidNoticeInfo//cbc:IssueDate", namespaces=NS))

    # Normalizamos el título una sola vez para comparar palabras clave.
    titulo_normalizado = normaliza(titulo)

    # Datos que guardaremos de esta licitación. La "categoria" y la "coincidencia"
    # se añaden justo abajo, cuando sepamos en qué grupo cae.
    registro = {
        "id": id_unico,
        "titulo": titulo,
        "enlace": enlace,
        "cpv": cpvs,
        "presupuesto_con_iva": presupuesto_con_iva,
        "presupuesto_sin_iva": presupuesto_sin_iva,
        "valor_estimado": valor_estimado,
        "fecha_fin_plazo": fecha_fin_plazo,
        "fecha_publicacion": fecha_publicacion,
        "fecha_actualizacion": fecha_actualizacion,
    }

    # Clasificamos recorriendo las categorías EN ORDEN (criticas, a_revisar,
    # pruebas...). La PRIMERA con la que coincida se queda con la licitación; por
    # eso el orden del YAML define la prioridad (de más a menos específica).
    for nombre, criterios in intereses.items():
        motivo = busca_coincidencia(cpvs, titulo_normalizado, criterios)
        if motivo:
            registro["categoria"] = nombre            # el nombre de la categoría del YAML
            registro["coincidencia"] = motivo
            resultados[nombre].append(registro)
            break  # ya está clasificada: no miramos categorías de menor prioridad
    # Si ninguna categoría coincide, la licitación se ignora (no la guardamos).

# --- 5. Mostramos los resultados agrupados, una sección por categoría --------
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

# --- 6. Resumen de esta ejecución -------------------------------------------
print("=" * 70)
# Un trocito de texto por categoría: "0 en criticas, 0 en a_revisar, 5 en pruebas".
detalle = ", ".join(f"{len(lista)} en {nombre}" for nombre, lista in resultados.items())
print(f"Resumen: {detalle}; sobre {len(entradas)} licitaciones en total.")

# --- 7. Guardamos las licitaciones en data/licitaciones.json ----------------
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
        # correcciones de presupuesto, ampliaciones de plazo...). NO tocamos "primera_vez".
        datos[clave]["ultima_vez"] = ahora
        datos[clave]["fecha_actualizacion"] = lic["fecha_actualizacion"]
        datos[clave]["presupuesto_con_iva"] = lic["presupuesto_con_iva"]
        datos[clave]["presupuesto_sin_iva"] = lic["presupuesto_sin_iva"]
        datos[clave]["valor_estimado"] = lic["valor_estimado"]
        datos[clave]["fecha_fin_plazo"] = lic["fecha_fin_plazo"]
        datos[clave]["fecha_publicacion"] = lic["fecha_publicacion"]

# Guardamos el diccionario completo.
# ensure_ascii=False -> conserva tildes y "ñ"; indent=2 -> deja el diff de Git legible.
with open(ruta_json, "w", encoding="utf-8") as f:
    json.dump(datos, f, ensure_ascii=False, indent=2)

# --- 8. Resumen de la persistencia ------------------------------------------
print("=" * 70)
print(f"Total de licitaciones en el archivo: {len(datos)}")
print(f"Nuevas en esta ejecución: {len(nuevas)}")
for lic in nuevas:
    print(f"  - [{lic['categoria']}] {lic['titulo']}")
