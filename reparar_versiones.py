# -*- coding: utf-8 -*-
"""reparar_versiones.py — REPARACIÓN DIRIGIDA del catálogo (paso 4 del ORDEN DE
DESPLIEGUE de versiones_schema.sql).

EL PROBLEMA (PASO 0 del 15/10/2026): hasta el arreglo de versiones, la ingesta escribía
la versión que le tocaba por el orden de lectura, casi siempre la VIEJA: 234.104
licitaciones con una versión vieja, 196.732 expedientes sin sus adjudicaciones y 2.817
bajas sin marcar. Además, las filas de antes del arreglo no tienen sello de versión
(atom_updated NULL), y una fila sin sello acepta CUALQUIER versión: hasta que se
reparen, un backfill podría volver a escribirles una vieja.

QUÉ HACE, en tres fases:
  1) --indexar (no necesita clave ni toca la base): baja los ZIP de la ventana (los
     mismos que backfill_catalogo.py --cargar), lee TODAS las versiones y guarda en un
     SQLite local, por expediente, SOLO la ganadora y su baja más reciente. La ganadora
     sale de la regla de feeds.es_mas_nueva (mayor atom:updated; a igual marca, el
     snapshot más reciente) aplicada a TODAS las versiones, así que no depende del orden
     de los ZIP ni de los .atom. Si coinciden marca y snapshot, gana la de mayor huella:
     cualquiera valdría, pero así sale siempre la misma. Guarda también el CORTE de cada
     ZIP: el snapshot de su .atom más nuevo (hasta ahí llega). Reanudable por ZIP; un
     MENSUAL que se indexó con el mes aún abierto («abierto») se vuelve a leer si han
     pasado más de 24 h, porque desde entonces ha crecido.
  2) --ensayo (POR DEFECTO; SOLO LECTURA, nada más que GET): copia el id, el sello y
     ultima_vez de cada fila de public.licitaciones y cruza. Dice cuántas filas se
     escribirían, cuántas se quedan como están y por qué, cuántos expedientes que faltan
     se insertarían, cuántas bajas tocan y qué adjudicaciones de LODEPA aparecen o
     desaparecen. No escribe nada.
  3) --escribir (con OK): manda por carga_versiones la ganadora de cada fila que la
     necesite, luego la de cada expediente que FALTA en la base y, si termina, las bajas.
       · Un expediente que falta solo se inserta si la purga del job catalogo no lo
         quitaría (mismo criterio que purga_catalogo.sql: publicado hace más de
         VENTANA_PURGA_ANIOS años y con el plazo cerrado). Así no reintroduce lo purgado
         ni se pelea con la purga. Medido el 03/11/2026 con los ZIP de junio a octubre:
         unos 200 huecos de verdad (la mayoría de septiembre, con la fuente muda).
       · Se reanuda SIN checkpoint: el estado es la propia base. Una fila reparada ya
         tiene su sello, así que la siguiente pasada ni la manda (y si la mandara, la
         guarda de la base la rechazaría). Se trocea con --max-filas o --max-minutos.
       · Las BAJAS solo se mandan en la pasada que termina: en una fila aún sin sello la
         base marcaría la baja aunque su ganadora sea una republicación posterior.
       · Para antes de las franjas del robot (radar.yml y el mantenimiento del domingo).

LO QUE NO SE TOCA (y por qué), en una fila SIN sello, ni su versión ni su baja:
  · si el código viejo la escribió (ultima_vez) después del CORTE de su fuente menos
    MARGEN_CORTE: podría tener una versión más nueva que cualquiera de los ZIP (el ZIP
    del mes va con retraso) y escribirle la ganadora la haría retroceder. El corte de
    una fuente es hasta donde el índice está COMPLETO: el menor de los cortes de sus
    mensuales abiertos (tras uno abierto falta el resto de su mes, aunque haya meses
    posteriores) o, si no hay ninguno, el mayor;
  · si alguna de sus versiones no trae fecha legible (sin zona horaria): esa podría ser
    la más nueva y no se puede comparar.
  Quedan contadas en el ensayo y sin sello; otra pasada con ZIP más nuevos las cubre.

ANTES DE --escribir (ver versiones_schema.sql, ORDEN DE DESPLIEGUE):
  · migración aplicada, código nuevo desplegado y radar.yml lanzado a mano (el script
    para si ninguna fila tiene sello: señal de que el código nuevo aún no ha corrido);
  · el índice COMPLETO de la ventana entera del día en que se hizo (por eso --escribir no
    admite --periodos ni --solo): con un mes de menos, la ganadora de una fila sin sello
    podría ser más vieja que lo que ya tiene. La ventana sale de la fecha del índice, no
    de la de hoy: si se escribe pasada la medianoche de fin de mes, vale el índice de la
    víspera (el corte protege lo escrito después);
  · NO lanzar backfill.yml y parar la purga del job catalogo mientras dure.

DESPUÉS: refrescos (backfill_catalogo.py --refrescar-competidores, --refrescar-desiertas
y --automarcar --simular, este último con OK) y VACUUM ANALYZE de las dos tablas en el
editor. Las consultas de comprobación están en el bloque 4 de versiones_schema.sql.

SEGURIDAD: la service_role y la URL salen de SUPABASE_SERVICE_ROLE y SUPABASE_URL (como
en backfill_catalogo.py); nunca se imprimen. --indexar no las necesita.

USO
  python reparar_versiones.py --indexar                 # baja y lee los ZIP
  $env:SUPABASE_SERVICE_ROLE="..."; python reparar_versiones.py --ensayo
  python reparar_versiones.py --escribir --max-minutos 120
"""

import os
import sys
import json
import math
import time
import zlib
import sqlite3
import zipfile
import argparse
import tempfile
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

from lxml import etree

import backfill_catalogo as bc
from feeds import extrae_entrada, instante, lee_pagina

sys.stdout.reconfigure(encoding="utf-8")

BD_LOCAL = Path("reparacion_versiones.sqlite")   # el índice (en .gitignore: es grande)
PAGINA = 1000          # filas por GET (si el servidor da menos, se sigue hasta una vacía)
DESCARGAS = 3          # ZIP bajando a la vez (el servidor da ~0,5 MB/s por conexión)
CADUCIDAD_MENSUAL = 24 * 3600   # un ZIP MENSUAL en caché con más de 24 h se vuelve a bajar
# Un mensual está COMPLETO si se leyó al menos 3 días después de acabar su mes (el ZIP
# llega con ~1 día de retraso). Si se leyó antes, está «abierto».
RETRASO_ZIP = timedelta(days=3)
# Una fila sin sello escrita por el código viejo después de (corte - MARGEN_CORTE) no se
# toca. El margen cubre que ultima_vez es la hora de ARRANQUE de aquella pasada, no la de
# lectura del feed (y una pasada del diario tarda menos de 3 h).
MARGEN_CORTE = timedelta(hours=3)
# La ventana de la purga del job catalogo (radar.yml: --purgar --ventana-anios 3). Un
# expediente que falta en la base solo se inserta si esa purga NO lo quitaría.
VENTANA_PURGA_ANIOS = 3
# Margen antes de una franja del robot: una llamada con sus reintentos puede tardar minutos.
MARGEN_FRANJA = timedelta(minutes=15)
_FORMATO = "%Y-%m-%dT%H:%M:%S.%f"

# Franjas del robot en UTC (MEDIDAS, ver versiones_schema.sql): no se empieza a escribir
# dentro y se para antes de que empiece la siguiente. (días 0=lunes, desde, hasta)
FRANJAS_ROBOT = (
    # radar.yml: el cron dice 05:23, pero GitHub lo arranca entre las ~10:00 y las ~14:00
    ((0, 1, 2, 3, 4), (5, 0), (14, 30)),
    # mantenimiento.yml: domingos a las 06:17
    ((6,), (6, 0), (7, 30)),
)


def utc(texto):
    """Fecha con zona -> texto UTC de ancho fijo, que ordena igual que el instante (así
    SQLite compara versiones con < y >). None si no se lee: no se adivina."""
    momento = instante(texto)
    return momento.astimezone(timezone.utc).strftime(_FORMATO) if momento else None


# --- El índice local (SQLite) -----------------------------------------------
def abre_indice(ruta):
    bd = sqlite3.connect(str(ruta))
    bd.execute("pragma journal_mode = wal")
    bd.execute("pragma synchronous = normal")   # se puede rehacer: no hace falta más
    bd.executescript("""
        create table if not exists version (
            id text primary key, fuente text, marca text, snap text, huella text,
            n_adj integer, lodepa integer, objeto blob);
        create table if not exists baja (id text primary key, cuando text, tipo text, crudo text);
        create table if not exists dudosa (id text primary key);
        create table if not exists unidad (
            clave text primary key, versiones integer, bajas integer, dudosas integer,
            ilegibles integer, corte text, hecha_en text);
    """)
    if "corte" not in [c[1] for c in bd.execute("pragma table_info(unidad)")]:
        bd.close()
        raise SystemExit(f"{ruta} es de una versión anterior del script (sin corte): bórralo y vuelve a --indexar.")
    return bd


# Upsert que conserva la GANADORA: solo sustituye si la que llega es más nueva.
_SQL_VERSION = """
insert into version values (?, ?, ?, ?, ?, ?, ?, ?)
on conflict (id) do update set
    fuente = excluded.fuente, marca = excluded.marca, snap = excluded.snap, huella = excluded.huella,
    n_adj = excluded.n_adj, lodepa = excluded.lodepa, objeto = excluded.objeto
where excluded.marca > version.marca
   or (excluded.marca = version.marca and excluded.snap > version.snap)
   or (excluded.marca = version.marca and excluded.snap = version.snap and excluded.huella > version.huella)
"""
_SQL_BAJA = """
insert into baja values (?, ?, ?, ?)
on conflict (id) do update set cuando = excluded.cuando, tipo = excluded.tipo, crudo = excluded.crudo
where excluded.cuando > baja.cuando
   or (excluded.cuando = baja.cuando and coalesce(excluded.tipo, '') > coalesce(baja.tipo, ''))
"""


# --- Fase 1: indexar ----------------------------------------------------------
_ZIPS = {}      # ZIP abiertos en ESTE proceso (cada trabajador abre los suyos)


def _cierra_zips():
    for z in _ZIPS.values():
        z.close()
    _ZIPS.clear()


def lee_atom(tarea):
    """Lee UN .atom (en un proceso aparte). Devuelve (miembro, entradas, versiones, bajas,
    dudosas, corte, error): cada versión ya convertida en el objeto que espera
    carga_versiones (comprimido) con lo que hace falta para elegir; dudosas, los ids con
    una versión sin fecha legible; corte, el snapshot de este .atom."""
    ruta, miembro, fuente = tarea
    try:
        z = _ZIPS.get(ruta)
        if z is None:
            z = _ZIPS[ruta] = zipfile.ZipFile(ruta)
        raiz = etree.fromstring(z.read(miembro))
    except Exception as e:  # noqa: BLE001  (miembro roto o XML ilegible: lo cuenta quien llama)
        return miembro, 0, [], [], [], None, f"{type(e).__name__}: {e}"
    entradas, bajas, snapshot = lee_pagina(raiz)
    snap_u = utc(snapshot)
    versiones, dudosas = [], []
    # TODAS las entradas, también si un id se repite en el .atom: el upsert del índice
    # elige, así un empate se resuelve igual (por huella) esté donde esté cada copia.
    for entrada in entradas:
        objeto = bc.licitacion_para_rpc(extrae_entrada(entrada, fuente), snapshot)
        idu = objeto["licitacion_id"]
        if not idu or idu.startswith("("):         # «(sin enlace)»: sin clave no hay versión
            continue
        marca = utc(objeto["atom_updated"])
        if marca is None or snap_u is None:
            dudosas.append((idu,))
            continue
        lodepa = int(any(a["cif_adjudicatario"] in bc.CIFS_LODEPA for a in objeto["adjudicaciones"]))
        versiones.append((idu, fuente, marca, snap_u, objeto["version_huella"], len(objeto["adjudicaciones"]),
                          lodepa, zlib.compress(json.dumps(objeto, ensure_ascii=False).encode("utf-8"))))
    filas_bajas = [(b["id"], utc(b["cuando"]), b.get("tipo"), b["cuando"])
                   for b in bajas if b.get("id") and utc(b.get("cuando"))]
    return miembro, len(entradas), versiones, filas_bajas, dudosas, snap_u, None


def _tareas_de(zip_path, fuente, temporal):
    """Los .atom del ZIP como tareas (ruta, miembro, fuente). Los ZIP anidados (algunos
    anuales traen mensuales dentro) se sacan a 'temporal', cada uno con nombre único."""
    tareas = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            nombre = info.filename.lower()
            if nombre.endswith(".atom"):
                tareas.append((str(zip_path), info.filename, fuente))
            elif nombre.endswith(".zip"):
                fd, destino = tempfile.mkstemp(suffix=".zip", dir=temporal)
                with os.fdopen(fd, "wb") as f:
                    f.write(z.read(info))
                tareas.extend(_tareas_de(destino, fuente, temporal))
    return tareas


def _reparte(tareas, procesos):
    """lee_atom sobre cada tarea, en 'procesos' procesos (o aquí mismo si es 1). Como
    mucho 2 tareas por proceso en vuelo: un ZIP anual tiene miles de .atom y sus
    resultados no deben acumularse en memoria."""
    if procesos <= 1:
        try:
            for t in tareas:
                yield lee_atom(t)
        finally:
            _cierra_zips()
        return
    with ProcessPoolExecutor(procesos) as ex:   # uno por ZIP: al cerrarlo sueltan el fichero
        pendientes, cola = set(), iter(tareas)
        while True:
            for t in cola:
                pendientes.add(ex.submit(lee_atom, t))
                if len(pendientes) >= 2 * procesos:
                    break
            if not pendientes:
                return
            hechas, pendientes = wait(pendientes, return_when=FIRST_COMPLETED)
            for f in hechas:
                yield f.result()


def _indexa_unidad(bd, clave, fuente, zip_path, procesos):
    t0 = time.time()
    n = {"entradas": 0, "versiones": 0, "bajas": 0, "dudosas": 0, "ilegibles": 0}
    corte = None
    with tempfile.TemporaryDirectory(prefix="reparar_") as temporal:
        tareas = _tareas_de(zip_path, fuente, temporal)
        for i, (miembro, entradas, versiones, bajas, dudosas, snap_u, error) in enumerate(_reparte(tareas, procesos), 1):
            if error:
                n["ilegibles"] += 1
                print(f"    AVISO: .atom ilegible {miembro} ({error})")
                continue
            bd.executemany(_SQL_VERSION, versiones)
            bd.executemany(_SQL_BAJA, bajas)
            bd.executemany("insert or ignore into dudosa values (?)", dudosas)
            n["entradas"] += entradas
            n["versiones"] += len(versiones)
            n["bajas"] += len(bajas)
            n["dudosas"] += len(dudosas)
            if snap_u and (corte is None or snap_u > corte):
                corte = snap_u
            if i % 200 == 0:
                bd.commit()
                print(f"    {clave}: {i:,}/{len(tareas):,} .atom · {n['versiones']:,} versiones "
                      f"({time.time() - t0:,.0f} s)", flush=True)
    bd.execute("insert or replace into unidad values (?, ?, ?, ?, ?, ?, ?)",
               (clave, n["versiones"], n["bajas"], n["dudosas"], n["ilegibles"], corte,
                datetime.now(timezone.utc).isoformat(timespec="seconds")))
    bd.commit()
    print(f"  {clave}: {len(tareas):,} .atom · {n['entradas']:,} entradas · {n['versiones']:,} versiones "
          f"· {n['bajas']:,} bajas · {n['dudosas']:,} sin fecha legible · {n['ilegibles']:,} ilegibles "
          f"· corte {corte} · {time.time() - t0:,.0f} s", flush=True)


def _descarga(unidad):
    """Baja el ZIP de una unidad (con reintentos). Un MENSUAL en caché de hace más de 24 h
    se vuelve a bajar: el del mes en curso crece cada día. Sin el ZIP el índice no está
    completo, así que si no lo consigue, para."""
    fuente, periodo = unidad
    destino = bc.CACHE_DIR / f"{fuente}_{periodo}.zip"
    if len(periodo) == 6 and destino.exists() and time.time() - destino.stat().st_mtime > CADUCIDAD_MENSUAL:
        print(f"  {destino.name} en caché tiene más de 24 h: lo vuelvo a bajar.")
        destino.unlink()
    for intento in range(1, 5):
        try:
            return bc.descarga_zip(bc.url_zip(fuente, periodo), destino)
        except Exception as e:  # noqa: BLE001
            print(f"  AVISO: fallo bajando {destino.name} (intento {intento}/4): {e}")
            time.sleep(30 * intento)
    raise SystemExit(f"No pude bajar {destino.name}: el índice quedaría incompleto. Relánzalo "
                     f"más tarde (lo ya indexado se conserva).")


def _fin_de_mes(periodo):
    """'AAAAMM' -> el primer instante (UTC) del mes siguiente."""
    a, m = int(periodo[:4]), int(periodo[4:6])
    a, m = (a + 1, 1) if m == 12 else (a, m + 1)
    return datetime(a, m, 1, tzinfo=timezone.utc)


def _abierta(clave, hecha_en):
    """¿Esta unidad es un MENSUAL que se leyó antes de que su ZIP estuviera completo? Le
    falta lo publicado entre su corte y el final de su mes."""
    periodo = clave.rsplit("_", 1)[1]
    return len(periodo) == 6 and datetime.fromisoformat(hecha_en) < _fin_de_mes(periodo) + RETRASO_ZIP


class _Descarga(threading.Thread):
    """Baja un ZIP por delante del que se está leyendo. Es un hilo «daemon»: si la lectura
    falla, el proceso sale sin esperar a que terminen las descargas en curso (un anual
    tarda más de una hora)."""

    def __init__(self, unidad):
        super().__init__(daemon=True)
        self.unidad, self.ruta, self.error = unidad, None, None
        self.start()

    def run(self):
        try:
            self.ruta = _descarga(self.unidad)
        except BaseException as e:  # noqa: BLE001  (se relanza en el hilo principal)
            self.error = e

    def resultado(self):
        self.join()
        if self.error is not None:
            raise self.error
        return self.ruta


def indexar(unidades, ruta_bd, procesos, limpiar=True):
    bd = abre_indice(ruta_bd)
    try:
        _indexar(bd, unidades, ruta_bd, procesos, limpiar)
    finally:
        bd.close()


def _indexar(bd, unidades, ruta_bd, procesos, limpiar):
    hace_un_dia = datetime.now(timezone.utc) - timedelta(seconds=CADUCIDAD_MENSUAL)
    vigentes = {c for c, h in bd.execute("select clave, hecha_en from unidad")
                if not (_abierta(c, h) and datetime.fromisoformat(h) < hace_un_dia)}
    pendientes = [u for u in unidades if f"{u[0]}_{u[1]}" not in vigentes]
    print("=" * 78)
    print(f"ÍNDICE de versiones -> {ruta_bd}")
    print(f"Unidades: {len(unidades)} · ya indexadas y al día: {len(unidades) - len(pendientes)} · "
          f"procesos: {procesos} · borrar ZIP al terminar cada uno: {limpiar}")
    print("=" * 78)
    t0 = time.time()
    # Las descargas van por delante (como mucho DESCARGAS ZIP a la vez), sin llenar el disco.
    descargas = {}
    for i, (fuente, periodo) in enumerate(pendientes):
        for u in pendientes[i:i + DESCARGAS]:
            if u not in descargas:
                descargas[u] = _Descarga(u)
        clave = f"{fuente}_{periodo}"
        zip_path = descargas.pop((fuente, periodo)).resultado()
        try:
            _indexa_unidad(bd, clave, fuente, zip_path, procesos)
        except zipfile.BadZipFile:
            # Un ZIP roto en caché (una descarga cortada que sí llegó a renombrarse): se
            # borra y se baja otra vez, una sola.
            print(f"  {zip_path.name} está roto: lo borro y lo vuelvo a bajar.")
            zip_path.unlink()
            zip_path = _descarga((fuente, periodo))
            _indexa_unidad(bd, clave, fuente, zip_path, procesos)
        if limpiar:
            for p in (zip_path, zip_path.with_name(zip_path.name + ".part")):
                try:
                    p.unlink()
                except OSError:
                    pass
    print(f"Índice terminado en {(time.time() - t0) / 60:,.0f} min.")
    _resumen_indice(bd, unidades)


def _faltan(bd, unidades):
    hechas = {c for (c,) in bd.execute("select clave from unidad")}
    return [f"{f}_{p}" for f, p in unidades if f"{f}_{p}" not in hechas]


def _ventana(bd):
    """La ventana por defecto del DÍA EN QUE SE HIZO el índice (su lectura más reciente):
    la que tiene que estar entera para escribir. Sin unidades, la de hoy."""
    hecha = bd.execute("select max(hecha_en) from unidad").fetchone()[0]
    return bc.construir_unidades(hoy=datetime.fromisoformat(hecha) if hecha else None)


def _resumen_indice(bd, unidades):
    claves = [f"{f}_{p}" for f, p in unidades]
    filas = {r[0]: r for r in bd.execute("select * from unidad")}
    print("  Unidades del índice (versiones · bajas · sin fecha · ilegibles · corte):")
    for c in claves:
        r = filas.get(c)
        print(f"    {c:22} " + ("FALTA" if r is None else
                                f"{r[1]:>9,} · {r[2]:>7,} · {r[3]:>5,} · {r[4]:>3,} · {r[5]}  ({r[6]})"))
    for fuente, n in bd.execute("select fuente, count(*) from version group by fuente order by fuente"):
        print(f"  {fuente}: {n:,} expedientes con versión ganadora")
    print(f"  bajas (la más reciente de cada expediente): {bd.execute('select count(*) from baja').fetchone()[0]:,}"
          f" · expedientes con alguna versión sin fecha legible: {bd.execute('select count(*) from dudosa').fetchone()[0]:,}")


# --- Lectura de la base (SOLO GET) -------------------------------------------
def _get(sesion, headers, url, params, descripcion):
    r = bc._con_reintentos(lambda: sesion.get(url, headers=headers, params=params, timeout=120), descripcion)
    return r.json()


def lista_base(sesion, url_base, headers, bd):
    """Copia a la tabla local en_base el id, la fuente, el año, el SELLO de versión y
    ultima_vez de TODAS las filas de public.licitaciones. Por páginas ordenadas por id
    (keyset), hasta una página VACÍA: si el servidor sirve menos filas de las pedidas, se
    sigue igual. Devuelve (filas leídas, count(*) de la base o None)."""
    bd.execute("drop table if exists en_base")
    bd.execute("create table en_base (id text primary key, fuente text, anio text, au text, snap text, "
               "huella text, uv text)")
    url = f"{url_base}/rest/v1/{bc.TABLA}"
    h = bc._sin_prefer(headers)
    ultimo, n, t0 = None, 0, time.time()
    while True:
        params = {"select": "licitacion_id,fuente,fecha_publicacion,atom_updated,version_snapshot,"
                            "version_huella,ultima_vez",
                  "order": "licitacion_id.asc", "limit": str(PAGINA)}
        if ultimo is not None:
            params["licitacion_id"] = f"gt.{ultimo}"
        filas = _get(sesion, h, url, params, "listar public.licitaciones")
        if not filas:
            break
        bd.executemany("insert or replace into en_base values (?, ?, ?, ?, ?, ?, ?)",
                       [(f["licitacion_id"], f.get("fuente"), (f.get("fecha_publicacion") or "")[:4] or None,
                         utc(f.get("atom_updated")), utc(f.get("version_snapshot")), f.get("version_huella"),
                         utc(f.get("ultima_vez"))) for f in filas])
        antes, n = n, n + len(filas)
        if n // 100_000 > antes // 100_000:
            print(f"    {n:,} filas leídas ({time.time() - t0:,.0f} s)", flush=True)
        ultimo = filas[-1]["licitacion_id"]
    bd.commit()
    total = bc._cuenta(sesion, url, h)
    print(f"  public.licitaciones: {n:,} filas leídas en {time.time() - t0:,.0f} s"
          + (f" (count(*): {total:,})" if total is not None else ""))
    return n, total


def _prepara_cortes(bd):
    """Tabla local corte: por fuente, hasta dónde está COMPLETO el índice y el límite (corte
    - MARGEN_CORTE) a partir del cual una fila sin sello no se toca. Es el MENOR corte de
    sus mensuales abiertos (a cada uno le falta el resto de su mes) o, si no hay ninguno,
    el mayor. Sin corte legible, límite '' (no se toca ninguna fila sin sello)."""
    por_fuente = {}
    for clave, corte, hecha in bd.execute("select clave, corte, hecha_en from unidad"):
        por_fuente.setdefault(clave.split("_", 1)[0], []).append((corte, _abierta(clave, hecha)))
    bd.execute("drop table if exists corte")
    bd.execute("create table corte (fuente text primary key, corte text, limite text)")
    for fuente, unidades in por_fuente.items():
        abiertas = [c for c, abierta in unidades if abierta]
        cortes = abiertas or [c for c, _a in unidades]
        corte = None if None in cortes else (min(cortes) if abiertas else max(cortes))
        limite = (datetime.strptime(corte, _FORMATO) - MARGEN_CORTE).strftime(_FORMATO) if corte else ""
        bd.execute("insert into corte values (?, ?, ?)", (fuente, corte, limite))
    return bd.execute("select fuente, corte from corte order by fuente").fetchall()


# Las filas de la base que tienen versión en el índice (de una fuente con corte).
_SQL_CANDIDATAS = """
from version v join en_base b on b.id = v.id join corte c on c.fuente = v.fuente
"""
# Lo que hay que mandar: la ganadora de cada fila de la base que GANA a lo que tiene
# guardado, con la MISMA regla que la guarda de carga_versiones (una fila sin sello acepta
# cualquiera; a igual marca, snapshot igual o posterior y contenido distinto), MENOS las
# filas sin sello que no se pueden comparar (ver la cabecera): escritas por el código viejo
# después del límite, o con alguna versión sin fecha legible.
_SQL_A_ESCRIBIR = _SQL_CANDIDATAS + """
where (b.au is null or v.marca > b.au
       or (v.marca = b.au and v.snap >= coalesce(b.snap, '') and v.huella is not b.huella))
  and not (b.au is null and coalesce(b.uv, '') > c.limite)
  and not (b.au is null and exists (select 1 from dudosa d where d.id = v.id))
"""


# Expedientes de los ZIP que NO están en la base, sin los que tienen alguna versión sin
# fecha legible (su versión más nueva podría ser esa). Cuáles se insertan lo decide
# la_purga_lo_quitaria con las fechas de su objeto.
_SQL_FALTAN = """
from version v
where not exists (select 1 from en_base b where b.id = v.id)
  and not exists (select 1 from dudosa d where d.id = v.id)
"""


def la_purga_lo_quitaria(objeto, hoy):
    """¿Borraría este expediente la purga del job catalogo? Mismo criterio que
    public.purga_catalogo (purga_catalogo.sql): publicado hace más de VENTANA_PURGA_ANIOS
    años Y con el plazo ya cerrado (o sin plazo); sin fecha de publicación no lo borra. Se
    compara por día y la frontera cuenta como borrado: mejor no insertar algo que la purga
    quitaría en su siguiente pasada. 'hoy' es la hora UTC de la pasada. La otra excepción
    de la purga (lo que está en decisiones o contratos) la aplica _insertables."""
    dia = hoy.date()
    try:
        umbral = dia.replace(year=dia.year - VENTANA_PURGA_ANIOS)
    except ValueError:                          # 29 de febrero
        umbral = dia.replace(year=dia.year - VENTANA_PURGA_ANIOS, day=28)
    publicada = (objeto.get("fecha_publicacion") or "")[:10]
    fin = (objeto.get("fecha_fin_plazo") or "")[:10]
    return bool(publicada) and publicada <= umbral.isoformat() and (not fin or fin <= dia.isoformat())


def _insertables(bd, hoy, protegidas):
    """Los expedientes que faltan en la base y se insertarían: (id, fuente, lodepa, objeto).
    'protegidas': los ids de decisiones y contratos, que la purga nunca borra."""
    for i, fuente, lodepa, comprimido in bd.execute("select v.id, v.fuente, v.lodepa, v.objeto "
                                                     + _SQL_FALTAN + " order by v.id").fetchall():
        objeto = json.loads(zlib.decompress(comprimido))
        if i in protegidas or not la_purga_lo_quitaria(objeto, hoy):
            yield i, fuente, lodepa, objeto


def _por_escribir(bd, hoy, protegidas):
    """Lo que manda una pasada, en orden: primero las filas de la base que lo necesitan y
    luego los expedientes que faltan. Da (id, objeto, nuevo)."""
    for i, comprimido in bd.execute("select v.id, v.objeto " + _SQL_A_ESCRIBIR + " order by v.id"):
        yield i, json.loads(zlib.decompress(comprimido)), False
    for i, _fuente, _lodepa, objeto in _insertables(bd, hoy, protegidas):
        yield i, objeto, True


def _ids_de(sesion, headers, url, descripcion):
    """Todos los licitacion_id (sin repetir) de una tabla, por páginas (keyset) hasta una
    vacía: el servidor sirve como mucho 1.000 filas por GET."""
    ids, ultimo = set(), None
    while True:
        params = {"select": "licitacion_id", "order": "licitacion_id.asc", "limit": str(PAGINA)}
        if ultimo is not None:
            params["licitacion_id"] = f"gt.{ultimo}"
        filas = _get(sesion, headers, url, params, descripcion)
        ids.update(f["licitacion_id"] for f in filas if f.get("licitacion_id"))
        if not filas or not filas[-1].get("licitacion_id"):
            return ids
        ultimo = filas[-1]["licitacion_id"]


def _protegidas(sesion, url_base, headers):
    """Los licitacion_id de decisiones (de todos los perfiles) y de contratos: la purga
    nunca los borra, así que uno de ellos que falte se inserta aunque sea viejo. Si una
    tabla no se puede leer, se sigue sin ella: solo se insertaría de menos."""
    h = bc._sin_prefer(headers)
    ids = set()
    for tabla in ("decisiones", "contratos"):
        try:
            ids |= _ids_de(sesion, h, f"{url_base}/rest/v1/{tabla}", f"leer {tabla}")
        except (RuntimeError, SystemExit) as e:
            print(f"  AVISO: no pude leer {tabla} ({e}); sus expedientes viejos que falten no se insertan.")
    return ids


def _cuantas(bd, sql):
    return bd.execute(sql).fetchone()[0]


def _cuenta_apartadas(bd):
    """Filas sin sello que se quedan sin tocar aunque tengan versión en el índice."""
    return {
        "posteriores_al_corte": _cuantas(bd, "select count(*) " + _SQL_CANDIDATAS
                                         + "where b.au is null and coalesce(b.uv, '') > c.limite"),
        "dudosas_en_base": _cuantas(bd, "select count(*) " + _SQL_CANDIDATAS
                                    + "where b.au is null and exists (select 1 from dudosa d where d.id = v.id)"),
    }


# --- Fase 2: ensayo (solo lectura) ----------------------------------------------
def ensayo(unidades, ruta_bd, reloj=None):
    """Cruza el índice con la base SIN escribir. Devuelve las cifras (las usan los tests).
    'reloj' da la hora UTC de la que sale la ventana de la purga (por defecto, ahora)."""
    bd = abre_indice(ruta_bd)
    try:
        return _ensayo(bd, unidades, (reloj or _reloj)())
    finally:
        bd.close()


def _ensayo(bd, unidades, hoy):
    unidades = unidades or _ventana(bd)
    sesion, headers, _endpoint, url_base = bc._preparar_upsert()
    print("=" * 78)
    print("ENSAYO de la reparación (solo lectura: no se escribe nada)")
    print("=" * 78)
    _resumen_indice(bd, unidades)
    faltan = _faltan(bd, unidades)
    if faltan:
        print(f"  OJO: el índice está INCOMPLETO (faltan {len(faltan)}: {', '.join(faltan)}). "
              f"--escribir no arrancará hasta completarlo con --indexar.")
    leidas, total = lista_base(sesion, url_base, headers, bd)
    if total is not None and leidas < total:
        print(f"  OJO: el listado trae {leidas:,} filas y la base tiene {total:,}: --escribir no arrancará así.")
    cortes = _prepara_cortes(bd)
    protegidas = _protegidas(sesion, url_base, headers)
    insertables = list(_insertables(bd, hoy, protegidas))

    c = {
        "base": _cuantas(bd, "select count(*) from en_base"),
        "con_sello": _cuantas(bd, "select count(*) from en_base where au is not null"),
        "a_escribir": _cuantas(bd, "select count(*) " + _SQL_A_ESCRIBIR),
        "sin_version_en_zip": _cuantas(bd, "select count(*) from en_base b where not exists "
                                           "(select 1 from version v where v.id = b.id)"),
        "zip_no_en_base": _cuantas(bd, "select count(*) from version v where not exists "
                                       "(select 1 from en_base b where b.id = v.id)"),
        "a_insertar": len(insertables),
        "faltan_dudosos": _cuantas(bd, "select count(*) from version v where not exists "
                                       "(select 1 from en_base b where b.id = v.id) and exists "
                                       "(select 1 from dudosa d where d.id = v.id)"),
        "solo_sin_fecha": _cuantas(bd, "select count(*) from dudosa d where not exists "
                                       "(select 1 from version v where v.id = d.id) and not exists "
                                       "(select 1 from en_base b where b.id = d.id)"),
        "viejos_protegidos": sum(1 for i, _f, _l, o in insertables
                                 if i in protegidas and la_purga_lo_quitaria(o, hoy)),
        "bajas_en_base": _cuantas(bd, "select count(*) from baja k join en_base b on b.id = k.id"),
        "bajas_de_insertables": len({i for i, *_r in insertables}
                                    & {k for (k,) in bd.execute("select id from baja")}),
        "bajas_fuera": _cuantas(bd, "select count(*) from baja k where not exists "
                                    "(select 1 from en_base b where b.id = k.id)"),
        "adj_a_insertar": _cuantas(bd, "select coalesce(sum(v.n_adj), 0) " + _SQL_A_ESCRIBIR)
                          + sum(len(o["adjudicaciones"]) for *_r, o in insertables),
        "adj_en_base": bc._cuenta(sesion, f"{url_base}/rest/v1/{bc.TABLA_ADJ}", bc._sin_prefer(headers)),
        **_cuenta_apartadas(bd),
    }
    print()
    print("  Corte de los ZIP (snapshot más nuevo leído, UTC): "
          + " · ".join(f"{f} {k}" for f, k in cortes))
    print(f"  Filas en la base: {c['base']:,} · ya con sello de versión: {c['con_sello']:,}")
    if not c["con_sello"]:
        print("  OJO: ninguna fila tiene sello: el código nuevo aún no ha corrido (paso 3: radar.yml a mano).")
    print(f"  A ESCRIBIR (su versión ganadora gana a la guardada): {c['a_escribir']:,} "
          f"({math.ceil(c['a_escribir'] / bc.LOTE_RPC):,} llamadas de {bc.LOTE_RPC})")
    for fuente, n in bd.execute("select v.fuente, count(*) " + _SQL_A_ESCRIBIR + " group by v.fuente"):
        print(f"     {fuente}: {n:,}")
    print(f"  Sin sello y SIN TOCAR aunque tienen versión en los ZIP: {c['posteriores_al_corte']:,} escritas por "
          f"el código viejo después del corte - {MARGEN_CORTE} · {c['dudosas_en_base']:,} con alguna "
          f"versión sin fecha legible")
    print(f"  En la base SIN versión en estos ZIP (se quedan como están): {c['sin_version_en_zip']:,}")
    for fuente, anio, n, sellados in bd.execute(
            "select fuente, anio, count(*), count(au) from en_base b where not exists "
            "(select 1 from version v where v.id = b.id) group by fuente, anio order by fuente, anio"):
        print(f"     {fuente} {anio or 'sin fecha'}: {n:,} ({sellados:,} ya con sello)")
    c["purga_los_quitaria"] = c["zip_no_en_base"] - c["a_insertar"] - c["faltan_dudosos"]
    print(f"  En los ZIP pero NO en la base: {c['zip_no_en_base']:,} · se INSERTAN {c['a_insertar']:,} (de ellos "
          f"{c['viejos_protegidos']:,} viejos, porque tienen decisión o contrato) · la purga los quitaría "
          f"{c['purga_los_quitaria']:,} (no se insertan) · con alguna versión sin fecha {c['faltan_dudosos']:,} "
          f"(no se insertan)")
    if c["solo_sin_fecha"]:
        print(f"     y {c['solo_sin_fecha']:,} que faltan y solo traen versiones sin fecha legible (no se insertan)")
    por_anio = {}
    for _i, fuente, _l, o in insertables:
        clave = (fuente, (o.get("fecha_publicacion") or "")[:4] or "sin fecha")
        por_anio[clave] = por_anio.get(clave, 0) + 1
    for (fuente, anio), n in sorted(por_anio.items()):
        print(f"     se insertan de {fuente} {anio}: {n:,}")
    print(f"  Bajas de expedientes de la base: {c['bajas_en_base']:,} · de los que se insertan: "
          f"{c['bajas_de_insertables']:,} · del resto, que no están: "
          f"{c['bajas_fuera'] - c['bajas_de_insertables']:,} (no se mandan)")
    adj = f"{c['adj_en_base']:,}" if c["adj_en_base"] is not None else "(no disponible)"
    print(f"  Adjudicaciones: hoy hay {adj} filas; las versiones a escribir traen {c['adj_a_insertar']:,} "
          f"(sustituyen a las que tengan esos expedientes)")
    c.update(_ensayo_lodepa(sesion, url_base, headers, bd, insertables))
    print()
    print(f"No se ha escrito nada. Para reparar: --escribir (con OK), fuera de {_texto_franjas()}.")
    return c


def _ensayo_lodepa(sesion, url_base, headers, bd, insertables=()):
    """Adjudicaciones de LODEPA que la reparación haría APARECER (también en expedientes
    que se insertan) o DESAPARECER. Importa porque el auto-marcado crea «ganadas» con las
    que aparecen y nunca desmarca las que desaparecen (esas se deciden a mano)."""
    h = bc._sin_prefer(headers)
    cifs = ",".join(bc.CIFS_LODEPA)
    en_base = {f["licitacion_id"] for f in _get(
        sesion, h, f"{url_base}/rest/v1/{bc.TABLA_ADJ}",
        {"select": "licitacion_id", "cif_adjudicatario": f"in.({cifs})"}, "leer adjudicaciones de LODEPA")}
    try:
        ganadas = {f["licitacion_id"] for f in _get(
            sesion, h, f"{url_base}/rest/v1/decisiones",
            {"select": "licitacion_id", "perfil": "eq.lodepa", "estado": "eq.ganada"}, "leer decisiones")}
    except (RuntimeError, SystemExit) as e:
        print(f"  (no pude leer las decisiones: {e})")
        ganadas = set()
    escribir = {}
    for i, lodepa, objeto in bd.execute("select v.id, v.lodepa, v.objeto " + _SQL_A_ESCRIBIR):
        if lodepa or i in en_base:
            escribir[i] = (lodepa, json.loads(zlib.decompress(objeto)))
    escribir.update({i: (lodepa, o) for i, _f, lodepa, o in insertables if lodepa})
    aparecen = sorted(i for i, (lodepa, _o) in escribir.items() if lodepa and i not in en_base)
    desaparecen = sorted(i for i, (lodepa, _o) in escribir.items() if not lodepa and i in en_base)
    print(f"  LODEPA: {len(aparecen)} adjudicaciones aparecen y {len(desaparecen)} desaparecen")
    for etiqueta, ids in (("aparece", aparecen), ("DESAPARECE", desaparecen)):
        for i in ids:
            o = escribir[i][1]
            marca = " · tiene decisión «ganada»" if i in ganadas else ""
            print(f"     {etiqueta}: {i.rsplit('/', 1)[-1]} · {o.get('estado_expediente')} · "
                  f"{(o.get('titulo') or '')[:70]}{marca}")
    return {"lodepa_aparecen": aparecen, "lodepa_desaparecen": desaparecen}


# --- Fase 3: escribir (con OK) -------------------------------------------------
def _franja(momento):
    """(dentro, inicio_de_la_proxima): si 'momento' (UTC) cae en una franja del robot, y
    cuándo empieza la siguiente."""
    proxima = None
    for dias in range(0, 8):
        dia = (momento + timedelta(days=dias)).date()
        for semana, (h0, m0), (h1, m1) in FRANJAS_ROBOT:
            if dia.weekday() not in semana:
                continue
            ini = datetime(dia.year, dia.month, dia.day, h0, m0, tzinfo=timezone.utc)
            fin = datetime(dia.year, dia.month, dia.day, h1, m1, tzinfo=timezone.utc)
            if ini <= momento < fin:
                return True, ini
            if ini > momento and (proxima is None or ini < proxima):
                proxima = ini
    return False, proxima


def _texto_franjas():
    dias = "LMXJVSD"
    return " y ".join(f"{''.join(dias[d] for d in semana)} {h0:02d}:{m0:02d}-{h1:02d}:{m1:02d}"
                      for semana, (h0, m0), (h1, m1) in FRANJAS_ROBOT) + " UTC"


def _reloj():
    return datetime.now(timezone.utc)


def escribir(unidades, ruta_bd, max_filas=0, max_minutos=0, pausa=0.2, reloj=_reloj,
             ignorar_franjas=False, ignorar_ilegibles=False):
    """Manda las versiones ganadoras de las filas de la base que las necesitan, inserta los
    expedientes que faltan (si la purga no los quitaría) y, si termina, manda las bajas.
    Para en seco si algo no está listo; se puede relanzar cuantas veces haga
    falta. Devuelve los contadores de carga_versiones sumados. 'unidades' es la ventana que
    tiene que estar ENTERA en el índice (None: la del día en que se hizo, ver _ventana);
    'reloj' da la hora UTC (los tests pasan una fija)."""
    bd = abre_indice(ruta_bd)
    try:
        return _escribir(bd, unidades, max_filas, max_minutos, pausa, reloj, ignorar_franjas, ignorar_ilegibles)
    finally:
        bd.close()


def _escribir(bd, unidades, max_filas, max_minutos, pausa, reloj, ignorar_franjas, ignorar_ilegibles):
    ahora = reloj()
    dentro, proxima = _franja(ahora)
    if dentro and not ignorar_franjas:
        raise SystemExit(f"Ahora ({ahora:%a %H:%M} UTC) es franja del robot: no se escribe. "
                         f"Lánzalo fuera de {_texto_franjas()}.")
    faltan = _faltan(bd, unidades or _ventana(bd))
    if faltan:
        raise SystemExit(f"El índice está INCOMPLETO (faltan {', '.join(faltan)}): sin esos ZIP la "
                         f"ganadora podría ser más vieja que lo guardado. Completa con --indexar.")
    ilegibles = _cuantas(bd, "select coalesce(sum(ilegibles), 0) from unidad")
    if ilegibles and not ignorar_ilegibles:
        raise SystemExit(f"Hay {ilegibles} .atom ilegibles en el índice (ver --ensayo): con ellos "
                         f"podría faltar la versión más nueva de algún expediente. Revísalo o usa --ignorar-ilegibles.")

    sesion, headers, _endpoint, url_base = bc._preparar_upsert()
    bc._comprueba_carga_versiones(sesion, url_base, headers)
    print("=" * 78)
    print("REPARACIÓN: versiones ganadoras -> carga_versiones (filas de la base y expedientes que faltan)")
    print("=" * 78)
    leidas, total_base = lista_base(sesion, url_base, headers, bd)
    if total_base is not None and leidas < total_base:
        raise SystemExit(f"El listado trae {leidas:,} filas y la base tiene {total_base:,}: no sigo con un "
                         f"listado incompleto (¿otra cosa está escribiendo a la vez?).")
    if not _cuantas(bd, "select count(*) from en_base where au is not null"):
        raise SystemExit("Ninguna fila tiene sello de versión: el código nuevo aún no ha corrido. Antes, "
                         "paso 3 del ORDEN DE DESPLIEGUE (lanzar radar.yml a mano y mirar el log).")
    cortes = _prepara_cortes(bd)
    apartadas = _cuenta_apartadas(bd)

    protegidas = _protegidas(sesion, url_base, headers)
    a_reparar = _cuantas(bd, "select count(*) " + _SQL_A_ESCRIBIR)
    a_insertar = sum(1 for _x in _insertables(bd, ahora, protegidas))
    total = a_reparar + a_insertar
    limite = ahora + timedelta(minutes=max_minutos) if max_minutos else None
    if proxima is not None and not ignorar_franjas:
        tope = proxima - MARGEN_FRANJA
        limite = tope if limite is None else min(limite, tope)
    print("  Corte de los ZIP (UTC): " + " · ".join(f"{f} {k}" for f, k in cortes))
    print(f"  A escribir: {total:,} ({a_reparar:,} de la base y {a_insertar:,} que faltan) · sin tocar: "
          f"{apartadas['posteriores_al_corte']:,} posteriores al corte y "
          f"{apartadas['dudosas_en_base']:,} con versiones sin fecha · tope de filas: {max_filas or 'sin tope'} · "
          f"parar a las: {f'{limite:%a %H:%M} UTC' if limite else 'sin tope'}")

    totales, enviadas, insertadas, t0 = {}, 0, 0, time.time()
    bd.execute("drop table if exists insertada")
    bd.execute("create table insertada (id text primary key)")
    pendientes = _por_escribir(bd, ahora, protegidas)
    # Se va siempre UNO por delante: si el tope coincide con lo que faltaba, la pasada ha
    # terminado (y lleva sus bajas), no se ha parado.
    siguiente = next(pendientes, None)
    parada = None
    while siguiente is not None:
        if max_filas and enviadas >= max_filas:
            parada = f"tope de {max_filas:,} filas"
            break
        if limite and reloj() >= limite:
            parada = f"hora límite ({limite:%H:%M} UTC)"
            break
        n = bc.LOTE_RPC if not max_filas else min(bc.LOTE_RPC, max_filas - enviadas)
        lote = []
        while siguiente is not None and len(lote) < n:
            lote.append(siguiente)
            siguiente = next(pendientes, None)
        totales = bc._suma(totales, bc.sube_versiones(sesion, url_base, headers, [o for _i, o, _n in lote]))
        nuevos = [(i,) for i, _o, nuevo in lote if nuevo]
        bd.executemany("insert or ignore into insertada values (?)", nuevos)
        insertadas += len(nuevos)
        enviadas += len(lote)
        if enviadas % (20 * bc.LOTE_RPC) < len(lote) or enviadas == total:
            seg = time.time() - t0
            ritmo = enviadas / max(seg, 0.001)
            print(f"    {enviadas:,}/{total:,} · {totales.get('aplicadas', 0):,} escritas · "
                  f"{totales.get('rechazadas', 0):,} sin cambios · {ritmo:,.0f} filas/s · "
                  f"quedan ~{(total - enviadas) / max(ritmo, 0.001) / 60:,.0f} min", flush=True)
        if pausa:
            time.sleep(pausa)
    pendientes.close()

    # BAJAS, solo si se ha terminado (ver la cabecera): de expedientes de la base, sin las
    # filas sin sello que esta pasada ha dejado sin tocar (la base marcaría su baja aunque
    # su versión sea posterior), y de los insertados en esta pasada. Decide la base: marca
    # la baja si es posterior a la versión guardada (y a la ya marcada). Repetirlas no
    # cambia nada.
    if parada is None and limite and reloj() >= limite:
        parada = f"hora límite ({limite:%H:%M} UTC), antes de las bajas"
    if parada is None:
        bajas = [{"id": i, "cuando": crudo, "tipo": tipo} for i, crudo, tipo in bd.execute("""
            select k.id, k.crudo, k.tipo from baja k join en_base b on b.id = k.id
            left join corte c on c.fuente = b.fuente
            where not (b.au is null and (coalesce(b.uv, '') > coalesce(c.limite, '')
                                         or exists (select 1 from dudosa d where d.id = k.id)))
            union all
            select k.id, k.crudo, k.tipo from baja k join insertada n on n.id = k.id
            order by 1""")]
        if bajas:
            totales = bc._suma(totales, bc.sube_versiones(sesion, url_base, headers, [], bc.bajas_para_rpc(bajas)))

    print("=" * 78)
    bc._informe_versiones(f"Reparación ({enviadas:,} de {total:,} enviadas; {insertadas:,} de ellas, "
                          f"expedientes que faltaban)", totales)
    if parada:
        print(f"  PARADA por {parada}: quedan {total - enviadas:,} y las bajas. Relanza --escribir "
              f"(no repite lo hecho).")
    else:
        print("  Terminada. Siguiente: refrescos (competidores, desiertas completo, automarcar --simular),")
        print("  VACUUM ANALYZE de licitaciones y adjudicaciones y las consultas del bloque 4 de versiones_schema.sql.")
    return totales


def main():
    ap = argparse.ArgumentParser(description="Reparación dirigida de versiones (paso 4 de versiones_schema.sql).")
    modo = ap.add_mutually_exclusive_group()
    modo.add_argument("--indexar", action="store_true", help="Bajar y leer los ZIP de la ventana (no toca la base).")
    modo.add_argument("--ensayo", action="store_true", help="Cruzar el índice con la base, solo lectura (por defecto).")
    modo.add_argument("--escribir", action="store_true", help="Escribir las versiones ganadoras (con OK).")
    ap.add_argument("--periodos", nargs="+", default=None,
                    help="Periodos (como backfill_catalogo.py). Por defecto, la ventana de --cargar. "
                         "Solo para --indexar/--ensayo: --escribir exige siempre la ventana entera.")
    ap.add_argument("--solo", choices=list(bc.FUENTES), default=None,
                    help="Una sola fuente (solo para --indexar/--ensayo).")
    ap.add_argument("--cache", default=None, help="Carpeta de los ZIP (por defecto: cache_backfill/).")
    ap.add_argument("--bd", default=str(BD_LOCAL), help=f"El índice SQLite (por defecto: {BD_LOCAL}).")
    ap.add_argument("--procesos", type=int, default=min(4, os.cpu_count() or 1),
                    help="Procesos para leer los .atom (1 = sin procesos aparte).")
    ap.add_argument("--conservar-zip", action="store_true", help="No borrar cada ZIP tras indexarlo.")
    ap.add_argument("--max-filas", type=int, default=0, help="Escribir como mucho N filas en esta pasada.")
    ap.add_argument("--max-minutos", type=int, default=0, help="Parar de escribir tras M minutos.")
    ap.add_argument("--pausa", type=float, default=0.2, help="Segundos de respiro entre llamadas.")
    ap.add_argument("--ignorar-franjas", action="store_true", help="Escribir aunque sea franja del robot.")
    ap.add_argument("--ignorar-ilegibles", action="store_true", help="Escribir aunque haya .atom ilegibles.")
    args = ap.parse_args()
    if args.escribir and (args.periodos or args.solo):
        ap.error("--escribir exige el índice de la ventana ENTERA: sin --periodos ni --solo.")

    if args.cache:
        bc.CACHE_DIR = Path(args.cache)
    bc.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if args.indexar:
        indexar(bc.construir_unidades(args.solo, args.periodos), args.bd, args.procesos,
                limpiar=not args.conservar_zip)
    elif args.escribir:
        # La ventana que se exige es la del día en que se hizo el índice (ver _ventana).
        escribir(None, args.bd, args.max_filas, args.max_minutos, args.pausa,
                 ignorar_franjas=args.ignorar_franjas, ignorar_ilegibles=args.ignorar_ilegibles)
    else:
        trozo = bc.construir_unidades(args.solo, args.periodos) if (args.periodos or args.solo) else None
        ensayo(trozo, args.bd)


if __name__ == "__main__":
    main()
