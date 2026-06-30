-- prueba_buscador.sql  ·  PASO 5 de BG-3: SQL para CUADRAR y DIAGNOSTICAR
-- Ejecuta en el SQL Editor de Supabase (rol postgres: ve todas las filas y NO
-- tiene el statement_timeout corto del rol authenticated, por eso aquí sí se
-- puede contar exacto lo que en la API daba 57014).
--
-- OJO tildes: el módulo manda el término SIN tildes (el tsv se guardó con
-- unaccent), por eso usamos 'climatizacion' a secas. Corre los que usan now()
-- casi a la vez que la consola.

-- ===========================================================================
-- A) CUADRAR los totales EXACTOS que ahora devuelve buscador_api.js
-- ===========================================================================

-- P1 (pequeño -> la API lo devuelve EXACTO): texto=aire, abierta, importeMax=200000
select count(*) as p1_total
from public.licitaciones
where tsv @@ websearch_to_tsquery('spanish', 'aire')
  and valor_estimado <= 200000
  and (fecha_fin_plazo >= now() or fecha_fin_plazo is null);

-- P4 (pequeño -> EXACTO): cpv overlaps ['90920000']
select count(*) as p4_total
from public.licitaciones
where cpv && array['90920000']::text[];

-- P3 (referencia): la API lo devuelve AHORA como estimado (≈) si supera 10.000;
-- aquí ves el número real para comparar el orden de magnitud.
select count(*) as p3_total_real
from public.licitaciones
where tsv @@ websearch_to_tsquery('spanish', 'climatizacion');

-- P5 (referencia): sin filtros = todo el catálogo. La API lo da estimado (≈).
select count(*) as p5_total_real from public.licitaciones;

-- ===========================================================================
-- B) DIAGNÓSTICO con EXPLAIN ANALYZE de las consultas que daban TIMEOUT
--    Mira el plan: si ves "Seq Scan" + "Sort" de cientos de miles de filas,
--    aplica buscador_indices.sql y vuelve a correrlos (deberías ver Index Scan).
-- ===========================================================================

-- B1) P5 — orden por valor_estimado DESC (sin filtros). Es la que más sufre:
--     sin índice adecuado, ordena las ~588k filas enteras.
explain (analyze, buffers)
select licitacion_id, titulo, valor_estimado
from public.licitaciones
order by valor_estimado desc nulls last, licitacion_id asc
limit 25;

-- B2) P3 — full-text ordenado por fecha_fin_plazo. Debe usar el GIN(tsv) para
--     filtrar y luego ordenar el subconjunto (rápido si el match no es enorme).
explain (analyze, buffers)
select licitacion_id, titulo, fecha_fin_plazo
from public.licitaciones
where tsv @@ websearch_to_tsquery('spanish', 'climatizacion')
order by fecha_fin_plazo asc nulls last, licitacion_id asc
limit 25;

-- B3) Pantalla de entrada (sin filtros, abiertas, orden fin de plazo asc).
explain (analyze, buffers)
select licitacion_id, titulo, fecha_fin_plazo
from public.licitaciones
where fecha_fin_plazo >= now() or fecha_fin_plazo is null
order by fecha_fin_plazo asc nulls last, licitacion_id asc
limit 25;

-- ===========================================================================
-- C) ccaa / lugar_ejecucion: confirmado a 0 -> filtros desactivados en BG-3.
--    Extraer la región del CODICE queda como tarea futura del pipeline.
-- ===========================================================================
select
  count(*) filter (where ccaa is not null)            as ccaa_no_null,
  count(*) filter (where lugar_ejecucion is not null) as lugar_no_null,
  count(*)                                             as total
from public.licitaciones;
