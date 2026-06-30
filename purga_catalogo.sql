-- ============================================================================
-- BG-2c · PURGA de la ventana de 3 años del catálogo (public.licitaciones)
--
-- Crea la función public.purga_catalogo(anios, simular, lote). Borra las
-- licitaciones publicadas hace MÁS de 'anios' años, EXCEPTO:
--   (a) las que sigan ABIERTAS   → fecha_fin_plazo >= ahora
--   (b) las que estén en la cartera → existen en public.contratos (por licitacion_id)
--   (c) las marcadas/seguidas por mí → existen en public.decisiones (por licitacion_id)
--
-- POR LOTES (clave para no chocar con el statement_timeout de PostgREST): cada
-- llamada borra COMO MUCHO 'lote' filas (rápida) y devuelve cuántas borró. El
-- cliente (backfill_catalogo.py --purgar) la llama EN BUCLE hasta que devuelve 0.
-- Un único DELETE de decenas de miles de filas vía RPC daba timeout (57014).
--
-- Con simular = true NO borra: devuelve el TOTAL de filas que se borrarían.
--
-- CÓMO USARLO: pega TODO en el SQL Editor de Supabase y pulsa Run (idempotente).
-- Previsualizar (total, sin borrar):  select public.purga_catalogo(3, true);
-- Borrar UNA tanda de 5.000:          select public.purga_catalogo(3, false, 5000);
-- Borrar TODO desde el editor, en tandas (bucle):
--   do $$ declare n int; begin
--     loop select public.purga_catalogo(3, false, 5000) into n;
--          raise notice 'tanda: %', n; exit when n = 0; end loop;
--   end $$;
--
-- SECURITY DEFINER: corre con los privilegios del propietario para leer
-- public.contratos/decisiones y borrar saltando RLS de forma controlada;
-- search_path fijo por seguridad. Solo service_role puede ejecutarla.
-- ============================================================================

-- Índice por fecha_publicacion: la purga filtra por esta columna y BG-1 no la
-- indexó. Sin él, cada tanda/cuenta recorre la tabla entera. Idempotente.
create index if not exists licitaciones_fecha_pub_idx on public.licitaciones (fecha_publicacion);

-- Quitamos la versión anterior (2 argumentos, DELETE único) para no dejar overloads.
drop function if exists public.purga_catalogo(int, boolean);

create or replace function public.purga_catalogo(
  anios   int     default 3,
  simular boolean default false,
  lote    int     default 5000
)
returns integer
language plpgsql
security definer
set search_path = public, pg_catalog
as $$
declare
  afectadas integer;
begin
  -- Red de seguridad (la protección real es el borrado por lotes + bucle cliente).
  set local statement_timeout = '300s';

  if simular then
    -- Cuenta TODAS las que se borrarían (no toca nada).
    select count(*) into afectadas
    from public.licitaciones l
    where l.fecha_publicacion < (now() - make_interval(years => anios))     -- más viejo que la ventana
      and (l.fecha_fin_plazo is null or l.fecha_fin_plazo < now())          -- NO abiertas (a)
      and not exists (select 1 from public.contratos  c where c.licitacion_id = l.licitacion_id)   -- (b)
      and not exists (select 1 from public.decisiones d where d.licitacion_id = l.licitacion_id);  -- (c)
    return afectadas;
  end if;

  -- Borra COMO MUCHO 'lote' filas que cumplan las condiciones, en UNA tanda.
  delete from public.licitaciones
  where ctid in (
    select l.ctid
    from public.licitaciones l
    where l.fecha_publicacion < (now() - make_interval(years => anios))
      and (l.fecha_fin_plazo is null or l.fecha_fin_plazo < now())
      and not exists (select 1 from public.contratos  c where c.licitacion_id = l.licitacion_id)
      and not exists (select 1 from public.decisiones d where d.licitacion_id = l.licitacion_id)
    limit lote
  );
  get diagnostics afectadas = row_count;
  return afectadas;   -- 0 ⇒ ya no quedan; el cliente para el bucle
end;
$$;

-- Solo el backend (service_role) puede ejecutarla. Nada para anon/public.
revoke all on function public.purga_catalogo(int, boolean, int) from public, anon;
grant execute on function public.purga_catalogo(int, boolean, int) to service_role;

-- Red de seguridad a NIVEL DE ROL (esto sí es efectivo): sube el statement_timeout
-- del rol que usa el backend. El `set local` de dentro de la función NO basta —el
-- contador del timeout se arma ANTES de entrar en la función, así que un único
-- statement largo expira igual; por eso el troceado real va en el cliente (con LIMIT).
-- Con esto, además, la simulación (count) y cualquier tanda holgada tienen margen.
alter role service_role set statement_timeout = '120s';

-- ----------------------------------------------------------------------------
-- NOTAS sobre las condiciones (léelas antes de activar):
--   · fecha_publicacion NULL → la fila NO se borra (no se puede datar; se conserva).
--   · fecha_fin_plazo NULL → cuenta como NO abierta: si además es vieja, se borra.
--     (la excepción (a) es literalmente "fecha_fin_plazo >= hoy"; NULL no lo cumple.)
--   · Protegidas (no se borran nunca, aunque sean viejas): (b) lo que esté en
--     public.contratos y (c) lo marcado/seguido en public.decisiones, ambas por
--     licitacion_id. La función depende de que esas dos tablas existan.
-- ----------------------------------------------------------------------------
