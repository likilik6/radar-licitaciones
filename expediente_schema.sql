-- expediente_schema.sql  ·  Buscar por Nº de expediente en el catálogo
-- ============================================================================
-- Añade a public.licitaciones el Nº de expediente del órgano (cbc:ContractFolderID
-- del CODICE, extraído por feeds.extrae_entrada) y el índice para buscarlo por
-- CONTENIDO e ignorando separadores ("12/2026" == "12-2026" == "12.2026").
--
-- Pega TODO en el SQL Editor de Supabase y pulsa "Run". Es IDEMPOTENTE
-- (if not exists / create or replace): re-ejecutar no rompe nada.
--
-- La columna es ADITIVA: hereda el GRANT (select a authenticated) y la RLS de la
-- tabla; no hace falta tocar permisos. La rellena el backfill/ingesta (upsert por
-- licitacion_id) reutilizando el extractor común.
-- ============================================================================

-- 1) Columna nueva (text; null si el feed no la trae). Se guarda TAL CUAL.
alter table public.licitaciones add column if not exists num_expediente text;

-- 2) pg_trgm (ya suele estar por el CPV-prefijo; idempotente).
create extension if not exists pg_trgm with schema extensions;

-- 3) Normalizador IMMUTABLE: MAYÚSCULAS + quita espacios, '/', '.' y '-'. Se usa
--    EXACTAMENTE IGUAL en el índice (paso 4) y en la RPC (buscador_rpc.sql), para
--    que "V/0013/A/26/2" y "v0013a262" casen. IMMUTABLE + total (coalesce) para
--    poder indexarla como expresión.
create or replace function public.norm_expediente(p text)
returns text
language sql
immutable
parallel safe
set search_path = pg_catalog
as $$ select regexp_replace(upper(coalesce(p, '')), '[[:space:]/.-]', '', 'g') $$;

-- 4) Índice GIN trigram sobre la expresión NORMALIZADA -> LIKE '%frag%' (contiene,
--    case-insensitive) usa índice (Bitmap Index Scan) para fragmentos de >=3 chars.
--    OJO: construirlo sobre ~588k puede tardar 1-2 min; si el editor da "Failed to
--    fetch" suele haberse creado igual (compruébalo en el panel). Para no bloquear
--    la tabla se puede lanzar SUELTO como:
--      create index concurrently licitaciones_expediente_trgm on public.licitaciones
--        using gin (public.norm_expediente(num_expediente) extensions.gin_trgm_ops);
create index if not exists licitaciones_expediente_trgm
  on public.licitaciones
  using gin (public.norm_expediente(num_expediente) extensions.gin_trgm_ops);

-- 5) Estadísticas al día (el conteo/plan del buscador dependen de buenas estimaciones).
analyze public.licitaciones;

-- Nota: el filtro de expediente NO cambia GRANT/RLS ni la lógica de purga; la RPC
-- que lo usa (buscador_rpc.sql) sigue siendo SECURITY INVOKER (respeta la RLS).
