-- buscador_indices.sql  ·  BG-3/BG-4: índices y objetos de apoyo del buscador
-- ============================================================================
-- Pega TODO en el SQL Editor de Supabase y pulsa "Run". Idempotente
-- (create ... if not exists / create or replace), no rompe nada al re-ejecutar.
--
-- ORDEN DE DESPLIEGUE (BG-4 inc2): ejecuta ESTE fichero ANTES que buscador_rpc.sql
-- (la RPC referencia public.cpv_texto y se apoya en estos índices). Si lo corres
-- después, la RPC se crea igual pero irá lenta hasta que existan los índices.
--
-- POR QUÉ (BG-3): buscar() ordena por un campo + desempate por licitacion_id (PK)
-- para que las páginas no se solapen. Sin un índice que case con ESE orden,
-- Postgres ordena la tabla entera (~588k) -> lento / timeout 57014. Estos índices
-- compuestos llevan ya el desempate, así que el orden sale del índice (Index Scan,
-- 25 filas) en vez de un Sort masivo.
--
-- NOVEDADES BG-4 inc2 (rendimiento de términos/prefijos AMPLIOS):
--   · pg_trgm + índice GIN trigram sobre cpv_texto(cpv) para que el CPV por
--     PREFIJO ("empieza por") use índice en vez de escanear las 588k con unnest+LIKE.
--   · Los 3 índices de orden en la dirección INVERSA (fin desc, importe asc,
--     publicación asc) que faltaban: la RPC amplia obtiene la página por Index Scan
--     del índice de orden + filtro (primeras 25 rápidas) y necesita las 6 combinaciones.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 0) Índices de ORDEN (campo + desempate licitacion_id). NULLS LAST y dirección
--    coinciden EXACTAMENTE con el ORDER BY de buscar()/RPC. Las 6 combinaciones
--    (3 campos x 2 direcciones) para que CUALQUIER orden salga del índice.
-- ----------------------------------------------------------------------------
-- Fin de plazo (DEFAULT del buscador): asc = "antes primero"; desc = inversa.
create index if not exists licitaciones_finplazo_id
  on public.licitaciones (fecha_fin_plazo asc nulls last, licitacion_id asc);
create index if not exists licitaciones_finplazo_desc_id
  on public.licitaciones (fecha_fin_plazo desc nulls last, licitacion_id asc);

-- Importe (valor_estimado): desc = "mayor primero" (P5); asc = inversa.
create index if not exists licitaciones_valor_desc_id
  on public.licitaciones (valor_estimado desc nulls last, licitacion_id asc);
create index if not exists licitaciones_valor_asc_id
  on public.licitaciones (valor_estimado asc nulls last, licitacion_id asc);

-- Fecha de publicación: desc = "más reciente primero"; asc = inversa.
create index if not exists licitaciones_fechapub_desc_id
  on public.licitaciones (fecha_publicacion desc nulls last, licitacion_id asc);
create index if not exists licitaciones_fechapub_asc_id
  on public.licitaciones (fecha_publicacion asc nulls last, licitacion_id asc);

-- ----------------------------------------------------------------------------
-- 1) CPV POR PREFIJO (BG-4 inc2): trigram sobre una representación textual del
--    array. array_to_string(cpv,' ') anclaría solo al PRIMER código, así que
--    cpv_texto() antepone un espacio a CADA código: " 90920000 90733000". Así
--    "empieza por 9073" = la cadena CONTIENE " 9073" (LIKE '% 9073%'), y eso lo
--    acelera el índice GIN trigram (patrón por contención). Ver buscador_rpc.sql.
-- ----------------------------------------------------------------------------
create extension if not exists pg_trgm with schema extensions;

-- IMMUTABLE + STRICT: apta para índice de expresión (debe coincidir EXACTAMENTE
-- con la expresión que usa la RPC). El espacio inicial marca el arranque de cada
-- código (incluido el primero).
create or replace function public.cpv_texto(p text[])
returns text
language sql
immutable
parallel safe
set search_path = pg_catalog
as $$ select ' ' || array_to_string(coalesce(p, '{}'::text[]), ' ') $$;

-- Índice GIN trigram sobre la expresión. OJO: construirlo sobre ~588k puede tardar
-- 1-2 min; si el editor muestra "Failed to fetch" suele haberse creado igual
-- (comprueba con \di o en el panel). Si prefieres no bloquear la tabla, ejecútalo
-- SUELTO (sin nada más seleccionado) como:
--   create index concurrently licitaciones_cpv_trgm on public.licitaciones
--     using gin (public.cpv_texto(cpv) extensions.gin_trgm_ops);
create index if not exists licitaciones_cpv_trgm
  on public.licitaciones using gin (public.cpv_texto(cpv) extensions.gin_trgm_ops);

-- ----------------------------------------------------------------------------
-- 2) Estadísticas al día (el conteo del buscador y la elección de plan dependen
--    de buenas estimaciones del planner).
-- ----------------------------------------------------------------------------
analyze public.licitaciones;

-- ----------------------------------------------------------------------------
-- NOTA (SIN PRISA): los índices de una sola columna de BG-1 quedan redundantes
-- con los compuestos para el ORDEN. Cuando quieras recuperar espacio:
--   drop index if exists public.licitaciones_valor_idx;   -- cubierto por _valor_(desc|asc)_id
--   drop index if exists public.licitaciones_fin_plazo;   -- cubierto por _finplazo(_desc)_id
-- (licitaciones_fuente_idx y licitaciones_cpv_gin NO se tocan: el cpv_gin sirve al
--  CPV EXACTO por overlaps `&&`; el trigram es para el PREFIJO.)
-- ----------------------------------------------------------------------------
