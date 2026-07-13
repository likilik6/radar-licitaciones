# -*- coding: utf-8 -*-
"""backfill_catalogo.py — BG-2: backfill de la ventana de 2 AÑOS al BUSCADOR.

Descarga los ZIP de Datos Abiertos de la PLACSP (licitaciones NO menores y
licitaciones agregadas NO menores), parsea TODOS los .atom con el MISMO extractor
del radar (feeds.extrae_entrada, sin duplicar lógica) y los vuelca a la tabla
public.licitaciones de Supabase.

VENTANA (--cargar, por defecto): 2 años → los dos años previos COMPLETOS por ZIP
ANUAL (p.ej. 2024 y 2025) + el año en curso por ficheros MENSUALES (202601..mes
actual). Se puede trocear con --periodos y --solo.

SEPARADO del radar: NO toca data/licitaciones.json. Es otra cosa (la base
consultable completa), no el destilado estático.

MODOS
  · dry-run (POR DEFECTO): NO escribe. Cuenta únicas por fuente/año, rango de
    fechas y estima el tamaño. (Para sondear sin bajar años: --muestra.)
  · diaria (--diario): ingesta incremental. Lee los feeds EN VIVO (como el radar) y
    hace UPSERT de TODAS las licitaciones (no solo las de interés). Para el cron.
  · purga (--purgar): borra del catálogo lo más viejo que la ventana, salvo lo
    abierto y lo que esté en public.contratos o public.decisiones (vía RPC
    purga_catalogo). --simular solo cuenta lo que se borraría. --ventana-anios N.
  · carga (--cargar): upsert idempotente por licitacion_id (conserva primera_vez,
    actualiza ultima_vez; tsv lo pone el trigger). En STREAMING (baja un ZIP →
    parsea → upsert → borra el ZIP antes del siguiente, para no llenar el disco) y
    REANUDABLE por checkpoint (unidad + .atom). Si el job se acerca a su límite de
    tiempo, se trocea por año/fuente con --periodos/--solo (los upserts son
    idempotentes, re-lanzar un trozo no duplica).

SEGURIDAD: la service_role y la URL se leen de variables de entorno
SUPABASE_SERVICE_ROLE y SUPABASE_URL (en local/secret). NUNCA en el repo ni en el
cliente; el script no las imprime. El dry-run/muestra no necesitan clave.

USO
  python backfill_catalogo.py --muestra                 # estimación por muestreo
  python backfill_catalogo.py                           # dry-run [año-1, año]
  # Carga (en Actions o local). PowerShell:
  $env:SUPABASE_SERVICE_ROLE="..."; python backfill_catalogo.py --cargar
  python backfill_catalogo.py --cargar --solo agregadas         # trocear por fuente
  python backfill_catalogo.py --cargar --solo estatal --periodos 2024   # y por año

OJO con el volumen: el servidor sirve los ZIP en 'chunked' (sin Content-Length); un
MES de estatal pesa ~190 MB comprimido y un AÑO ~2 GB. En streaming solo hay UN ZIP
en disco a la vez.
"""

import os
import sys
import json
import time
import zipfile
import tempfile
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

import requests
from lxml import etree

# Reutilizamos del radar: la cabecera de navegador (obligatoria), el namespace
# ATOM para localizar las <entry> y el EXTRACTOR de campos CODICE (único punto de
# extracción del proyecto). Nada de esto se duplica aquí.
from feeds import CABECERAS, ATOM_NS, FEEDS, descarga_entradas, extrae_entrada, normaliza_cif

# Acentos y "ñ" correctos en la consola de Windows.
sys.stdout.reconfigure(encoding="utf-8")

# --- Constantes -------------------------------------------------------------
SUPABASE_URL = "https://uzktrhpgkyctlnqgdsys.supabase.co"
TABLA = "licitaciones"
TABLA_ADJ = "adjudicaciones"                # competencia por CIF (una fila por lote/adjudicatario)
# Clave de idempotencia del upsert de adjudicaciones: las columnas PLANAS del UNIQUE
# de adjudicaciones_schema.sql (lote/cif son NOT NULL DEFAULT '' justo para esto).
ADJ_ON_CONFLICT = "licitacion_id,lote,cif_adjudicatario"

# --- FASE D2 · AUTO-MARCAR GANADAS por CIF ----------------------------------
# ÚNICO SITIO documentado con los CIF de LODEPA. Preparado para crecer (otras razones
# sociales o UTEs con CIF propio): añade más strings a la lista y listo. Se normalizan
# con el MISMO normaliza_cif del extractor (mayúsculas, sin espacios/./-/) para que
# casen con los cif_adjudicatario ya normalizados de public.adjudicaciones.
#   · B86833753 = Logística y Desarrollo para la Protección Ambiental SL (LODEPA).
CIFS_LODEPA = [c for c in (normaliza_cif(x) for x in [
    "B86833753",
]) if c]
# RPC SECURITY DEFINER que hace el cruce en Supabase (automarcar_ganadas.sql). El
# pipeline solo la INVOCA: la regla del cruce y la escritura en decisiones viven ahí.
RPC_AUTOMARCAR = "automarcar_ganadas_lodepa"

# --- FASE E · COMPETENCIA ---------------------------------------------------
# RPC SECURITY DEFINER que reconstruye public.competidores (agregado por CIF) desde
# public.adjudicaciones (competidores_schema.sql). El pipeline solo la INVOCA tras
# upsertar adjudicaciones; el rebuild completo se midió en ~3.5 s.
RPC_REFRESCAR_COMPETIDORES = "refrescar_competidores"
LIMITE_GRATIS_MB = 500          # plan gratuito de Supabase (~500 MB de base de datos)
BASE = "https://contrataciondelsectorpublico.gob.es/sindicacion"

# Las dos fuentes: nº de sindicación y nombre base del fichero de Datos Abiertos.
# Los ZIP anuales se llaman {fichero}_{AAAA}.zip (también hay mensuales _{AAAAMM}.zip).
FUENTES = {
    "estatal":   {"sind": "643",  "fichero": "licitacionesPerfilesContratanteCompleto3"},
    "agregadas": {"sind": "1044", "fichero": "PlataformasAgregadasSinMenores"},
}

CACHE_DIR = Path("cache_backfill")          # ZIP descargados (reutilizables / reanudables)
ESTADO_LOAD = Path("backfill_estado.json")  # checkpoint de carga: .atom ya subidos

# Momento de esta ejecución (UTC, ISO) para ultima_vez en los upserts.
AHORA_ISO = datetime.now(timezone.utc).isoformat()


def url_zip(fuente, periodo):
    """URL del ZIP de una fuente para un periodo ('2025' anual, '202506' mensual)."""
    info = FUENTES[fuente]
    return f"{BASE}/sindicacion_{info['sind']}/{info['fichero']}_{periodo}.zip"


# --- Descarga (streaming, reanudable a nivel de fichero) --------------------
def descarga_zip(url, destino):
    """Descarga 'url' a 'destino' por streaming. Reanudable: si el destino ya existe
    (terminado, no .part), no lo vuelve a bajar. Como el servidor va en 'chunked'
    sin Content-Length, el progreso se muestra solo por bytes acumulados."""
    if destino.exists():
        print(f"  caché: {destino.name} ya descargado ({destino.stat().st_size/1e6:,.0f} MB), lo reutilizo.")
        return destino
    parcial = destino.with_name(destino.name + ".part")
    print(f"  descargando {url}")
    with requests.get(url, headers=CABECERAS, stream=True, timeout=120) as r:
        r.raise_for_status()
        bajados = 0
        hito = 25 << 20            # avisar cada ~25 MB
        siguiente = hito
        t0 = time.time()
        with open(parcial, "wb") as f:
            for trozo in r.iter_content(chunk_size=1 << 20):
                if not trozo:
                    continue
                f.write(trozo)
                bajados += len(trozo)
                if bajados >= siguiente:
                    mb = bajados / 1e6
                    vel = mb / max(time.time() - t0, 0.1)
                    print(f"    {mb:,.0f} MB ({vel:,.1f} MB/s)")
                    siguiente += hito
    parcial.rename(destino)
    print(f"  OK: {destino.name} ({destino.stat().st_size/1e6:,.0f} MB)")
    return destino


# --- Recorrido de .atom dentro del ZIP (incl. ZIP anidados) -----------------
def itera_atoms(zip_path):
    """Genera (nombre, bytes) de cada .atom del ZIP. Entra también en ZIP anidados
    (algunos anuales contienen mensuales). Memoria acotada: procesa un miembro cada
    vez; los ZIP anidados se vuelcan a un temporal en disco, no a RAM."""
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            low = info.filename.lower()
            if low.endswith(".atom"):
                yield info.filename, z.read(info)
            elif low.endswith(".zip"):
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tf:
                    tf.write(z.read(info))
                    tmp = tf.name
                try:
                    yield from itera_atoms(tmp)
                finally:
                    os.unlink(tmp)


def entradas_de(blob):
    """Parsea un .atom (bytes) y devuelve la lista de elementos <atom:entry> (lxml)."""
    raiz = etree.fromstring(blob)
    return raiz.findall("atom:entry", ATOM_NS)


def anio_de(reg):
    """Año de la licitación, para agrupar: usa fecha_publicacion; si falta, el fin de
    plazo; si falta, la de actualización. 's/f' si no hay ninguna fecha utilizable."""
    for campo in ("fecha_publicacion", "fecha_fin_plazo", "fecha_actualizacion"):
        v = reg.get(campo)
        if v and len(v) >= 4 and v[:4].isdigit():
            return v[:4]
    return "s/f"


# --- Mapeo a las columnas de public.licitaciones ----------------------------
def fila_para_tabla(reg):
    """Mapea el dict del extractor a las columnas de public.licitaciones.

    - lugar_ejecucion y ccaa NO se incluyen: van null. El extractor aún NO los saca
      (queda como iteración posterior; el código NUTS sí está en reg['region_codigo']
      pero el mapeo a CCAA/lugar se hará cuando se aborde esa mejora).
    - tsv lo calcula el trigger de la tabla (no se envía).
    - primera_vez NO se envía: en INSERT toma el default now(); en UPDATE queda intacto.
    - ultima_vez = ahora (se actualiza siempre).
    """
    return {
        "licitacion_id": reg["id"],
        "titulo": reg["titulo"],
        "objeto": reg["objeto"],
        "num_expediente": reg["num_expediente"],
        "organo_contratacion": reg["organismo"],
        "cpv": reg["cpv"],
        "fuente": reg["fuente"],
        "presupuesto_con_iva": reg["presupuesto_con_iva"],
        "presupuesto_sin_iva": reg["presupuesto_sin_iva"],
        "valor_estimado": reg["valor_estimado"],
        "fecha_publicacion": reg["fecha_publicacion"],
        "fecha_fin_plazo": reg["fecha_fin_plazo"],
        "enlace": reg["enlace"],
        "ultima_vez": AHORA_ISO,
    }


def _blen(x):
    """Bytes UTF-8 de un texto (0 si es None)."""
    return len(x.encode("utf-8")) if x else 0


def estima_bytes_fila(reg):
    """Estimación APROXIMADA del tamaño en disco de una fila en Postgres, devuelta
    descompuesta para poder sumar tabla e índices por separado. No es exacta (la
    contabilidad real de Postgres depende de TOAST, alineación, fillfactor...), pero
    sirve para saber el ORDEN DE MAGNITUD frente al límite de 500 MB."""
    b_text = _blen(reg["titulo"]) + _blen(reg["objeto"]) + _blen(reg["organismo"]) + _blen(reg["enlace"])
    b_cpv = sum(_blen(c) + 4 for c in reg["cpv"])         # texto[] con algo de overhead
    b_busca = _blen(reg["titulo"]) + _blen(reg["objeto"]) + _blen(reg["organismo"])  # base del tsv
    fijos = 28 + 8 * 5                                    # cabecera de fila + ~5 numéricos/fechas
    fila_tabla = b_text + b_cpv + b_busca + fijos         # incluye la columna tsv
    return fila_tabla, b_busca, b_cpv


# --- DRY-RUN ----------------------------------------------------------------
def dry_run(anios, fuentes):
    print("=" * 78)
    print("DRY-RUN (no se escribe nada en Supabase)")
    print(f"Años: {anios}  ·  Fuentes: {fuentes}")
    print("=" * 78)

    vistos = set()                 # ids ya contados (dedup global)
    por_fuente = Counter()
    por_fuente_anio = Counter()
    sum_tabla = 0
    sum_idx_tsv = 0
    sum_idx_cpv = 0
    fmin = fmax = None
    n_leidas = 0                   # entradas leídas (con repeticiones)
    n_ficheros = 0

    for fuente in fuentes:
        for anio in anios:
            destino = CACHE_DIR / f"{fuente}_{anio}.zip"
            try:
                descarga_zip(url_zip(fuente, str(anio)), destino)
            except Exception as e:
                print(f"  AVISO: no pude descargar {fuente} {anio} ({e}); salto.")
                continue
            print(f"  parseando {destino.name} ...")
            for nombre, blob in itera_atoms(destino):
                n_ficheros += 1
                try:
                    entradas = entradas_de(blob)
                except Exception as e:
                    print(f"    AVISO: .atom ilegible {nombre} ({e}); salto.")
                    continue
                for entrada in entradas:
                    reg = extrae_entrada(entrada, fuente)
                    n_leidas += 1
                    rid = reg["id"]
                    if rid in vistos:
                        continue
                    vistos.add(rid)
                    por_fuente[fuente] += 1
                    por_fuente_anio[(fuente, anio_de(reg))] += 1
                    ft, it, ic = estima_bytes_fila(reg)
                    sum_tabla += ft
                    sum_idx_tsv += it
                    sum_idx_cpv += ic
                    fp = (reg["fecha_publicacion"] or "")[:10]
                    if len(fp) == 10:
                        fmin = fp if (fmin is None or fp < fmin) else fmin
                        fmax = fp if (fmax is None or fp > fmax) else fmax
                if n_ficheros % 200 == 0:
                    print(f"    ... {n_ficheros:,} ficheros · {len(vistos):,} únicas · {n_leidas:,} leídas")

    _informe(vistos, por_fuente, por_fuente_anio, sum_tabla, sum_idx_tsv, sum_idx_cpv,
             fmin, fmax, n_leidas)


def _informe(vistos, por_fuente, por_fuente_anio, sum_tabla, sum_idx_tsv, sum_idx_cpv,
             fmin, fmax, n_leidas):
    n = len(vistos)
    print()
    print("=" * 78)
    print("RESULTADO DEL DRY-RUN")
    print("=" * 78)
    print(f"Entradas leídas (con repeticiones entre snapshots): {n_leidas:,}")
    print(f"Licitaciones ÚNICAS (por licitacion_id):           {n:,}")
    print()
    print("Únicas por fuente:")
    for f, c in sorted(por_fuente.items()):
        print(f"  · {f:10} {c:,}")
    print()
    print("Únicas por fuente y año (año de fecha_publicacion, con fallbacks):")
    for (f, a), c in sorted(por_fuente_anio.items()):
        print(f"  · {f:10} {a}: {c:,}")
    print()
    print(f"Rango de fecha_publicacion: {fmin or '?'}  →  {fmax or '?'}")
    print()

    if n == 0:
        print("Sin filas: no hay nada que estimar.")
        return

    # --- Estimación de tamaño en disco (APROXIMADA) -------------------------
    tabla_mb = sum_tabla / 1e6
    idx_tsv_mb = sum_idx_tsv * 1.0 / 1e6        # GIN(tsv) ~ del orden del texto indexado
    idx_cpv_mb = sum_idx_cpv * 1.2 / 1e6        # GIN(cpv)
    idx_btree_mb = n * 3 * 16 / 1e6             # btrees(fecha_fin_plazo, fuente, valor_estimado)
    indices_mb = idx_tsv_mb + idx_cpv_mb + idx_btree_mb
    total_mb = tabla_mb + indices_mb
    media_kb = sum_tabla / n / 1024

    print("Estimación de tamaño en disco (APROXIMADA, orden de magnitud):")
    print(f"  · Tabla (datos + columna tsv):     {tabla_mb:8,.1f} MB   (~{media_kb:.2f} KB/fila)")
    print(f"  · Índice GIN(tsv):                 {idx_tsv_mb:8,.1f} MB")
    print(f"  · Índice GIN(cpv):                 {idx_cpv_mb:8,.1f} MB")
    print(f"  · Índices btree (3):               {idx_btree_mb:8,.1f} MB")
    print(f"  · TOTAL estimado:                  {total_mb:8,.1f} MB")
    print()
    margen = LIMITE_GRATIS_MB - total_mb
    if total_mb <= LIMITE_GRATIS_MB:
        print(f"  ✔ Entra en el plan gratuito (~{LIMITE_GRATIS_MB} MB): margen ≈ {margen:,.0f} MB.")
    else:
        print(f"  ✘ NO entra en el plan gratuito (~{LIMITE_GRATIS_MB} MB): se pasa ≈ {-margen:,.0f} MB.")
    print()
    print("PARADA. Revisa estos números. Si das el OK, la carga es:")
    print('  $env:SUPABASE_SERVICE_ROLE = "...";  python backfill_catalogo.py --cargar')


# --- CARGA (upsert idempotente, en STREAMING y reanudable) ------------------
def _carga_estado():
    """Checkpoint de carga. Devuelve (unidades_ok, atoms_ok):
      - unidades_ok: claves 'fuente_periodo' de ZIP ya procesados POR COMPLETO
        (en una reanudación NO se vuelven a descargar).
      - atoms_ok: claves 'fuente_periodo::nombre.atom' ya subidos, para reanudar a
        media unidad si el job se cortó. Al cerrar una unidad sus .atom se podan."""
    if ESTADO_LOAD.exists():
        d = json.loads(ESTADO_LOAD.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return set(d.get("unidades", [])), set(d.get("atoms", []))
    return set(), set()


def _guarda_estado(unidades_ok, atoms_ok):
    ESTADO_LOAD.write_text(
        json.dumps({"unidades": sorted(unidades_ok), "atoms": sorted(atoms_ok)}, ensure_ascii=False),
        encoding="utf-8",
    )


def construir_unidades(solo=None, periodos=None):
    """Lista ordenada de (fuente, periodo) a procesar. POR DEFECTO, la ventana de
    2 años: años previos COMPLETOS por ZIP ANUAL (p.ej. '2024','2025') + año en
    curso por ficheros MENSUALES ('202601'..mes actual). Los tokens de 'periodos'
    (4 dígitos = anual, 6 = mensual) y 'solo' permiten TROCEAR por año/fuente."""
    fuentes = [solo] if solo else list(FUENTES)
    if periodos:
        usar = list(periodos)
    else:
        # Orden de MÁS NUEVO a MÁS ANTIGUO: meses del año en curso (descendente) y
        # luego los dos años previos anuales. Procesar así, con dedup por ejecución,
        # deja en la tabla el snapshot MÁS RECIENTE de cada licitación.
        hoy = datetime.now()
        a = hoy.year
        usar = [f"{a}{m:02d}" for m in range(hoy.month, 0, -1)] + [str(a - 1), str(a - 2)]
    return [(f, p) for f in fuentes for p in usar]


def _con_reintentos(hacer_peticion, descripcion, reintentos=4):
    """Ejecuta hacer_peticion() (un callable que devuelve una requests.Response) con
    REINTENTOS y backoff exponencial. Reintenta ante 429/5xx y errores de red; lanza de
    inmediato ante un 4xx de datos (≠429), y SystemExit al agotar los intentos. Devuelve
    la respuesta 2xx. Lo comparten el upsert (_sube_lote) y el DELETE de adjudicaciones
    (_borra_adjudicaciones): la ruta destructiva debe ser tan resiliente como la de subida."""
    ultimo = None
    for intento in range(1, reintentos + 1):
        try:
            r = hacer_peticion()
            if r.status_code in (200, 201, 204):
                return r
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            ultimo = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            ultimo = e
        espera = 2 ** intento
        print(f"      reintento {intento}/{reintentos} en {espera}s ({descripcion}: {ultimo})")
        time.sleep(espera)
    raise SystemExit(f"Fallo en {descripcion} tras {reintentos} intentos: {ultimo}")


def _sube_lote(sesion, url, headers, filas, reintentos=4):
    """Sube un lote (lista de filas, sin licitacion_id repetido) con reintentos y
    backoff. Lanza si agota los reintentos o ante un 4xx de datos (que no sea 429)."""
    cuerpo = json.dumps(filas, ensure_ascii=False).encode("utf-8")
    _con_reintentos(lambda: sesion.post(url, headers=headers, data=cuerpo, timeout=120),
                    "subir un lote", reintentos)


def _preparar_upsert():
    """Prepara la sesión de upsert: lee la service_role y la URL de Supabase de las
    variables de entorno (NUNCA del repo) y devuelve (sesion, headers, endpoint,
    url_base) listos para POST con merge-duplicates (upsert por licitacion_id)."""
    token = os.environ.get("SUPABASE_SERVICE_ROLE")
    if not token:
        sys.exit("ERROR: falta la service_role. Defínela en SUPABASE_SERVICE_ROLE "
                 "(NUNCA en el repo). El dry-run/muestra no la necesitan.")
    url_base = os.environ.get("SUPABASE_URL") or SUPABASE_URL
    sesion = requests.Session()
    headers = {
        "apikey": token,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # merge-duplicates = upsert ON CONFLICT (licitacion_id) DO UPDATE de las
        # columnas enviadas; return=minimal para no traernos las filas de vuelta.
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    endpoint = f"{url_base}/rest/v1/{TABLA}?on_conflict=licitacion_id"
    return sesion, headers, endpoint, url_base


# --- ADJUDICACIONES (competencia por CIF) — upsert a public.adjudicaciones -----
# ADITIVO al backfill: se apoya en la MISMA sesión/headers de upsert. El extractor
# (feeds.extrae_entrada) ya emite reg["adjudicaciones"] (lista, una por lote/
# adjudicatario). Aquí solo mapeamos a columnas y subimos.
def fila_adjudicacion(licitacion_id, adj):
    """Mapea un dict de feeds.extrae_adjudicaciones (+ su licitacion_id) a las columnas
    de public.adjudicaciones. Aplica el convenio de la tabla: lote/cif None -> ''
    (columnas planas para el on_conflict del upsert). Los demás campos van tal cual
    (None = null). updated_at = ahora."""
    return {
        "licitacion_id": licitacion_id,
        "lote": adj["lote"] or "",
        "resultado_code": adj["resultado_code"],
        "resultado": adj["resultado"],
        "cif_adjudicatario": adj["cif_adjudicatario"] or "",
        "id_scheme": adj["id_scheme"],
        "adjudicatario": adj["adjudicatario"],
        "es_pyme": adj["es_pyme"],
        "importe_sin_iva": adj["importe_sin_iva"],
        "importe_con_iva": adj["importe_con_iva"],
        "n_ofertas": adj["n_ofertas"],
        "fecha_adjudicacion": adj["fecha_adjudicacion"],
        "estado_expediente": adj["estado_expediente"],
        # D1.1 (aditivo, todas nullable): presupuesto de SU lote + sistema de
        # contratación (0..4 crudo: distingue AM de contrato basado en AM) + tipo.
        "presupuesto_lote_sin_iva": adj.get("presupuesto_lote_sin_iva"),
        "sistema_contratacion": adj.get("sistema_contratacion"),
        "tipo_contrato": adj.get("tipo_contrato"),
        "updated_at": AHORA_ISO,
    }


def _dedup_adj(filas):
    """Colapsa filas con la MISMA clave (licitacion_id, lote, cif_adjudicatario): un
    upsert de PostgREST NO permite afectar dos veces la misma fila en ON CONFLICT.
    La última gana. Imprescindible antes de subir un lote."""
    por_clave = {}
    for f in filas:
        por_clave[(f["licitacion_id"], f["lote"], f["cif_adjudicatario"])] = f
    return list(por_clave.values())


def _tabla_adjudicaciones_lista(sesion, url_base, headers, reintentos=3):
    """¿Está creada public.adjudicaciones? Sirve para NO romper la ingesta del catálogo
    si el despliegue del código va por delante de la ejecución de adjudicaciones_schema.sql.

    Distingue el caso REAL de tabla ausente (404/PGRST205 -> devuelve False y salta las
    adjudicaciones, avisando) de un BLIP transitorio (5xx / error de red): ante lo
    transitorio REINTENTA con backoff, y si tras los reintentos sigue sin poder confirmar,
    asume que la tabla SÍ existe y continúa (mejor que saltar en silencio todo un
    re-backfill de horas; si de verdad faltara, el primer upsert fallaría ruidosamente y
    con reintentos). Se re-evalúa en cada ejecución."""
    h = {**headers, "Range": "0-0"}
    url = f"{url_base}/rest/v1/{TABLA_ADJ}?select=id"
    ultimo = None
    for intento in range(1, reintentos + 1):
        try:
            r = sesion.get(url, headers=h, timeout=60)
        except requests.RequestException as e:
            ultimo = e
        else:
            if r.status_code < 400:
                return True
            # 404 / PGRST205 = la tabla no existe: caso esperado, saltar sin reintentar.
            if r.status_code == 404 or "PGRST205" in r.text or "does not exist" in r.text.lower():
                print(f"  AVISO: public.{TABLA_ADJ} no existe todavía (HTTP {r.status_code}). "
                      f"¿Ejecutaste adjudicaciones_schema.sql? SALTO la escritura de "
                      f"adjudicaciones (el catálogo de licitaciones se ingiere con normalidad).")
                return False
            ultimo = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        if intento < reintentos:
            espera = 2 ** intento
            print(f"    sondeo de public.{TABLA_ADJ}: reintento {intento}/{reintentos} "
                  f"en {espera}s ({ultimo})")
            time.sleep(espera)
    print(f"  AVISO: no pude confirmar public.{TABLA_ADJ} ({ultimo}); asumo que existe y "
          f"continúo (los upserts reintentan y fallarían ruidosamente si no estuviera).")
    return True


def _borra_adjudicaciones(sesion, url_base, headers, licitacion_ids, trozo=50):
    """DELETE de las filas de adjudicaciones de esos licitacion_id (en trozos, por el
    límite de longitud de la URL). Se usa SOLO en la ingesta diaria, antes de
    reinsertar: así un expediente re-publicado cuyo adjudicatario cambió REEMPLAZA sus
    filas y no deja huérfanas. Sobre la tabla vacía (primera carga) es un no-op barato.

    in.("url1","url2",...): comillas dobles porque los licitacion_id son URLs largas;
    requests las url-encodea y PostgREST las decodifica (no llevan comas ni comillas)."""
    ids = list(licitacion_ids)
    base = f"{url_base}/rest/v1/{TABLA_ADJ}"
    h = {**headers, "Prefer": "return=minimal"}
    for i in range(0, len(ids), trozo):
        chunk = ids[i:i + trozo]
        expr = "in.(" + ",".join('"' + x.replace('"', '\\"') + '"' for x in chunk) + ")"
        # Mismos reintentos/backoff que el upsert: un 429/5xx transitorio NO debe abortar
        # la ingesta a mitad del delete+insert (dejaría expedientes sin sus filas).
        _con_reintentos(lambda expr=expr: sesion.delete(base, headers=h,
                                                        params={"licitacion_id": expr}, timeout=120),
                        "DELETE adjudicaciones")


def sube_adjudicaciones(sesion, url_base, headers, filas, reemplazar):
    """Sube filas de adjudicaciones y devuelve cuántas subió.
      · reemplazar=False (RE-BACKFILL): upsert idempotente por la clave del PASO 1
        (merge-duplicates). La tabla se puebla con el estado ACTUAL de los ZIP y cada
        licitacion_id se procesa una sola vez por ejecución, así que no hay huérfanas.
      · reemplazar=True (INGESTA DIARIA): delete+insert por licitacion_id, para que un
        expediente re-publicado con el adjudicatario corregido reemplace sus filas.
    Deduplica por la clave antes de subir (ver _dedup_adj). Usa los MISMOS headers de
    upsert (merge-duplicates) que las licitaciones; el endpoint apunta a adjudicaciones."""
    filas = _dedup_adj(filas)
    if not filas:
        return 0
    if reemplazar:
        _borra_adjudicaciones(sesion, url_base, headers, {f["licitacion_id"] for f in filas})
    endpoint = f"{url_base}/rest/v1/{TABLA_ADJ}?on_conflict={ADJ_ON_CONFLICT}"
    for i in range(0, len(filas), 500):
        _sube_lote(sesion, endpoint, headers, filas[i:i + 500])
    return len(filas)


def _resumen_adjudicaciones(sesion, url_base, headers):
    """count(*) total de adjudicaciones vía REST + el SQL exacto para los distintos
    (licitacion_id con adjudicación y CIF distintos), que la API REST no calcula."""
    base = f"{url_base}/rest/v1/{TABLA_ADJ}"
    total = _cuenta(sesion, base, headers, "?select=id")
    print("-" * 78)
    print("ADJUDICACIONES (competencia por CIF)")
    print(f"  filas totales en public.adjudicaciones: "
          + (f"{total:,}" if total is not None else "(no disponible)"))
    print("  Distintos (la API REST no hace count(distinct); míralo en el SQL Editor):")
    print("    select count(*) filas,")
    print("           count(distinct licitacion_id) expedientes_con_adjudicacion,")
    print("           count(distinct cif_adjudicatario) filter (where cif_adjudicatario <> '') cif_distintos,")
    print("           count(*) filter (where cif_adjudicatario =  '') lotes_desiertos")
    print("      from public.adjudicaciones;")


def _informe_automarcar(res):
    """Imprime, legible, el jsonb que devuelve la RPC automarcar_ganadas_lodepa."""
    modo = "SIMULACIÓN (no escribe)" if res.get("simular") else "APLICADO"
    print("-" * 78)
    print(f"AUTO-MARCAR GANADAS (LODEPA) · {modo}")
    marcadas = res.get("marcadas") or []
    print(f"  marcadas como 'ganada': {res.get('n_marcadas', 0)}"
          f"   ·   ya estaban ganadas: {res.get('ya_ganadas', 0)}")
    for m in marcadas:
        imp = m.get("importe_sin_iva")
        imp_txt = f"{float(imp):,.0f} €" if imp is not None else "—"
        print(f"    [{m.get('accion')}] {m.get('adjudicatario') or '—'}  ·  {imp_txt}"
              f"  ·  (antes: {m.get('estado_anterior') if m.get('estado_anterior') is not None else 'sin fila'})")
        print(f"        {m.get('licitacion_id')}")
    disc = res.get("discrepancias") or []
    if disc:
        print(f"  DISCREPANCIAS (estado manual distinto; NO se tocan, revísalas): {len(disc)}")
        for d in disc:
            print(f"    · estado actual '{d.get('estado_actual')}'  ·  {d.get('adjudicatario') or '—'}")
            print(f"        {d.get('licitacion_id')}")
    else:
        print("  discrepancias: 0")


def automarcar_ganadas(sesion, url_base, headers, simular=False, reintentos=4):
    """Fase D2: cruza public.adjudicaciones (CIF de LODEPA) → public.decisiones vía la
    RPC SECURITY DEFINER automarcar_ganadas_lodepa. NO escribe decisiones desde aquí:
    la regla y la escritura viven en la función SQL (idempotente). Devuelve el dict del
    informe, o None si se salta (sin CIF configurados, RPC no desplegada o fallo).

    NO es fatal: corre al FINAL de la ingesta, así que un blip suyo no debe tirar un
    día entero ya ingerido. Distingue RPC-ausente (404/PGRST202 -> avisa y salta) de un
    5xx/red transitorio (reintenta con backoff)."""
    if not CIFS_LODEPA:
        print("  AUTO-MARCAR: sin CIF de LODEPA configurados (CIFS_LODEPA vacío); salto.")
        return None
    url = f"{url_base}/rest/v1/rpc/{RPC_AUTOMARCAR}"
    # Prefer sin merge-duplicates: es una llamada a función, no un upsert de tabla.
    h = {k: v for k, v in headers.items() if k != "Prefer"}
    cuerpo = {"cifs": CIFS_LODEPA, "simular": bool(simular)}
    ultimo = None
    for intento in range(1, reintentos + 1):
        try:
            r = sesion.post(url, headers=h, json=cuerpo, timeout=120)
        except requests.RequestException as e:
            ultimo = e
        else:
            if r.status_code == 200:
                try:
                    res = r.json()
                except ValueError:
                    print(f"  AVISO: respuesta inesperada de {RPC_AUTOMARCAR}: {r.text[:200]!r}; salto.")
                    return None
                _informe_automarcar(res)
                return res
            # RPC no desplegada todavía: caso esperado (orden de despliegue). Salta.
            if r.status_code == 404 or "PGRST202" in r.text or "Could not find the function" in r.text:
                print(f"  AVISO: la RPC public.{RPC_AUTOMARCAR} no existe todavía (HTTP {r.status_code}). "
                      f"¿Ejecutaste automarcar_ganadas.sql? SALTO el auto-marcado "
                      f"(la ingesta de licitaciones/adjudicaciones ya se hizo con normalidad).")
                return None
            # 4xx de datos (≠404): no reintentar, pero NO tumbar la ingesta ya hecha.
            if 400 <= r.status_code < 500 and r.status_code != 429:
                print(f"  AVISO: {RPC_AUTOMARCAR} devolvió HTTP {r.status_code}: {r.text[:300]}; salto.")
                return None
            ultimo = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        if intento < reintentos:
            espera = 2 ** intento
            print(f"    auto-marcar: reintento {intento}/{reintentos} en {espera}s ({ultimo})")
            time.sleep(espera)
    print(f"  AVISO: no pude ejecutar {RPC_AUTOMARCAR} ({ultimo}); salto el auto-marcado "
          f"(se reintentará en la próxima ingesta; el cruce es idempotente).")
    return None


def automarcar_oneshot(simular=False):
    """Fase D2 · pasada ÚNICA de auto-marcado sobre TODO lo ya backfilleado, SIN
    reingerir nada (segundos, no horas). Para la primera pasada y para previsualizar:
      python backfill_catalogo.py --automarcar --simular   # dime cuántas marcaría
      python backfill_catalogo.py --automarcar             # aplícalo
    El cruce diario ya corre solo dentro de --diario; esto es el disparo manual."""
    sesion, headers, _endpoint, url_base = _preparar_upsert()
    print("=" * 78)
    print("AUTO-MARCAR GANADAS (Fase D2) — pasada única (no reingiere el catálogo)")
    print(f"CIF de LODEPA: {CIFS_LODEPA}  ·  {'SIMULAR (no escribe)' if simular else 'APLICAR'}")
    print("=" * 78)
    res = automarcar_ganadas(sesion, url_base, headers, simular=simular)
    if res is None:
        # RPC no desplegada o fallo (ya avisado): sal con código ≠0 para que se note en
        # un disparo manual/CI (a diferencia de la ingesta, aquí ES el objetivo).
        sys.exit("Auto-marcado no ejecutado (ver AVISO arriba).")


def refrescar_competidores(sesion, url_base, headers, reintentos=4):
    """Fase E: reconstruye public.competidores (agregado por CIF) invocando la RPC
    refrescar_competidores (competidores_schema.sql). Se llama tras upsertar
    adjudicaciones. NO es fatal (corre al final): distingue RPC-ausente (404/PGRST202
    -> avisa y salta) de un 5xx/red transitorio (reintenta). Devuelve el nº de CIF, o
    None si se salta."""
    url = f"{url_base}/rest/v1/rpc/{RPC_REFRESCAR_COMPETIDORES}"
    h = {k: v for k, v in headers.items() if k != "Prefer"}
    ultimo = None
    for intento in range(1, reintentos + 1):
        try:
            r = sesion.post(url, headers=h, json={}, timeout=120)
        except requests.RequestException as e:
            ultimo = e
        else:
            if r.status_code == 200:
                try:
                    n = int(r.text.strip())
                except (ValueError, AttributeError):
                    n = None
                print(f"  Competidores (Fase E) refrescados: "
                      + (f"{n:,} CIF" if n is not None else "(ok)"))
                return n
            if r.status_code == 404 or "PGRST202" in r.text or "Could not find the function" in r.text:
                print(f"  AVISO: la RPC public.{RPC_REFRESCAR_COMPETIDORES} no existe todavía "
                      f"(HTTP {r.status_code}). ¿Ejecutaste competidores_schema.sql? SALTO el "
                      f"refresco de competidores (el resto de la ingesta va con normalidad).")
                return None
            if 400 <= r.status_code < 500 and r.status_code != 429:
                print(f"  AVISO: {RPC_REFRESCAR_COMPETIDORES} devolvió HTTP {r.status_code}: "
                      f"{r.text[:300]}; salto.")
                return None
            ultimo = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        if intento < reintentos:
            espera = 2 ** intento
            print(f"    refrescar competidores: reintento {intento}/{reintentos} en {espera}s ({ultimo})")
            time.sleep(espera)
    print(f"  AVISO: no pude refrescar competidores ({ultimo}); salto (se reintenta en la "
          f"próxima ingesta; es un rebuild idempotente).")
    return None


def refrescar_competidores_oneshot():
    """Fase E · refresco manual de public.competidores (sin reingerir). Para la primera
    carga o forzar un rebuild:  python backfill_catalogo.py --refrescar-competidores"""
    sesion, headers, _endpoint, url_base = _preparar_upsert()
    print("=" * 78)
    print("REFRESCAR COMPETIDORES (Fase E) — rebuild del agregado por CIF")
    print("=" * 78)
    if refrescar_competidores(sesion, url_base, headers) is None:
        sys.exit("Refresco de competidores no ejecutado (ver AVISO arriba).")


def carga(unidades, limpiar=True):
    """Procesa las unidades (fuente, periodo) en STREAMING: descarga un ZIP →
    parsea sus .atom → upsert → libera el ZIP antes del siguiente, para no llenar
    el disco del runner. Reanudable por checkpoint (unidad y .atom)."""
    sesion, headers, endpoint, url_base = _preparar_upsert()
    unidades_ok, atoms_ok = _carga_estado()

    print("=" * 78)
    print("CARGA a Supabase (upsert idempotente por licitacion_id, en streaming)")
    print(f"Unidades: {len(unidades)}  ·  ya completas: {len(unidades_ok)}  ·  "
          f"borrar ZIP tras procesar: {limpiar}")
    print("=" * 78)

    # Dedup por EJECUCIÓN: como las unidades van de más nueva a más antigua, la
    # primera vez que vemos un licitacion_id es su snapshot más reciente; las
    # reapariciones posteriores (re-publicaciones antiguas) se saltan. Recorta los
    # upserts redundantes (~2-3×) sin cambiar el resultado en las columnas que
    # guardamos (estables; el estado, que no guardamos, es lo que varía).
    vistos_run = set()
    # ¿Está creada public.adjudicaciones? Si no, se ingiere el catálogo igual y solo
    # se salta la parte de adjudicaciones (defensa para el orden de despliegue).
    escribir_adj = _tabla_adjudicaciones_lista(sesion, url_base, headers)

    total = 0
    total_adj = 0
    for fuente, periodo in unidades:
        clave_u = f"{fuente}_{periodo}"
        if clave_u in unidades_ok:
            print(f"  {clave_u}: ya completo (checkpoint), lo salto.")
            continue
        destino = CACHE_DIR / f"{clave_u}.zip"
        try:
            descarga_zip(url_zip(fuente, periodo), destino)
        except Exception as e:
            print(f"  AVISO: no pude descargar {clave_u} ({e}); salto.")
            continue

        filas_unidad = 0
        for nombre, blob in itera_atoms(destino):
            clave_a = f"{clave_u}::{nombre}"
            if clave_a in atoms_ok:
                continue
            try:
                entradas = entradas_de(blob)
            except Exception as e:
                print(f"    AVISO: .atom ilegible {nombre} ({e}); salto.")
                continue
            # Dedup DENTRO del .atom (un mismo licitacion_id no puede ir dos veces en
            # el mismo lote: Postgres no deja afectar dos veces la misma fila en ON
            # CONFLICT) Y entre unidades de esta ejecución (vistos_run).
            por_id = {}
            adj_por_id = {}
            for entrada in entradas:
                reg = extrae_entrada(entrada, fuente)
                rid = reg["id"]
                if rid in vistos_run:
                    continue
                por_id[rid] = fila_para_tabla(reg)
                # Adjudicaciones del snapshot más reciente de este expediente (las de
                # reapariciones antiguas se saltan con el mismo dedup por licitacion_id).
                if reg["adjudicaciones"]:
                    adj_por_id[rid] = [fila_adjudicacion(rid, a) for a in reg["adjudicaciones"]]
            filas = list(por_id.values())
            for i in range(0, len(filas), 500):
                lote = filas[i:i + 500]
                _sube_lote(sesion, endpoint, headers, lote)
                total += len(lote)
                filas_unidad += len(lote)
            # Adjudicaciones: upsert idempotente (sin borrar en el re-backfill; ver
            # sube_adjudicaciones). Aditivo al upsert del catálogo, no lo cambia.
            if escribir_adj:
                adj_filas = [r for filas_id in adj_por_id.values() for r in filas_id]
                total_adj += sube_adjudicaciones(sesion, url_base, headers, adj_filas, reemplazar=False)
            vistos_run.update(por_id.keys())
            atoms_ok.add(clave_a)
            _guarda_estado(unidades_ok, atoms_ok)

        # Unidad terminada: la marcamos completa, podamos sus .atom del checkpoint (ya
        # no hacen falta) y liberamos el ZIP para no llenar el disco del runner.
        unidades_ok.add(clave_u)
        atoms_ok = {a for a in atoms_ok if not a.startswith(clave_u + "::")}
        _guarda_estado(unidades_ok, atoms_ok)
        print(f"  {clave_u}: COMPLETO · {filas_unidad:,} filas (acumulado {total:,})")
        if limpiar:
            for p in (destino, destino.with_name(destino.name + ".part")):
                try:
                    p.unlink()
                except OSError:
                    pass

    print("=" * 78)
    print(f"Carga terminada. Filas upsertadas (con repeticiones entre .atom): {total:,}")
    print(f"Filas de adjudicaciones upsertadas: {total_adj:,}")
    _resumen_post_carga(sesion, url_base, headers)
    if escribir_adj:
        _resumen_adjudicaciones(sesion, url_base, headers)
        # Fase D2: tras poblar adjudicaciones, cruza los CIF de LODEPA -> decisiones.
        # Idempotente y O(un puñado) por el índice del CIF; re-lanzar el backfill no
        # duplica marcas. Aditivo: no toca lo ya ingerido si la RPC no está desplegada.
        automarcar_ganadas(sesion, url_base, headers)
        # Fase E: reconstruye el agregado por CIF (public.competidores) con lo recién
        # backfilleado. Rebuild completo (~3.5 s); salta con aviso si la RPC no existe.
        refrescar_competidores(sesion, url_base, headers)


def _cuenta(sesion, base_tabla, headers, filtro="?select=licitacion_id"):
    """count(*) exacto vía PostgREST (cabecera Content-Range), opcionalmente filtrado."""
    h = {**headers, "Prefer": "count=exact", "Range": "0-0"}
    try:
        r = sesion.get(f"{base_tabla}{filtro}", headers=h, timeout=120)
        cr = r.headers.get("Content-Range", "")
        if "/" in cr and cr.split("/")[-1].isdigit():
            return int(cr.split("/")[-1])
    except requests.RequestException:
        pass
    return None


def _resumen_post_carga(sesion, url_base, headers):
    """Reporta count(*) total y por fuente/año vía REST. El tamaño físico de la tabla
    no lo expone la API REST: se imprime el SQL para mirarlo en el editor de Supabase."""
    base_tabla = f"{url_base}/rest/v1/{TABLA}"
    anios = (datetime.now().year - 2, datetime.now().year - 1, datetime.now().year)
    print("=" * 78)
    print("RESUMEN EN SUPABASE")
    print("=" * 78)
    total = _cuenta(sesion, base_tabla, headers)
    print(f"  count(*) total: {total:,}" if total is not None else "  count(*) total: (no disponible)")
    for fuente in FUENTES:
        c = _cuenta(sesion, base_tabla, headers, f"?select=licitacion_id&fuente=eq.{fuente}")
        print(f"  {fuente}: {c:,}" if c is not None else f"  {fuente}: (no disponible)")
        suma = 0
        for y in anios:
            cy = _cuenta(sesion, base_tabla, headers,
                         f"?select=licitacion_id&fuente=eq.{fuente}"
                         f"&fecha_publicacion=gte.{y}-01-01&fecha_publicacion=lt.{y + 1}-01-01")
            if cy is not None:
                suma += cy
                print(f"     {y}: {cy:,}")
        if c is not None:
            print(f"     (otros años / sin fecha): {c - suma:,}")
    print()
    print("Tamaño REAL de la tabla — la API REST no expone el tamaño físico; ejecútalo")
    print("en el SQL Editor de Supabase:")
    print("  select pg_size_pretty(pg_total_relation_size('public.licitaciones'));")


# --- INGESTA DIARIA (incremental, desde el feed EN VIVO) --------------------
def diario(fuentes):
    """Ingesta incremental DIARIA para el cron: lee los feeds EN VIVO (estatal +
    agregadas) con la MISMA descarga del radar (feeds.descarga_entradas) y hace
    UPSERT en el catálogo de TODAS las licitaciones (no solo las de interés),
    conservando primera_vez y actualizando ultima_vez (el tsv lo pone el trigger).
    Reutiliza el extractor feeds.extrae_entrada y la maquinaria de upsert."""
    sesion, headers, endpoint, url_base = _preparar_upsert()
    print("=" * 78)
    print("INGESTA DIARIA del catálogo (upsert de TODO el feed en vivo)")
    print(f"Fuentes: {fuentes}")
    print("=" * 78)

    total = 0
    total_adj = 0
    escribir_adj = _tabla_adjudicaciones_lista(sesion, url_base, headers)
    vistos = set()   # dedup por ejecución: el feed trae cada licitación en varias páginas
    for feed in FEEDS:
        if feed["fuente"] not in fuentes:
            continue
        entradas, paginas, tope = descarga_entradas(feed["url"])
        aviso = "  [TOPE de páginas: puede faltar histórico reciente]" if tope else ""
        print(f"feed «{feed['fuente']}»: {len(entradas):,} entradas en {paginas} pág{aviso}")
        # La primera aparición de un id es su snapshot más reciente (el feed va de lo
        # más nuevo a lo más viejo): nos quedamos con esa.
        por_id = {}
        adj_por_id = {}
        for entrada in entradas:
            reg = extrae_entrada(entrada, feed["fuente"])
            rid = reg["id"]
            if rid in vistos:
                continue
            por_id[rid] = fila_para_tabla(reg)
            if reg["adjudicaciones"]:
                adj_por_id[rid] = [fila_adjudicacion(rid, a) for a in reg["adjudicaciones"]]
        filas = list(por_id.values())
        for i in range(0, len(filas), 500):
            lote = filas[i:i + 500]
            _sube_lote(sesion, endpoint, headers, lote)
            total += len(lote)
        # Adjudicaciones: delete+insert por licitacion_id (reemplazar=True), para que un
        # expediente re-publicado con el adjudicatario corregido reemplace sus filas.
        n_adj = 0
        if escribir_adj:
            adj_filas = [r for filas_id in adj_por_id.values() for r in filas_id]
            n_adj = sube_adjudicaciones(sesion, url_base, headers, adj_filas, reemplazar=True)
            total_adj += n_adj
        vistos.update(por_id.keys())
        print(f"  upsert «{feed['fuente']}»: {len(filas):,} filas (acumulado {total:,})"
              f"  ·  adjudicaciones: {n_adj:,}")

    print("=" * 78)
    print(f"Ingesta diaria terminada. Filas upsertadas: {total:,}  ·  adjudicaciones: {total_adj:,}")
    _resumen_post_carga(sesion, url_base, headers)
    if escribir_adj:
        _resumen_adjudicaciones(sesion, url_base, headers)
        # Fase D2: mismo cruce idempotente que en la carga. Corre la pasada COMPLETA
        # (no solo lo nuevo): es O(un puñado) por el índice del CIF y así se auto-cura
        # si un día se saltó. Marca en decisiones cualquier nueva GANADA de LODEPA.
        automarcar_ganadas(sesion, url_base, headers)
        # Fase E: reconstruye el agregado por CIF (public.competidores) con las
        # adjudicaciones nuevas del día. Rebuild completo (~3.5 s); salta si no existe.
        refrescar_competidores(sesion, url_base, headers)


# --- PURGA de la ventana (vía RPC en Supabase) ------------------------------
def _rpc_purga(sesion, url, headers, anios, simular, lote):
    """Una llamada a la RPC public.purga_catalogo. Devuelve el entero que responde:
    el TOTAL a borrar si simular, o el nº borrado en ESTA tanda si no."""
    try:
        r = sesion.post(url, headers=headers,
                        json={"anios": anios, "simular": simular, "lote": lote}, timeout=600)
    except requests.RequestException as e:
        sys.exit(f"Purga: error de red ({e}).")
    if r.status_code != 200:
        sys.exit(f"Purga falló: HTTP {r.status_code}: {r.text[:300]}")
    try:
        return int(r.text.strip())
    except ValueError:
        sys.exit(f"Purga: respuesta inesperada de la RPC: {r.text[:120]!r}")


def purgar(anios=3, simular=False, lote=5000):
    """Purga la ventana vía RPC public.purga_catalogo. Borra lo publicado hace más
    de 'anios' años SALVO lo abierto, lo de public.contratos y lo de public.decisiones.
    EN LOTES: cada llamada borra ≤ 'lote' filas (rápida, por debajo del
    statement_timeout de PostgREST) y aquí REPETIMOS hasta agotar. Un único DELETE
    grande daba timeout (57014). simular=True solo CUENTA el total (no borra)."""
    token = os.environ.get("SUPABASE_SERVICE_ROLE")
    if not token:
        sys.exit("ERROR: falta la service_role en SUPABASE_SERVICE_ROLE (NUNCA en el repo).")
    url_base = os.environ.get("SUPABASE_URL") or SUPABASE_URL
    url = f"{url_base}/rest/v1/rpc/purga_catalogo"
    headers = {"apikey": token, "Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    sesion = requests.Session()

    if simular:
        n = _rpc_purga(sesion, url, headers, anios, True, lote)
        print(f"SIMULACIÓN (no borra): {n:,} filas se borrarían (ventana de {anios} años).")
        return

    # NADA de contar por adelantado: un count(*) de las coincidentes (con los dos
    # NOT EXISTS) recorre toda la tabla y EXCEDE el statement_timeout de PostgREST
    # — era justo lo que fallaba ANTES de imprimir nada. Vamos directos al bucle:
    # cada DELETE lleva LIMIT, así que para en cuanto junta 'lote' filas (corto).
    print(f"PURGA EN BUCLE · lotes de {lote:,} · ventana de {anios} años (repito hasta 0) ...")
    total, tanda = 0, 0
    while True:
        tanda += 1
        n = _rpc_purga(sesion, url, headers, anios, False, lote)
        total += n
        print(f"  tanda {tanda}: {n:,} borradas (acumulado {total:,})")
        if n < lote:           # devolvió menos que el lote ⇒ ya no quedan filas
            break
        if tanda > 100000:     # cinturón de seguridad contra un bucle infinito
            print("  AVISO: demasiadas tandas; paro por seguridad.")
            break
    print(f"Purga terminada: {total:,} filas borradas en {tanda} tanda(s).")


# --- MUESTREO (estimación por meses, sin bajar años enteros) ----------------
def meses_recientes(n):
    """Hasta n periodos 'AAAAMM' del AÑO EN CURSO, del más reciente al más antiguo,
    sin cruzar al año anterior (la muestra es del año en curso)."""
    hoy = datetime.now()
    y, m = hoy.year, hoy.month
    out = []
    while len(out) < n and m >= 1:
        out.append(f"{y}{m:02d}")
        m -= 1
    return out


def _media(xs):
    return sum(xs) / len(xs) if xs else 0.0


def muestra(meses_n, fuentes):
    print("=" * 78)
    print("MUESTREO mensual (no se baja ningún año entero; no se escribe nada)")
    periodos = meses_recientes(meses_n)
    print(f"Meses del año en curso: {periodos}  ·  Fuentes: {fuentes}")
    print("=" * 78)

    # sets[fuente][mes] = set de licitacion_id únicos vistos en ese fichero mensual.
    sets = {f: {} for f in fuentes}
    for fuente in fuentes:
        for mes in periodos:
            destino = CACHE_DIR / f"{fuente}_{mes}.zip"
            try:
                descarga_zip(url_zip(fuente, mes), destino)
            except Exception as e:
                print(f"  AVISO: no pude descargar {fuente} {mes} ({e}); salto.")
                continue
            ids = set()
            nfich = 0
            for nombre, blob in itera_atoms(destino):
                nfich += 1
                try:
                    entradas = entradas_de(blob)
                except Exception as e:
                    print(f"    AVISO: .atom ilegible {nombre} ({e}); salto.")
                    continue
                for entrada in entradas:
                    ids.add(extrae_entrada(entrada, fuente)["id"])
            sets[fuente][mes] = ids
            print(f"  {fuente:10} {mes}: {len(ids):,} únicas  ({nfich} .atom)")

    _informe_muestra(sets, fuentes)


def _informe_muestra(sets, fuentes):
    KB_FILA = 1.5                          # tamaño/fila con índices (medido en el smoke test)

    def mb(filas):
        return filas * KB_FILA / 1000      # 1,5 KB/fila ≈ 0,0015 MB/fila

    print()
    print("=" * 78)
    print("ESTIMACIÓN POR MUESTREO")
    print("=" * 78)

    ritmo_naive_anual = 0.0                 # Σ fuentes (media únicas/mes × 12)  — cuenta repeticiones
    ritmo_adj_anual = 0.0                   # Σ fuentes (altas nuevas/mes × 12)  — ajustado por solape

    for fuente in fuentes:
        meses_ord = sorted(sets[fuente])    # cronológico, antiguo → reciente
        if not meses_ord:
            print(f"\nFuente «{fuente}»: sin datos.")
            continue
        counts = [len(sets[fuente][m]) for m in meses_ord]
        media = _media(counts)
        union = set()
        for m in meses_ord:
            union |= sets[fuente][m]
        suma = sum(counts)
        # Altas NUEVAS por mes: ids no vistos en meses anteriores de la muestra.
        acc, incr = set(), []
        for m in meses_ord:
            incr.append(len(sets[fuente][m] - acc))
            acc |= sets[fuente][m]
        # El 1er mes es "todo nuevo" frente a vacío; el ritmo real de altas se mide
        # desde el 2º mes. Con un solo mes, no hay ajuste posible (usamos ese).
        nuevas_mes = _media(incr[1:]) if len(incr) > 1 else incr[0]

        print(f"\nFuente «{fuente}»:")
        print(f"  únicas/mes:        {', '.join(f'{m}:{c:,}' for m, c in zip(meses_ord, counts))}")
        print(f"  media únicas/mes:  {media:,.0f}")
        print(f"  distintas en los {len(meses_ord)} meses (dedup): {len(union):,}   "
              f"(suma {suma:,} → solape {suma - len(union):,})")
        print(f"  altas nuevas/mes (ajustado por solape): {nuevas_mes:,.0f}")

        ritmo_naive_anual += media * 12
        ritmo_adj_anual += nuevas_mes * 12

    print()
    print("-" * 78)
    print("EXTRAPOLACIÓN (ambas fuentes juntas)  ·  tamaño/fila ≈ 1,5 KB con índices")
    print(f"Límite plan free ≈ {LIMITE_GRATIS_MB} MB  →  ~{int(LIMITE_GRATIS_MB / (KB_FILA / 1000)):,} filas")
    print("-" * 78)

    def reporta(filas_anio):
        f1, f2 = filas_anio, filas_anio * 2
        m1, m2 = mb(f1), mb(f2)
        v1 = "ENTRA" if m1 <= LIMITE_GRATIS_MB else "NO entra"
        v2 = "ENTRA" if m2 <= LIMITE_GRATIS_MB else "NO entra"
        print(f"    1 año:  {f1:>10,.0f} filas → {m1:>7,.0f} MB   [{v1}]")
        print(f"    2 años: {f2:>10,.0f} filas → {m2:>7,.0f} MB   [{v2}]")

    print("\n  (A) Por ritmo mensual × 12 / × 24  (lo pedido; cuenta repeticiones):")
    reporta(ritmo_naive_anual)
    print("\n  (B) Ajustado por solape: altas nuevas/mes × 12 / × 24")
    print("      (realista para el nº de filas DISTINTAS que ocupará la tabla):")
    reporta(ritmo_adj_anual)

    print()
    print("NOTA: el feed re-publica licitaciones antiguas cuando cambia su estado, así que")
    print("la misma licitacion_id reaparece mes a mes. Por eso (A) sobreestima las filas")
    print("DISTINTAS (lo que ocupa la tabla); (B) es la cifra a mirar frente al límite.")
    print("\nMUESTREO terminado. No se ha cargado nada.")


# --- CLI --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Backfill del histórico reciente al buscador (BG-2a).")
    ap.add_argument("--cargar", action="store_true",
                    help="Sube a Supabase (upsert). Sin esta opción: dry-run (no escribe).")
    ap.add_argument("--diario", action="store_true",
                    help="Ingesta incremental: upsert de TODO el feed EN VIVO (estatal+agregadas). Para el cron.")
    ap.add_argument("--purgar", action="store_true",
                    help="Purga la ventana (RPC purga_catalogo): borra lo viejo salvo abierto/en contratos.")
    ap.add_argument("--automarcar", action="store_true",
                    help="Fase D2: pasada única de auto-marcado GANADAS (CIF de LODEPA) sin reingerir. "
                         "Con --simular solo dice cuántas marcaría.")
    ap.add_argument("--refrescar-competidores", action="store_true",
                    help="Fase E: reconstruye public.competidores (agregado por CIF) sin reingerir.")
    ap.add_argument("--simular", action="store_true",
                    help="Con --purgar: solo CUENTA lo que se borraría. Con --automarcar: previsualiza (no escribe).")
    ap.add_argument("--ventana-anios", type=int, default=3,
                    help="Años de la ventana para --purgar (por defecto 3).")
    ap.add_argument("--lote", type=int, default=5000,
                    help="Filas por tanda en --purgar (por defecto 5000). El cliente repite hasta agotar.")
    ap.add_argument("--muestra", action="store_true",
                    help="Estimación por muestreo de meses recientes (no baja años; no escribe).")
    ap.add_argument("--meses", type=int, default=3,
                    help="Nº de meses recientes a muestrear (con --muestra; por defecto 3).")
    ap.add_argument("--anios", nargs="+", type=int, default=None,
                    help="Años para el dry-run (por defecto: año anterior y año en curso).")
    ap.add_argument("--periodos", nargs="+", default=None,
                    help="Periodos concretos para --cargar: tokens AAAA (anual) o AAAAMM "
                         "(mensual). Por defecto: ventana de 2 años (2 años previos anuales "
                         "+ año en curso mensual). Sirve para trocear el job.")
    ap.add_argument("--solo", choices=list(FUENTES), default=None,
                    help="Procesar solo una fuente (por defecto: las dos). Sirve para trocear.")
    ap.add_argument("--conservar-zip", action="store_true",
                    help="No borrar los ZIP tras procesarlos (por defecto se borran en --cargar "
                         "para no llenar el disco del runner).")
    ap.add_argument("--cache", default=None,
                    help="Carpeta para los ZIP en caché (por defecto: cache_backfill/).")
    args = ap.parse_args()

    global CACHE_DIR
    if args.cache:
        CACHE_DIR = Path(args.cache)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    anio_actual = datetime.now().year
    anios = args.anios or [anio_actual - 1, anio_actual]
    fuentes = [args.solo] if args.solo else list(FUENTES)

    if args.muestra:
        muestra(args.meses, fuentes)
    elif args.diario:
        diario(fuentes)
    elif args.automarcar:
        automarcar_oneshot(args.simular)
    elif args.refrescar_competidores:
        refrescar_competidores_oneshot()
    elif args.purgar:
        purgar(args.ventana_anios, args.simular, args.lote)
    elif args.cargar:
        carga(construir_unidades(args.solo, args.periodos), limpiar=not args.conservar_zip)
    else:
        dry_run(anios, fuentes)


if __name__ == "__main__":
    main()
