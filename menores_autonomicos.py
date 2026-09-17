# -*- coding: utf-8 -*-
"""menores_autonomicos.py — F1 de «Menores autonómicos»: carga en public.menores los
contratos menores de las plataformas AUTONÓMICAS. Hoy, solo la Junta de Andalucía.

La tabla ya tiene ~1,39 M menores ESTATALES (PLACSP, sindicación 1143, los carga
backfill_catalogo.py). Ningún órgano de la Junta publica sus menores ahí (F0: 0 de los
248 órganos de 2025 entre los 7.672 de la estatal), así que Andalucía es un hueco
entero: 306.381 contratos en la ventana de 3 años.

FUENTE (medida en F0, 17/09/2026)
  · CKAN de la Junta: un paquete por año
    (contratacion-menor-plataforma-de-contratacion-andalucia-AAAA) con un recurso CSV
    (en 2023, un .csv.zip) y otro JSON. Se usa el CSV.
  · El fichero se REPUBLICA ENTERO cada pocos meses con otro nombre: el cambio se
    detecta por last_modified + size del recurso, no por el nombre.
  · Servidor lento e inestable (90-180 KB/s, cortes 10054): la descarga se reanuda con
    Range (responde 206) y reintenta con espera. 208 MB tardaron 1.148 s con 4 cortes.
  · 23 columnas separadas por '|', CRLF, valores rellenos con espacios. cp1252 en
    2023-2025 y UTF-8 en 2026. Fechas ISO con zona en 2023-2024 y dd/mm/aaaa en
    2025-2026. Decimal con punto, salvo 2025 (coma). Sin columna CPV.

MODOS
  · simulación (POR DEFECTO): descarga/lee, normaliza y cuenta. No escribe NADA.
  · --cargar: escribe con la RPC menores_carga_lote (menores_f1.sql) en lotes de 500.
    La RPC solo escribe lo nuevo o lo que cambia, y rechaza filas estatales.
  · --cpv-portal: rellena el CPV con la ficha del portal de la Junta (servicio interno,
    sin documentar; si falla, la carga sigue sin CPV). OJO: en F1 el portal ignoró la
    consulta por lotes (ver enriquece_cpv): hoy no rellena nada y se corta solo.
  · --estado RUTA: recuerda qué versión de cada recurso se cargó y salta las que no han
    cambiado (salvo --forzar). Solo se guarda con --cargar: una simulación no puede
    marcar como hecho algo que no ha escrito.

DUPLICADOS CON LA ESTATAL: si un contrato de la Junta apareciera TAMBIÉN en la estatal,
se queda la estatal (ver «GEMELAS» abajo). Hoy la puerta por órganos sale vacía y no
se consulta nada más.

SEGURIDAD: la service_role y la URL se leen de SUPABASE_SERVICE_ROLE y SUPABASE_URL
(nunca del repo). El repo y los logs de Actions son PÚBLICOS: este script no imprime
NIF ni nombres de adjudicatarios, solo recuentos, órganos y claves de contrato.

PERSONAS FÍSICAS: los DNI/NIE salen hacia la base SOLO con la máscara de la AEPD
('***4567**' o '****4567*'): en el NIF, en el nombre del adjudicatario y en el objeto.
Las máscaras que ya trae la Junta se llevan a esa misma forma (mascara_canonica): con
dos formas distintas de la misma persona se reconstruía su DNI. Los DNI completos solo
viven en memoria para la regla de gemelas y nunca van en una URL (busca_candidatas).

USO
  python menores_autonomicos.py --fuente andalucia                     # simula la ventana
  python menores_autonomicos.py --fuente andalucia --anios 2025 --csv-local 2025=menores_2025.csv
  $env:SUPABASE_SERVICE_ROLE="..."; python menores_autonomicos.py --fuente andalucia --cargar --estado estado.json
"""

import argparse
import codecs
import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

# Del radar: la cabecera de navegador (sin ella el WAF de la Junta también corta), la
# validación de fechas (calendario real + ventana plausible) y la normalización de CIF
# que usan TODAS las tablas. Nada de esto se duplica aquí.
from feeds import CABECERAS, a_fecha, normaliza_cif

RAIZ = Path(__file__).resolve().parent
FUENTES_JSON = RAIZ / "data" / "menores_fuentes.json"
ALIAS_JSON = RAIZ / "data" / "menores_alias_organos.json"

# Fuentes con cargador escrito. Las demás de menores_fuentes.json llegarán en F2+.
FUENTES_CON_CARGADOR = ("andalucia",)

# Retención de public.menores (decisión de Alejandro): 3 años móviles por fecha de
# adjudicación. Lo anterior no se carga (purga_menores lo borraría igual).
VENTANA_ANIOS = 3

TAM_LOTE = 500          # filas por llamada a menores_carga_lote (lo que midió F0)
TAM_LOTE_CPV = 1000     # ID por petición al portal (5,4 s y ~5 MB por petición)
PAUSA_CPV = 1.0         # s entre peticiones al portal (en serie, por cortesía)
FALLOS_CPV_SEGUIDOS = 5  # tras 5 lotes fallidos seguidos se deja de pedir CPV
PAGINA = 1000           # filas por página de PostgREST (su tope por petición)
TROZO_CLAVES = 100      # NIF (o patrones de máscara) por consulta de candidatas estatales
DIAS_TROZO_PATRON = 15  # días que abarca una consulta por patrón (+15 a cada lado = 45)
TROZO_BORRADO = 100     # claves por DELETE
# Paso (3): un órgano con más filas de la fuente en la base no se recorre (se avisa).
# EXPLAIN del 17/09/2026 (paginación por clave, un órgano): con ~18.600 filas Postgres
# usa menores_organo_fuente_idx y ordena; con ~55.000 ya recorre la clave primaria
# filtrando, que en el Micro es leer la tabla entera.
TOPE_FILAS_ORGANO = 20000
MAX_DETALLE = 500       # entradas máximas en las listas del informe

# --- GEMELAS · parámetros de la regla (F0, medir_duplicados.md) --------------
DIAS_GEMELA = 15
TOLERANCIA_IMPORTE = Decimal("0.01")
UMBRAL_NOMBRE = 0.8

# ============================================================================
# ANDALUCÍA · datos de la fuente
# ============================================================================
CKAN_API = "https://www.juntadeandalucia.es/datosabiertos/portal/api/3/action/"
PORTAL_DATOS = "https://www.juntadeandalucia.es/datosabiertos/portal"
PAQUETE_ANDALUCIA = "contratacion-menor-plataforma-de-contratacion-andalucia-{anio}"
# Paquete y recurso CSV verificados en F0. No se usan para decidir (manda CKAN, que es
# quien sabe si hay versión nueva); sirven para elegir el recurso si un año trajera más
# de un CSV y para avisar si la Junta cambia el recurso de sitio.
RECURSOS_VERIFICADOS = {
    2023: ("e592c965-baf9-4451-b6b0-2903a1f78a91", "1e5a1fe4-225d-4ee2-9c30-408969d13d3e"),
    2024: ("cdf0e880-0a19-46cf-a09e-856328d85700", "8438488e-9d85-47f7-8820-b8f0ef45f264"),
    2025: ("00510697-b39d-4e19-b142-14565baafabd", "5a95ae9f-8842-4944-bcf9-2db6b08f5394"),
    2026: ("9fd6091c-535a-4762-b4f0-938c61ec0e95", "2a2dc763-53b0-4918-87ec-ec218a910de5"),
}
# Ficha pública del contrato. Verificado en F0 con 8 ID de los 4 años: abre el contrato
# correcto (la URL antigua pdc_sirec/...jsf redirige aquí con un 302).
ENLACE_ANDALUCIA = ("https://www.juntadeandalucia.es/haciendayadministracionpublica/apl/"
                    "pdc-front-publico/perfiles-licitaciones/detalle-licitacion?idExpediente={id}")
# Índice que consulta esa misma ficha por dentro (main.js del portal). Trae codigosCpv.
CPV_PORTAL_URL = ("https://www.juntadeandalucia.es/haciendayadministracionpublica/apl/"
                  "pdc-front-publico/elastic/sirec_pdc_expedientes_details/_search")

# Cabecera IDÉNTICA en los 4 años (F0). Si cambia, se para ese fichero: mejor avisar que
# cargar columnas cruzadas.
CABECERA_ANDALUCIA = (
    "ID_EXPEDIENTE|ORGANO_CONTRATACION|NUM_EXPEDIENTE|TITULO|DESCRIPCION|TIPO_CONTRATO|"
    "PROCEDIMIENTO_ADJUDICACION|DURACION_CONTRATO|DURACION_MEDIDA|LUGAR_EJECUCION_CODIGO|"
    "LUGAR_EJECUCION_DENOMINACION|VALOR_ESTIMADO|IMPORTE_ADJUDICACION_SIN_IVA|"
    "IMPORTE_ADJUDICACION_CON_IVA|FINANCIACION_EUROPEA|FINANCIADO_POR|TASA_COFINANCIACION|"
    "NUM_LICITADORES_PRESENTADOS|NIF_ADJUDICATARIO|ADJUDICATARIO_DENOMINACION|"
    "FECHA_ADJUDICACION|FECHA_FORMALIZACION|ESTADO").split("|")
COL = {nombre: i for i, nombre in enumerate(CABECERA_ANDALUCIA)}
N_COLUMNAS = len(CABECERA_ANDALUCIA)

# Columnas que recibe menores_carga_lote (menores_f1.sql). TODAS las filas llevan TODAS
# las claves: jsonb_to_recordset no se queja si falta una, pero la pondría a null y la
# RPC la «actualizaría» a null en silencio.
CAMPOS_RPC = ("licitacion_id", "objeto", "cpv", "importe_sin_iva", "importe_con_iva",
              "organo_contratacion", "adjudicatario", "cif_adjudicatario",
              "cifs_adjudicatarios", "fecha_adjudicacion", "num_expediente", "enlace",
              "fuente", "n_adjudicatarios")

CACHE_DIR = Path("cache_menores")   # CSV descargados (reanudables); nunca al repo


class ErrorFormato(Exception):
    """El fichero no tiene el formato medido (cabecera distinta, zip sin CSV...)."""


class ErrorCarga(Exception):
    """La base rechazó un lote o una petición: se para la carga con el motivo."""


# ============================================================================
# CONFIGURACIÓN (data/menores_fuentes.json y data/menores_alias_organos.json)
# ============================================================================
def lee_fuentes(ruta=FUENTES_JSON):
    """{codigo: {etiqueta, nombre, cargada}} sin las claves de comentario ('_nota')."""
    datos = json.loads(Path(ruta).read_text(encoding="utf-8-sig"))
    return {k: v for k, v in datos.items() if not k.startswith("_")}


def etiqueta_fuente(fuentes, codigo):
    """Etiqueta legible de una fuente; si no está en el fichero, el código tal cual."""
    return (fuentes.get(codigo) or {}).get("etiqueta") or codigo


def lee_alias(fuente, ruta=ALIAS_JSON):
    """Alias de órganos de UNA fuente, ya normalizados: {organo estatal: organo fuente}.
    Se revisan a mano: la regla de gemelas no admite parecidos difusos (ver normorg)."""
    ruta = Path(ruta)
    if not ruta.exists():
        return {}
    mapa = json.loads(ruta.read_text(encoding="utf-8-sig")).get(fuente) or {}
    return {normorg(k): normorg(v) for k, v in mapa.items() if not k.startswith("_")}


# ============================================================================
# NORMALIZACIÓN (funciones puras: las prueba test_menores_autonomicos.py)
# ============================================================================
_FECHA_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ].*)?$")
_FECHA_DMY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def normaliza_fecha(texto):
    """Fecha de adjudicación del CSV -> ('AAAA-MM-DD' | None, formato).
    formato: 'iso' | 'dmy' | 'vacia' | 'invalida'.

    ISO con zona (2023-2024: '2023-03-14T00:00:00+0100', con +0100 y +0200 mezclados):
    se toman LITERALMENTE los 10 primeros caracteres. Pasarla a UTC bajaría un día las
    82.732 filas de 2023 que llevan hora 00:00 en horario de verano o invierno.
    dd/mm/aaaa (2025-2026): se reordena. Las dos pasan por feeds.a_fecha (calendario real
    y ventana plausible [2000-01-01, hoy+730])."""
    t = (texto or "").strip()
    if not t:
        return None, "vacia"
    if _FECHA_ISO.match(t):
        fecha = a_fecha(t[:10])
        return (fecha, "iso") if fecha else (None, "invalida")
    m = _FECHA_DMY.match(t)
    if m:
        fecha = a_fecha(f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}")
        return (fecha, "dmy") if fecha else (None, "invalida")
    return None, "invalida"


def resta_anios(dia, anios):
    """Mismo día N años antes. Un 29 de febrero cae en el 28 (no hay 29 en el año
    destino y replace() lanzaría ValueError)."""
    try:
        return dia.replace(year=dia.year - anios)
    except ValueError:
        return dia.replace(year=dia.year - anios, day=28)


def en_ventana(fecha_iso, desde_iso):
    """¿Entra en la ventana? Las filas SIN fecha entran (no se pueden datar; se cuentan
    aparte). Comparar 'AAAA-MM-DD' como texto equivale a comparar fechas."""
    return fecha_iso is None or fecha_iso >= desde_iso


_NUMERO = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)$")


def normaliza_importe(texto):
    """Importe del CSV -> (Decimal | None, estado 'ok' | 'vacio' | 'invalido').

    El separador decimal cambia por año: punto en 2023/2024/2026 y COMA en 2025
    (83.546 filas '1234,56'); también hay ',50' y '.9' sin cero delante. No se ha visto
    separador de miles, así que UN solo separador es siempre el decimal. Con los dos, el
    último es el decimal y el otro tiene que agrupar de 3 en 3; si no, 'invalido' (mejor
    null que un importe multiplicado por 1.000)."""
    t = (texto or "").strip().replace(" ", "")
    if not t:
        return None, "vacio"
    comas, puntos = t.count(","), t.count(".")
    if comas and puntos:
        decimal_sep = "," if t.rfind(",") > t.rfind(".") else "."
        miles = "." if decimal_sep == "," else ","
        entero, _, fraccion = t.rpartition(decimal_sep)
        if t.count(decimal_sep) != 1 or not re.fullmatch(
                r"-?\d{1,3}(?:%s\d{3})+" % re.escape(miles), entero):
            return None, "invalido"
        t = entero.replace(miles, "") + "." + fraccion
    elif comas + puntos > 1:
        sep = "," if comas else "."
        if not re.fullmatch(r"-?\d{1,3}(?:%s\d{3})+" % re.escape(sep), t):
            return None, "invalido"
        t = t.replace(sep, "")
    elif comas == 1:
        t = t.replace(",", ".")
    if not _NUMERO.match(t):
        return None, "invalido"
    try:
        return Decimal(t), "ok"
    except InvalidOperation:
        return None, "invalido"


# --- NIF ---------------------------------------------------------------------
_CIF_JURIDICA = re.compile(r"[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]")
_DNI8 = re.compile(r"\d{8}[A-Z]")
_DNI7 = re.compile(r"\d{7}[A-Z]")
# DNI SIN letra: 8 dígitos, o 9 con un 0 delante. En la ventana, 11 NIF con 8 dígitos
# (9 a nombre de una persona) y 10 con '0' + 8 dígitos, todos a nombre de personas. Un
# NIF portugués (9 dígitos) nunca empieza por 0. Enmascarar por error un número que no
# fuera DNI solo cuesta perder ese número; no enmascararlo publica un DNI.
_DNI_SIN_LETRA = re.compile(r"0?\d{8}")
_NIE = re.compile(r"[XYZ]\d{7}[A-Z]")
_KLM = re.compile(r"[KLM]\d{7}[A-Z]")
# Las DOS únicas formas de máscara que salen hacia la base (AEPD).
_MASCARA_DNI = re.compile(r"\*{3}[0-9]{4}\*{2}")
_MASCARA_NIE = re.compile(r"\*{4}[0-9]{4}\*")
LETRAS_DNI = "TRWAGMYFPDXBNJZSQVHLCKE"


def es_nif_espanol(valor):
    """Forma de NIF español (sin comprobar la letra de control): CIF de persona
    jurídica, DNI de 8 o 7 dígitos (o sin letra), NIE o NIF K/L/M."""
    return any(p.fullmatch(valor) for p in (_CIF_JURIDICA, _DNI8, _DNI7, _DNI_SIN_LETRA, _NIE, _KLM))


def quita_es(valor):
    """'ESB12345678' -> ('B12345678', True). Solo si lo que queda es un NIF español:
    'ES' delante de otra cosa se deja (podría ser parte de un IVA extranjero)."""
    if valor.startswith("ES") and es_nif_espanol(valor[2:]):
        return valor[2:], True
    return valor, False


def tipo_nif(valor):
    """Clase de un NIF ya normalizado (para el informe)."""
    if "*" in valor:
        return "ya_enmascarado"
    if _CIF_JURIDICA.fullmatch(valor):
        return "juridica"
    if _DNI8.fullmatch(valor):
        return "dni"
    if _DNI7.fullmatch(valor):
        return "dni7"
    if _DNI_SIN_LETRA.fullmatch(valor):
        return "dni_sin_letra"
    if _NIE.fullmatch(valor) or _KLM.fullmatch(valor):
        return "nie_klm"
    return "extranjero_otro"


def mascara_canonica(valor):
    """Una máscara que YA trae la Junta -> la forma AEPD, o '*********' si no se puede.

    Por qué no se deja tal cual: la Junta enmascara con 25 formas distintas ('***1234**'
    en 17.545 NIF, pero también '12****78Z', '123****8Q'...). Juntando la de un contrato
    con la '***dddd**' de otro de la misma persona se ven 7 de 8 dígitos más la letra, y
    la letra de control da el que falta: en la ventana hay 10 personas cuyo DNI completo
    sale así (revisión de F1, verificado en las 4 que se podían comprobar).
    Si todas las máscaras dejan ver SOLO la misma ventana, juntarlas no enseña nada más:
      · 9 posiciones que empiezan por X/Y/Z/K/L/M o ya con forma '****1234*': NIE o
        NIF K/L/M, visibles las posiciones 5 a 8.
      · otras 9 posiciones: DNI, visibles las posiciones 4 a 7.
    Si esa ventana no trae sus 4 dígitos (p. ej. '***123***', 402 NIF) o la máscara no
    tiene 9 posiciones, no identifica a nadie: '*********', que normaliza_nifs descarta."""
    if len(valor) == 9:
        if valor[0] in "XYZKLM" or _MASCARA_NIE.fullmatch(valor):
            if re.fullmatch(r"[0-9]{4}", valor[4:8]):
                return "****" + valor[4:8] + "*"
        elif re.fullmatch(r"[0-9]{4}", valor[3:7]):
            return "***" + valor[3:7] + "**"
    return "*" * 9


def enmascara(valor):
    """Personas físicas enmascaradas al estilo AEPD (decisión de Alejandro):
      DNI 12345678Z -> ***4567**    (dígitos 4 a 7)
      DNI 1234567Z  -> se rellena a 01234567Z -> ***3456**
      DNI sin letra 12345678 (o 012345678) -> ***4567**, la misma que con letra
      NIE X1234567L / NIF K1234567L -> ****4567*   (caracteres 5 a 8)
      lo que ya viene con '*' -> mascara_canonica
    El CIF de persona jurídica y lo extranjero no se tocan.
    F0: 5.956 DNI completos solo en 2025; la estatal usa estas mismas máscaras."""
    if "*" in valor:
        return mascara_canonica(valor)
    if _DNI8.fullmatch(valor):
        return "***" + valor[3:7] + "**"
    if _DNI7.fullmatch(valor):
        return enmascara("0" + valor)
    if _DNI_SIN_LETRA.fullmatch(valor):
        return "***" + valor[-8:][3:7] + "**"
    if _NIE.fullmatch(valor) or _KLM.fullmatch(valor):
        return "****" + valor[4:8] + "*"
    return valor


def tiene_digitos(valor):
    return bool(re.search(r"[0-9]", valor or ""))


def normaliza_nifs(texto):
    """NIF_ADJUDICATARIO -> (nifs, originales, marcas).

      · nifs: los NIF del contrato, ya enmascarados, en el orden del fichero y sin
        repetir. El primero es cif_adjudicatario (la fuente no da importe por
        adjudicatario, así que no hay un «principal» mejor).
      · originales: los DNI/NIE COMPLETOS del contrato (sin repetir), antes de
        enmascarar. Solo viven en memoria para la regla de gemelas: comparar un DNI
        completo con otro completo exige igualdad exacta, no la máscara. Nunca se cargan,
        se imprimen ni salen en una URL (ver busca_candidatas).
      · marcas: una por parte (tipo_nif) más 'sin_digitos', 'prefijo_es',
        'mascara_reducida' y 'mascara_descartada', para el informe.

    2023-2024 terminan en ';' (101.222 filas de 2023). Hay ~10 filas/año con varios NIF
    separados por ';' y algún nombre metido en el campo: la parte sin ningún dígito se
    descarta (también '*********', que no identifica a nadie, y las máscaras de la Junta
    que se quedan así al llevarlas a la forma AEPD)."""
    t = (texto or "").strip().rstrip(";").strip()
    nifs, originales, marcas = [], [], []
    for trozo in t.split(";"):
        valor = normaliza_cif(trozo)
        if valor is None:
            continue
        if not tiene_digitos(valor):
            marcas.append("sin_digitos")
            continue
        valor, quitado = quita_es(valor)
        if quitado:
            marcas.append("prefijo_es")
        tipo = tipo_nif(valor)
        marcas.append(tipo)
        mascara = enmascara(valor)
        if tipo == "ya_enmascarado" and mascara != valor:
            marcas.append("mascara_reducida" if tiene_digitos(mascara) else "mascara_descartada")
        if not tiene_digitos(mascara):
            continue
        if "*" not in valor and mascara != valor and valor not in originales:
            originales.append(valor)
        if mascara not in nifs:
            nifs.append(mascara)
    return nifs, originales, marcas


# DNI y NIE con la letra de control BUENA dentro de un texto libre (nombre del
# adjudicatario u objeto). Exigir la letra evita tapar referencias de producto o de
# expediente que por casualidad tengan 8 cifras y una letra.
_DNI_EN_TEXTO = re.compile(r"(?<![A-Za-z0-9])([0-9]{8})[ .-]?([A-Za-z])(?![A-Za-z0-9])")
_NIE_EN_TEXTO = re.compile(r"(?<![A-Za-z0-9])([XYZxyz])[ .-]?([0-9]{7})[ .-]?([A-Za-z])(?![A-Za-z0-9])")


def _dni_valido(numero, letra):
    return LETRAS_DNI[int(numero) % 23] == letra.upper()


def protege_texto(texto, por_partes=False):
    """Tapa DNI/NIE de personas físicas en un texto que se va a cargar -> (texto, n).

    · por_partes (ADJUDICATARIO_DENOMINACION): cada parte separada por ';' que ENTERA
      tiene forma de DNI/NIE (con o sin letra, válida o no) pasa a su máscara. En la
      ventana hay 4 contratos cuyo «nombre» es el propio DNI; en 3 de ellos el NIF de la
      misma fila sí salía enmascarado, así que la máscara no servía de nada.
    · además, en cualquier texto, un DNI o NIE con la letra de control buena dentro de
      una frase (3 objetos en la ventana)."""
    if not texto:
        return texto, 0
    n = 0
    if por_partes:
        partes = texto.split(";")
        for i, parte in enumerate(partes):
            valor = quita_es(normaliza_cif(parte) or "")[0]
            if valor and tipo_nif(valor) in ("dni", "dni7", "dni_sin_letra", "nie_klm"):
                antes, despues = parte[:len(parte) - len(parte.lstrip())], parte[len(parte.rstrip()):]
                partes[i] = antes + enmascara(valor) + despues
                n += 1
        texto = ";".join(partes)

    def dni(m):
        nonlocal n
        if not _dni_valido(m.group(1), m.group(2)):
            return m.group(0)
        n += 1
        return enmascara(m.group(1) + m.group(2).upper())

    def nie(m):
        nonlocal n
        numero = str("XYZ".index(m.group(1).upper())) + m.group(2)
        if not _dni_valido(numero, m.group(3)):
            return m.group(0)
        n += 1
        return enmascara(m.group(1).upper() + m.group(2) + m.group(3).upper())

    texto = _NIE_EN_TEXTO.sub(nie, _DNI_EN_TEXTO.sub(dni, texto))
    return texto, n


def elige_objeto(titulo, descripcion):
    """El texto más largo entre DESCRIPCION y TITULO (a igualdad, la descripción).
    2023 trae DESCRIPCION vacía en 91.336 de 101.336 filas; en 2024-2026 suele ser igual
    al título o un poco más larga."""
    titulo, descripcion = (titulo or "").strip(), (descripcion or "").strip()
    return (descripcion if len(descripcion) >= len(titulo) else titulo) or None


def sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFKD", texto or "")
                   if not unicodedata.combining(c))


def normorg(texto):
    """Nombre de órgano para comparar por IGUALDAD EXACTA: sin tildes, mayúsculas, solo
    letras, dígitos y espacios (colapsados), sin '(En Transición)' ni punto final.
    Nada de parecido difuso: con 0,83 de parecido se confunde el SEPE con el SAE."""
    t = sin_tildes(texto).upper()
    t = re.sub(r"\(\s*EN\s+TRANSICION\s*\)", " ", t)
    t = re.sub(r"[^A-Z0-9]+", " ", t)
    return " ".join(t.split())


# Formas jurídicas y partículas que no distinguen a nadie en un nombre.
FORMAS_JURIDICAS = {
    "SL", "SLU", "SLL", "SLP", "SLNE", "SA", "SAU", "SAL", "SC", "SCA", "SCP", "SCL",
    "SCOOP", "COOP", "CB", "SOCIEDAD", "LIMITADA", "ANONIMA", "UNIPERSONAL", "LABORAL",
    "PROFESIONAL", "COOPERATIVA",
}
PARTICULAS = {"DE", "DEL", "LA", "LAS", "LOS", "EL"}


def tokens_nombre(texto):
    """Conjunto de palabras de un nombre sin formas jurídicas ni partículas. Los puntos se
    quitan ANTES de separar para que 'S.L.U.' quede 'SLU' y no 'S L U'; las letras sueltas
    que queden ('S. L.', iniciales) no cuentan."""
    t = normorg(sin_tildes(texto or "").replace(".", ""))
    return {p for p in t.split() if len(p) > 1 and p not in FORMAS_JURIDICAS and p not in PARTICULAS}


def similitud_nombres(a, b):
    """Jaccard de tokens_nombre: 1,0 = mismas palabras en cualquier orden. 0 si alguno
    queda vacío (no hay con qué comparar)."""
    ta, tb = tokens_nombre(a), tokens_nombre(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ============================================================================
# LECTURA DEL CSV DE ANDALUCÍA (streaming)
# ============================================================================
def _abridor(ruta):
    """Devuelve (abrir, es_zip, zip_abierto). abrir() da un fichero BINARIO nuevo cada vez
    (hace falta dos veces: detectar la codificación y leer). 2023 viene como .csv.zip con
    un único CSV dentro; se lee del zip sin descomprimirlo a disco."""
    with open(ruta, "rb") as f:
        magia = f.read(4)
    if magia.startswith(b"PK"):
        zf = zipfile.ZipFile(ruta)
        csvs = [i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".csv")]
        if len(csvs) != 1:
            zf.close()
            raise ErrorFormato(f"el zip {Path(ruta).name} trae {len(csvs)} CSV (se esperaba 1)")
        return (lambda: zf.open(csvs[0])), True, zf
    return (lambda: open(ruta, "rb")), False, None


def detecta_codificacion(abrir):
    """UTF-8 si el fichero ENTERO lo es; si no, cp1252. En trozos de 4 MB con un
    decodificador incremental: no se carga el fichero (hasta 224 MB) en memoria. Un
    fichero cp1252 casi nunca es UTF-8 válido (2025: 265.462 bytes no ASCII)."""
    decodificador = codecs.getincrementaldecoder("utf-8")()
    with abrir() as f:
        inicio = f.read(3)
        bom = inicio == codecs.BOM_UTF8
        try:
            decodificador.decode(b"" if bom else inicio)
            while True:
                trozo = f.read(4 << 20)
                if not trozo:
                    break
                decodificador.decode(trozo)
            decodificador.decode(b"", final=True)
        except UnicodeDecodeError:
            return "cp1252"
    return "utf-8-sig" if bom else "utf-8"


class _LineasConEco:
    """Iterador de líneas que recuerda las que ha consumido el csv.reader para el último
    registro: así se sabe si un registro necesitó las comillas para salir bien (el
    recuento de '|' del texto crudo no da 22)."""

    def __init__(self, texto):
        self.texto = texto
        self.consumidas = []

    def __iter__(self):
        return self

    def __next__(self):
        linea = next(self.texto)
        self.consumidas.append(linea)
        return linea


def clave_andalucia(id_num):
    return f"and:{id_num}"


def evalua_registro(campos, desde_iso):
    """Un registro (campos ya sin espacios) -> resultado:
      ('fuera_de_ventana', formato_fecha)
      ('estado', ESTADO, formato_fecha)          no adjudicado: fuera
      ('ok', fila, meta)                         fila con las columnas de CAMPOS_RPC
    meta = (formato_fecha, estado_importe_sin, estado_importe_con, marcas_nif,
            originales_nif, objeto_sale_del_titulo, dni_tapados_adjudicatario,
            dni_tapados_objeto)"""
    fecha, formato = normaliza_fecha(campos[COL["FECHA_ADJUDICACION"]])
    if not en_ventana(fecha, desde_iso):
        return ("fuera_de_ventana", formato)
    estado = campos[COL["ESTADO"]]
    # 'Resuelto' en casi todo; 'Evaluación', 'Borrador', 'Publicada - convocat' y
    # 'Anulado' (7-15 por año) no están adjudicados.
    if normorg(estado) != "RESUELTO":
        return ("estado", estado or "(vacío)", formato)

    id_num = int(campos[COL["ID_EXPEDIENTE"]])
    importe_sin, est_sin = normaliza_importe(campos[COL["IMPORTE_ADJUDICACION_SIN_IVA"]])
    importe_con, est_con = normaliza_importe(campos[COL["IMPORTE_ADJUDICACION_CON_IVA"]])
    nifs, originales, marcas = normaliza_nifs(campos[COL["NIF_ADJUDICATARIO"]])
    titulo, descripcion = campos[COL["TITULO"]], campos[COL["DESCRIPCION"]]
    cifs = sorted(set(nifs))
    # Los DNI también se cuelan en los textos: se tapan igual que en el NIF.
    objeto, tapados_objeto = protege_texto(elige_objeto(titulo, descripcion))
    adjudicatario, tapados_adj = protege_texto(
        campos[COL["ADJUDICATARIO_DENOMINACION"]].rstrip(";").strip(), por_partes=True)
    fila = {
        "licitacion_id": clave_andalucia(id_num),
        "objeto": objeto,
        "cpv": None,                           # null = no tocar el CPV que ya tenga
        # Cada importe de SU columna. Nunca se deriva uno del otro: el IVA no siempre es
        # el 21 % (2025: 73.291 filas con otro tipo) y 2023 no trae el con IVA en 91.307.
        "importe_sin_iva": importe_sin,
        "importe_con_iva": importe_con,
        "organo_contratacion": campos[COL["ORGANO_CONTRATACION"]] or None,
        "adjudicatario": adjudicatario or None,
        "cif_adjudicatario": nifs[0] if nifs else None,
        "cifs_adjudicatarios": cifs,
        "fecha_adjudicacion": fecha,
        "num_expediente": campos[COL["NUM_EXPEDIENTE"]] or None,
        "enlace": ENLACE_ANDALUCIA.format(id=id_num),
        "fuente": "andalucia",
        "n_adjudicatarios": len(cifs) or None,
    }
    objeto_del_titulo = len(descripcion.strip()) < len(titulo.strip())
    meta = (formato, est_sin, est_con, tuple(marcas), tuple(originales), objeto_del_titulo,
            tapados_adj, tapados_objeto)
    return ("ok", fila, meta)


def lee_fichero_andalucia(ruta, desde_iso, ya_vistos=None, etiqueta=""):
    """Lee un CSV (o .csv.zip) de Andalucía en streaming y devuelve (filas, originales, info):
      · filas: {licitacion_id: fila} de lo que se cargaría (resuelto y en ventana).
      · originales: {licitacion_id: [NIF sin enmascarar]} solo para buscar gemelas.
      · info: todos los recuentos del fichero para el informe.

    ID repetidos: en 2025 hay 35 ID con 2 filas que solo difieren en el órgano ('IFAPA
    Centro X' frente a 'Instituto Andaluz de Investigación... Servicios Centrales') y el
    orden entre ellas varía de una versión a otra. Desempate DETERMINISTA: gana la fila
    cuyo órgano ordena primero (en la práctica el centro IFAPA concreto) y, a igualdad de
    órgano, la de texto completo menor. Es un mínimo sobre un orden total: da lo mismo en
    qué orden vengan.

    ya_vistos: claves de ficheros anteriores de la misma ejecución. F0 no vio ningún ID en
    dos años (0 de 377.186); si pasa, gana el primer fichero y se cuenta."""
    t0 = time.time()
    abrir, es_zip, zf = _abridor(ruta)
    cuenta = Counter()
    fuera_por_estado = Counter()
    organos_repetidos = defaultdict(set)
    por_id = {}
    try:
        codificacion = detecta_codificacion(abrir)
        with abrir() as binario:
            # errors='replace': un byte que cp1252 no define no debe tumbar la carga de un
            # año entero; se cuenta en caracteres_irrecuperables (F0: 0 en los 4 años).
            texto = io.TextIOWrapper(binario, encoding=codificacion, errors="replace", newline="")
            eco = _LineasConEco(texto)
            lector = csv.reader(eco, delimiter="|", quotechar='"')
            cabecera_vista = False
            while True:
                try:
                    registro = next(lector)
                except StopIteration:
                    break
                except csv.Error:
                    cuenta["lineas_leidas"] += len(eco.consumidas)
                    cuenta["mal_formados"] += 1
                    eco.consumidas.clear()
                    continue
                crudo = "".join(eco.consumidas)
                n_lineas = len(eco.consumidas)
                eco.consumidas.clear()
                if not cabecera_vista:
                    cabecera = [c.strip().lstrip("\ufeff") for c in registro]
                    if cabecera != CABECERA_ANDALUCIA:
                        raise ErrorFormato(f"cabecera distinta de la medida en F0 ({len(cabecera)} "
                                           f"columnas; primeras: {cabecera[:3]})")
                    cabecera_vista = True
                    continue
                cuenta["lineas_leidas"] += n_lineas
                if not registro or (len(registro) == 1 and not registro[0].strip()):
                    cuenta["lineas_vacias"] += 1
                    continue
                cuenta["registros"] += 1
                if len(registro) != N_COLUMNAS:
                    cuenta["mal_formados"] += 1
                    continue
                # Un '|' dentro de un título entrecomillado: un split simple daría 24
                # columnas (F0: 12/32/19/1 por año, todas bien con las comillas).
                if crudo.count("|") != N_COLUMNAS - 1:
                    cuenta["reparadas_por_comillas"] += 1
                if "\ufffd" in crudo:
                    cuenta["caracteres_irrecuperables"] += 1
                campos = [c.strip() for c in registro]
                id_txt = campos[COL["ID_EXPEDIENTE"]]
                if not id_txt.isdigit():
                    cuenta["id_invalido"] += 1
                    continue
                id_num = int(id_txt)           # relleno a 6 (2023-24) o 12 (2025-26)
                clave = (campos[COL["ORGANO_CONTRATACION"]], "|".join(campos))
                previo = por_id.get(id_num)
                if previo is not None:
                    cuenta["filas_repetidas_descartadas"] += 1
                    organos_repetidos[id_num].update((previo[0][0], clave[0]))
                    if clave >= previo[0]:
                        continue
                por_id[id_num] = (clave, evalua_registro(campos, desde_iso))
                if cuenta["registros"] % 25000 == 0:
                    print(f"    {etiqueta}: {cuenta['registros']:,} registros leídos…", flush=True)
    finally:
        if zf is not None:
            zf.close()

    filas, originales = {}, {}
    formato_fecha, nif, importe = Counter(), Counter(), Counter()
    for id_num, (_clave, resultado) in por_id.items():
        licitacion_id = clave_andalucia(id_num)
        if ya_vistos is not None:
            if licitacion_id in ya_vistos:
                cuenta["ids_ya_en_otro_fichero"] += 1
                continue
            ya_vistos.add(licitacion_id)
        cuenta["ids_distintos"] += 1
        tipo = resultado[0]
        if tipo == "fuera_de_ventana":
            formato_fecha[resultado[1]] += 1
            cuenta["fuera_de_ventana"] += 1
            continue
        cuenta["en_ventana"] += 1
        if tipo == "estado":
            formato_fecha[resultado[2]] += 1
            fuera_por_estado[resultado[1]] += 1
            continue
        fila, (formato, est_sin, est_con, marcas, orig, del_titulo, tap_adj, tap_obj) = resultado[1], resultado[2]
        formato_fecha[formato] += 1
        cuenta["filas_validas"] += 1
        if formato == "vacia":
            cuenta["sin_fecha"] += 1
        elif formato == "invalida":
            cuenta["fecha_invalida"] += 1
        if est_sin != "ok":
            importe["sin_iva_null"] += 1
            importe["sin_iva_no_numerico"] += est_sin == "invalido"
        if est_con != "ok":
            importe["con_iva_null"] += 1
            importe["con_iva_no_numerico"] += est_con == "invalido"
        marcas_c = Counter(marcas)
        nif["juridica"] += marcas_c["juridica"]
        nif["dni_enmascarados"] += marcas_c["dni"] + marcas_c["dni7"] + marcas_c["dni_sin_letra"]
        nif["dni7_rellenados"] += marcas_c["dni7"]
        nif["dni_sin_letra"] += marcas_c["dni_sin_letra"]
        nif["nie_klm_enmascarados"] += marcas_c["nie_klm"]
        nif["ya_enmascarados"] += marcas_c["ya_enmascarado"]
        nif["mascaras_reducidas"] += marcas_c["mascara_reducida"]
        nif["mascaras_descartadas"] += marcas_c["mascara_descartada"]
        nif["extranjeros_otros"] += marcas_c["extranjero_otro"]
        nif["partes_sin_digitos_descartadas"] += marcas_c["sin_digitos"]
        nif["prefijo_es_quitado"] += marcas_c["prefijo_es"]
        nif["adjudicatario_con_dni_tapado"] += tap_adj > 0
        nif["objeto_con_dni_tapado"] += tap_obj > 0
        n = len(fila["cifs_adjudicatarios"])
        nif["filas_multi"] += n > 1
        nif["filas_vacias"] += n == 0
        cuenta["objeto_del_titulo"] += del_titulo
        filas[licitacion_id] = fila
        if orig:                                     # solo personas físicas con DNI/NIE completo
            originales[licitacion_id] = list(orig)

    ejemplos = [{"licitacion_id": clave_andalucia(i),
                 "elegido": por_id[i][0][0],
                 "descartados": sorted(organos_repetidos[i] - {por_id[i][0][0]})}
                for i in sorted(organos_repetidos)]
    info = {
        "codificacion": codificacion,
        "zip": es_zip,
        "lineas_leidas": cuenta["lineas_leidas"],
        "lineas_vacias": cuenta["lineas_vacias"],
        "registros": cuenta["registros"],
        "mal_formados": cuenta["mal_formados"],
        "reparadas_por_comillas": cuenta["reparadas_por_comillas"],
        "caracteres_irrecuperables": cuenta["caracteres_irrecuperables"],
        "id_invalido": cuenta["id_invalido"],
        "ids_distintos": cuenta["ids_distintos"],
        "ids_repetidos": {
            "ids": len(organos_repetidos),
            "filas_descartadas": cuenta["filas_repetidas_descartadas"],
            "criterio": "gana el órgano que ordena primero; a igualdad, el texto completo menor",
            "ejemplos": ejemplos[:MAX_DETALLE],
        },
        "ids_ya_en_otro_fichero": cuenta["ids_ya_en_otro_fichero"],
        "formato_fecha": dict(formato_fecha),
        "fuera_de_ventana": cuenta["fuera_de_ventana"],
        "en_ventana": cuenta["en_ventana"],
        "fuera_por_estado": dict(fuera_por_estado),
        "filas_validas": cuenta["filas_validas"],
        "sin_fecha": cuenta["sin_fecha"],
        "fecha_invalida": cuenta["fecha_invalida"],
        "importe_sin_iva_null": importe["sin_iva_null"],
        "importe_sin_iva_no_numerico": importe["sin_iva_no_numerico"],
        "importe_con_iva_null": importe["con_iva_null"],
        "importe_con_iva_no_numerico": importe["con_iva_no_numerico"],
        "objeto_del_titulo": cuenta["objeto_del_titulo"],
        "nif": dict(nif),
        "segundos_lectura": round(time.time() - t0, 1),
    }
    return filas, originales, info


# ============================================================================
# DESCARGA (CKAN de la Junta, reanudable con Range)
# ============================================================================
def _get_con_reintentos(url, params=None, intentos=4, timeout=60):
    """GET sencillo (API de CKAN) con reintentos y espera creciente. Devuelve también el
    404: CKAN contesta así ({"success": false}) a un paquete que aún no existe."""
    ultimo = None
    for intento in range(1, intentos + 1):
        try:
            r = requests.get(url, params=params, headers=CABECERAS, timeout=timeout)
            if r.status_code in (200, 404):
                return r
            ultimo = f"HTTP {r.status_code}"
            if 400 <= r.status_code < 500 and r.status_code != 429:
                break
        except requests.RequestException as e:
            ultimo = type(e).__name__
        if intento < intentos:
            time.sleep(10 * intento)
    raise RuntimeError(f"no pude leer {url} ({ultimo})")


def url_descarga(package_id, resource_id, url_ckan):
    """URL de descarga por el host PÚBLICO. CKAN devuelve a veces el host interno
    gdc-pdpopendata-ckan.paas.junta-andalucia.es, que RECHAZA las conexiones; la misma
    ruta por www.juntadeandalucia.es funciona. El nombre del fichero sale de la URL."""
    fichero = (url_ckan or "").rstrip("/").split("/")[-1]
    return f"{PORTAL_DATOS}/dataset/{package_id}/resource/{resource_id}/download/{fichero}"


def recurso_andalucia(anio):
    """Metadatos del CSV de un año en CKAN: {package_id, resource_id, last_modified, size,
    url, fichero}. None si el paquete de ese año aún no existe."""
    r = _get_con_reintentos(CKAN_API + "package_show",
                            params={"id": PAQUETE_ANDALUCIA.format(anio=anio)})
    try:
        datos = r.json()
    except ValueError:
        raise RuntimeError(f"CKAN devolvió algo que no es JSON (HTTP {r.status_code})") from None
    if not datos.get("success"):
        if r.status_code == 404:
            return None
        raise RuntimeError(f"CKAN no dio el paquete {anio}: {str(datos.get('error'))[:200]}")
    paquete = datos["result"]
    csvs = [x for x in paquete.get("resources") or [] if (x.get("format") or "").upper() == "CSV"]
    if not csvs:
        raise ErrorFormato(f"el paquete {anio} no trae ningún recurso CSV")
    verificado = RECURSOS_VERIFICADOS.get(anio)
    elegido = next((x for x in csvs if verificado and x.get("id") == verificado[1]), None)
    if elegido is None:
        if verificado:
            print(f"  AVISO: {anio}: el recurso CSV ya no es el verificado en F0; uso el más reciente.")
        elegido = max(csvs, key=lambda x: x.get("last_modified") or "")
    # CKAN da size como entero (F0, los 8 recursos), pero la API no lo garantiza: un
    # texto rompería la comparación con los bytes descargados.
    tamano = elegido.get("size")
    tamano = int(tamano) if str(tamano).strip().isdigit() else None
    return {
        "package_id": paquete["id"],
        "resource_id": elegido["id"],
        "last_modified": elegido.get("last_modified"),
        "size": tamano,
        "fichero": (elegido.get("url") or "").rstrip("/").split("/")[-1],
        "url": url_descarga(paquete["id"], elegido["id"], elegido.get("url")),
    }


def descarga_reanudable(url, destino, tamano=None, intentos=10, pausa_max=180):
    """Descarga 'url' a 'destino' REANUDANDO con Range tras cada corte.

    Medido en F0: 90-180 KB/s y cortes 10054 cada pocos minutos; el servidor responde
    206 a Range, así que cada reintento sigue donde se quedó. 'intentos' cuenta solo los
    reintentos SIN avance (con avance se sigue sin límite): 208 MB necesitaron 4 cortes.
    «Avance» = pasar del MÁXIMO de bytes alcanzado, no de lo que había al empezar el
    intento: si el servidor ignorase el Range (200) y cortase siempre a mitad, cada vuelta
    empezaría de cero y parecería avanzar; en la revisión de F1 eso daba vueltas hasta el
    timeout del job (41 peticiones y seguía).
    Si ya existe el destino completo, se reutiliza."""
    destino = Path(destino)
    if destino.exists() and (tamano is None or destino.stat().st_size == tamano):
        print(f"  caché: {destino.name} ya descargado ({destino.stat().st_size / 1e6:,.1f} MB)")
        return destino
    parcial = destino.with_name(destino.name + ".part")
    sin_avance = 0
    maximo = parcial.stat().st_size if parcial.exists() else 0
    ignora_range = False
    while True:
        tengo = parcial.stat().st_size if parcial.exists() else 0
        if tamano is not None and tengo > tamano:
            parcial.unlink()
            tengo = 0
        if tamano is not None and tengo == tamano:
            break
        # identity: con gzip, Content-Length sería el tamaño comprimido y el Range no
        # casaría con los bytes escritos.
        cabeceras = {**CABECERAS, "Accept-Encoding": "identity"}
        if tengo:
            cabeceras["Range"] = f"bytes={tengo}-"
        antes, t0, error = tengo, time.time(), None
        try:
            with requests.get(url, headers=cabeceras, stream=True, timeout=(30, 120)) as r:
                if r.status_code == 416:
                    parcial.unlink(missing_ok=True)     # el trozo no casa con el fichero
                    raise RuntimeError("HTTP 416: se empieza de cero")
                if r.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status_code}")
                if r.status_code == 200 and tengo:
                    tengo = antes = 0               # ignoró el Range: se empieza de cero
                    ignora_range = True
                if r.status_code == 206:
                    total = r.headers.get("Content-Range", "").rpartition("/")[2]
                else:
                    total = r.headers.get("Content-Length", "")
                if total.isdigit() and int(total) != tamano:
                    if tamano is not None:
                        print(f"    AVISO: el servidor dice {int(total):,} bytes y CKAN {tamano:,}; manda el servidor.")
                    tamano = int(total)
                hito = (tengo // (20 << 20) + 1) * (20 << 20)
                with open(parcial, "ab" if tengo else "wb") as f:
                    for trozo in r.iter_content(chunk_size=1 << 20):
                        if not trozo:
                            continue
                        f.write(trozo)
                        tengo += len(trozo)
                        if tengo >= hito:
                            vel = (tengo - antes) / 1e3 / max(time.time() - t0, 0.1)
                            print(f"    {tengo / 1e6:,.0f} MB" + (f" de {tamano / 1e6:,.0f}" if tamano else "")
                                  + f" ({vel:,.0f} KB/s)", flush=True)
                            hito += 20 << 20
            if tamano is None:
                break                               # sin tamaño conocido: terminó el envío
        except (requests.RequestException, RuntimeError, OSError) as e:
            # Solo el TIPO de los errores de red: su texto lleva la URL completa.
            error = type(e).__name__ if isinstance(e, requests.RequestException) else str(e)
        ahora = parcial.stat().st_size if parcial.exists() else 0
        if tamano is not None and ahora == tamano:
            continue                                # completo: lo confirma la vuelta
        sin_avance = 0 if ahora > maximo else sin_avance + 1
        maximo = max(maximo, ahora)
        if sin_avance >= intentos:
            motivo = error or "respuesta incompleta"
            if ignora_range:
                motivo += "; el servidor ignora Range y corta antes de terminar"
            raise RuntimeError(f"descarga sin avance tras {intentos} intentos ({motivo})")
        espera = min(15 * max(sin_avance, 1), pausa_max)
        print(f"    corte a {ahora / 1e6:,.1f} MB ({error or 'respuesta incompleta'}); "
              f"reanudo en {espera} s", flush=True)
        time.sleep(espera)
    parcial.replace(destino)
    print(f"  OK: {destino.name} ({destino.stat().st_size / 1e6:,.1f} MB)")
    return destino


# ============================================================================
# CPV DEL PORTAL (opcional, --cpv-portal)
# ============================================================================
def cpv_de_hit(fuente_hit):
    """codigosCpv de la ficha -> lista de CPV de 8 dígitos sin repetir, en su orden:
    [{'codigo': '33600000-6'}, ...] -> ['33600000']. La estatal guarda 8 dígitos."""
    codigos = []
    for c in (fuente_hit or {}).get("codigosCpv") or []:
        bruto = c.get("codigo") if isinstance(c, dict) else c
        digitos = re.sub(r"\D", "", str(bruto or ""))[:8]
        if len(digitos) == 8 and digitos not in codigos:
            codigos.append(digitos)
    return codigos


def enriquece_cpv(filas, sesion=None, pausa=PAUSA_CPV, intentos=3):
    """Rellena fila['cpv'] desde el índice de la ficha del portal. Devuelve los recuentos.

    Servicio INTERNO sin documentar. NUNCA tumba la carga: un lote que falla entero deja
    cpv = null en sus filas (la RPC conserva el CPV que ya hubiera) y tras
    FALLOS_CPV_SEGUIDOS lotes seguidos se deja de pedir. Un contrato que el portal
    devuelve SIN CPV también queda null: un [] borraría un CPV bueno por una respuesta
    incompleta.

    OJO, medido en F1 (17/09/2026, 2 peticiones): el portal IGNORA la consulta 'ids'.
    Con 1.000 ID de 2026 devolvió HTTP 200 en 5,4 s y 1.000 documentos cualquiera (total
    948.458), NINGUNO de los pedidos; 999 traían CPV, que es la cifra que F0 tomó por
    buena. Por eso solo se acepta un hit cuyo _id esté en el lote pedido, y si un lote no
    trae NINGUNO de los pedidos se deja de pedir en esa ejecución (seguir serían ~300
    descargas de 5 MB para nada). La forma que sí filtra, verificada en F0, es
    {"query": {"match": {"_id": ID}}}: una petición por contrato."""
    sesion = sesion or requests.Session()
    cabeceras = {**CABECERAS, "Content-Type": "application/json", "Accept": "application/json"}
    ids = sorted(int(k.split(":", 1)[1]) for k in filas)
    res = Counter()
    seguidos = 0
    for i in range(0, len(ids), TAM_LOTE_CPV):
        lote = ids[i:i + TAM_LOTE_CPV]
        if seguidos >= FALLOS_CPV_SEGUIDOS:
            res["lotes_no_pedidos"] += 1
            res["filas_no_pedidas"] += len(lote)
            continue
        res["pedidos"] += len(lote)                 # solo lo que de verdad se pidió
        cuerpo = json.dumps({"query": {"ids": {"values": [str(x) for x in lote]}},
                             "size": len(lote)})
        hits = None
        for intento in range(1, intentos + 1):
            try:
                r = sesion.post(CPV_PORTAL_URL, headers=cabeceras, data=cuerpo, timeout=120)
                if r.status_code == 200:
                    hits = r.json()["hits"]["hits"]
                    break
            except (requests.RequestException, ValueError, KeyError, TypeError):
                pass
            if intento < intentos:
                time.sleep(5 * intento)
        if hits is None:
            res["lotes_fallidos"] += 1
            seguidos += 1
            print(f"    CPV: lote {i // TAM_LOTE_CPV + 1} fallido ({len(lote)} filas sin CPV)", flush=True)
        else:
            seguidos = 0
            res["lotes_ok"] += 1
            pedidos = set(lote)
            propios = 0
            for h in hits:
                id_txt = str(h.get("_id", "")).strip()
                if not id_txt.isdigit() or int(id_txt) not in pedidos:
                    res["hits_no_pedidos"] += 1          # nunca se asigna un CPV ajeno
                    continue
                propios += 1
                fila = filas.get(clave_andalucia(int(id_txt)))
                codigos = cpv_de_hit(h.get("_source"))
                if fila is not None and codigos:
                    fila["cpv"] = codigos
                    res["con_cpv"] += 1
            if hits and not propios:
                res["consulta_ignorada"] = 1
                seguidos = FALLOS_CPV_SEGUIDOS           # no se piden más lotes
                print(f"  AVISO: el portal ignoró la consulta (devolvió {len(hits)} contratos, ninguno "
                      f"de los pedidos). Se deja de pedir CPV en esta ejecución.", flush=True)
        if (i // TAM_LOTE_CPV + 1) % 20 == 0:
            print(f"    CPV: {res['pedidos']:,} pedidos, {res['con_cpv']:,} con CPV", flush=True)
        if i + TAM_LOTE_CPV < len(ids) and seguidos < FALLOS_CPV_SEGUIDOS:
            time.sleep(pausa)
    if seguidos >= FALLOS_CPV_SEGUIDOS and not res["consulta_ignorada"]:
        print(f"  AVISO: el portal falló {FALLOS_CPV_SEGUIDOS} lotes seguidos; el resto va sin CPV.")
    return dict(res)


# ============================================================================
# SUPABASE (PostgREST con service_role)
# ============================================================================
def en_lista(valores):
    """Filtro in.(...) de PostgREST con cada valor entre comillas: las claves 'and:1' y
    los nombres de órgano llevan ':', ',' o paréntesis, que sin comillas son sintaxis."""
    return "in.(" + ",".join('"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'
                             for v in valores) + ")"


class ClienteSupabase:
    """Lo mínimo para el cargador: RPC, lectura paginada y borrado, con reintentos ante
    429/5xx/red y parada ante un 4xx de datos. Cuenta las peticiones (el informe y la
    prueba de que la puerta vacía no consulta nada).

    OJO con los logs públicos: un error de red de requests trae la URL, y la URL de las
    consultas por CIF lleva NIF. Por eso aquí solo se imprime el TIPO de error."""

    def __init__(self, url_base, token, sesion=None):
        self.url = url_base.rstrip("/")
        self.ses = sesion or requests.Session()
        self.cab = {"apikey": token, "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json", "Accept": "application/json"}
        self.peticiones = Counter()

    def _pide(self, metodo, ruta, descripcion, params=None, cuerpo=None, cabeceras=None,
              con_mensaje=True, reintentos=4):
        url = f"{self.url}/rest/v1/{ruta}"
        datos = None if cuerpo is None else json.dumps(cuerpo, ensure_ascii=False).encode("utf-8")
        h = {**self.cab, **(cabeceras or {})}
        ultimo = None
        for intento in range(1, reintentos + 1):
            self.peticiones[f"{metodo} {ruta.split('?')[0]}"] += 1
            try:
                r = getattr(self.ses, metodo.lower())(url, headers=h, params=params, data=datos, timeout=300)
            except requests.RequestException as e:
                ultimo = type(e).__name__
            else:
                if r.status_code in (200, 201, 204, 206):
                    return r
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    raise ErrorCarga(f"{descripcion}: HTTP {r.status_code} {_motivo(r, con_mensaje)}")
                ultimo = f"HTTP {r.status_code}"
            if intento < reintentos:
                espera = 2 ** intento
                print(f"      reintento {intento}/{reintentos} en {espera}s ({descripcion}: {ultimo})", flush=True)
                time.sleep(espera)
        raise ErrorCarga(f"{descripcion}: sin respuesta tras {reintentos} intentos ({ultimo})")

    def rpc(self, nombre, cuerpo, descripcion=None, params=None):
        return self._pide("POST", f"rpc/{nombre}", descripcion or f"RPC {nombre}",
                          params=params, cuerpo=cuerpo).json()

    def rpc_paginada(self, nombre, cuerpo):
        """Función que devuelve un conjunto: se pagina con limit/offset porque PostgREST
        corta cada respuesta (la estatal tiene 7.676 órganos). Se sigue hasta una página
        VACÍA y no hasta una incompleta: si el tope del servidor fuera menor que PAGINA, una
        lista de órganos cortada abriría o cerraría la puerta en falso. Cuesta 1 petición."""
        salida, desde = [], 0
        while True:
            trozo = self.rpc(nombre, cuerpo, params={"limit": PAGINA, "offset": desde})
            if not trozo:
                return salida
            salida.extend(trozo)
            desde += len(trozo)

    def filas(self, tabla, params, descripcion, con_mensaje=True):
        """Todas las filas que casen, paginando POR CLAVE: order=licitacion_id y, desde la
        segunda página, licitacion_id=gt.<última>. params es una lista de pares (admite la
        misma columna dos veces: fecha gte y lte) y tiene que pedir licitacion_id.

        Nada de offset: cada página con offset vuelve a recorrer todo lo anterior (coste
        cuadrático; con ~300 páginas del SAS, lecturas de la tabla entera en el Micro).
        Lleva su propio limit y order: si quien llama mete otro, PostgREST se queda con uno
        y la paginación recorre la tabla entera (el fallo de los 278 s de informe_empresa.py)."""
        if any(k in ("limit", "offset", "order") for k, _ in params):
            raise ValueError("filas() pagina sola: quita 'limit', 'offset' y 'order' de los params")
        salida, ultima = [], None
        while True:
            pagina = list(params) + [("order", "licitacion_id"), ("limit", PAGINA)]
            if ultima is not None:
                pagina.append(("licitacion_id", f"gt.{ultima}"))
            trozo = self._pide("GET", tabla, descripcion, params=pagina, con_mensaje=con_mensaje).json()
            salida.extend(trozo)
            if len(trozo) < PAGINA:
                return salida
            ultima = trozo[-1]["licitacion_id"]

    def cuenta(self, tabla, params, descripcion):
        """Nº de filas que casan (Prefer count=exact, 1 fila de respuesta). None si el
        servidor no devuelve el total en Content-Range."""
        r = self._pide("GET", tabla, descripcion, params=list(params) + [("limit", 1)],
                       cabeceras={"Prefer": "count=exact"})
        total = (r.headers.get("Content-Range") or "").rpartition("/")[2]
        return int(total) if total.isdigit() else None

    def borra(self, tabla, params, descripcion):
        """DELETE; devuelve cuántas filas borró (Prefer count=exact -> Content-Range)."""
        r = self._pide("DELETE", tabla, descripcion, params=params,
                       cabeceras={"Prefer": "return=minimal,count=exact"})
        total = (r.headers.get("Content-Range") or "").rpartition("/")[2]
        return int(total) if total.isdigit() else 0


def _motivo(respuesta, con_mensaje):
    """Motivo legible de un error de PostgREST. Sin el mensaje cuando la petición llevaba
    NIF: algunos errores de sintaxis repiten el valor que no entendieron."""
    try:
        cuerpo = respuesta.json()
    except ValueError:
        return respuesta.text[:300] if con_mensaje else ""
    if not isinstance(cuerpo, dict):
        return ""
    if con_mensaje:
        return f"[{cuerpo.get('code')}] {cuerpo.get('message')} {cuerpo.get('hint') or ''}".strip()
    return f"[{cuerpo.get('code')}]"


def cliente_desde_entorno():
    """Cliente con SUPABASE_SERVICE_ROLE y SUPABASE_URL (como backfill_catalogo.py). None
    si no hay service_role: modo simulación local."""
    token = os.environ.get("SUPABASE_SERVICE_ROLE")
    if not token:
        return None
    url = os.environ.get("SUPABASE_URL")
    if not url:
        from backfill_catalogo import SUPABASE_URL as url   # el mismo valor por defecto
    return ClienteSupabase(url, token)


def valor_rpc(item):
    """Una fila de una función que devuelve SETOF text: según la versión de PostgREST
    llega como 'texto' o como {'menores_organos': 'texto'}."""
    if isinstance(item, dict):
        return next(iter(item.values()), None)
    return item


# ============================================================================
# GEMELAS (el mismo contrato en la estatal y en la autonómica)
# ============================================================================
# Regla validada en F0 (medir_duplicados.md). Una fila autonómica a y una estatal e son
# gemelas si se cumplen las cuatro:
#   (i)   identidad: NIF de a = e.cif_adjudicatario (admitiendo 'ES'+NIF). Si alguno está
#         enmascarado, la máscara tiene que coincidir Y el nombre parecerse ≥ 0,8.
#   (ii)  importe: los dos SIN IVA existen y difieren ≤ 0,01 €. Nunca sin IVA con con IVA.
#   (iii) fecha: la estatal no es nula y |días| ≤ 15.
#   (iv)  órgano: normorg(e.organo) está entre los órganos de ESA fuente o en los alias.
# Solo se actúa con emparejamiento 1:1; lo demás va a revisión y la autonómica se carga.
# Se queda la ESTATAL (es la fuente que ya usa todo el proyecto).
#
# F0 sobre 2025 completo: la regla marca 0 de 189 pares ingenuos (CIF + importe + fecha),
# 0 de 460 dudosos y 0 de 33.496 pares por importe. La regla ingenua habría borrado 139
# contratos legítimos (13 de 13 revisados a mano eran distintos).

COLUMNAS_GEMELA = ("licitacion_id,cif_adjudicatario,adjudicatario,importe_sin_iva,"
                   "fecha_adjudicacion,organo_contratacion,num_expediente")


def _decimal(valor):
    if valor is None:
        return None
    try:
        return valor if isinstance(valor, Decimal) else Decimal(str(valor))
    except InvalidOperation:
        return None


def identificadores(a, originales=None):
    """NIF de a con los que comparar: los DNI/NIE COMPLETOS que solo están en memoria
    (originales) en lugar de su máscara, y los demás NIF de la fila tal cual. Una fila
    leída de la base no trae originales: allí solo hay máscaras."""
    originales = [o for o in (originales or []) if o]
    tapados = {enmascara(o) for o in originales}
    return originales + [c for c in (a.get("cifs_adjudicatarios") or []) if c not in tapados]


def identidad(a, e, originales=None):
    """Condición (i). a trae cifs_adjudicatarios (ya enmascarados) y adjudicatario;
    originales, sus DNI/NIE completos si se leyeron del fichero en esta ejecución.
      · Ninguno enmascarado en origen: igualdad EXACTA del NIF, sin mirar el nombre.
        Comparar la máscara de un DNI completo casaría a dos personas distintas con los
        mismos dígitos 4-7 y nombre parecido (y la fila andaluza se BORRARÍA), y dejaría
        fuera al mismo DNI con un nombre de pila de más (Jaccard 0,75).
      · Alguno enmascarado: la máscara AEPD de los dos coincide (con sus 4 dígitos) Y el
        nombre se parece ≥ 0,8."""
    e_cif = quita_es(normaliza_cif(e.get("cif_adjudicatario")) or "")[0]
    if not e_cif:
        return False
    for bruto in identificadores(a, originales):
        a_cif = quita_es(normaliza_cif(bruto) or "")[0]
        if not a_cif:
            continue
        if "*" not in a_cif and "*" not in e_cif:
            if a_cif == e_cif:
                return True
            continue
        mascara = enmascara(a_cif)
        if tiene_digitos(mascara) and mascara == enmascara(e_cif) and \
                similitud_nombres(a.get("adjudicatario"), e.get("adjudicatario")) >= UMBRAL_NOMBRE:
            return True
    return False


def es_gemela(a, e, organos_validos, originales=None):
    """Las cuatro condiciones. organos_validos = normorg(órganos de la fuente) ∪ alias."""
    if normorg(e.get("organo_contratacion")) not in organos_validos:
        return False
    ia, ie = _decimal(a.get("importe_sin_iva")), _decimal(e.get("importe_sin_iva"))
    if ia is None or ie is None or abs(ia - ie) > TOLERANCIA_IMPORTE:
        return False
    fa, fe = a.get("fecha_adjudicacion"), e.get("fecha_adjudicacion")
    if not fa or not fe:
        return False
    if abs((date.fromisoformat(fa[:10]) - date.fromisoformat(fe[:10])).days) > DIAS_GEMELA:
        return False
    return identidad(a, e, originales)


def claves_busqueda(a, originales=()):
    """Claves del ÍNDICE EN MEMORIA de empareja (no de las consultas a la base): cada NIF,
    con 'ES' delante, su máscara y los DNI completos de originales, porque la estatal
    guarda muchos DNI completos y por la máscara no se encontrarían."""
    claves = set()
    for bruto in list(a.get("cifs_adjudicatarios") or []) + list(originales or []):
        valor = quita_es(normaliza_cif(bruto) or "")[0]
        if not valor:
            continue
        claves.add(valor)
        if tiene_digitos(enmascara(valor)):
            claves.add(enmascara(valor))
        if "*" not in valor:
            claves.add("ES" + valor)
    return claves


def empareja(filas_a, candidatas_e, organos_validos, originales=None):
    """Aplica la regla a todos los pares posibles y separa lo seguro de lo dudoso.
    Devuelve (gemelas, revision): listas de (licitacion_id autonómica, licitacion_id estatal).
    gemelas = pares 1:1; revision = pares donde a casa con varias e o e con varias a."""
    originales = originales or {}
    indice = defaultdict(list)
    for e in candidatas_e:
        valor = quita_es(normaliza_cif(e.get("cif_adjudicatario")) or "")[0]
        if not valor:
            continue
        indice[valor].append(e)
        mascara = enmascara(valor)
        if mascara != valor and tiene_digitos(mascara):
            indice[mascara].append(e)
    pares = []
    for a in filas_a:
        vistas = set()
        orig = originales.get(a["licitacion_id"])
        for clave in claves_busqueda(a, orig):
            for e in indice.get(clave, ()):
                if e["licitacion_id"] in vistas:
                    continue
                vistas.add(e["licitacion_id"])
                if es_gemela(a, e, organos_validos, orig):
                    pares.append((a["licitacion_id"], e["licitacion_id"]))
    grado_a = Counter(p[0] for p in pares)
    grado_e = Counter(p[1] for p in pares)
    gemelas = [p for p in pares if grado_a[p[0]] == 1 and grado_e[p[1]] == 1]
    revision = [p for p in pares if not (grado_a[p[0]] == 1 and grado_e[p[1]] == 1)]
    return gemelas, revision


class PuertaOrganos:
    """Paso (1): si ningún órgano estatal coincide con uno de la fuente (o con un alias),
    no puede haber gemelas y no se consulta nada más. Hoy: 0 de 7.672.

    Los órganos de la fuente salen de los ficheros leídos en esta ejecución Y de la base
    (menores_organos(fuente)): un año que no ha cambiado no se lee, pero sus filas siguen
    ahí y pueden tener gemela nueva en la estatal (sentido contrario)."""

    def __init__(self, cliente, fuente, alias):
        self.cliente = cliente
        self.fuente = fuente
        self.alias = alias                         # {estatal normalizado: fuente normalizado}
        self.estatales = None                      # {normalizado}
        self.en_base = {}                          # {normalizado: {nombres tal cual}}
        self.de_ficheros = set()

    def carga_base(self):
        if self.estatales is None:
            self.estatales = {normorg(valor_rpc(x)) for x in
                              self.cliente.rpc_paginada("menores_organos", {"p_fuente": "estatal"})}
            for x in self.cliente.rpc_paginada("menores_organos", {"p_fuente": self.fuente}):
                nombre = valor_rpc(x)
                self.en_base.setdefault(normorg(nombre), set()).add(nombre)

    def anade_ficheros(self, filas):
        self.de_ficheros.update(normorg(f["organo_contratacion"]) for f in filas.values()
                                if f.get("organo_contratacion"))

    def calcula(self):
        """-> (interseccion, organos_validos, organos_objetivo_de_la_fuente)."""
        self.carga_base()
        de_fuente = (set(self.en_base) | self.de_ficheros) - {""}
        validos = de_fuente | set(self.alias)
        interseccion = self.estatales & validos
        objetivo = {x for x in interseccion if x in de_fuente} | \
                   {self.alias[x] for x in interseccion if x in self.alias}
        return interseccion, validos, objetivo

    def resumen(self):
        interseccion, validos, _ = self.calcula()
        return {"organos_estatales": len(self.estatales),
                "organos_fuente": len((set(self.en_base) | self.de_ficheros) - {""}),
                "alias": len(self.alias),
                "en_comun": len(interseccion),
                "ejemplos_en_comun": sorted(interseccion)[:50]}


def patrones_persona(mascara):
    """Máscara AEPD -> patrones LIKE que casan con ella Y con el DNI/NIE completo que
    tape ('***4567**' -> '___4567__' y 'ES___4567__'). En la URL solo van los 4 dígitos
    que ya son públicos. [] si no es una máscara completa."""
    if _MASCARA_DNI.fullmatch(mascara) or _MASCARA_NIE.fullmatch(mascara):
        patron = mascara.replace("*", "_")
        return [patron, "ES" + patron]
    return []


def busca_candidatas(cliente, filas_a, originales):
    """Paso (2): candidatas estatales de las filas a. -> (candidatas, nº de consultas).

    · Personas jurídicas y NIF extranjeros: por CIF exacto
      (cif_adjudicatario=in.(...), índice menores_cif_idx), en trozos de TROZO_CLAVES
      NIF distintos (cada uno una sola vez) con la fecha a ±15 días de las filas del trozo.
    · Personas físicas: por PATRÓN de su máscara (patrones_persona), NUNCA por el DNI
      completo. La URL de un GET queda en los logs de la API de Supabase, y un DNI que
      este cargador no guarda no debe acabar ahí (revisión de F1: 3 de 17 consultas del
      SAS 2026 llevaban un DNI completo). El patrón no usa índice: se acota la fecha por
      trozos de DIAS_TROZO_PATRON días; con 100 patrones y 45 días, Postgres recorre
      menores_fecha_asc (~72.000 filas) y filtra (EXPLAIN, 17/09/2026). El patrón también
      trae DNI distintos con los mismos 4 dígitos: los descarta la regla (identidad)."""
    exactas, por_patron = defaultdict(list), []
    for a in filas_a:
        if a.get("importe_sin_iva") is None or not a.get("fecha_adjudicacion"):
            continue                               # sin importe o sin fecha no hay gemela
        for bruto in identificadores(a, originales.get(a["licitacion_id"])):
            valor = quita_es(normaliza_cif(bruto) or "")[0]
            if not valor:
                continue
            mascara = enmascara(valor)
            if mascara == valor and "*" not in valor:
                exactas[valor].append(a["fecha_adjudicacion"])
                exactas["ES" + valor].append(a["fecha_adjudicacion"])
            else:                                  # persona: máscara completa o nada
                por_patron += [(a["fecha_adjudicacion"], p) for p in patrones_persona(mascara)]
    consultas = []
    claves = sorted(exactas)
    for i in range(0, len(claves), TROZO_CLAVES):
        trozo = claves[i:i + TROZO_CLAVES]
        fechas = [f for c in trozo for f in exactas[c]]
        consultas.append((min(fechas), max(fechas), ("cif_adjudicatario", en_lista(trozo))))
    trozo, inicio = set(), None
    for fecha, patron in sorted(set(por_patron)) + [(None, None)]:
        if trozo and (fecha is None or len(trozo) >= TROZO_CLAVES or
                      (date.fromisoformat(fecha) - date.fromisoformat(inicio)).days > DIAS_TROZO_PATRON):
            filtro = "(" + ",".join(f"cif_adjudicatario.like.{p}" for p in sorted(trozo)) + ")"
            consultas.append((inicio, ultima, ("or", filtro)))
            trozo = set()
        if fecha is None:
            break
        if not trozo:
            inicio = fecha
        trozo.add(patron)
        ultima = fecha
    candidatas = {}
    for primera, ultima, filtro in consultas:
        desde = (date.fromisoformat(primera) - timedelta(days=DIAS_GEMELA)).isoformat()
        hasta = (date.fromisoformat(ultima) + timedelta(days=DIAS_GEMELA)).isoformat()
        params = [("select", COLUMNAS_GEMELA), ("fuente", "eq.estatal"), filtro,
                  ("fecha_adjudicacion", f"gte.{desde}"), ("fecha_adjudicacion", f"lte.{hasta}")]
        for e in cliente.filas("menores", params, "candidatas estatales por CIF", con_mensaje=False):
            candidatas[e["licitacion_id"]] = e
    return list(candidatas.values()), len(consultas)


def borra_autonomicas(cliente, fuente, claves):
    """DELETE de filas autonómicas por clave, SIEMPRE con el filtro de fuente: aunque una
    clave se colara con forma de URL estatal, este DELETE no podría tocar la estatal."""
    if fuente == "estatal":
        raise ValueError("borra_autonomicas no borra nunca filas estatales")
    borradas = 0
    claves = sorted(claves)
    for i in range(0, len(claves), TROZO_BORRADO):
        borradas += cliente.borra("menores", [("licitacion_id", en_lista(claves[i:i + TROZO_BORRADO])),
                                              ("fuente", f"eq.{fuente}")],
                                  "borrar gemelas autonómicas")
    return borradas


def resuelve_gemelas(cliente, puerta, filas, originales, cargar):
    """Pasos (1) y (2) para las filas de UN fichero. Quita de 'filas' las gemelas (no se
    cargan) y, con --cargar, las borra de la base por si ya estaban. Devuelve el informe."""
    if cliente is None:
        return {"puerta": "saltada: sin credenciales (simulación local)"}
    puerta.anade_ficheros(filas)
    interseccion, validos, objetivo = puerta.calcula()
    info = {"puerta": "consultada", "organos_en_comun": len(interseccion)}
    if not interseccion:
        info.update({"filas_revisadas": 0, "consultas_por_cif": 0, "gemelas_descartadas": 0,
                     "gemelas_borradas": 0, "ambiguas": 0})
        return info
    revisar = [f for f in filas.values() if normorg(f.get("organo_contratacion")) in objetivo]
    candidatas, n_consultas = busca_candidatas(cliente, revisar, originales)
    gemelas, revision = empareja(revisar, candidatas, validos, originales)
    ids = {a for a, _ in gemelas}
    for a in ids:
        filas.pop(a, None)
    borradas = borra_autonomicas(cliente, puerta.fuente, ids) if (cargar and ids) else 0
    info.update({
        "filas_revisadas": len(revisar), "consultas_por_cif": n_consultas,
        "candidatas_estatales": len(candidatas),
        "gemelas_descartadas": len(ids), "gemelas_borradas": borradas,
        "ambiguas": len({a for a, _ in revision}),
        "detalle_gemelas": [{"autonomica": a, "estatal": e} for a, e in gemelas][:MAX_DETALLE],
        "revision": [{"autonomica": a, "estatal": e} for a, e in revision][:MAX_DETALLE],
    })
    return info


def gemelas_en_base(cliente, puerta, excluir, cargar, todos_leidos=False):
    """Paso (3), sentido contrario: la estatal llegó DESPUÉS. Repite (2) con las filas de
    la fuente que YA están en la base en los órganos en común (sin las de esta ejecución,
    que ya pasaron por (2)).

    · todos_leidos: si esta ejecución leyó TODOS los años de la ventana, lo que queda en
      la base de esos órganos son filas de esta misma ejecución (ya revisadas) o de fuera
      de la ventana (las borra la purga). No se lee nada.
    · Por órgano: primero se cuenta (índice menores_organo_fuente_idx, sin leer filas) y
      un órgano con más de TOPE_FILAS_ORGANO filas no se recorre: queda en el informe para
      repasarlo con --forzar, que pasa el paso (2) por todos los ficheros. El SAS son
      ~300.000 filas: leerlas dos veces por semana para descartar casi todas no compensa."""
    if cliente is None:
        return {"puerta": "saltada: sin credenciales (simulación local)"}
    interseccion, validos, objetivo = puerta.calcula()
    info = {"puerta": "consultada", "organos_en_comun": len(interseccion)}
    if not interseccion:
        info.update({"filas_revisadas": 0, "gemelas_borradas": 0, "ambiguas": 0})
        return info
    if todos_leidos:
        info.update({"filas_revisadas": 0, "gemelas_borradas": 0, "ambiguas": 0,
                     "omitido": "se leyeron todos los años de la ventana: sus filas ya pasaron por el paso 2"})
        return info
    en_base, grandes = {}, []
    for organo in sorted(objetivo):
        nombres = sorted(puerta.en_base.get(organo, ()))
        if not nombres:
            continue                               # solo está en los ficheros de hoy
        filtro = [("fuente", f"eq.{puerta.fuente}"), ("organo_contratacion", en_lista(nombres))]
        total = cliente.cuenta("menores", [("select", "licitacion_id")] + filtro,
                               "recuento de filas autonómicas de un órgano en común")
        if total is None or total > TOPE_FILAS_ORGANO:
            grandes.append({"organo": organo, "filas": total})
            print(f"  AVISO: '{organo}' tiene {'?' if total is None else f'{total:,}'} filas en la base "
                  f"(tope {TOPE_FILAS_ORGANO:,}): no se recorre; repásalo con --forzar.", flush=True)
            continue
        for f in cliente.filas("menores", [("select", COLUMNAS_GEMELA + ",cifs_adjudicatarios")] + filtro,
                               "filas autonómicas de un órgano en común"):
            if f["licitacion_id"] not in excluir:
                en_base[f["licitacion_id"]] = f
    revisar = list(en_base.values())
    candidatas, n_consultas = busca_candidatas(cliente, revisar, {})
    gemelas, revision = empareja(revisar, candidatas, validos)
    ids = {a for a, _ in gemelas}
    borradas = borra_autonomicas(cliente, puerta.fuente, ids) if (cargar and ids) else 0
    info.update({
        "filas_revisadas": len(revisar), "consultas_por_cif": n_consultas,
        "organos_sin_recorrer": len(grandes), "detalle_organos_sin_recorrer": grandes[:MAX_DETALLE],
        "candidatas_estatales": len(candidatas), "gemelas": len(ids),
        "gemelas_borradas": borradas, "ambiguas": len({a for a, _ in revision}),
        "detalle_gemelas": [{"autonomica": a, "estatal": e} for a, e in gemelas][:MAX_DETALLE],
        "revision": [{"autonomica": a, "estatal": e} for a, e in revision][:MAX_DETALLE],
    })
    return info


# ============================================================================
# CARGA POR LOTES (RPC menores_carga_lote)
# ============================================================================
def fila_para_rpc(fila):
    """Fila -> objeto json con EXACTAMENTE las claves de CAMPOS_RPC. Los importes van como
    número (float de un Decimal de 2 decimales: la ida y vuelta es exacta)."""
    if fila.get("fuente") in (None, "", "estatal"):
        raise ValueError(f"fila sin fuente autonómica: {fila.get('licitacion_id')}")
    salida = {}
    for campo in CAMPOS_RPC:
        valor = fila.get(campo)
        salida[campo] = float(valor) if isinstance(valor, Decimal) else valor
    return salida


def lotes_rpc(filas, tam=TAM_LOTE):
    """Lotes para la RPC. La RPC rechaza ENTERO un lote con claves repetidas; aquí se
    comprueba antes sobre el conjunto (que ningún lote pueda llevarlas)."""
    filas = list(filas)
    claves = [f["licitacion_id"] for f in filas]
    if len(set(claves)) != len(claves):
        raise ValueError("hay claves repetidas: no se generan lotes")
    return [[fila_para_rpc(f) for f in filas[i:i + tam]] for i in range(0, len(filas), tam)]


def carga_filas(cliente, filas, etiqueta):
    """Escribe con menores_carga_lote y suma lo que devuelve. Un 4xx para la carga."""
    suma = Counter()
    lotes = lotes_rpc(sorted(filas.values(), key=lambda f: f["licitacion_id"]))
    for n, lote in enumerate(lotes, 1):
        try:
            res = cliente.rpc("menores_carga_lote", {"p_filas": lote},
                              descripcion=f"{etiqueta}: lote {n}/{len(lotes)}")
        except ErrorCarga as e:
            pista = (" ¿Está ejecutado menores_f1.sql?" if "PGRST202" in str(e) or "404" in str(e) else "")
            raise ErrorCarga(f"la base rechazó un lote y se para la carga: {e}.{pista}") from None
        for k in ("recibidas", "insertadas", "actualizadas", "iguales", "conflictos"):
            suma[k] += int((res or {}).get(k) or 0)
        suma["lotes"] += 1
        if n % 20 == 0 or n == len(lotes):
            print(f"    {etiqueta}: lote {n}/{len(lotes)} · insertadas {suma['insertadas']:,} · "
                  f"actualizadas {suma['actualizadas']:,} · iguales {suma['iguales']:,} · "
                  f"conflictos {suma['conflictos']:,}", flush=True)
    return dict(suma)


# ============================================================================
# ESTADO (--estado) E INFORME (--informe)
# ============================================================================
def lee_estado(ruta):
    if not ruta or not Path(ruta).exists():
        return {}
    try:
        datos = json.loads(Path(ruta).read_text(encoding="utf-8"))
        return datos if isinstance(datos, dict) else {}
    except ValueError:
        print(f"  AVISO: {ruta} no es JSON válido; empiezo con el estado vacío.")
        return {}


def recurso_sin_cambios(estado, fuente, recurso):
    """¿Ya se procesó ESTA versión del recurso? Mismo last_modified y mismo size."""
    previo = (estado.get(fuente) or {}).get(recurso["resource_id"])
    return bool(previo) and previo.get("last_modified") == recurso["last_modified"] \
        and previo.get("size") == recurso["size"]


def guarda_estado(ruta, estado, fuente, recurso):
    estado.setdefault(fuente, {})[recurso["resource_id"]] = {
        "last_modified": recurso["last_modified"],
        "size": recurso["size"],
        "procesado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_name(ruta.name + ".tmp")
    temporal.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")
    temporal.replace(ruta)                         # nunca un estado a medio escribir


def suma_recuentos(destino, origen):
    """Suma recursiva de los números de dos dicts (para los totales del informe)."""
    for k, v in origen.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            destino[k] = round(destino.get(k, 0) + v, 1) if isinstance(v, float) else destino.get(k, 0) + v
        elif isinstance(v, dict) and k != "recurso":
            suma_recuentos(destino.setdefault(k, {}), v)
    return destino


def compacto(info):
    """Un dict de recuentos sin sus listas de detalle, para imprimirlo en una línea."""
    return {k: v for k, v in info.items() if not isinstance(v, (list, dict))}


def resumen_markdown(informe):
    """Resumen para GITHUB_STEP_SUMMARY (público: solo recuentos)."""
    t = informe["totales"]
    carga = t.get("carga") or {}
    lineas = [
        f"## Menores autonómicos · {informe['etiqueta']} · {informe['modo']}",
        "",
        f"Ventana desde **{informe['ventana_desde']}** · años {', '.join(map(str, informe['anios']))}"
        f" · {informe['segundos']:,} s",
        "",
        "| Año | Situación | Leídas | En ventana | Fuera por ESTADO | Válidas | CPV | Gemelas | "
        "Insertadas | Actualizadas | Iguales | Conflictos |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for f in informe["ficheros"]:
        c = f.get("carga") or {}
        g = f.get("gemelas") or {}
        lineas.append(
            f"| {f['anio']} | {f.get('situacion', '')} | {f.get('lineas_leidas', 0):,} | "
            f"{f.get('en_ventana', 0):,} | {sum((f.get('fuera_por_estado') or {}).values()):,} | "
            f"{f.get('filas_validas', 0):,} | {(f.get('cpv') or {}).get('con_cpv', '—')} | "
            f"{g.get('gemelas_descartadas', '—')} | {c.get('insertadas', '—')} | "
            f"{c.get('actualizadas', '—')} | {c.get('iguales', '—')} | {c.get('conflictos', '—')} |")
    lineas += ["", f"**Total válidas:** {t.get('filas_validas', 0):,} · insertadas "
               f"{carga.get('insertadas', 0):,} · actualizadas {carga.get('actualizadas', 0):,} · "
               f"iguales {carga.get('iguales', 0):,} · conflictos {carga.get('conflictos', 0):,}"]
    nif = t.get("nif") or {}
    lineas += ["", f"**Personas físicas:** DNI enmascarados {nif.get('dni_enmascarados', 0):,} · NIE/KLM "
               f"{nif.get('nie_klm_enmascarados', 0):,} · máscaras de la Junta reducidas "
               f"{nif.get('mascaras_reducidas', 0):,} y descartadas {nif.get('mascaras_descartadas', 0):,} · "
               f"DNI tapados en el adjudicatario {nif.get('adjudicatario_con_dni_tapado', 0):,} y en el objeto "
               f"{nif.get('objeto_con_dni_tapado', 0):,}"]
    puerta = informe.get("puerta_organos") or {}
    if puerta:
        lineas += ["", f"**Puerta de órganos:** {compacto(puerta)}"]
    sin_recorrer = (informe.get("gemelas_en_base") or {}).get("organos_sin_recorrer")
    if sin_recorrer:
        lineas += ["", f"**AVISO:** {sin_recorrer} órganos en común con demasiadas filas para el sentido "
                       f"contrario (tope {TOPE_FILAS_ORGANO:,}); repásalos con «forzar»."]
    if informe["errores"]:
        lineas += ["", "**Errores:**"] + [f"- {e}" for e in informe["errores"]]
    return "\n".join(lineas) + "\n"


# ============================================================================
# ORQUESTACIÓN
# ============================================================================
def anios_por_defecto(desde, hoy):
    """Los ficheros son anuales por fecha de adjudicación: los años que toca la ventana."""
    return list(range(desde.year, hoy.year + 1))


def procesa(args):
    t_inicio = time.time()
    fuentes = lee_fuentes()
    fuente = args.fuente
    if fuente not in fuentes:
        sys.exit(f"ERROR: la fuente '{fuente}' no está en {FUENTES_JSON.name}.")
    if fuente not in FUENTES_CON_CARGADOR:
        sys.exit(f"ERROR: la fuente '{fuente}' aún no tiene cargador (hoy: {', '.join(FUENTES_CON_CARGADOR)}).")
    etiqueta = etiqueta_fuente(fuentes, fuente)

    hoy = date.today()
    desde = date.fromisoformat(args.desde) if args.desde else resta_anios(hoy, VENTANA_ANIOS)
    csv_local = {}
    for par in args.csv_local or []:
        anio, _, ruta = par.partition("=")
        if not anio.strip().isdigit() or not ruta:
            sys.exit(f"ERROR: --csv-local espera AÑO=RUTA (recibido: {par})")
        csv_local[int(anio)] = Path(ruta)
    if args.anios:
        anios = sorted({int(x) for x in re.split(r"[,\s]+", args.anios.strip()) if x})
    elif csv_local:
        anios = sorted(csv_local)                  # con ficheros locales, no se baja nada más
    else:
        anios = anios_por_defecto(desde, hoy)

    cliente = cliente_desde_entorno()
    if args.cargar and cliente is None:
        sys.exit("ERROR: --cargar necesita SUPABASE_SERVICE_ROLE (y SUPABASE_URL) en el entorno. "
                 "Sin --cargar se simula sin escribir nada.")
    modo = "carga" if args.cargar else "simulación"
    estado = lee_estado(args.estado)
    alias = lee_alias(fuente)
    puerta = PuertaOrganos(cliente, fuente, alias) if cliente else None
    cache = Path(args.cache) if args.cache else CACHE_DIR

    informe = {
        "fuente": fuente, "etiqueta": etiqueta, "modo": modo,
        "inicio": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "hoy": hoy.isoformat(), "ventana_desde": desde.isoformat(), "anios": anios,
        "opciones": {"cargar": bool(args.cargar), "cpv_portal": bool(args.cpv_portal),
                     "forzar": bool(args.forzar), "csv_local": sorted(csv_local)},
        "credenciales": cliente is not None,
        "ficheros": [], "errores": [],
    }
    print("=" * 78)
    print(f"MENORES AUTONÓMICOS · {etiqueta} · {modo.upper()}")
    print(f"Ventana desde {desde.isoformat()} · años {anios} · CPV del portal: "
          f"{'sí' if args.cpv_portal else 'no'} · forzar: {'sí' if args.forzar else 'no'}")
    if cliente is None:
        print("Sin credenciales: la puerta de gemelas se salta (no se consulta la base).")
    print("=" * 78)

    ya_vistos, ids_de_ejecucion = set(), set()
    codigo_salida = 0
    try:
        for anio in anios:
            t0 = time.time()
            fichero = {"anio": anio}
            informe["ficheros"].append(fichero)
            print(f"\n--- {etiqueta} {anio} ---")
            recurso = None
            try:
                if anio in csv_local:
                    ruta = csv_local[anio]
                    fichero.update({"origen": "local", "ruta": ruta.name})
                    print(f"  fichero local: {ruta}")
                else:
                    recurso = recurso_andalucia(anio)
                    if recurso is None:
                        fichero["situacion"] = "sin paquete"
                        print(f"  el paquete de {anio} aún no existe en CKAN; nada que hacer.")
                        continue
                    fichero.update({"origen": "ckan", "recurso": recurso})
                    print(f"  recurso {recurso['fichero']} · {(recurso['size'] or 0) / 1e6:,.1f} MB · "
                          f"modificado {recurso['last_modified']}")
                    if not args.forzar and recurso_sin_cambios(estado, fuente, recurso):
                        fichero["situacion"] = "sin cambios"
                        print("  sin cambios desde la última carga (mismo last_modified y size): se salta.")
                        continue
                    cache.mkdir(parents=True, exist_ok=True)
                    extension = ".csv.zip" if recurso["fichero"].lower().endswith(".zip") else ".csv"
                    huella = re.sub(r"\D", "", recurso["last_modified"] or "")[:14]
                    destino = cache / f"{fuente}_{anio}_{recurso['resource_id'][:8]}_{huella}_{recurso['size']}{extension}"
                    t_desc = time.time()
                    ruta = descarga_reanudable(recurso["url"], destino, recurso["size"])
                    fichero["segundos_descarga"] = round(time.time() - t_desc, 1)

                filas, originales, info = lee_fichero_andalucia(ruta, desde.isoformat(), ya_vistos, str(anio))
                fichero.update(info)
                print(f"  leídas {info['lineas_leidas']:,} líneas · {info['ids_distintos']:,} ID · en ventana "
                      f"{info['en_ventana']:,} · fuera por estado {sum(info['fuera_por_estado'].values()):,} · "
                      f"válidas {info['filas_validas']:,} ({info['segundos_lectura']} s)")
                if info["ids_repetidos"]["ids"]:
                    print(f"  ID repetidos: {info['ids_repetidos']['ids']} (desempate por órgano)")
                if info["mal_formados"] or info["caracteres_irrecuperables"]:
                    print(f"  AVISO: {info['mal_formados']} registros mal formados, "
                          f"{info['caracteres_irrecuperables']} con caracteres irrecuperables")

                # Antes de quitar gemelas: el paso (3) no debe volver a mirar estas claves.
                ids_de_ejecucion.update(filas)
                t_gem = time.time()
                fichero["gemelas"] = resuelve_gemelas(cliente, puerta, filas, originales, args.cargar)
                fichero["segundos_gemelas"] = round(time.time() - t_gem, 1)
                if cliente is not None and not fichero["gemelas"].get("organos_en_comun"):
                    print("  gemelas: 0 órganos en común con la estatal; no se consulta nada más.")
                else:
                    print(f"  gemelas: {compacto(fichero['gemelas'])}")

                if args.cpv_portal and filas:
                    t_cpv = time.time()
                    print(f"  CPV del portal para {len(filas):,} filas…")
                    fichero["cpv"] = enriquece_cpv(filas)
                    fichero["segundos_cpv"] = round(time.time() - t_cpv, 1)
                    print(f"  CPV: {fichero['cpv']}")

                if args.cargar:
                    t_carga = time.time()
                    fichero["carga"] = carga_filas(cliente, filas, str(anio))
                    fichero["segundos_carga"] = round(time.time() - t_carga, 1)
                    if recurso is not None and args.estado:
                        guarda_estado(args.estado, estado, fuente, recurso)
                    fichero["situacion"] = "cargado"
                else:
                    fichero["situacion"] = "simulado"
            except ErrorCarga:
                raise
            except Exception as e:  # noqa: BLE001 — un año roto no para los demás
                fichero["situacion"] = "error"
                mensaje = f"{anio}: {type(e).__name__}: {e}"
                informe["errores"].append(mensaje)
                print(f"  ERROR {mensaje}")
                codigo_salida = 1
            finally:
                fichero["segundos"] = round(time.time() - t0, 1)

        if cliente is not None:
            print("\n--- gemelas en la base (la estatal llegó después) ---")
            t_gem = time.time()
            leidos = {f["anio"] for f in informe["ficheros"] if f.get("situacion") in ("cargado", "simulado")}
            todos_leidos = set(anios_por_defecto(desde, hoy)) <= leidos
            informe["gemelas_en_base"] = gemelas_en_base(cliente, puerta, ids_de_ejecucion, args.cargar,
                                                         todos_leidos)
            informe["gemelas_en_base"]["segundos"] = round(time.time() - t_gem, 1)
            informe["puerta_organos"] = puerta.resumen()
            print(f"  {compacto(informe['gemelas_en_base'])}")
            print(f"  puerta: {compacto(informe['puerta_organos'])}")
            informe["peticiones_supabase"] = dict(cliente.peticiones)
        else:
            informe["puerta_organos"] = {"puerta": "saltada: sin credenciales (simulación local)"}
    except ErrorCarga as e:
        informe["errores"].append(str(e))
        print(f"\nERROR: {e}")
        codigo_salida = 2
    finally:
        informe["totales"] = {}
        for f in informe["ficheros"]:
            suma_recuentos(informe["totales"], {k: v for k, v in f.items() if k != "anio"})
        informe["segundos"] = round(time.time() - t_inicio, 1)
        informe["fin"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _escribe_salidas(args, informe)

    t = informe["totales"]
    print("\n" + "=" * 78)
    print(f"TOTAL {etiqueta}: {t.get('lineas_leidas', 0):,} líneas · en ventana {t.get('en_ventana', 0):,} · "
          f"válidas {t.get('filas_validas', 0):,} · {informe['segundos']:,} s")
    if args.cargar:
        c = t.get("carga") or {}
        print(f"  insertadas {c.get('insertadas', 0):,} · actualizadas {c.get('actualizadas', 0):,} · "
              f"iguales {c.get('iguales', 0):,} · conflictos {c.get('conflictos', 0):,}")
        if c.get("insertadas", 0) > 50000:
            print("  RECUERDA: tras una carga grande, VACUUM (ANALYZE) public.menores en el SQL Editor.")
        if not (fuentes.get(fuente) or {}).get("cargada"):
            print(f"  RECUERDA: '{fuente}' sigue con cargada=false en data/menores_fuentes.json.")
    print("=" * 78)
    return codigo_salida


def _escribe_salidas(args, informe):
    if args.informe:
        ruta = Path(args.informe)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(informe, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"\nInforme: {ruta}")
    resumen = os.environ.get("GITHUB_STEP_SUMMARY")
    if resumen:
        with open(resumen, "a", encoding="utf-8") as f:
            f.write(resumen_markdown(informe))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Carga de menores AUTONÓMICOS a public.menores (F1: Andalucía).")
    ap.add_argument("--fuente", required=True, help="Fuente autonómica (hoy solo 'andalucia').")
    ap.add_argument("--anios", default=None,
                    help="Años a procesar, p. ej. '2024,2025'. Por defecto, los que toca la ventana "
                         "(o los de --csv-local si se dan).")
    ap.add_argument("--desde", default=None,
                    help="Inicio de la ventana AAAA-MM-DD (por defecto, hoy menos 3 años).")
    ap.add_argument("--cargar", action="store_true",
                    help="Escribe en Supabase (RPC menores_carga_lote). Sin esto, SIMULA.")
    ap.add_argument("--cpv-portal", action="store_true",
                    help="Rellena el CPV con la ficha del portal de la Junta. OJO: en F1 el portal ignoró "
                         "la consulta por lotes; si vuelve a pasar, se corta tras la primera petición.")
    ap.add_argument("--forzar", action="store_true",
                    help="Procesa aunque el recurso no haya cambiado según --estado.")
    ap.add_argument("--estado", default=None,
                    help="JSON con la versión de cada recurso ya cargada (solo se escribe con --cargar).")
    ap.add_argument("--informe", default=None, help="JSON de salida con todos los recuentos.")
    ap.add_argument("--csv-local", nargs="+", action="extend", default=None, metavar="AÑO=RUTA",
                    help="Usa un fichero ya descargado para ese año en vez de bajarlo.")
    ap.add_argument("--cache", default=None, help="Carpeta de descargas (por defecto cache_menores/).")
    args = ap.parse_args()
    sys.exit(procesa(args))


if __name__ == "__main__":
    main()
