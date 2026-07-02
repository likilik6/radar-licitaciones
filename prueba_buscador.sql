-- prueba_buscador.sql  ·  PASO 5 de BG-3: SQL para CUADRAR y DIAGNOSTICAR
-- Ejecuta en el SQL Editor de Supabase (rol postgres: ve todas las filas y NO
-- tiene el statement_timeout corto del rol authenticated).
--
-- OJO tildes: 'climatizacion' sin tildes (el tsv se guardó con unaccent). Corre
-- los que usan now() casi a la vez que la consola.

-- ===========================================================================
-- A) CUADRAR los totales que devuelve buscador_api.js
-- ===========================================================================

-- P1 (vía RPC, EXACTO): texto=aire, abierta, importeMax=200000
select count(*) as p1_total
from public.licitaciones
where tsv @@ websearch_to_tsquery('spanish', 'aire')
  and valor_estimado <= 200000
  and (fecha_fin_plazo >= now() or fecha_fin_plazo is null);

-- P3 (vía RPC, EXACTO): texto=climatizacion (la RPC cuenta exacto el subconjunto)
select count(*) as p3_total
from public.licitaciones
where tsv @@ websearch_to_tsquery('spanish', 'climatizacion');

-- P4 (sin texto -> PostgREST, EXACTO): cpv overlaps ['90920000']
select count(*) as p4_total
from public.licitaciones
where cpv && array['90920000']::text[];

-- P5 (sin texto -> PostgREST, la API lo da ESTIMADO): todo el catálogo
select count(*) as p5_total_real from public.licitaciones;

-- ===========================================================================
-- B) DIAGNÓSTICO: por qué P3 daba timeout y por qué la CTE MATERIALIZED lo cura
-- ===========================================================================

-- B1) Plan MALO (lo que hacía PostgREST): ORDER BY fecha + filtro tsv al vuelo.
--     Espera ver un Index Scan por licitaciones_finplazo_id "Filter: tsv @@ ..."
--     recorriendo muchísimas filas (rows removed by filter enorme) -> lento.
explain (analyze, buffers)
select licitacion_id, titulo, fecha_fin_plazo
from public.licitaciones
where tsv @@ websearch_to_tsquery('spanish', 'climatizacion')
order by fecha_fin_plazo asc nulls last, licitacion_id asc
limit 25;

-- B2) Plan BUENO (lo que hace la RPC): CTE MATERIALIZED -> Bitmap Index Scan
--     sobre licitaciones_tsv_gin PRIMERO, luego Sort del subconjunto + Limit.
explain (analyze, buffers)
with filtrado as materialized (
  select licitacion_id, fecha_fin_plazo, valor_estimado, fecha_publicacion
  from public.licitaciones
  where tsv @@ websearch_to_tsquery('spanish', 'climatizacion')
)
select licitacion_id
from filtrado
order by fecha_fin_plazo asc nulls last, licitacion_id asc
limit 25;

-- B3) P5 — orden por valor_estimado DESC sin filtros (debe usar el compuesto
--     licitaciones_valor_desc_id -> Index Scan, 25 filas, sin Sort masivo).
explain (analyze, buffers)
select licitacion_id, titulo, valor_estimado
from public.licitaciones
order by valor_estimado desc nulls last, licitacion_id asc
limit 25;

-- ===========================================================================
-- C) PROBAR LA RPC directamente (debe cuadrar con A). 'climatizacion' es ligero;
--    si el editor diera "Failed to fetch" con un término amplio, usa uno raro.
-- ===========================================================================
select (public.buscar_licitaciones('climatizacion'))->>'total'                          as p3_rpc_total;
select json_array_length((public.buscar_licitaciones('climatizacion'))->'filas')         as p3_rpc_filas_pagina;
-- Con filtros, igual que P1:
select (public.buscar_licitaciones(p_texto => 'aire', p_estado => 'abierta',
                                   p_importe_max => 200000))->>'total'                    as p1_rpc_total;

-- ===========================================================================
-- D) ccaa / lugar_ejecucion: confirmado a 0 -> filtros desactivados en BG-3.
-- ===========================================================================
select
  count(*) filter (where ccaa is not null)            as ccaa_no_null,
  count(*) filter (where lugar_ejecucion is not null) as lugar_no_null,
  count(*)                                             as total
from public.licitaciones;

-- ===========================================================================
-- E) BG-4 inc2 — DIAGNÓSTICO del timeout con términos/prefijos AMPLIOS y
--    validación del nuevo plan (conteo ACOTADO + rama selectiva/amplia + trigram).
--    Requisito: haber corrido buscador_indices.sql y buscador_rpc.sql.
--    El editor (rol postgres) muestra el tiempo de ejecución de cada consulta.
-- ===========================================================================

-- E1) *** LA COMPROBACIÓN CLAVE (regresión de sargabilidad) ***
--     PROBE con EXPLAIN **SIN ANALYZE** (no ejecuta -> instantáneo, no puede dar
--     timeout): confirma que el plan usa BITMAP INDEX SCAN, NO Seq Scan.
--     Esto reproduce el WHERE que construye ahora la RPC (predicado CONSTANTE, sin
--     `IS NULL OR`). websearch_to_tsquery(const) se pliega a una tsquery constante.
--     Espera: Limit -> Aggregate -> Bitmap Heap Scan -> Bitmap Index Scan
--             (licitaciones_tsv_gin).  Si ves "Seq Scan on licitaciones" -> MAL.
explain
select count(*) from (
  select 1 from public.licitaciones
  where tsv @@ websearch_to_tsquery('spanish', unaccent('climatizacion'))   -- SELECTIVO (era el que regresaba)
  limit 5001
) s;

explain
select count(*) from (
  select 1 from public.licitaciones
  where tsv @@ websearch_to_tsquery('spanish', unaccent('cai'))             -- AMPLIO
  limit 5001
) s;

-- E1b) PROBE del PREFIJO CPV: debe usar Bitmap Index Scan de licitaciones_cpv_trgm
--      (NO Seq Scan). "90" (2 chars) también, por el espacio inicial de cpv_texto.
explain
select count(*) from (
  select 1 from public.licitaciones
  where public.cpv_texto(cpv) like any (array['% 9073%'])
  limit 5001
) s;

explain
select count(*) from (
  select 1 from public.licitaciones
  where public.cpv_texto(cpv) like any (array['% 90%'])
  limit 5001
) s;

-- E2) RAMA AMPLIA (página por índice de ORDEN + filtro, primeras 25). enable_sort
--     off obliga a obtener el orden del índice. Espera: Index Scan usando
--     licitaciones_finplazo_id con "Filter: (tsv @@ ...)", Limit 25 (no Sort).
set enable_sort = off;
explain
select licitacion_id, titulo, fecha_fin_plazo
from public.licitaciones
where tsv @@ websearch_to_tsquery('spanish', unaccent('cai'))
order by fecha_fin_plazo asc nulls last, licitacion_id asc
limit 25 offset 0;
reset enable_sort;

-- E3) RAMA SELECTIVA (climatizacion) — CON enable_sort NORMAL (como corre ahora la
--     RPC: enable_sort=off solo se aplica, con SET LOCAL, a la rama AMPLIA). Espera:
--     CTE materializada con Bitmap Index Scan (licitaciones_tsv_gin) DENTRO y un Sort
--     BARATO del subconjunto (~3146) + Limit. Coste total moderado (NO 1e10).
explain
with filtrado as materialized (
  select licitacion_id, fecha_fin_plazo, valor_estimado, fecha_publicacion
  from public.licitaciones
  where tsv @@ websearch_to_tsquery('spanish', unaccent('climatizacion'))
)
select licitacion_id from filtrado
order by fecha_fin_plazo asc nulls last, licitacion_id asc
limit 25 offset 0;

-- E3-mal) COMPARATIVA (diagnóstico): el MISMO plan con enable_sort=off (lo que hacía
--     la versión anterior a nivel de función). Debe verse el Sort con coste
--     DESORBITADO (~1e10) y/o un plan raro -> era la causa del timeout de la selectiva.
set enable_sort = off;
explain
with filtrado as materialized (
  select licitacion_id, fecha_fin_plazo, valor_estimado, fecha_publicacion
  from public.licitaciones
  where tsv @@ websearch_to_tsquery('spanish', unaccent('climatizacion'))
)
select licitacion_id from filtrado
order by fecha_fin_plazo asc nulls last, licitacion_id asc
limit 25 offset 0;
reset enable_sort;

-- E4) La RPC de punta a punta para los 4 casos (ya con ANALYZE = wall-clock real).
--     No debe dar 0A000 ni timeout. climatizacion => topado=false, total exacto
--     (~3146); cai / 9073 / 90 => topado=true, total=5000, filas=25. Todos RÁPIDOS.
explain analyze select public.buscar_licitaciones('climatizacion');
explain analyze select public.buscar_licitaciones('cai');
explain analyze select public.buscar_licitaciones(p_cpv_prefijo => array['9073']);
explain analyze select public.buscar_licitaciones(p_cpv_prefijo => array['90']);

-- E5) Comprobar el JSON de salida (total / aproximado / topado / nº de filas).
select
  caso,
  r->>'total'                          as total,
  r->>'aproximado'                     as aproximado,
  r->>'topado'                         as topado,
  json_array_length(r->'filas')        as filas_pagina
from (
  select 'climatizacion' as caso, public.buscar_licitaciones('climatizacion') as r
  union all select 'cai',   public.buscar_licitaciones('cai')
  union all select '9073',  public.buscar_licitaciones(p_cpv_prefijo => array['9073'])
  union all select '90',    public.buscar_licitaciones(p_cpv_prefijo => array['90'])
  union all select 'clima+cpv9073', public.buscar_licitaciones(p_texto => 'climatizacion', p_cpv_prefijo => array['9073'])
) t;
