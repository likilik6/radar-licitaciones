-- ============================================================================
-- Fase E · COMPETENCIA — tabla agregada public.competidores (por CIF)
--
-- Agrega public.adjudicaciones por cif_adjudicatario para la vista web "Competencia"
-- (buscar un adjudicatario por nombre o CIF y ver qué gana). Decisión de arquitectura
-- MEDIDA (EXPLAIN ANALYZE, 08/07/2026):
--   · Buscar por NOMBRE al vuelo sobre adjudicaciones (535k) = Seq Scan ~1.2 s -> NO.
--   · Agregar TODO por CIF = ~3.5 s (se hace 1 vez y en el --diario) -> tabla agregada.
--   · Ficha por CIF exacto = 0,16 ms con el índice de D1 -> PostgREST directo, sin RPC.
-- Resultado: 77.6k CIF válidos. Búsqueda por nombre = ILIKE sobre nombre_busqueda
-- (todas las variantes del nombre, unaccent+upper) con índice GIN pg_trgm.
--
-- CLAVE del nombre: un mismo CIF publica con MUCHAS variantes de nombre (CRIOGES tiene
-- 10; solo UNA contiene "CRIOGES"). Por eso nombre_busqueda concatena TODAS las
-- variantes: así "crioges" encuentra el CIF aunque el nombre canónico sea otro.
--
-- PRIVADA (igual que adjudicaciones/decisiones): lectura solo authenticated. La ingesta
-- (service_role) refresca vía la función refrescar_competidores().
--
-- CÓMO USARLO: pega TODO en el SQL Editor de Supabase y pulsa Run. Idempotente
-- (create if not exists / create or replace). Al final hace una primera carga.
--
-- NOTA extensiones: pg_trgm y unaccent viven en el schema `extensions` en este proyecto
-- (verificado). Por eso el índice GIN necesita `extensions` en el search_path y la
-- función cualifica `extensions.unaccent`.
-- ============================================================================

create extension if not exists pg_trgm  with schema extensions;
create extension if not exists unaccent with schema extensions;

-- ----------------------------------------------------------------------------
-- 1) Tabla · una fila por CIF
-- ----------------------------------------------------------------------------
create table if not exists public.competidores (
  cif                   text primary key,           -- CIF NORMALIZADO (mayúsculas, sin separadores)
  nombre_canonico       text,                        -- nombre para MOSTRAR (el más frecuente)
  nombre_busqueda       text,                        -- TODAS las variantes, unaccent+upper (para ILIKE)
  n_lotes               integer not null default 0,  -- filas de adjudicaciones (lotes ganados)
  n_expedientes         integer not null default 0,  -- licitacion_id distintos
  importe_total_sin_iva numeric,                     -- Σ importe_sin_iva
  pct_una_oferta        numeric,                     -- % de lotes con n_ofertas=1 (señal de exclusividad)
  primera_adjudicacion  date,
  ultima_adjudicacion   date,
  updated_at            timestamptz default now()
);

-- ----------------------------------------------------------------------------
-- 2) Índices · trgm para ILIKE por nombre; importe para el top-20 (cif ya es PK)
-- ----------------------------------------------------------------------------
-- El opclass gin_trgm_ops vive en `extensions`: lo ponemos en el search_path SOLO
-- para el create index (el `set` es de sesión; el SQL Editor lo respeta en este script).
set search_path = public, extensions, pg_catalog;
create index if not exists competidores_nombre_trgm on public.competidores using gin (nombre_busqueda gin_trgm_ops);
reset search_path;
create index if not exists competidores_importe_idx on public.competidores (importe_total_sin_iva desc nulls last);

-- ----------------------------------------------------------------------------
-- 3) Permisos + RLS · PRIVADO (solo authenticated lee)
-- ----------------------------------------------------------------------------
revoke all on public.competidores from anon;
grant select on public.competidores to authenticated;

alter table public.competidores enable row level security;
drop policy if exists "competidores_select_authenticated" on public.competidores;
create policy "competidores_select_authenticated" on public.competidores
  for select to authenticated using (true);

-- La ingesta (service_role) refresca la tabla: necesita escribir y truncar.
grant select, insert, update, delete, truncate on public.competidores to service_role;

-- ----------------------------------------------------------------------------
-- 4) Función de refresco (rebuild completo, ~3.5 s). La llama el pipeline en el
--    --diario. SECURITY DEFINER (corre como owner): así service_role solo necesita
--    EXECUTE. TRUNCATE+INSERT es TRANSACCIONAL: los lectores ven la versión ANTERIOR
--    hasta el commit (nunca la tabla vacía). Devuelve el nº de filas (CIF) resultante.
-- ----------------------------------------------------------------------------
create or replace function public.refrescar_competidores()
returns integer
language plpgsql
security definer
set search_path = public, extensions, pg_catalog
as $$
declare n integer;
begin
  truncate public.competidores;
  insert into public.competidores (
    cif, nombre_canonico, nombre_busqueda, n_lotes, n_expedientes,
    importe_total_sin_iva, pct_una_oferta, primera_adjudicacion, ultima_adjudicacion, updated_at)
  select a.cif_adjudicatario,
         mode() within group (order by a.adjudicatario),                       -- nombre más frecuente
         extensions.unaccent(upper(string_agg(distinct coalesce(a.adjudicatario, ''), ' '))),  -- variantes
         count(*),
         count(distinct a.licitacion_id),
         sum(a.importe_sin_iva),
         round(100.0 * count(*) filter (where a.n_ofertas = 1) / nullif(count(*), 0), 1),
         min(a.fecha_adjudicacion),
         max(a.fecha_adjudicacion),
         now()
  from public.adjudicaciones a
  where a.cif_adjudicatario <> ''                    -- desiertos fuera
    and a.cif_adjudicatario ~ '^[A-Z0-9]+$'          -- CIF basura de origen fuera (documentado en D1)
  group by a.cif_adjudicatario;
  get diagnostics n = row_count;
  analyze public.competidores;
  return n;
end;
$$;

revoke all on function public.refrescar_competidores() from public, anon;
grant execute on function public.refrescar_competidores() to service_role;

-- ----------------------------------------------------------------------------
-- 5) Primera carga (para no esperar al primer --diario).
-- ----------------------------------------------------------------------------
select public.refrescar_competidores() as competidores_cargados;
