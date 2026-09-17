"""medir_menores.py — línea base de rendimiento de public.menores.

Para qué: los menores autonómicos (F1 Andalucía, luego País Vasco, Cataluña...) van a
multiplicar public.menores. El criterio de aceptación de cada carga es «no empeora»,
y eso solo se puede decir comparando con una medición de ANTES hecha de la MISMA
manera. Este script es esa manera: se lanza igual antes y después de cada carga.

    python medir_menores.py --etiqueta F0 --salida ruta/menores_F0.json

SOLO LECTURA, por dos vías:
  · EXPLAIN (ANALYZE, BUFFERS) por la conexión claude_ro (secrets/supabase_ro.url, en
    modo readonly): tiempo en el servidor, bloques de caché y si hay Seq Scan.
  · GET/HEAD por PostgREST con la clave del .env (lo mismo que informe_empresa.py):
    lo que tarda de verdad desde aquí, red incluida. No escribe nada.

Qué mide:
  1. Tamaños: base, heap e índices de menores (cada índice).
  2. Caché: aciertos de bloques de la base, de menores y de cada índice. Son contadores
     ACUMULADOS desde el último reset de estadísticas: para comparar antes/después se
     restan dos tomas (el JSON guarda los contadores brutos, no solo el %).
  3. La consulta de menores_resumen_cif (ficha de competidor) para 5 CIF, más el plan
     genérico del peor caso (plpgsql cachea el plan tras 5 llamadas).
  4. La vista Menores: primera página por defecto, con el nicho, y la de un CIF (la
     entrada desde la ficha), con el conteo que pide el front.
  5. El bloque 3b de informe_empresa.py para LODEPA, sin y con criba de exclusión.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import psycopg2

import informe_empresa as ie

RAIZ = Path(__file__).resolve().parent
DSN_RO = RAIZ / "secrets" / "supabase_ro.url"

# Los competidores de referencia del encargo F0: tres seguidos, LODEPA y el CIF con más
# menores de toda la tabla (B84498955, el peor caso de la ficha).
CIFS_FICHA = ["A82850611", "A28345577", "B86833753", "B85578573", "B84498955"]
PEOR_CASO = "B84498955"

# Lo que pide la vista Menores (menores_api.js, M_COLUMNAS) y su tamaño de página.
COLUMNAS_VISTA = ("licitacion_id,objeto,cpv,importe_sin_iva,organo_contratacion,adjudicatario,"
                  "cif_adjudicatario,cifs_adjudicatarios,fecha_adjudicacion,num_expediente,"
                  "enlace,n_adjudicatarios")
POR_PAGINA = 25
UMBRAL_COUNT_EXACTO = 10000          # menores_api.js: por debajo, el front pide count exacto

# Frases de exclusión con las que se revisó el nicho de LODEPA (16/09/2026).
EXCLUYE_LODEPA = "cabina de bioseguridad,campana de flujo laminar,vitrina de gases"

# Cuerpo de public.menores_resumen_cif (menores_ficha.sql), tal cual.
SQL_FICHA = """
select count(*) as n, sum(m.importe_sin_iva) as importe_total,
       max(m.fecha_adjudicacion) filter (where m.fecha_adjudicacion >= date '2018-01-01') as ultimo,
       min(m.fecha_adjudicacion) filter (where m.fecha_adjudicacion >= date '2018-01-01') as primero,
       count(*) filter (where m.fecha_adjudicacion is null) as n_sin_fecha,
       count(*) filter (where m.fecha_adjudicacion < date '2018-01-01') as n_fecha_rara,
       count(*) filter (where m.importe_sin_iva is null) as n_sin_importe,
       count(*) filter (where m.n_adjudicatarios > 1 and m.cif_adjudicatario is distinct from %(cif)s) as n_compartidos,
       coalesce(sum(m.importe_sin_iva) filter (where m.n_adjudicatarios > 1
                and m.cif_adjudicatario is distinct from %(cif)s), 0) as importe_compartido
from public.menores m
where m.cifs_adjudicatarios && array[%(cif)s]
"""


def conecta_ro():
    lineas = [l.strip() for l in DSN_RO.read_text(encoding="utf-8", errors="replace").splitlines()]
    dsn = next((l for l in lineas if l and not l.startswith("#")), None)
    if not dsn:
        sys.exit(f"ERROR: no hay DSN en {DSN_RO}")
    con = psycopg2.connect(dsn)
    con.set_session(readonly=True, autocommit=True)
    return con


def nicho_de_intereses() -> tuple[list[str], str]:
    """El nicho EXACTO que usa la vista Menores: misma lógica que _nicho_menores() de
    generar_web.py (criticas + a_revisar, palabras sin tildes unidas con « or »)."""
    import unicodedata
    import yaml
    with open(RAIZ / "intereses.yaml", encoding="utf-8") as f:
        criterios = yaml.safe_load(f) or {}

    def sin_tildes(t):
        return "".join(c for c in unicodedata.normalize("NFD", str(t).lower())
                       if unicodedata.category(c) != "Mn")
    cpv, kws = [], []
    for grupo in ("criticas", "a_revisar"):
        crit = criterios.get(grupo) or {}
        for c in crit.get("cpv") or []:
            s = str(c).strip()
            if s and s not in cpv:
                cpv.append(s)
        for p in crit.get("palabras_clave") or []:
            s = sin_tildes(p).strip()
            if s and s not in kws:
                kws.append(s)
    return cpv, " or ".join(kws)


def explica(cur, sql: str, params=None, analizar: bool = True) -> dict:
    opciones = "ANALYZE, BUFFERS, FORMAT JSON" if analizar else "FORMAT JSON"
    cur.execute(f"EXPLAIN ({opciones}) {sql}", params)
    raiz = cur.fetchone()[0][0]
    nodos = []

    def recorre(n, nivel=0):
        etiqueta = n["Node Type"]
        if n.get("Index Name"):
            etiqueta += f" [{n['Index Name']}]"
        if analizar and "Actual Rows" in n:
            etiqueta += f" filas={n['Actual Rows']}"
        nodos.append({"nivel": nivel, "nodo": etiqueta, "tipo": n["Node Type"],
                      "relacion": n.get("Relation Name")})
        for hijo in n.get("Plans", []):
            recorre(hijo, nivel + 1)
    recorre(raiz["Plan"])
    return {
        "ms": raiz.get("Execution Time"),
        "planificacion_ms": raiz.get("Planning Time"),
        "bloques_cache": raiz["Plan"].get("Shared Hit Blocks"),
        "bloques_disco": raiz["Plan"].get("Shared Read Blocks"),
        "filas_estimadas": raiz["Plan"].get("Plan Rows"),
        "seq_scan_menores": any(x["tipo"] == "Seq Scan" and x["relacion"] == "menores"
                                for x in nodos),
        "plan": ["  " * x["nivel"] + x["nodo"] for x in nodos],
    }


def repite_explain(cur, sql, params, veces) -> dict:
    """La 1.ª ejecución puede ir a disco; las siguientes ya en caché. Se guardan las dos."""
    tomas = [explica(cur, sql, params) for _ in range(veces)]
    return {"primera": tomas[0], "ms_todas": [t["ms"] for t in tomas],
            "ms_mediana": statistics.median(t["ms"] for t in tomas)}


def cronometra(fn, veces) -> dict:
    tiempos, ultimo = [], None
    for _ in range(veces):
        t = time.perf_counter()
        ultimo = fn()
        tiempos.append(round(time.perf_counter() - t, 3))
    return {"s_todas": tiempos, "s_mediana": statistics.median(tiempos), "ultimo": ultimo}


# ----------------------------------------------------------------------------- bloques

def tamanos(cur) -> dict:
    cur.execute("""select pg_database_size(current_database()), pg_relation_size('public.menores'),
                          pg_table_size('public.menores'), pg_indexes_size('public.menores'),
                          pg_total_relation_size('public.menores'),
                          (select reltuples::bigint from pg_class where oid = 'public.menores'::regclass)""")
    base, heap, tabla, indices, total, filas = cur.fetchone()
    cur.execute("""select indexrelname, pg_relation_size(indexrelid), idx_scan
                   from pg_stat_user_indexes where relname = 'menores'
                   order by pg_relation_size(indexrelid) desc""")
    return {"base_bytes": base, "menores_heap_bytes": heap, "menores_tabla_bytes": tabla,
            "menores_indices_bytes": indices, "menores_total_bytes": total,
            "menores_filas_estimadas": filas,
            "bytes_por_fila": round(total / filas) if filas else None,
            "indices": [{"indice": n, "bytes": b, "idx_scan": s} for n, b, s in cur.fetchall()]}


def cache(cur) -> dict:
    cur.execute("""select blks_hit, blks_read, stats_reset from pg_stat_database
                   where datname = current_database()""")
    hit, read, reset = cur.fetchone()
    cur.execute("""select heap_blks_hit, heap_blks_read, idx_blks_hit, idx_blks_read
                   from pg_statio_user_tables where relname = 'menores'""")
    hh, hr, ih, ir = cur.fetchone()
    cur.execute("""select indexrelname, idx_blks_hit, idx_blks_read from pg_statio_user_indexes
                   where relname = 'menores' order by indexrelname""")
    pct = lambda a, b: round(100 * a / (a + b), 2) if (a or 0) + (b or 0) else None
    return {"desde_reset": str(reset),
            "base": {"hit": hit, "read": read, "pct": pct(hit, read)},
            "menores_heap": {"hit": hh, "read": hr, "pct": pct(hh, hr)},
            "menores_indices": {"hit": ih, "read": ir, "pct": pct(ih, ir)},
            "por_indice": [{"indice": n, "hit": h, "read": r, "pct": pct(h, r)}
                           for n, h, r in cur.fetchall()]}


def ficha(cur, veces) -> dict:
    salida = {}
    for cif in CIFS_FICHA:
        cur.execute(SQL_FICHA, {"cif": cif})
        fila = cur.fetchone()
        salida[cif] = {"n": fila[0], "importe_total": float(fila[1]) if fila[1] is not None else None,
                       "ultimo": str(fila[2]),
                       **repite_explain(cur, SQL_FICHA, {"cif": cif}, veces)}
    # Plan GENÉRICO del peor caso: lo que usaría plpgsql a partir de la 6.ª llamada.
    cur.execute("deallocate all")
    cur.execute("prepare ficha_gen(text) as " + SQL_FICHA.replace("%(cif)s", "$1"))
    cur.execute("set plan_cache_mode = force_generic_plan")
    try:
        salida["generico_" + PEOR_CASO] = explica(cur, "execute ficha_gen(%s)", (PEOR_CASO,))
    finally:
        cur.execute("reset plan_cache_mode")
        cur.execute("deallocate all")
    return salida


def sql_vista(where: str) -> str:
    cols = COLUMNAS_VISTA
    return (f"select {cols} from public.menores {where} "
            f"order by fecha_adjudicacion desc nulls last, licitacion_id asc "
            f"limit {POR_PAGINA} offset 0")


def vista(cur, sb, veces, nombre, where_sql, where_params, filtro_rest) -> dict:
    """Primera página + el conteo que pide el front (planned; exacto si < 10.000)."""
    orden = "order=fecha_adjudicacion.desc.nullslast,licitacion_id.asc"
    params_rest = f"select={COLUMNAS_VISTA}&{filtro_rest + '&' if filtro_rest else ''}{orden}&offset=0&limit={POR_PAGINA}"
    res = {"servidor_pagina": repite_explain(cur, sql_vista(where_sql), where_params, veces)}
    estimado = explica(cur, f"select 1 from public.menores {where_sql}", where_params,
                       analizar=False)["filas_estimadas"]
    res["conteo_estimado_planner"] = estimado
    res["pide_count_exacto"] = estimado < UMBRAL_COUNT_EXACTO
    if res["pide_count_exacto"]:
        res["servidor_count_exacto"] = repite_explain(
            cur, f"select count(*) from public.menores {where_sql}", where_params, veces)

    def pagina():
        return len(sb._pide("menores", params_rest).json())

    def conteo(modo):
        destino = (f"{sb.url}/rest/v1/menores?select=licitacion_id"
                   f"{'&' + filtro_rest if filtro_rest else ''}")
        r = sb.ses.head(destino, headers={"Prefer": f"count={modo}"}, timeout=60)
        sb.peticiones += 1
        if r.status_code >= 400:
            sys.exit(f"ERROR {r.status_code} en el conteo {modo} de {nombre}")
        return r.headers.get("Content-Range")
    res["rest_pagina"] = cronometra(pagina, veces)
    res["rest_count_planned"] = cronometra(lambda: conteo("planned"), veces)
    if res["pide_count_exacto"]:
        res["rest_count_exact"] = cronometra(lambda: conteo("exact"), veces)
    return res


def informe_3b(sb) -> dict:
    """El bloque 3b tal como lo llama main() para LODEPA sin --cpv (CPV deducidos)."""
    cif = "B86833753"
    adjudicaciones, catalogo = ie.bloque2_adjudicaciones(sb, cif)
    menores_emp = ie.bloque3_menores_cif(sb, cif)
    cpv_info = ie.bloque4_cpv(adjudicaciones, catalogo, menores_emp["filas"])
    candidatos = [c["cpv"] for c in cpv_info["cpv_deducidos"]]
    criba = ie.criba_cpv(sb, candidatos, sb.cuenta("licitaciones", ""))
    cpvs = criba["usados"]
    salida = {"cpvs": cpvs}
    for etiqueta, excluye in (("sin_criba", ""), ("con_exclusiones", EXCLUYE_LODEPA)):
        antes = sb.peticiones
        t = time.perf_counter()
        r = ie.bloque3b_menores_nicho(sb, cpvs, [], ie.trocea_terminos(excluye))
        salida[etiqueta] = {"s": round(time.perf_counter() - t, 3), "n": r["n"],
                            "n_solo_por_codigo": r["criba"]["n_solo_por_codigo"] if r["criba"] else None,
                            "peticiones": sb.peticiones - antes}
    return salida


def main() -> None:
    ap = argparse.ArgumentParser(description="Línea base de rendimiento de public.menores (solo lectura).")
    ap.add_argument("--etiqueta", required=True, help="p. ej. F0 o F1-tras-vacuum")
    ap.add_argument("--salida", required=True, help="fichero JSON de salida")
    ap.add_argument("--repeticiones", type=int, default=3)
    args = ap.parse_args()

    url, clave, _ = ie.credenciales()
    sb = ie.Supabase(url, clave)
    con = conecta_ro()
    cur = con.cursor()
    nicho_cpv, nicho_kw = nicho_de_intereses()
    arranque = time.time()

    medida = {"etiqueta": args.etiqueta,
              "cuando": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
              "repeticiones": args.repeticiones}
    print("· tamaños y caché (antes)")
    medida["tamanos"] = tamanos(cur)
    medida["cache_antes"] = cache(cur)
    print("· ficha de competidor (menores_resumen_cif)")
    medida["ficha"] = ficha(cur, args.repeticiones)

    print("· vista Menores: por defecto")
    medida["vista_defecto"] = vista(cur, sb, args.repeticiones, "defecto", "", None, "")

    print("· vista Menores: nicho")
    conds = ["cpv_txt ilike %s"] * len(nicho_cpv) + ["tsv @@ websearch_to_tsquery('spanish', %s)"]
    where_nicho = "where (" + " or ".join(conds) + ")"
    params_nicho = [f"% {c}%" for c in nicho_cpv] + [nicho_kw]
    rest_nicho = "or=(" + ",".join([f"cpv_txt.ilike.*%20{c}*" for c in nicho_cpv] +
                                   [f"tsv.wfts(spanish).{quote(nicho_kw)}"]) + ")"
    medida["nicho"] = {"cpv": nicho_cpv, "palabras": nicho_kw}
    medida["vista_nicho"] = vista(cur, sb, args.repeticiones, "nicho", where_nicho, params_nicho, rest_nicho)

    print("· vista Menores: un CIF (entrada desde la ficha)")
    medida["vista_cif"] = {}
    for cif in ("A82850611", PEOR_CASO):
        medida["vista_cif"][cif] = vista(cur, sb, args.repeticiones, "cif " + cif,
                                         "where cifs_adjudicatarios && %s::text[]", ([cif],),
                                         f"cifs_adjudicatarios=ov.%7B{cif}%7D")

    print("· informe_empresa.py bloque 3b (LODEPA)")
    medida["informe_3b_lodepa"] = informe_3b(sb)

    medida["cache_despues"] = cache(cur)
    medida["duracion_s"] = round(time.time() - arranque, 1)
    medida["peticiones_rest"] = sb.peticiones

    destino = Path(args.salida)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(medida, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"OK · {destino} · {medida['duracion_s']} s")


if __name__ == "__main__":
    main()
