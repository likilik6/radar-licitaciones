# Utilidades comunes a filtrar.py y generar_web.py.
# Aquí va SOLO lo que comparten los dos, para no duplicar la lógica ni tener que
# importar un script desde el otro: importar filtrar.py ejecutaría su descarga
# del feed (no tiene "if __name__ == ...") y eso no lo queremos al generar la web.
import os
import unicodedata
from pathlib import Path


def normaliza(texto):
    """Devuelve el texto en minúsculas y sin tildes, para poder comparar
    'sin distinguir mayúsculas ni tildes'."""
    texto = texto.lower()
    # NFKD separa cada letra de su tilde; nos quedamos con lo que NO es una tilde.
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in texto if not unicodedata.combining(c))


def busca_coincidencia(cpvs, titulo_normalizado, criterios):
    """Comprueba si una licitación encaja con un grupo de criterios.
    'criterios' es un bloque con dos listas: 'cpv' y 'palabras_clave'.
    Devuelve un texto explicando QUÉ criterio coincidió, o None si no coincide nada.

    Vive aquí (y no en filtrar.py) porque lo necesitan el robot, la poda y las pruebas;
    tenerlo en el script obligaba a copiarlo para probarlo, y una copia se desincroniza.
    """
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


# ---------------------------------------------------------------------------
# PRIORIDAD de las categorías: por qué está en el código y no en el panel
#
# Una licitación se queda en la PRIMERA categoría que casa, así que el orden ES la
# prioridad. Durante meses ese orden salía del diccionario de radar_config... y eso
# no era controlable desde el panel: la columna radar_config.config es JSONB, y
# jsonb guarda las claves de cada objeto ordenadas por (longitud, bytes). Guardar
# {criticas, a_revisar, pruebas} devuelve {pruebas, criticas, a_revisar}.
#
# Consecuencia real, medida el 02/10/2026: «pruebas» ganaba a «criticas» porque su
# nombre tiene una letra menos, y por eso 11 tarjetas buenas estaban enterradas en
# el cajón de pruebas (entre ellas 2 M€ de Bioseguridad de Murcia Oeste y los 779
# monitores de calidad del aire del CIBER). La prioridad la decidía la longitud del
# nombre del grupo. Por eso ahora se declara aquí, explícita y comprobada.
#
# Regla: primero las conocidas en este orden; después las que no conozcamos, en el
# orden en que vengan (determinista); y «pruebas» SIEMPRE al final, porque es el
# cajón de pruebas y no debe ganarle a nada.
# ---------------------------------------------------------------------------
ORDEN_CATEGORIAS = ("criticas", "a_revisar")
CATEGORIA_ULTIMA = "pruebas"


# ---------------------------------------------------------------------------
# EL RADAR (data/licitaciones.json): una ficha por expediente, con su versión MÁS NUEVA
#
# Hasta el PASO 0 del 15/10/2026, filtrar.py clasificaba TODAS las versiones que traen
# las ~20 páginas del feed y luego las fusionaba una detrás de otra: la última en
# fusionarse, que es la más VIEJA, pisaba plazo e importes. Así el Radar enseñaba
# «cerrado el 14/10» en una licitación con el plazo ampliado al 16/10. Ahora:
#   1. de cada expediente se elige la versión que gana (feeds.es_mas_nueva);
#   2. se clasifica ESA versión con los criterios;
#   3. se fusiona: una ficha guardada solo se pisa con una versión más nueva.
# La regla es la misma que la de la base (ver feeds.py, «VERSIONES Y BAJAS»).
# ---------------------------------------------------------------------------
# Lo que se guarda de cada ficha (además de primera_vez y ultima_vez).
CAMPOS_FICHA = ("id", "titulo", "enlace", "cpv", "fuente", "num_expediente", "organismo",
                "plataforma", "region", "region_codigo", "categoria", "coincidencia",
                "presupuesto_con_iva", "presupuesto_sin_iva", "valor_estimado", "fecha_fin_plazo",
                "fecha_publicacion", "fecha_actualizacion", "version_snapshot", "estado_expediente")
# Lo que se refresca cuando llega una versión más nueva: todo menos el id, la fuente
# (el origen no cambia) y primera_vez.
CAMPOS_REFRESCO = tuple(c for c in CAMPOS_FICHA if c not in ("id", "fuente"))


def _version(ficha):
    return (ficha.get("fecha_actualizacion"), ficha.get("version_snapshot"))


def fusiona_radar(datos, lic, ahora):
    """Mete en 'datos' (el contenido de data/licitaciones.json) la versión 'lic' de un
    expediente. Devuelve True si la ficha es NUEVA.
      · Si no estaba: se añade con primera_vez = ultima_vez = ahora.
      · Si estaba: ultima_vez = ahora (la hemos visto) y, SOLO si 'lic' es más nueva
        que la guardada (o la guardada no tiene fecha), se refrescan sus datos. Una
        versión vieja que llega después no pisa nada.
      · Una versión posterior a la baja marcada la desmarca (se republicó)."""
    from feeds import es_mas_nueva, instante
    clave = lic["id"]
    ficha = datos.get(clave)
    if ficha is None:
        datos[clave] = {**{c: lic.get(c) for c in CAMPOS_FICHA}, "primera_vez": ahora, "ultima_vez": ahora}
        return True
    ficha["ultima_vez"] = ahora
    if ficha.get("fecha_actualizacion") and not es_mas_nueva(_version(lic), _version(ficha)):
        return False
    for c in CAMPOS_REFRESCO:
        ficha[c] = lic.get(c)
    # Igual que en la base: la baja se conserva solo si es POSTERIOR a esta versión.
    baja, version = instante(ficha.get("baja_en")), instante(lic.get("fecha_actualizacion"))
    if baja is not None and version is not None and version >= baja:
        ficha.pop("baja_en", None)
        ficha.pop("baja_tipo", None)
    return False


def radar_pasada(datos, registros, criterios, territorio_ok, ahora, resultados=None):
    """Una pasada del Radar sobre las versiones leídas del feed.

    registros: dicts de feeds.extrae_entrada con, además, 'version_snapshot'. Puede
    haber varios del mismo expediente (uno por versión), en cualquier orden.
    criterios: {categoría: {'cpv': [...], 'palabras_clave': [...]}} en orden de
    prioridad (ordena_categorias). territorio_ok(plataforma, region_codigo) -> bool.
    resultados: si se pasa un dict, se rellena {categoría: [fichas]} para el log.

    De cada expediente se clasifica SOLO su versión más nueva. Si casa, se fusiona
    (fusiona_radar). Si no casa pero la ficha ya estaba, se refrescan sus datos
    cambiantes (plazo, importes, fechas, estado, enlace...) y se CONSERVAN los que
    deciden la poda (título, CPV, territorio, categoría), como hacía el código de antes:
    una versión con el título roto (mojibake «MonitorizaciÃ³n», visto en el Gobierno
    Vasco desde el 07/10) no puede hacer desaparecer una ficha buena.
    Devuelve la lista de fichas NUEVAS (las de data/licitaciones.json)."""
    from feeds import es_mas_nueva
    ultimas = {}
    for reg in registros:
        actual = ultimas.get(reg["id"])
        if actual is None or es_mas_nueva(_version(reg), _version(actual)):
            ultimas[reg["id"]] = reg
    nuevas = []
    for reg in ultimas.values():
        titulo_normalizado = normaliza(reg.get("titulo") or "")
        categoria = motivo = None
        for nombre, crit in criterios.items():
            motivo = busca_coincidencia(reg.get("cpv") or [], titulo_normalizado, crit)
            if motivo:
                # Cazada por interés; ahora debe pasar el filtro de TERRITORIO.
                if territorio_ok(reg.get("plataforma"), reg.get("region_codigo")):
                    categoria = nombre
                break   # ya decidida (guardada o descartada por territorio)
        if categoria:
            lic = {**reg, "categoria": categoria, "coincidencia": motivo}
            if resultados is not None:
                resultados.setdefault(categoria, []).append(lic)
            if fusiona_radar(datos, lic, ahora):
                nuevas.append(datos[lic["id"]])
        elif reg["id"] in datos:
            vieja = datos[reg["id"]]
            fusiona_radar(datos, {**reg, **{c: vieja.get(c) for c in CAMPOS_FIJOS_SI_NO_CASA}}, ahora)
    return nuevas


# Lo que NO se toca de una ficha guardada cuando su versión nueva ya no casa con los
# criterios: es lo que decide la poda (ver radar_pasada).
CAMPOS_FIJOS_SI_NO_CASA = ("titulo", "cpv", "plataforma", "region", "region_codigo",
                           "categoria", "coincidencia")


def marca_bajas_radar(datos, bajas):
    """Marca en las fichas del Radar las bajas del feed (<at:deleted-entry>): baja_en y
    baja_tipo, si la baja es POSTERIOR a la versión guardada y a la baja ya marcada.
    Nunca borra la ficha. bajas: dicts {id, cuando, tipo} (feeds.parsea_baja).
    Devuelve cuántas ha marcado."""
    from feeds import instante
    marcadas = 0
    for baja in bajas:
        ficha = datos.get(baja.get("id"))
        cuando = instante(baja.get("cuando"))
        if ficha is None or cuando is None:
            continue
        version = instante(ficha.get("fecha_actualizacion"))
        anterior = instante(ficha.get("baja_en"))
        if (version is None or cuando > version) and (anterior is None or cuando > anterior):
            ficha["baja_en"], ficha["baja_tipo"] = baja["cuando"], baja.get("tipo")
            marcadas += 1
    return marcadas


def ordena_categorias(categorias):
    """Devuelve las categorías en orden de PRIORIDAD (un dict nuevo, mismo contenido).

    No inventa ni descarta grupos: si una categoría conocida no está, se la salta; y
    si aparece una nueva en el panel, entra en medio (nunca por delante de 'criticas'
    ni por detrás de 'pruebas'), para que añadir un grupo no cambie en silencio lo que
    ya funcionaba."""
    if not isinstance(categorias, dict) or not categorias:
        return {}
    orden = [n for n in ORDEN_CATEGORIAS if n in categorias]
    orden += [n for n in categorias if n not in ORDEN_CATEGORIAS and n != CATEGORIA_ULTIMA]
    if CATEGORIA_ULTIMA in categorias:
        orden.append(CATEGORIA_ULTIMA)
    return {n: categorias[n] for n in orden}


# ---------------------------------------------------------------------------
# Credencial para leer radar_config (la configuración del panel ⚙)
#
# POR QUÉ EXISTE ESTO: hasta septiembre de 2026 la config se leía con la clave
# "publishable" (pública), porque la tabla tenía lectura para todo el mundo. Al
# preparar el segundo perfil esa lectura se cierra: cada perfil solo puede ver
# su propia fila. Desde entonces el robot tiene que identificarse.
#
# ORDEN DE BÚSQUEDA:
#   1. SUPABASE_SERVICE_ROLE  -> es la que usa GitHub Actions (secret ya existente)
#   2. SUPABASE_SECRET_KEY    -> la del .env, para cuando lo ejecutas en tu portátil
#   3. la publishable          -> solo como respaldo; dejará de servir cuando se
#                                cierre la lectura pública de radar_config
# ---------------------------------------------------------------------------
def en_actions():
    """True si esto corre dentro de GitHub Actions (el robot), no en tu portátil."""
    return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"


def lee_env(ruta=None):
    """Lee un .env sencillo (CLAVE=valor). Nunca imprime los valores."""
    ruta = Path(ruta) if ruta else Path(__file__).resolve().parent / ".env"
    datos = {}
    if not ruta.exists():
        return datos
    try:
        # utf-8-sig quita la marca invisible que ponen el Bloc de notas y PowerShell
        # al guardar; sin eso, la PRIMERA clave del fichero no se reconoce nunca.
        # Y errors="replace" evita que un acento mal guardado tumbe todo el robot.
        texto = ruta.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return datos
    for linea in texto.splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        datos[clave.strip()] = valor.strip().strip('"').strip("'")
    return datos


def credencial_config(publishable):
    """Devuelve (clave, origen) para leer radar_config. 'origen' es un texto
    corto para el log, que ayuda a saber con qué credencial se leyó. NUNCA
    devuelve la clave en el texto del origen."""
    servicio = os.environ.get("SUPABASE_SERVICE_ROLE")
    if servicio:
        return servicio, "SUPABASE_SERVICE_ROLE"

    # Solo ahora hace falta mirar el .env (en Actions no existe).
    secreta = os.environ.get("SUPABASE_SECRET_KEY") or lee_env().get("SUPABASE_SECRET_KEY")
    if secreta:
        return secreta, "SUPABASE_SECRET_KEY (.env)"

    return publishable, "clave publishable (respaldo)"


def get_con_reintentos(url, params, headers, timeout=30, intentos=3, pausa=4):
    """GET que reintenta los fallos PASAJEROS. Devuelve la respuesta ya validada.

    POR QUÉ: desde que no poder leer radar_config aborta el workflow, un hipo de red
    o un 503 de Supabase tumbaría la publicación de toda la mañana. Antes daba igual
    porque se seguía con intereses.yaml; ahora no, así que hay que ser tolerante con
    lo pasajero y tajante con lo que no lo es.

    NO reintenta los 4xx (401, 403, 404...): un permiso denegado no se arregla
    insistiendo, y reintentarlo solo retrasa el error tres veces.
    """
    import time
    import requests

    ultimo = None
    for intento in range(1, intentos + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if 400 <= r.status_code < 500:
                r.raise_for_status()          # definitivo: no insistimos
            if r.status_code >= 500 or r.status_code == 429:
                raise RuntimeError(f"respuesta {r.status_code} del servidor")
            r.raise_for_status()
            return r
        except Exception as e:
            ultimo = e
            # Un 4xx ya se ha lanzado arriba y no debe reintentarse.
            respuesta = getattr(e, "response", None)
            if respuesta is not None and 400 <= respuesta.status_code < 500:
                raise
            if intento < intentos:
                print(f"AVISO: fallo al leer la configuración (intento {intento} de "
                      f"{intentos}): {e}. Reintento en {pausa}s...")
                time.sleep(pausa)
    raise ultimo
