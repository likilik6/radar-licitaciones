# feeds.py — fuentes de datos del radar y "extractor" común (descarga + paginación).
#
# Aquí vive lo que comparten filtrar.py y fetch.py para NO duplicarlo:
#   - la LISTA de feeds ATOM/CODICE que procesa el radar (cada uno con su "fuente"),
#   - la cabecera de navegador (sin ella el WAF del servidor bloquea la petición),
#   - la función que descarga un feed siguiendo la paginación (atom:link rel="next").
#
# El MISMO extractor sirve para todos los feeds porque todos vienen en el mismo
# formato (ATOM con el bloque CODICE donde está el CPV). Añadir un feed nuevo es
# solo añadir una entrada a la lista FEEDS de abajo.
import re
import time
import requests
from datetime import date
from lxml import etree

# Espacio de nombres mínimo para navegar el ATOM: localizar las entradas
# (<atom:entry>) y el enlace a la página siguiente (<atom:link rel="next">).
# El resto de namespaces (CPV, presupuesto...) los usa filtrar.py al extraer.
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# El servidor rechaza las peticiones que no parezcan de un navegador, así que
# enviamos una cabecera "User-Agent" que imita a un navegador real. OBLIGATORIO.
CABECERAS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

# La LISTA de feeds que vigila el radar. Cada uno lleva su etiqueta de ORIGEN
# ("fuente") para poder distinguir de dónde salió cada licitación:
#   - "estatal":   sindicación 643 (Plataforma de Contratación del Sector Público).
#   - "agregadas": sindicación 1044 (CCAA y entidades locales con plataforma propia
#                  que se "enganchan" a la estatal; mismo formato ATOM/CODICE).
FEEDS = [
    {
        "fuente": "estatal",
        "url": "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom",
    },
    {
        "fuente": "agregadas",
        "url": "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_1044/PlataformasAgregadasSinMenores.atom",
    },
]

# Tope de páginas por feed en cada ejecución.
# Los feeds paginan con atom:link rel="next", pero NO son "500 fijos": cada "next"
# apunta a un snapshot anterior en el tiempo, así que la cadena se remonta hacia
# atrás muchos días (cientos de páginas si se agotara). Como la automatización
# corre de lunes a viernes, el lunes debe cubrir el hueco del fin de semana; con
# ~20 páginas (≈10.000 entradas por feed) hay margen de sobra sin que el job se
# dispare. Si se alcanza el tope, descarga_entradas avisa (no se trunca en silencio).
MAX_PAGINAS = 20

# Pausa (segundos) entre página y página, por cortesía con el servidor.
PAUSA_ENTRE_PAGINAS = 0.5


def _siguiente_pagina(raiz):
    """Devuelve la URL de la página siguiente (atom:link rel="next") o None si no hay."""
    for link in raiz.findall("atom:link", ATOM_NS):
        if link.get("rel") == "next":
            return link.get("href")
    return None


def descarga_entradas(url, max_paginas=MAX_PAGINAS, pausa=PAUSA_ENTRE_PAGINAS):
    """Descarga un feed ATOM siguiendo la paginación rel="next" hasta agotarla o
    hasta llegar a 'max_paginas'. Mantiene la cabecera de navegador en cada petición.

    Devuelve una tupla (entradas, paginas_leidas, tope_alcanzado):
      - entradas:        lista de elementos <atom:entry> (lxml) de TODAS las páginas.
      - paginas_leidas:  cuántas páginas se llegaron a descargar.
      - tope_alcanzado:  True si se paró por el tope (quedaba 'next' sin leer),
                         para poder avisar de que puede faltar histórico.

    Si una página falla (error de red o respuesta no válida), se avisa y se deja
    de paginar ESE feed con lo que se llevara descargado, para no tumbar todo el
    trabajo por un fallo puntual."""
    entradas = []
    paginas_leidas = 0
    while url and paginas_leidas < max_paginas:
        try:
            respuesta = requests.get(url, headers=CABECERAS, timeout=60)
            respuesta.raise_for_status()
            raiz = etree.fromstring(respuesta.content)
        except Exception as error:
            # Fallo puntual (red, 5xx, XML corrupto...): avisamos y paramos este feed.
            print(f"  AVISO: no se pudo leer una página ({error}); se deja de paginar este feed.")
            return entradas, paginas_leidas, False

        entradas.extend(raiz.findall("atom:entry", ATOM_NS))
        paginas_leidas += 1
        url = _siguiente_pagina(raiz)

        # Pausa solo si vamos a pedir otra página.
        if url and paginas_leidas < max_paginas:
            time.sleep(pausa)

    # Si todavía queda un 'next' es que paramos por el tope, no porque se agotara.
    tope_alcanzado = bool(url)
    return entradas, paginas_leidas, tope_alcanzado


# ============================================================================
# EXTRACTOR de campos CODICE (el ÚNICO punto de extracción del proyecto)
# ----------------------------------------------------------------------------
# Convierte una <atom:entry> en un dict plano con los campos que nos interesan.
# Lo usan TANTO el radar (filtrar.py) COMO el backfill del buscador
# (backfill_catalogo.py): así la lógica de XPaths CODICE vive en UN solo sitio y
# no se duplica. Si algún día el feed cambia una ruta, se toca aquí y punto.
# ============================================================================

# Espacios de nombres completos del feed (ATOM + las tres extensiones CODICE):
#   - atom: el formato del feed (título, enlace, id, updated).
#   - cbc:  componentes básicos (donde vive el código CPV, importes, fechas...).
#   - cac:  componentes agregados (proyecto, presupuesto, proceso de licitación...).
#   - place: extensión de la Plataforma (ContractFolderStatus, fecha de publicación...).
#   - pce:  extensión de la Plataforma, componentes BÁSICOS (donde vive el
#           ContractFolderStatusCode: estado del expediente, ADJ/RES/EV...).
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "place": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
    "pce": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2",
}


def a_texto(valor):
    """Limpia un texto del feed. Devuelve None si no venía (en vez de cadena vacía),
    para poder guardar 'null' aguas abajo."""
    if valor is None:
        return None
    valor = valor.strip()
    return valor or None


def a_numero(valor):
    """Convierte un importe del feed (texto como '722654.59') a float. Devuelve None
    si el campo no venía o no se puede convertir, para no romper cuando falte el dato."""
    valor = a_texto(valor)
    if valor is None:
        return None
    try:
        return float(valor)
    except ValueError:
        return None


def a_entero(valor):
    """Convierte un texto del feed a int. Acepta '12' y '12.0'. None si no se puede."""
    valor = a_texto(valor)
    if valor is None:
        return None
    try:
        return int(valor)
    except ValueError:
        try:
            return int(float(valor))
        except ValueError:
            return None


def a_booleano(valor):
    """Convierte 'true'/'false' (SMEAwardedIndicator) a bool. None si no viene/otro."""
    valor = a_texto(valor)
    if valor is None:
        return None
    v = valor.lower()
    if v in ("true", "1", "si", "sí", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return None


def a_fecha(valor):
    """Fecha del feed ('2026-06-03' o '2026-06-03T00:00:00') recortada a 'AAAA-MM-DD'
    para una columna DATE. None si no viene o no es una fecha de CALENDARIO válida.
    Validamos con datetime.date (no solo el formato): así una fecha malformada del feed
    (p. ej. '2026-13-40' o '2026-02-30') se descarta como None en vez de provocar un
    400 de Postgres que abortaría el lote entero al insertar."""
    valor = a_texto(valor)
    if valor is None:
        return None
    d = valor[:10]
    if len(d) == 10 and d[4] == "-" and d[7] == "-":
        try:
            date(int(d[:4]), int(d[5:7]), int(d[8:10]))   # valida calendario real
            return d
        except ValueError:
            return None
    return None


def normaliza_cif(valor):
    """Normaliza un CIF/NIF para el cruce por CIF (fase D2) y la agregación (fase E):
    MAYÚSCULAS y sin espacios, puntos, guiones ni barras. None si no hay valor.
    OJO: se aplica IGUAL en el extractor y donde se comparen los CIF de LODEPA."""
    valor = a_texto(valor)
    if valor is None:
        return None
    limpio = re.sub(r"[\s./-]", "", valor).upper()
    return limpio or None


# Etiquetas legibles del cbc:ResultCode (code list oficial TenderResultCode-2.09 de
# la PLACSP). El código crudo se guarda aparte (resultado_code); esto es solo para
# leerlo. Para AGREGAR por "adjudicado vs desierto" en la fase E, el criterio fiable
# es la PRESENCIA de adjudicatario (cif_adjudicatario != ''), no el código.
TENDER_RESULT_LABELS = {
    "1": "Adjudicado provisionalmente",
    "2": "Adjudicado definitivamente",
    "3": "Desierto",
    "4": "Desistimiento",
    "5": "Renuncia",
    "6": "Desierto provisionalmente",
    "7": "Desierto definitivamente",
    "8": "Adjudicado",
    "9": "Formalizado",
    "10": "Licitador mejor valorado",
    "11": "Encargo formalizado",
}


def _id_adjudicatario(winning):
    """De un cac:WinningParty devuelve (id_bruto, schemeName). Prefiere el ID con
    schemeName='NIF'; si no hay, coge el PRIMER PartyIdentification/ID que haya y
    devuelve su scheme (puede ser 'UTE', 'OTROS'...). (id_bruto sin normalizar aún.)"""
    ids = winning.findall("cac:PartyIdentification/cbc:ID", NS)
    if not ids:
        return None, None
    elegido = None
    for idel in ids:
        if (idel.get("schemeName") or "").upper() == "NIF":
            elegido = idel
            break
    if elegido is None:
        elegido = ids[0]
    return a_texto(elegido.text), a_texto(elegido.get("schemeName"))


def extrae_adjudicaciones(entrada):
    """Extrae los bloques cac:TenderResult (uno por LOTE) del CODICE de una <entry>.

    Devuelve una LISTA de dicts, UNA FILA POR (lote, adjudicatario):
      - un lote adjudicado a un único CIF -> una fila;
      - un lote con varios cac:WinningParty (acuerdo marco / UTE separadas) -> una
        fila por adjudicatario (el importe del lote se repite en cada una);
      - un lote DESIERTO (TenderResult sin WinningParty) -> una fila con
        cif_adjudicatario / adjudicatario / id_scheme = None.
    Lista VACÍA si el expediente aún no trae adjudicación.

    La PRESENCIA del bloque TenderResult es la señal fiable: NO se condiciona la
    extracción al estado del expediente (hay estados intermedios, p. ej.
    'parcialmente adjudicada'). El estado se guarda solo como contexto.

    Campos None cuando el feed no los trae (el convenio '' para lote/cif de la
    tabla se aplica AL UPSERTAR, no aquí; ver backfill_catalogo.fila_adjudicacion)."""
    # Estado del expediente (ADJ/RES/EV/...): SOLO contexto; no decide si extraer.
    estado_exp = a_texto(entrada.findtext(
        ".//place:ContractFolderStatus/pce:ContractFolderStatusCode", namespaces=NS))

    filas = []
    for tr in entrada.findall(".//place:ContractFolderStatus/cac:TenderResult", NS):
        resultado_code = a_texto(tr.findtext("cbc:ResultCode", namespaces=NS))
        resultado = TENDER_RESULT_LABELS.get(resultado_code) if resultado_code else None
        # Datos comunes del lote (compartidos por todos sus adjudicatarios).
        proyecto = tr.find("cac:AwardedTenderedProject", NS)
        lote = importe_sin_iva = importe_con_iva = None
        if proyecto is not None:
            lote = a_texto(proyecto.findtext("cbc:ProcurementProjectLotID", namespaces=NS))
            base_total = "cac:LegalMonetaryTotal/"
            importe_sin_iva = a_numero(proyecto.findtext(base_total + "cbc:TaxExclusiveAmount", namespaces=NS))
            importe_con_iva = a_numero(proyecto.findtext(base_total + "cbc:PayableAmount", namespaces=NS))
        comun = {
            "lote": lote,
            "resultado_code": resultado_code,
            "resultado": resultado,
            "es_pyme": a_booleano(tr.findtext("cbc:SMEAwardedIndicator", namespaces=NS)),
            "importe_sin_iva": importe_sin_iva,
            "importe_con_iva": importe_con_iva,
            "n_ofertas": a_entero(tr.findtext("cbc:ReceivedTenderQuantity", namespaces=NS)),
            "fecha_adjudicacion": a_fecha(tr.findtext("cbc:AwardDate", namespaces=NS)),
            "estado_expediente": estado_exp,
        }

        ganadores = tr.findall("cac:WinningParty", NS)
        if not ganadores:
            # Lote desierto / sin adjudicatario: una fila con CIF/nombre None.
            filas.append({**comun, "cif_adjudicatario": None, "id_scheme": None, "adjudicatario": None})
            continue
        for win in ganadores:
            id_bruto, scheme = _id_adjudicatario(win)
            filas.append({
                **comun,
                "cif_adjudicatario": normaliza_cif(id_bruto),
                "id_scheme": scheme,
                "adjudicatario": a_texto(win.findtext("cac:PartyName/cbc:Name", namespaces=NS)),
            })
    return filas


def extrae_entrada(entrada, fuente):
    """Extrae a un dict plano los campos CODICE de una <atom:entry> (elemento lxml).
    'fuente' es la etiqueta de origen ('estatal' / 'agregadas'). Devuelve SIEMPRE
    las mismas claves; los campos que no vengan quedan en None (o lista vacía en cpv)."""
    # Título y enlace.
    titulo = entrada.findtext("atom:title", default="(sin título)", namespaces=NS).strip()
    link = entrada.find("atom:link", NS)
    enlace = link.get("href") if link is not None else "(sin enlace)"

    # Id único de la licitación (atom:id == entry.id). Si faltara, caemos al enlace.
    id_unico = entrada.findtext("atom:id", default="", namespaces=NS).strip() or enlace
    # Fecha de actualización del aviso (atom:updated).
    fecha_actualizacion = entrada.findtext("atom:updated", default="", namespaces=NS).strip()

    # Códigos CPV: pueden ser varios; los recogemos todos.
    cpvs = [c.text for c in entrada.findall(".//cbc:ItemClassificationCode", NS) if c.text]

    # Objeto del contrato: el nombre del proyecto principal (cac:ProcurementProject),
    # hijo directo del ContractFolderStatus (NO el de un lote, que cuelga de
    # ProcurementProjectLot). El atom:title suele ser un resumen; el objeto es éste.
    base_proyecto = ".//place:ContractFolderStatus/cac:ProcurementProject/"
    objeto = a_texto(entrada.findtext(base_proyecto + "cbc:Name", namespaces=NS))

    # Importes (mismo bloque BudgetAmount del proyecto principal, no de un lote).
    base_presupuesto = base_proyecto + "cac:BudgetAmount/"
    presupuesto_con_iva = a_numero(entrada.findtext(base_presupuesto + "cbc:TotalAmount", namespaces=NS))
    presupuesto_sin_iva = a_numero(entrada.findtext(base_presupuesto + "cbc:TaxExclusiveAmount", namespaces=NS))
    valor_estimado = a_numero(entrada.findtext(base_presupuesto + "cbc:EstimatedOverallContractAmount", namespaces=NS))

    # Fin del plazo de presentación de ofertas.
    fecha_fin_plazo = a_texto(entrada.findtext(
        ".//place:ContractFolderStatus/cac:TenderingProcess"
        "/cac:TenderSubmissionDeadlinePeriod/cbc:EndDate", namespaces=NS))
    # Fecha de publicación del anuncio (fecha de emisión del aviso).
    fecha_publicacion = a_texto(entrada.findtext(
        ".//place:ValidNoticeInfo//cbc:IssueDate", namespaces=NS))

    # Territorio: quién contrata (organismo), la plataforma agregadora y la región.
    base_parte = ".//place:ContractFolderStatus/place:LocatedContractingParty/cac:Party/"
    organismo = a_texto(entrada.findtext(base_parte + "cac:PartyName/cbc:Name", namespaces=NS))
    plataforma = a_texto(entrada.findtext(base_parte + "cac:AgentParty/cac:PartyName/cbc:Name", namespaces=NS))
    base_lugar = ".//place:ContractFolderStatus/cac:ProcurementProject/cac:RealizedLocation/"
    region = a_texto(entrada.findtext(base_lugar + "cbc:CountrySubentity", namespaces=NS))
    region_codigo = a_texto(entrada.findtext(base_lugar + "cbc:CountrySubentityCode", namespaces=NS))

    # Nº de EXPEDIENTE del órgano (cbc:ContractFolderID, hijo directo de
    # ContractFolderStatus). Es el código interno del expediente (p. ej. "V/0013/A/26/2"),
    # NO el atom:id (ese es la clave universal 'id'). Se guarda TAL CUAL, sin normalizar:
    # la normalización para buscar (mayúsculas, quitar / . - espacios) se hace en la BD.
    num_expediente = a_texto(entrada.findtext(
        ".//place:ContractFolderStatus/cbc:ContractFolderID", namespaces=NS))

    # Adjudicaciones (cac:TenderResult, una por lote/adjudicatario). Lista vacía si el
    # expediente aún no tiene adjudicación. ADITIVO: los consumidores actuales del dict
    # (filtrar.py, backfill fila_para_tabla) leen claves explícitas y lo ignoran; solo
    # lo usa el nuevo upsert a public.adjudicaciones. NO cambia el JSON del Radar.
    adjudicaciones = extrae_adjudicaciones(entrada)

    return {
        "id": id_unico,
        "titulo": titulo,
        "objeto": objeto,
        "enlace": enlace,
        "cpv": cpvs,
        "fuente": fuente,
        "num_expediente": num_expediente,
        "organismo": organismo,
        "plataforma": plataforma,
        "region": region,
        "region_codigo": region_codigo,
        "presupuesto_con_iva": presupuesto_con_iva,
        "presupuesto_sin_iva": presupuesto_sin_iva,
        "valor_estimado": valor_estimado,
        "fecha_fin_plazo": fecha_fin_plazo,
        "fecha_publicacion": fecha_publicacion,
        "fecha_actualizacion": fecha_actualizacion,
        "adjudicaciones": adjudicaciones,
    }
