-- ============================================================================
-- BG-2c · PURGA de la ventana de 3 años del catálogo (public.licitaciones)
--
-- Crea la función public.purga_catalogo(anios, simular) que el cron llama por RPC.
-- Borra las licitaciones publicadas hace MÁS de 'anios' años, EXCEPTO:
--   (a) las que sigan ABIERTAS   → fecha_fin_plazo >= ahora
--   (b) las que estén en la cartera → existen en public.contratos (por licitacion_id)
--   (c) las marcadas/seguidas por mí → existen en public.decisiones (por licitacion_id)
--
-- Con simular = true NO borra: solo devuelve cuántas filas se borrarían (para
-- revisar antes de activar). Devuelve el nº de filas afectadas.
--
-- CÓMO USARLO: pega TODO en el SQL Editor de Supabase y pulsa Run (idempotente).
-- Previsualizar SIN borrar:   select public.purga_catalogo(3, true);
-- Purga real:                 select public.purga_catalogo(3, false);
--
-- SECURITY DEFINER: corre con los privilegios del propietario (postgres) para poder
-- leer public.contratos y borrar saltando RLS de forma controlada; search_path fijo
-- por seguridad. Solo la puede ejecutar service_role (el backend), nunca anon.
-- ============================================================================

create or replace function public.purga_catalogo(anios int default 3, simular boolean default false)
returns integer
language plpgsql
security definer
set search_path = public, pg_catalog
as $$
declare
  afectadas integer;
begin
  if simular then
    -- Solo CUENTA lo que se borraría (no toca nada).
    select count(*) into afectadas
    from public.licitaciones l
    where l.fecha_publicacion < (now() - make_interval(years => anios))     -- más viejo que la ventana
      and (l.fecha_fin_plazo is null or l.fecha_fin_plazo < now())          -- NO abiertas (a)
      and not exists (                                                       -- NO en la cartera (b)
        select 1 from public.contratos c where c.licitacion_id = l.licitacion_id
      )
      and not exists (                                                       -- NO marcadas/seguidas (c)
        select 1 from public.decisiones d where d.licitacion_id = l.licitacion_id
      );
    return afectadas;
  end if;

  with eliminadas as (
    delete from public.licitaciones l
    where l.fecha_publicacion < (now() - make_interval(years => anios))
      and (l.fecha_fin_plazo is null or l.fecha_fin_plazo < now())
      and not exists (
        select 1 from public.contratos c where c.licitacion_id = l.licitacion_id
      )
      and not exists (
        select 1 from public.decisiones d where d.licitacion_id = l.licitacion_id
      )
    returning 1
  )
  select count(*) into afectadas from eliminadas;
  return afectadas;
end;
$$;

-- Solo el backend (service_role) puede ejecutarla. Nada para anon/public.
revoke all on function public.purga_catalogo(int, boolean) from public, anon;
grant execute on function public.purga_catalogo(int, boolean) to service_role;

-- ----------------------------------------------------------------------------
-- NOTAS sobre las condiciones (léelas antes de activar):
--   · fecha_publicacion NULL → la fila NO se borra (no se puede datar; se conserva).
--   · fecha_fin_plazo NULL → cuenta como NO abierta: si además es vieja, se borra.
--     (la excepción (a) es literalmente "fecha_fin_plazo >= hoy"; NULL no lo cumple.)
--   · Protegidas (no se borran nunca, aunque sean viejas): (b) lo que esté en
--     public.contratos y (c) lo marcado/seguido en public.decisiones, ambas por
--     licitacion_id. La función depende de que esas dos tablas existan.
-- ----------------------------------------------------------------------------
