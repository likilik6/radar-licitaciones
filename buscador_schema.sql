-- ============================================================================
-- BUSCADOR GENERAL DE LICITACIONES  ·  BG-1: solo el modelo de datos
--
-- Tabla única public.licitaciones para el buscador PRIVADO (estatal 643 +
-- agregadas 1044). SEPARADO del Radar: data/licitaciones.json sigue siendo el
-- destilado estático; esto es la base consultable sobre Supabase.
--
-- PRIVADO: lectura solo tras login (rol authenticated). El anónimo no ve nada.
--
-- CÓMO USARLO: pega TODO este archivo en el SQL Editor de Supabase
-- (SQL Editor -> New query) y pulsa "Run". Es idempotente: usa
-- "if not exists" / "create or replace" / "drop ... if exists", así que se
-- puede re-ejecutar sin romper nada.
--
-- NOTA (regla post-mayo-2026): toda tabla nueva en public necesita GRANT
-- explícito al rol Y política RLS; ninguna por separado basta. Aquí están las dos.
-- ============================================================================

-- 0) unaccent para que "Licitación" y "licitacion" casen en la búsqueda.
--    En Supabase las extensiones viven en el esquema "extensions".
create extension if not exists unaccent with schema extensions;

-- ----------------------------------------------------------------------------
-- 1) Tabla
-- ----------------------------------------------------------------------------
create table if not exists public.licitaciones (
  licitacion_id        text primary key,          -- entry.id del feed ATOM (clave universal)
  titulo               text,
  objeto               text,
  organo_contratacion  text,
  cpv                  text[],                     -- lista de códigos CPV
  fuente               text,                       -- 'estatal' | 'agregadas' (ver check abajo)
  presupuesto_con_iva  numeric,
  presupuesto_sin_iva  numeric,
  valor_estimado       numeric,
  fecha_publicacion    timestamptz,
  fecha_fin_plazo      timestamptz,
  lugar_ejecucion      text,
  ccaa                 text,
  enlace               text,
  primera_vez          timestamptz default now(), -- primera vez que la vimos en el feed
  ultima_vez           timestamptz default now(), -- última vez que la vimos / refrescamos
  tsv                  tsvector,                   -- índice de texto (lo rellena el trigger de abajo)
  constraint licitaciones_fuente_chk check (fuente in ('estatal','agregadas'))
);

-- ----------------------------------------------------------------------------
-- 2) tsv por TRIGGER (no columna generada: unaccent() es STABLE y una columna
--    generada exige expresión inmutable). Pesos: titulo=A, objeto=B, organo=C.
--    Config 'spanish' + unaccent. search_path fijo en la función como buena
--    práctica de seguridad y para resolver unaccent esté donde esté instalado.
-- ----------------------------------------------------------------------------
create or replace function public.licitaciones_tsv_update()
returns trigger
language plpgsql
set search_path = extensions, public, pg_catalog
as $$
begin
  -- En upserts que solo refrescan ultima_vez no recalculamos el tsv (es caro).
  if (tg_op = 'UPDATE'
      and new.titulo              is not distinct from old.titulo
      and new.objeto              is not distinct from old.objeto
      and new.organo_contratacion is not distinct from old.organo_contratacion) then
    return new;
  end if;

  new.tsv :=
      setweight(to_tsvector('spanish', unaccent(coalesce(new.titulo,              ''))), 'A')
   || setweight(to_tsvector('spanish', unaccent(coalesce(new.objeto,              ''))), 'B')
   || setweight(to_tsvector('spanish', unaccent(coalesce(new.organo_contratacion, ''))), 'C');
  return new;
end;
$$;

drop trigger if exists licitaciones_tsv_trg on public.licitaciones;
create trigger licitaciones_tsv_trg
  before insert or update on public.licitaciones
  for each row execute function public.licitaciones_tsv_update();

-- ----------------------------------------------------------------------------
-- 3) Índices
-- ----------------------------------------------------------------------------
create index if not exists licitaciones_tsv_gin    on public.licitaciones using gin (tsv);
create index if not exists licitaciones_cpv_gin    on public.licitaciones using gin (cpv);
create index if not exists licitaciones_fin_plazo  on public.licitaciones (fecha_fin_plazo);
create index if not exists licitaciones_fuente_idx on public.licitaciones (fuente);
create index if not exists licitaciones_valor_idx  on public.licitaciones (valor_estimado);

-- ----------------------------------------------------------------------------
-- 4) Permisos + RLS  ·  PRIVADO (solo authenticated lee)
-- ----------------------------------------------------------------------------
-- Nada para el anónimo (defensa en profundidad; por defecto ya no hay grants).
revoke all on public.licitaciones from anon;

-- Lectura para usuarios con sesión.
grant select on public.licitaciones to authenticated;

alter table public.licitaciones enable row level security;
drop policy if exists "licitaciones_select_authenticated" on public.licitaciones;
create policy "licitaciones_select_authenticated" on public.licitaciones
  for select to authenticated using (true);

-- ----------------------------------------------------------------------------
-- 5) Escritura (INGESTA) — BG-2: corre con la service_role key (backfill_catalogo.py
--    y, después, el cron). service_role SALTA la RLS, pero (regla post-mayo-2026)
--    necesita GRANT EXPLÍCITO de privilegios; el bypass de RLS no basta por sí solo.
--    Idempotente: re-ejecutar este grant no rompe nada.
grant select, insert, update on public.licitaciones to service_role;
-- ----------------------------------------------------------------------------

-- ============================================================================
-- NOTA (30/10/2026): QUÉ DOCUMENTA ESTE FICHERO Y QUÉ NO
--
-- Es el esquema de BG-1 (15/07/2026) tal como se ejecutó, y la única definición en
-- el repo de la tabla, su trigger del tsv, la RLS y la política de lectura. La nota
-- va al final para no mover las líneas que cita ccaa_schema.sql:5 (38-39).
--
-- * No hace falta volver a ejecutarlo: ya está aplicado. Si se ejecuta, es
--   idempotente, pero rehace la función y el disparador del tsv (no recalcula filas;
--   la función de la base tiene la misma lógica sin el comentario de la línea 59) y
--   la política de lectura.
--
-- * La tabla de hoy tiene 21 columnas y 18 índices. Lo que falta aquí lo añaden:
--     num_expediente + licitaciones_expediente_trgm ........ expediente_schema.sql
--     estado_adjudicacion, n_lotes_desiertos + 2 índices ... desiertas_schema.sql
--     nuts_codigo + licitaciones_ccaa_idx .................. ccaa_schema.sql
--     6 índices de orden y licitaciones_cpv_trgm ........... buscador_indices.sql
--     licitaciones_fecha_pub_idx ........................... purga_catalogo.sql
--
-- * Permisos: los que Supabase da por defecto, más amplios que los grants de los
--   puntos 4 y 5. Por la API, authenticated solo lee y anon no tiene acceso. Ajustarlos
--   sería un cambio de permisos aparte.
-- ============================================================================
