# Importamos las librerías que vamos a usar:
# - requests: para descargar contenido de internet
# - feedparser: para leer y entender el feed ATOM (un formato de noticias/datos)
import requests
import feedparser

# Esta es la dirección del feed ATOM de la Plataforma de Contratación del Sector Público.
URL = "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom"

# El servidor rechaza las peticiones que no parezcan venir de un navegador.
# Por eso enviamos una cabecera "User-Agent" que imita a un navegador real.
cabeceras = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

# Descargamos el feed. El resultado se guarda en la variable "respuesta".
respuesta = requests.get(URL, headers=cabeceras)

# Le pasamos el texto descargado a feedparser para que lo analice (lo "parsee").
feed = feedparser.parse(respuesta.content)

# Mostramos cuántas entradas (licitaciones) hay en el feed.
print("Número de entradas:", len(feed.entries))

# Cogemos la primera entrada de la lista y mostramos algunos de sus datos.
primera = feed.entries[0]
print("Título:", primera.title)
print("Enlace:", primera.link)
print("Fecha de actualización:", primera.updated)
