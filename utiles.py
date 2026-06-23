# Utilidades comunes a filtrar.py y generar_web.py.
# Aquí va SOLO lo que comparten los dos, para no duplicar la lógica ni tener que
# importar un script desde el otro: importar filtrar.py ejecutaría su descarga
# del feed (no tiene "if __name__ == ...") y eso no lo queremos al generar la web.
import unicodedata


def normaliza(texto):
    """Devuelve el texto en minúsculas y sin tildes, para poder comparar
    'sin distinguir mayúsculas ni tildes'."""
    texto = texto.lower()
    # NFKD separa cada letra de su tilde; nos quedamos con lo que NO es una tilde.
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in texto if not unicodedata.combining(c))
