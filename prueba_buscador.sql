-- prueba_buscador.sql  ·  PASO 5 de BG-3: SQL equivalente a cada buscar(...)
-- Para cuadrar el "total" que devuelve buscador_api.js. Ejecuta en el SQL Editor
-- (ve todas las filas; la RLS no aplica al rol del editor, y la política es
-- using(true), así que coincide con lo que ve un usuario authenticated).
--
-- OJO tildes: el módulo ya manda el término SIN tildes (unaccent se aplicó al
-- guardar el tsv), por eso aquí usamos 'climatizacion' a secas, no hace falta
-- unaccent() en la query. Corre P1/P7 casi a la vez que la consola: usan now().

-- P1: texto=aire, estado=abierta, importeMax=200000
select count(*) as p1_total
from public.licitaciones
where tsv @@ websearch_to_tsquery('spanish', 'aire')
  and valor_estimado <= 200000
  and (fecha_fin_plazo >= now() or fecha_fin_plazo is null);

-- P3: texto=climatizacion  (estado default 'todas' porque hay filtro de texto)
-- Debe coincidir con buscar('climatización') y con buscar('climatizacion').
select count(*) as p3_total
from public.licitaciones
where tsv @@ websearch_to_tsquery('spanish', 'climatizacion');

-- P4: cpv overlaps ['90920000']  (estado default 'todas')
select count(*) as p4_total
from public.licitaciones
where cpv && array['90920000']::text[];

-- P5: estado='todas', sin más filtros -> total = todo el catálogo (~587k)
select count(*) as p5_total
from public.licitaciones;

-- EXTRA: población de ccaa / lugar_ejecucion (decide si esos filtros sirven)
select
  count(*) filter (where ccaa is not null)            as ccaa_no_null,
  count(*) filter (where lugar_ejecucion is not null) as lugar_no_null,
  count(*)                                             as total
from public.licitaciones;
