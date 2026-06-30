-- buscador_indices.sql  ·  BG-3: índices para orden + paginación ESTABLE
-- ============================================================================
-- Pega TODO en el SQL Editor de Supabase y pulsa "Run". Idempotente
-- (create index if not exists), no rompe nada al re-ejecutar.
--
-- POR QUÉ: buscar() ordena por un campo + desempate por licitacion_id (PK) para
-- que las páginas no se solapen. Sin un índice que case con ESE orden, Postgres
-- ordena la tabla entera (~588k) -> lento / timeout 57014, sobre todo al ordenar
-- por valor_estimado DESC sin filtros (la prueba P5). Estos índices compuestos
-- llevan ya el desempate, así que el orden sale del índice (Index Scan, 25 filas)
-- en vez de un Sort masivo. Es la solución "índice adecuado", NO subir el
-- statement_timeout.
--
-- NULLS LAST y la dirección coinciden con lo que pide buscador_api.js:
--   .order(campo, { ascending, nullsFirst:false }).order('licitacion_id', asc)
-- El desempate licitacion_id va ASC; por eso el índice principal es para la
-- dirección de orden MÁS habitual de cada campo (la inversa cae a Sort, raro).
-- ============================================================================

-- Orden por importe, mayor primero (P5: valor_estimado DESC, NULLs al final).
create index if not exists licitaciones_valor_desc_id
  on public.licitaciones (valor_estimado desc nulls last, licitacion_id);

-- Orden por fin de plazo, antes primero (DEFAULT del buscador y vista de entrada).
create index if not exists licitaciones_finplazo_id
  on public.licitaciones (fecha_fin_plazo asc nulls last, licitacion_id);

-- Orden por fecha de publicación, más reciente primero (uso habitual).
create index if not exists licitaciones_fechapub_desc_id
  on public.licitaciones (fecha_publicacion desc nulls last, licitacion_id);

-- Refresca estadísticas: el conteo HÍBRIDO de buscar() usa la estimación del
-- planner (count:'planned'); con stats al día acierta mejor el orden de magnitud.
analyze public.licitaciones;

-- ----------------------------------------------------------------------------
-- NOTA (SIN PRISA): los índices de una sola columna de BG-1 quedan redundantes
-- con estos compuestos para el ORDEN (el compuesto sirve también de filtro por
-- la 1ª columna). Cuando quieras recuperar espacio, puedes quitarlos:
--   - licitaciones_valor_idx  (valor_estimado)   <- cubierto por licitaciones_valor_desc_id
--   - licitaciones_fin_plazo  (fecha_fin_plazo)  <- cubierto por licitaciones_finplazo_id
-- OJO: fecha_fin_plazo lo usan también la purga (BG-2c) y el filtro 'abierta';
-- el compuesto sigue sirviendo para esos rangos, así que es seguro. Descomenta
-- cuando lo decidas (no corre prisa):
--   drop index if exists public.licitaciones_valor_idx;
--   drop index if exists public.licitaciones_fin_plazo;
-- (licitaciones_fuente_idx y licitaciones_cpv_gin NO se tocan: no hay compuesto
--  que los reemplace.)
-- ----------------------------------------------------------------------------
