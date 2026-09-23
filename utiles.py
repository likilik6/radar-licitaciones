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
