#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BACKUP PREVIO A LA MIGRACIÓN DE PERFILES  ·  Fase F1, paso 1
============================================================================
Salva TODO lo que la migración a dos perfiles (lodepa / avensis) va a tocar,
para poder volver atrás si algo sale mal:

  1. Las 6 tablas privadas, en JSON y en CSV.
  2. Un restaurar_datos.sql con los INSERT de esas filas (rollback de datos).
  3. La foto del esquema ANTES: claves, índices, políticas RLS (public y
     storage), permisos por rol y funciones. Es el "antes" contra el que
     comparar el "después".
  4. Los PDFs del bucket de Storage, con su ruta original.
  5. Una verificación cruzada (¿sobra o falta algún PDF?) y un MANIFIESTO.

NO MODIFICA NADA. Solo lee. Se puede ejecutar las veces que haga falta.

DÓNDE ESCRIBE: por defecto en tu OneDrive, NO en el repositorio, porque el
repositorio es público y esto lleva datos privados.

CREDENCIALES (las dos ya están en tu disco, no hay que crear ninguna):
  · SUPABASE_RO_URL (en el .env) o secrets/supabase_ro.url -> conexión de SOLO
    LECTURA (rol claude_ro), para las tablas y el esquema.
  · .env  (SUPABASE_URL + SUPABASE_SECRET_KEY)  -> solo para descargar los
    PDFs del bucket, que no son accesibles con la conexión de lectura.

USO:
    python backup_perfiles.py                  # backup completo
    python backup_perfiles.py --sin-storage    # solo tablas y esquema
    python backup_perfiles.py --salida "D:\\otra\\carpeta"

    python backup_perfiles.py --completar "...\\backups\\perfiles_20260923_1932"
        Si algún PDF falló (pasa: cortes de SSL), esto baja SOLO los que
        falten, comprueba que los que ya estaban tengan el tamaño correcto y
        rehace el MANIFIESTO. Se puede repetir las veces que haga falta.
============================================================================
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import Json
except ImportError:
    sys.exit("Falta psycopg2. Instálalo con:  pip install psycopg2-binary")

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    sys.exit("Falta requests. Instálalo con:  pip install requests")

try:  # urllib3 viene con requests; si cambiara de sitio, seguimos sin reintentos de red
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    Retry = None

# Cuántas veces se reintenta la descarga de un PDF y cuánto se espera entre
# intentos. Los cortes de SSL (UNEXPECTED_EOF) son intermitentes: casi siempre
# van bien al segundo intento.
INTENTOS = 3
PAUSA = 3  # segundos


RAIZ = Path(__file__).resolve().parent
BUCKET = "documentos"

# Las 6 tablas privadas que la migración va a tocar.
TABLAS = [
    "decisiones",
    "contratos",
    "documentos",
    "cartera",
    "cartera_documentos",
    "radar_config",
]

# Tablas de catálogo: NO se salvan (son millones de filas y se regeneran solas
# desde el feed), pero sí se salva su esquema, porque comparten permisos.
TABLAS_CATALOGO = [
    "licitaciones",
    "adjudicaciones",
    "competidores",
    "menores",
    "menores_cobertura",
    "refrescos",
]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def aviso(texto: str) -> None:
    print(texto, flush=True)


def titulo(texto: str) -> None:
    print("\n" + texto, flush=True)
    print("-" * len(texto), flush=True)


def json_seguro(valor):
    """Convierte a algo serializable lo que psycopg2 devuelve (fechas, Decimal)."""
    if isinstance(valor, (dt.datetime, dt.date, dt.time)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, (bytes, memoryview)):
        return bytes(valor).hex()
    return str(valor)


def lee_env(ruta: Path) -> dict:
    """Lee un .env sencillo (CLAVE=valor). No imprime nunca los valores."""
    datos = {}
    if not ruta.exists():
        return datos
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, _, valor = linea.partition("=")
        datos[clave.strip()] = valor.strip().strip('"').strip("'")
    return datos


def sha256_fichero(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(65536), b""):
            h.update(bloque)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 1 · Conexión de solo lectura
# ---------------------------------------------------------------------------

def cadena_solo_lectura() -> str:
    """La conexión del rol claude_ro. NUNCA va escrita en este fichero: el repo
    es público. Se busca, por este orden, en tres sitios que están todos fuera
    del control de versiones (.gitignore:20-21):
      1. la variable de entorno SUPABASE_RO_URL
      2. el .env del proyecto, clave SUPABASE_RO_URL
      3. secrets/supabase_ro.url  (el sitio de siempre, sigue valiendo)
    """
    del_entorno = os.environ.get("SUPABASE_RO_URL") or lee_env(RAIZ / ".env").get(
        "SUPABASE_RO_URL"
    )
    if del_entorno:
        return del_entorno.strip()

    ruta = RAIZ / "secrets" / "supabase_ro.url"
    if ruta.exists():
        # El fichero puede llevar comentarios (#) y líneas en blanco: me quedo
        # con la primera línea que sea de verdad una cadena de conexión.
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea and not linea.startswith("#"):
                return linea
    return ""


def conecta_lectura():
    url = cadena_solo_lectura()
    if not url:
        sys.exit(
            "No encuentro la conexión de solo lectura (rol claude_ro).\n"
            "Ponla como SUPABASE_RO_URL en el .env, o deja el fichero\n"
            "secrets/supabase_ro.url. Sin ella no puedo salvar las tablas."
        )
    con = psycopg2.connect(url, connect_timeout=30)
    con.set_session(readonly=True, autocommit=True)
    return con


# ---------------------------------------------------------------------------
# 2 · Volcado de las tablas privadas
# ---------------------------------------------------------------------------

def exporta_tablas(con, destino: Path) -> dict:
    carpeta = destino / "datos"
    carpeta.mkdir(parents=True, exist_ok=True)
    recuentos = {}
    sql_restauracion = [
        "-- ROLLBACK DE DATOS · filas de las 6 tablas privadas tal y como estaban",
        "-- antes de la migración de perfiles.",
        "--",
        "-- CÓMO USARLO (solo si hay que volver atrás): abre el SQL Editor de",
        "-- Supabase, borra el contenido de la tabla afectada y pega su bloque.",
        "-- Cada bloque lleva su propio DELETE comentado por seguridad: tienes que",
        "-- descomentarlo tú a mano. Así no se borra nada por accidente.",
        "",
    ]

    for tabla in TABLAS:
        cur = con.cursor()
        cur.execute(
            """
            select column_name, data_type
              from information_schema.columns
             where table_schema = 'public' and table_name = %s
             order by ordinal_position
            """,
            (tabla,),
        )
        definicion = cur.fetchall()
        columnas = [f[0] for f in definicion]
        # Las columnas JSON hay que envolverlas al generar los INSERT: psycopg2
        # no sabe convertir un dict de Python a jsonb por su cuenta.
        es_json = [f[1] in ("json", "jsonb") for f in definicion]
        if not columnas:
            aviso(f"  · {tabla}: NO EXISTE en la base. Lo anoto y sigo.")
            recuentos[tabla] = None
            cur.close()
            continue

        cur.execute(f'select * from public."{tabla}"')
        filas = cur.fetchall()
        recuentos[tabla] = len(filas)

        # --- JSON (formato fiel: conserva los jsonb como objetos) ---
        registros = [dict(zip(columnas, fila)) for fila in filas]
        (carpeta / f"{tabla}.json").write_text(
            json.dumps(registros, ensure_ascii=False, indent=1, default=json_seguro),
            encoding="utf-8",
        )

        # --- CSV (para abrirlo en Excel de un vistazo) ---
        with open(carpeta / f"{tabla}.csv", "w", encoding="utf-8-sig", newline="") as f:
            escritor = csv.writer(f, delimiter=";")
            escritor.writerow(columnas)
            for fila in filas:
                escritor.writerow(
                    [
                        json.dumps(v, ensure_ascii=False, default=json_seguro)
                        if isinstance(v, (dict, list))
                        else ("" if v is None else v)
                        for v in fila
                    ]
                )

        # --- SQL de restauración (escapado por el propio driver: es exacto) ---
        sql_restauracion.append(f"-- ===== {tabla} ({len(filas)} filas) =====")
        sql_restauracion.append(f"-- delete from public.{tabla};")
        listado = ", ".join(f'"{c}"' for c in columnas)
        for fila in filas:
            plantilla = f"insert into public.{tabla} ({listado}) values (" + \
                ", ".join(["%s"] * len(columnas)) + ");"
            valores = tuple(
                Json(v) if (marca_json and v is not None) else v
                for v, marca_json in zip(fila, es_json)
            )
            sentencia = cur.mogrify(plantilla, valores).decode("utf-8")
            sql_restauracion.append(sentencia)
        sql_restauracion.append("")
        cur.close()

        aviso(f"  · {tabla}: {len(filas)} filas  ->  JSON + CSV")

    (destino / "restaurar_datos.sql").write_text(
        "\n".join(sql_restauracion), encoding="utf-8"
    )
    return recuentos


# ---------------------------------------------------------------------------
# 3 · Foto del esquema "antes"
# ---------------------------------------------------------------------------

CONSULTAS_ESQUEMA = {
    "columnas": """
        select table_name, ordinal_position, column_name, data_type,
               is_nullable, column_default
          from information_schema.columns
         where table_schema = 'public'
         order by table_name, ordinal_position
    """,
    "restricciones (PK, UNIQUE, FK, CHECK)": """
        select rel.relname as tabla, con.conname as nombre,
               pg_get_constraintdef(con.oid) as definicion
          from pg_constraint con
          join pg_class rel on rel.oid = con.conrelid
          join pg_namespace nsp on nsp.oid = rel.relnamespace
         where nsp.nspname = 'public'
         order by rel.relname, con.conname
    """,
    "indices": """
        select tablename, indexname, indexdef
          from pg_indexes
         where schemaname = 'public'
         order by tablename, indexname
    """,
    "politicas RLS de public": """
        select tablename, policyname, permissive, roles, cmd, qual, with_check
          from pg_policies
         where schemaname = 'public'
         order by tablename, policyname
    """,
    "politicas RLS de storage": """
        select tablename, policyname, permissive, roles, cmd, qual, with_check
          from pg_policies
         where schemaname = 'storage'
         order by tablename, policyname
    """,
    "RLS activada": """
        select relname, relrowsecurity, relforcerowsecurity
          from pg_class
         where relnamespace = 'public'::regnamespace and relkind = 'r'
         order by relname
    """,
    "permisos de tabla (relacl en crudo)": """
        select relname, coalesce(array_to_string(relacl, ' | '), '(por defecto)') as permisos
          from pg_class
         where relnamespace = 'public'::regnamespace and relkind = 'r'
         order by relname
    """,
    "permisos efectivos por rol": """
        select c.relname as tabla, r.rolname as rol,
               has_table_privilege(r.rolname, c.oid, 'select') as puede_leer,
               has_table_privilege(r.rolname, c.oid, 'insert') as puede_insertar,
               has_table_privilege(r.rolname, c.oid, 'update') as puede_modificar,
               has_table_privilege(r.rolname, c.oid, 'delete') as puede_borrar
          from pg_class c
          cross join (select unnest(array['anon','authenticated','service_role']) as rolname) r
         where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
         order by c.relname, r.rolname
    """,
    "funciones": """
        select p.proname as nombre,
               pg_get_function_identity_arguments(p.oid) as argumentos,
               p.prosecdef as security_definer,
               coalesce(array_to_string(p.proconfig, ' | '), '') as config,
               has_function_privilege('anon', p.oid, 'execute') as anon_ejecuta,
               has_function_privilege('authenticated', p.oid, 'execute') as authenticated_ejecuta,
               has_function_privilege('service_role', p.oid, 'execute') as service_role_ejecuta,
               coalesce(array_to_string(p.proacl, ' | '), '(por defecto)') as permisos
          from pg_proc p
          join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public'
         order by p.proname
    """,
    "triggers": """
        select event_object_table, trigger_name, action_timing, event_manipulation
          from information_schema.triggers
         where trigger_schema = 'public'
         order by event_object_table, trigger_name
    """,
    "event triggers": """
        select evtname, evtevent, evtenabled, p.proname as funcion
          from pg_event_trigger e
          join pg_proc p on p.oid = e.evtfoid
         order by evtname
    """,
}


def volca_esquema(con, destino: Path) -> dict:
    esquema = {}
    lineas = [
        "FOTO DEL ESQUEMA **ANTES** DE LA MIGRACIÓN DE PERFILES",
        "=" * 70,
        "Guarda este fichero. Es con lo que hay que comparar si algo va mal.",
        "",
    ]
    for titulo_bloque, sql in CONSULTAS_ESQUEMA.items():
        cur = con.cursor()
        try:
            cur.execute(sql)
            columnas = [d[0] for d in cur.description]
            filas = cur.fetchall()
        except Exception as e:  # noqa: BLE001
            con.rollback() if not con.autocommit else None
            lineas += [f"## {titulo_bloque}", f"  ERROR: {e}", ""]
            esquema[titulo_bloque] = {"error": str(e)}
            cur.close()
            continue

        registros = [
            {c: json_seguro(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
             for c, v in zip(columnas, fila)}
            for fila in filas
        ]
        esquema[titulo_bloque] = registros

        lineas.append(f"## {titulo_bloque}  ({len(filas)} filas)")
        for fila in filas:
            partes = [
                f"{c}={'' if v is None else v}"
                for c, v in zip(columnas, fila)
            ]
            lineas.append("  " + "  ".join(partes))
        lineas.append("")
        cur.close()
        aviso(f"  · {titulo_bloque}: {len(filas)} filas")

    (destino / "esquema_antes.txt").write_text("\n".join(lineas), encoding="utf-8")
    (destino / "esquema_antes.json").write_text(
        json.dumps(esquema, ensure_ascii=False, indent=1, default=json_seguro),
        encoding="utf-8",
    )
    return esquema


# ---------------------------------------------------------------------------
# 4 · Descarga de los PDFs del bucket
# ---------------------------------------------------------------------------

def crea_sesion(clave: str) -> requests.Session:
    """Una sola conexión reutilizada, con reintentos automáticos de red."""
    s = requests.Session()
    s.headers.update({"apikey": clave, "Authorization": f"Bearer {clave}"})
    if Retry is not None:
        reintentos = Retry(
            total=INTENTOS,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "HEAD"],
        )
        adaptador = HTTPAdapter(max_retries=reintentos, pool_maxsize=4)
        s.mount("https://", adaptador)
        s.mount("http://", adaptador)
    return s


def storage_lista(sesion: requests.Session, url: str, bucket: str,
                  prefijo: str = "") -> list:
    """Lista recursivamente los objetos de un bucket. Devuelve rutas completas."""
    encontrados = []
    desplazamiento = 0
    while True:
        r = sesion.post(
            f"{url}/storage/v1/object/list/{bucket}",
            json={
                "prefix": prefijo,
                "limit": 1000,
                "offset": desplazamiento,
                "sortBy": {"column": "name", "order": "asc"},
            },
            timeout=60,
        )
        r.raise_for_status()
        lote = r.json()
        if not lote:
            break
        for item in lote:
            nombre = item.get("name")
            if not nombre:
                continue
            ruta = f"{prefijo}{nombre}"
            # id == None significa que es una carpeta: hay que entrar.
            if item.get("id") is None:
                encontrados += storage_lista(sesion, url, bucket, prefijo=f"{ruta}/")
            else:
                tam = (item.get("metadata") or {}).get("size")
                encontrados.append({"ruta": ruta, "tamano": tam})
        if len(lote) < 1000:
            break
        desplazamiento += 1000
    return encontrados


def diagnostica_objeto(sesion: requests.Session, url: str, ruta: str,
                       tam_listado) -> str:
    """Cuando un objeto se resiste, pregunta a la API qué dice de él."""
    try:
        r = sesion.head(f"{url}/storage/v1/object/{BUCKET}/{ruta}", timeout=60)
        largo = r.headers.get("Content-Length")
        tipo = r.headers.get("Content-Type")
        return (
            f"la API responde {r.status_code}; tamaño según la cabecera: {largo} bytes; "
            f"tipo: {tipo}; tamaño según el listado del bucket: {tam_listado} bytes"
        )
    except Exception as e:  # noqa: BLE001
        return f"ni siquiera responde a una consulta de metadatos: {e}"


def descarga_objeto(sesion: requests.Session, url: str, ruta: str, salida: Path,
                    tam_esperado) -> dict:
    """Descarga un objeto con reintentos. Devuelve el resultado, no lanza.

    Escribe primero en un fichero .parcial y solo lo deja bueno cuando el
    tamaño cuadra, para que un corte a medias no deje un PDF truncado que
    parezca correcto.
    """
    salida.parent.mkdir(parents=True, exist_ok=True)
    temporal = salida.with_suffix(salida.suffix + ".parcial")
    ultimo_error = ""

    for intento in range(1, INTENTOS + 1):
        try:
            with sesion.get(
                f"{url}/storage/v1/object/{BUCKET}/{ruta}",
                timeout=(30, 180),
                stream=True,
            ) as r:
                r.raise_for_status()
                escritos = 0
                with open(temporal, "wb") as f:
                    for trozo in r.iter_content(chunk_size=262144):
                        if trozo:
                            f.write(trozo)
                            escritos += len(trozo)

            if tam_esperado is not None and escritos != int(tam_esperado):
                raise IOError(
                    f"llegó incompleto: {escritos} bytes de {tam_esperado}"
                )

            temporal.replace(salida)
            return {
                "ruta": ruta,
                "bytes": escritos,
                "sha256": sha256_fichero(salida),
                "intentos": intento,
            }

        except Exception as e:  # noqa: BLE001
            ultimo_error = str(e)
            temporal.unlink(missing_ok=True)
            if intento < INTENTOS:
                aviso(f"    · {ruta}: intento {intento} fallido ({e}). "
                      f"Reintento en {PAUSA}s...")
                time.sleep(PAUSA)

    return {"ruta": ruta, "error": ultimo_error, "intentos": INTENTOS}


def descarga_storage(url: str, clave: str, destino: Path,
                     solo_faltantes: bool = False) -> dict:
    sesion = crea_sesion(clave)
    carpeta = destino / "storage" / BUCKET
    carpeta.mkdir(parents=True, exist_ok=True)

    # Qué buckets hay (para que quede constancia de si hay más de uno).
    buckets = []
    try:
        r = sesion.get(f"{url}/storage/v1/bucket", timeout=60)
        r.raise_for_status()
        buckets = [
            {"nombre": b.get("name"), "publico": b.get("public")} for b in r.json()
        ]
        aviso(f"  · Buckets en el proyecto: {buckets}")
    except Exception as e:  # noqa: BLE001
        aviso(f"  · No he podido listar los buckets: {e}")

    objetos = storage_lista(sesion, url, BUCKET)
    aviso(f"  · Objetos en el bucket '{BUCKET}': {len(objetos)}")

    descargados, fallidos, saltados = [], [], []
    for obj in objetos:
        ruta = obj["ruta"]
        tam = obj.get("tamano")
        salida = carpeta / ruta

        # En modo --completar, lo que ya está bien descargado no se vuelve a bajar.
        if solo_faltantes and salida.exists():
            tam_local = salida.stat().st_size
            if tam is None or tam_local == int(tam):
                saltados.append(
                    {"ruta": ruta, "bytes": tam_local, "sha256": sha256_fichero(salida)}
                )
                continue
            aviso(f"    · {ruta}: el fichero local tiene {tam_local} bytes y "
                  f"debería tener {tam}. Lo bajo otra vez.")

        resultado = descarga_objeto(sesion, url, ruta, salida, tam)
        if "error" in resultado:
            detalle = diagnostica_objeto(sesion, url, ruta, tam)
            resultado["diagnostico"] = detalle
            fallidos.append(resultado)
            aviso(f"    ! {ruta}: NO se ha podido descargar tras {INTENTOS} intentos.")
            aviso(f"      Último error: {resultado['error']}")
            aviso(f"      Diagnóstico: {detalle}")
        else:
            descargados.append(resultado)
            if resultado["intentos"] > 1:
                aviso(f"    · {ruta}: descargado al intento {resultado['intentos']}.")

    completos = descargados + saltados
    if solo_faltantes:
        aviso(f"  · Ya estaban bien: {len(saltados)}  ·  "
              f"Descargados ahora: {len(descargados)}  ·  "
              f"Siguen fallando: {len(fallidos)}")
    aviso(f"  · Total en la carpeta: {len(completos)} de {len(objetos)}")

    return {"buckets": buckets, "objetos": objetos,
            "descargados": completos, "nuevos": descargados,
            "saltados": saltados, "fallidos": fallidos}


# ---------------------------------------------------------------------------
# 5 · Verificación cruzada: filas de la base <-> objetos del bucket
# ---------------------------------------------------------------------------

def rutas_desde_la_base(con) -> set:
    cur = con.cursor()
    rutas = set()
    for tabla in ("documentos", "cartera_documentos"):
        try:
            cur.execute(f"select ruta from public.{tabla} where ruta is not null")
            rutas |= {f[0] for f in cur.fetchall()}
        except Exception as e:  # noqa: BLE001
            aviso(f"  · No he podido leer {tabla}.ruta: {e}")
    cur.close()
    return rutas


def rutas_desde_el_backup(destino: Path) -> set:
    """Las mismas rutas, pero leídas del backup ya guardado (modo --completar)."""
    rutas = set()
    for tabla in ("documentos", "cartera_documentos"):
        fichero = destino / "datos" / f"{tabla}.json"
        if not fichero.exists():
            continue
        for fila in json.loads(fichero.read_text(encoding="utf-8")):
            if fila.get("ruta"):
                rutas.add(fila["ruta"])
    return rutas


def verifica_cruce(rutas_bd: set, info_storage: dict) -> dict:
    rutas_bucket = {o["ruta"] for o in info_storage.get("objetos", [])}
    en_disco = {d["ruta"] for d in info_storage.get("descargados", [])}

    huerfanos = sorted(rutas_bucket - rutas_bd)   # PDF sin fila que lo reclame
    colgados = sorted(rutas_bd - rutas_bucket)    # fila que apunta a un PDF que no está
    # Lo verdaderamente grave: la web lo enseña, existe en el bucket, y NO está
    # en tu copia de seguridad.
    sin_salvar = sorted(rutas_bd & rutas_bucket - en_disco)

    return {
        "rutas_en_la_base": len(rutas_bd),
        "objetos_en_el_bucket": len(rutas_bucket),
        "objetos_en_tu_copia": len(en_disco),
        "huerfanos": huerfanos,
        "colgados": colgados,
        "sin_salvar": sin_salvar,
    }


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

def escribe_manifiesto(destino: Path, recuentos: dict, info_storage: dict,
                       cruce: dict) -> list:
    """Escribe el MANIFIESTO y devuelve la lista de problemas encontrados."""
    lineas = [
        "MANIFIESTO DEL BACKUP",
        "=" * 70,
        f"Fecha: {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}",
        f"Carpeta: {destino}",
        "",
        "TABLAS SALVADAS (filas)",
    ]
    for tabla, n in recuentos.items():
        lineas.append(f"  · {tabla}: {'NO EXISTE' if n is None else n}")

    lineas += [
        "",
        "STORAGE",
        f"  · Buckets: {info_storage.get('buckets')}",
        f"  · Objetos en el bucket: {len(info_storage.get('objetos', []))}",
        f"  · Objetos guardados en esta carpeta: {len(info_storage.get('descargados', []))}",
        f"  · Fallos de descarga: {len(info_storage.get('fallidos', []))}",
    ]
    for f in info_storage.get("fallidos", []):
        lineas.append(f"      - {f['ruta']}")
        lineas.append(f"        error: {f.get('error', '')}")
        if f.get("diagnostico"):
            lineas.append(f"        diagnóstico: {f['diagnostico']}")

    if cruce:
        lineas += [
            "",
            "VERIFICACIÓN CRUZADA (base <-> bucket <-> tu copia)",
            f"  · Rutas registradas en la base: {cruce['rutas_en_la_base']}",
            f"  · Objetos en el bucket: {cruce['objetos_en_el_bucket']}",
            f"  · Objetos en tu copia: {cruce.get('objetos_en_tu_copia', 0)}",
            "",
            f"  · SIN SALVAR (la web los usa y NO están en tu copia): "
            f"{len(cruce.get('sin_salvar', []))}",
        ]
        for r in cruce.get("sin_salvar", []):
            lineas.append(f"      - {r}   <-- ESTO HAY QUE ARREGLARLO ANTES DE SEGUIR")
        lineas.append(
            f"  · Huérfanos (PDF en el bucket sin fila en la base): {len(cruce['huerfanos'])}"
        )
        for r in cruce["huerfanos"]:
            lineas.append(f"      - {r}")
        lineas.append(
            f"  · Colgados (fila en la base sin PDF en el bucket): {len(cruce['colgados'])}"
        )
        for r in cruce["colgados"]:
            lineas.append(f"      - {r}")

    lineas += [
        "",
        "CONTENIDO DE LA CARPETA",
        "  datos/*.json         las filas, formato fiel (recomendado para restaurar)",
        "  datos/*.csv          las mismas filas, para abrirlas en Excel",
        "  restaurar_datos.sql  los INSERT para devolver las filas a su sitio",
        "  esquema_antes.txt    claves, índices, políticas RLS y permisos de ANTES",
        "  esquema_antes.json   lo mismo, en formato para comparar con un programa",
        f"  storage/{BUCKET}/    los PDFs con su ruta original",
        "",
    ]
    (destino / "MANIFIESTO.txt").write_text("\n".join(lineas), encoding="utf-8")

    problemas = []
    if any(n is None for n in recuentos.values()):
        problemas.append("alguna tabla no existe")
    if info_storage.get("fallidos"):
        problemas.append(f"{len(info_storage['fallidos'])} PDFs no se han podido descargar")
    if cruce.get("sin_salvar"):
        problemas.append(
            f"{len(cruce['sin_salvar'])} PDFs que la web usa NO están en la copia"
        )
    if cruce.get("colgados"):
        problemas.append(f"{len(cruce['colgados'])} filas apuntan a un PDF que no está")
    return problemas


def recuentos_desde_el_backup(destino: Path) -> dict:
    """Cuenta las filas ya salvadas, para regenerar el manifiesto sin la base."""
    recuentos = {}
    for tabla in TABLAS:
        fichero = destino / "datos" / f"{tabla}.json"
        if fichero.exists():
            recuentos[tabla] = len(json.loads(fichero.read_text(encoding="utf-8")))
        else:
            recuentos[tabla] = None
    return recuentos


def completa_backup(destino: Path) -> int:
    """Baja solo lo que falte de un backup anterior y regenera el MANIFIESTO."""
    print("=" * 70)
    print("COMPLETAR UN BACKUP ANTERIOR")
    print("=" * 70)
    print(f"Carpeta: {destino}")

    if not (destino / "datos").exists():
        sys.exit(
            f"En {destino} no hay una carpeta 'datos': no parece un backup de este\n"
            "script. Comprueba la ruta."
        )

    entorno = lee_env(RAIZ / ".env")
    url = (os.environ.get("SUPABASE_URL") or entorno.get("SUPABASE_URL") or "").rstrip("/")
    clave = os.environ.get("SUPABASE_SECRET_KEY") or entorno.get("SUPABASE_SECRET_KEY")
    if not url or not clave:
        sys.exit("No encuentro SUPABASE_URL / SUPABASE_SECRET_KEY en .env.")

    titulo("Descargando solo lo que falta")
    info_storage = descarga_storage(url, clave, destino, solo_faltantes=True)

    titulo("Rehaciendo la verificación y el manifiesto")
    rutas_bd = rutas_desde_el_backup(destino)
    cruce = verifica_cruce(rutas_bd, info_storage)
    recuentos = recuentos_desde_el_backup(destino)
    problemas = escribe_manifiesto(destino, recuentos, info_storage, cruce)

    total_bucket = len(info_storage.get("objetos", []))
    total_copia = len(info_storage.get("descargados", []))
    print("\n" + "=" * 70)
    if problemas:
        print("SIGUE INCOMPLETO: " + "; ".join(problemas))
        print(f"Tienes {total_copia} de {total_bucket} objetos.")
        print("Vuelve a lanzar el mismo comando: solo intentará los que falten.")
    else:
        print(f"BACKUP COMPLETO Y VERIFICADO: {total_copia} de {total_bucket} objetos.")
        print("Ya se puede seguir con la migración.")
    print(f"Carpeta: {destino}")
    print("=" * 70)
    return 1 if problemas else 0


def destino_por_defecto() -> Path:
    onedrive = (
        Path.home()
        / "OneDrive - LODEPASL"
        / "LODEPA (NUEVO)"
        / "CONCURSO"
        / "RADAR-LICITACIONES"
        / "backups"
    )
    if onedrive.parent.exists():
        return onedrive
    return Path.home() / "radar-backups"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Backup previo a la migración de perfiles (no modifica nada)."
    )
    p.add_argument("--salida", default=None,
                   help="Carpeta donde dejar el backup (por defecto, tu OneDrive).")
    p.add_argument("--sin-storage", action="store_true",
                   help="No descargar los PDFs (solo tablas y esquema).")
    p.add_argument("--completar", metavar="CARPETA", default=None,
                   help="Baja solo los PDFs que falten de un backup anterior y "
                        "rehace el MANIFIESTO. Se puede repetir las veces que haga falta.")
    args = p.parse_args()

    if args.completar:
        return completa_backup(Path(args.completar))

    marca = dt.datetime.now().strftime("%Y%m%d_%H%M")
    base = Path(args.salida) if args.salida else destino_por_defecto()
    destino = base / f"perfiles_{marca}"
    destino.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("BACKUP PREVIO A LA MIGRACIÓN DE PERFILES")
    print("=" * 70)
    print(f"Destino: {destino}")
    print("Este script NO modifica nada: solo lee y copia.")

    titulo("1/4 · Conectando a la base (solo lectura)")
    con = conecta_lectura()
    cur = con.cursor()
    cur.execute("select current_user, current_database()")
    usuario, basedatos = cur.fetchone()
    cur.close()
    aviso(f"  · Conectado como '{usuario}' a '{basedatos}' en modo solo lectura.")

    titulo("2/4 · Salvando las 6 tablas privadas")
    recuentos = exporta_tablas(con, destino)

    titulo("3/4 · Guardando la foto del esquema (claves, RLS, permisos)")
    volca_esquema(con, destino)

    info_storage = {"objetos": [], "descargados": [], "fallidos": [], "buckets": []}
    cruce = {}
    if args.sin_storage:
        titulo("4/4 · Storage: SALTADO (--sin-storage)")
    else:
        titulo("4/4 · Descargando los PDFs del bucket")
        entorno = lee_env(RAIZ / ".env")
        url = (os.environ.get("SUPABASE_URL") or entorno.get("SUPABASE_URL") or "").rstrip("/")
        clave = os.environ.get("SUPABASE_SECRET_KEY") or entorno.get("SUPABASE_SECRET_KEY")
        if not url or not clave:
            aviso("  ! No encuentro SUPABASE_URL / SUPABASE_SECRET_KEY en .env.")
            aviso("    Los PDFs NO se han salvado. Repite con esas variables puestas,")
            aviso("    o descarga el bucket a mano desde el panel de Supabase.")
        else:
            info_storage = descarga_storage(url, clave, destino)
            cruce = verifica_cruce(rutas_desde_la_base(con), info_storage)

    problemas = escribe_manifiesto(destino, recuentos, info_storage, cruce)
    con.close()

    print("\n" + "=" * 70)
    if problemas:
        print("BACKUP TERMINADO CON AVISOS: " + "; ".join(problemas))
        print("Mira el MANIFIESTO.txt antes de seguir con la migración.")
        print("Para bajar solo lo que falte, sin repetirlo todo:")
        print(f'  .\\.venv\\Scripts\\python.exe backup_perfiles.py --completar "{destino}"')
    else:
        print("BACKUP COMPLETO Y VERIFICADO. Todo cuadra.")
    print(f"Carpeta: {destino}")
    print("=" * 70)
    return 1 if problemas else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\nCancelado por el usuario. No se ha modificado nada.")
