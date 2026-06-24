-- ============================================================================
-- radar_config: la configuración del radar (qué CPVs/fuentes/territorios caza,
-- y los ajustes de vista del panel). UNA sola fila, con una columna JSON flexible.
--
-- CÓMO USARLO: pega TODO este archivo en el editor SQL de tu proyecto Supabase
-- (SQL Editor -> New query) y pulsa "Run". Es de UNA sola vez. A partir de aquí,
-- todos los ajustes se tocan desde el panel web (⚙️), nunca más desde SQL.
--
-- Es seguro re-ejecutarlo: usa "if not exists" / "on conflict" / "drop policy if
-- exists", así que no rompe nada si ya estaba.
-- ============================================================================

-- 1) La tabla. Una columna JSON (config) donde cabe cualquier ajuste, presente o
--    futuro, sin tener que volver a tocar SQL. La restricción id=1 garantiza que
--    solo haya UNA fila (la configuración global del radar).
create table if not exists public.radar_config (
  id         smallint primary key default 1,
  config     jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  constraint radar_config_una_fila check (id = 1)
);

-- 2) Sembramos la fila única (vacía). Si ya existe, no hace nada.
insert into public.radar_config (id, config)
values (1, '{}'::jsonb)
on conflict (id) do nothing;

-- 3) Seguridad a nivel de fila (RLS), igual que tus otras tablas.
alter table public.radar_config enable row level security;

-- LECTURA: para todos. El robot (GitHub Actions) lee la config con la clave
-- publishable (pública). La config NO es secreta: equivale a que tu intereses.yaml,
-- que ya es público, diga qué busca el radar.
drop policy if exists "radar_config_lectura" on public.radar_config;
create policy "radar_config_lectura" on public.radar_config
  for select using (true);

-- ESCRITURA: solo usuarios con sesión (tu login) pueden crear/actualizar la config
-- desde el panel. Un visitante anónimo NO puede cambiarla.
drop policy if exists "radar_config_insert" on public.radar_config;
create policy "radar_config_insert" on public.radar_config
  for insert to authenticated with check (true);

drop policy if exists "radar_config_update" on public.radar_config;
create policy "radar_config_update" on public.radar_config
  for update to authenticated using (true) with check (true);

-- ============================================================================
-- Forma del JSON de "config" (lo rellenará el panel ⚙️; aquí solo de referencia):
--   {
--     "cpv":         ["39811200", "9073"],          -- CPV que caza el radar
--     "fuentes":     ["estatal", "agregadas"],       -- qué feeds leer
--     "plataformas": ["Estado", "Gobierno de Navarra"],  -- "Estado" = estatal
--     "regiones":    ["ES220", "ES300"],             -- códigos NUTS (vacío = todas)
--     "vista": { "ocultar_caducadas": true, "pestana_inicial": "activas",
--                "orden_inicial": "dias", "dias_nuevo": 7 }
--   }
-- Una lista vacía o ausente = "no filtrar por eso" (p.ej. sin "cpv" -> usa intereses.yaml).
-- ============================================================================
