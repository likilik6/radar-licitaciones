-- ============================================================================
-- refrescos_y_desiertas.sql · Que los agregados se refresquen sin pasarse de tiempo
--                             y que la web sepa si están al día
-- 21/09/2026
--
-- POR QUÉ
--   public.refrescar_desiertas lleva sin ejecutarse desde el 17/09 y solo se veía en el log
--   de Actions. Medido el 21/09 con la base tranquila:
--     · La RPC REAGREGA LAS 605.316 ADJUDICACIONES ENTERAS en cada llamada. El p_lote de
--       5.000 limita lo que ESCRIBE, no lo que LEE.
--     · Esa lectura cuesta 20,4 s: 105.731 bloques de disco y 26.835 bloques TEMPORALES
--       (el agregado no cabe en los 3,5 MB de work_mem y se va a disco).
--     · Hay 14.556 licitaciones pendientes -> 3 tandas + una final = 4 llamadas, ~80-120 s
--       de lectura repetida. El tope de service_role son 120 s, así que con el servidor
--       ocupado (justo después de la ingesta) una llamada lo roza o lo pasa... y cada
--       reintento vuelve a pagar el escaneo entero. Por eso se rinde.
--
-- QUÉ SE HACE
--   1) La RPC acepta p_dias: solo recalcula las licitaciones cuyas adjudicaciones se han
--      tocado en esos días. Medido: 4,4-6,9 s (0,14 s en caliente) frente a 20,4 s.
--      Con p_dias = 0 o null hace el ESCANEO COMPLETO de siempre, intacto para el backfill.
--      La ventana hace de red de seguridad SIN cola de pendientes: si un día falla, al día
--      siguiente esos ids siguen dentro de la ventana (7 días = una semana de margen).
--   2) public.refrescos: una fila por agregado con el ÚLTIMO INTENTO, haya ido bien o mal.
--      Así la web puede decir «último intento: falló el 17» en vez de enseñar datos viejos
--      como si estuvieran al día. Lo escribe el cargador (backfill_catalogo.py), que es
--      quien se entera de los fallos: si la RPC se pasa de tiempo, su transacción se
--      deshace y no podría dejar constancia de nada.
--
-- CÓMO: en el SQL Editor, CADA PASO SOLO, en una pestaña LIMPIA. Es idempotente.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- PASO 1 · La tabla de refrescos (3 filas: desiertas, competidores, cobertura)
-- ---------------------------------------------------------------------------
create table if not exists public.refrescos (
  clave     text        not null primary key,
  intento   timestamptz not null,
  ok        boolean     not null,
  ultimo_ok timestamptz,
  filas     bigint,
  detalle   jsonb,
  error     text
);

comment on table public.refrescos is
  'Último intento de refresco de cada agregado (desiertas, competidores, menores_cobertura), haya ido bien o mal. Lo escribe el cargador con refresco_marca(); la web solo lo lee.';
comment on column public.refrescos.clave is
  'Qué agregado: desiertas | competidores | menores_cobertura.';
comment on column public.refrescos.intento is
  'Cuándo se intentó por última vez, SALGA BIEN O MAL.';
comment on column public.refrescos.ok is
  'Si ese último intento salió bien. En false, los datos del agregado son los de ultimo_ok.';
comment on column public.refrescos.ultimo_ok is
  'Última vez que el refresco terminó bien: la fecha a la que están los datos de verdad.';
comment on column public.refrescos.filas is
  'Lo que hizo el último intento BUENO (licitaciones actualizadas, CIF agregados...).';
comment on column public.refrescos.error is
  'Mensaje del último intento si falló; null si fue bien.';

alter table public.refrescos enable row level security;

drop policy if exists refrescos_select_authenticated on public.refrescos;
create policy refrescos_select_authenticated
  on public.refrescos for select to authenticated using (true);

-- En Supabase las tablas nuevas de `public` nacen con TODOS los permisos para anon y
-- authenticated (default ACL) y ahí solo las tapa la RLS: se revoca por ROL.
revoke all on public.refrescos from public;
revoke all on public.refrescos from anon, authenticated;
grant select on public.refrescos to authenticated;
grant select, insert, update, delete on public.refrescos to service_role;


-- ---------------------------------------------------------------------------
-- PASO 2 · La función que anota cada intento (solo el cargador puede llamarla)
-- ---------------------------------------------------------------------------
create or replace function public.refresco_marca(
  p_clave   text,
  p_ok      boolean,
  p_filas   bigint  default null,
  p_detalle jsonb   default null,
  p_error   text    default null)
returns void
language plpgsql
security definer
set search_path = public, pg_catalog, pg_temp
as $$
begin
  if p_clave is null or length(trim(p_clave)) = 0 then
    raise exception 'refresco_marca: hace falta una clave';
  end if;
  insert into public.refrescos as r (clave, intento, ok, ultimo_ok, filas, detalle, error)
  values (trim(p_clave), now(), coalesce(p_ok, false),
          case when p_ok then now() end,
          case when p_ok then p_filas end,
          case when p_ok then p_detalle end,
          case when p_ok then null else left(coalesce(p_error, 'sin detalle'), 500) end)
  on conflict (clave) do update
     set intento   = excluded.intento,
         ok        = excluded.ok,
         -- Un fallo NO borra la fecha del último acierto: es la que dice a cuándo están
         -- los datos de verdad.
         ultimo_ok = coalesce(excluded.ultimo_ok, r.ultimo_ok),
         filas     = coalesce(excluded.filas, r.filas),
         detalle   = coalesce(excluded.detalle, r.detalle),
         error     = excluded.error;
end;
$$;

comment on function public.refresco_marca(text, boolean, bigint, jsonb, text) is
  'Anota en public.refrescos el último intento de un agregado. La llama el cargador; la web no puede.';

revoke all on function public.refresco_marca(text, boolean, bigint, jsonb, text) from public;
revoke all on function public.refresco_marca(text, boolean, bigint, jsonb, text) from anon, authenticated;
grant execute on function public.refresco_marca(text, boolean, bigint, jsonb, text) to service_role;

notify pgrst, 'reload schema';


-- ---------------------------------------------------------------------------
-- PASO 3 · La RPC de desiertas, ahora con ventana de días
--   OJO: primero se BORRA la versión de un solo parámetro. Si se dejaran las dos,
--   PostgREST no sabría a cuál llamar («Could not choose the best candidate function»).
-- ---------------------------------------------------------------------------
drop function if exists public.refrescar_desiertas(integer);

create or replace function public.refrescar_desiertas(
  p_lote integer default 0,
  p_dias integer default 7)
returns integer
language plpgsql
volatile
set search_path = public, pg_catalog
as $$
declare
  v_n integer;
begin
  with tocadas as (
    -- La VENTANA. Con p_dias > 0 solo se miran las licitaciones cuyas adjudicaciones se han
    -- escrito en los últimos p_dias días (4,4-6,9 s medidos). Con 0 o null, todas: es el
    -- escaneo completo de siempre (20,4 s), el que hace falta para el backfill y como
    -- repaso de seguridad.
    select distinct a.licitacion_id
      from public.adjudicaciones a
     where p_dias is null or p_dias <= 0
        or a.updated_at >= now() - make_interval(days => p_dias)
  ), calc as (
    -- Para esas licitaciones se cuentan TODAS sus adjudicaciones (no solo las recientes):
    -- el estado depende del conjunto.
    select a.licitacion_id,
           count(*) filter (where a.cif_adjudicatario <> '')                                   as n_gan,
           count(*) filter (where a.cif_adjudicatario =  '' and a.resultado_code =  '3')       as n_des,
           count(*) filter (where a.cif_adjudicatario =  '' and a.resultado_code in ('4','5')) as n_ret
      from public.adjudicaciones a
      join tocadas t on t.licitacion_id = a.licitacion_id
     group by a.licitacion_id
  ), estado as (
    select licitacion_id,
           case when n_gan = 0 and n_des > 0                then 'desierta_total'
                when n_gan > 0 and n_des > 0                then 'desierta_parcial'
                when n_gan = 0 and n_des = 0 and n_ret > 0  then 'retirada'
                when n_gan > 0                              then 'adjudicada'
                else 'sin_ganador' end                      as est,
           n_des::integer                                   as n_des
      from calc
  ), pendientes as (
    -- Solo lo que CAMBIA. El join descarta además las adjudicaciones cuya licitación ya no
    -- está en el catálogo (purgada).
    select e.licitacion_id, e.est, e.n_des
      from estado e
      join public.licitaciones l on l.licitacion_id = e.licitacion_id
     where l.estado_adjudicacion is distinct from e.est
        or l.n_lotes_desiertos   is distinct from e.n_des
     limit (case when p_lote > 0 then p_lote else null end)   -- LIMIT NULL = sin límite
  )
  update public.licitaciones l
     set estado_adjudicacion = p.est,
         n_lotes_desiertos   = p.n_des
    from pendientes p
   where p.licitacion_id = l.licitacion_id;

  get diagnostics v_n = row_count;
  return v_n;
end;
$$;

comment on function public.refrescar_desiertas(integer, integer) is
  'Pone al día licitaciones.estado_adjudicacion / n_lotes_desiertos desde adjudicaciones. p_dias > 0 = solo lo tocado esos días (rápido, uso diario); 0 o null = escaneo completo (backfill y repaso semanal). p_lote > 0 limita las filas escritas por llamada.';

revoke all on function public.refrescar_desiertas(integer, integer) from public;
revoke all on function public.refrescar_desiertas(integer, integer) from anon, authenticated;
grant execute on function public.refrescar_desiertas(integer, integer) to service_role;

notify pgrst, 'reload schema';


-- ---------------------------------------------------------------------------
-- PASO 4 · Comprobación (no escribe nada)
-- ---------------------------------------------------------------------------
-- Que solo queda una versión de la función y con los permisos justos:
select p.oid::regprocedure::text as funcion, p.proacl::text as permisos
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public' and p.proname in ('refrescar_desiertas', 'refresco_marca')
 order by 1;

-- Cuántas licitaciones están pendientes ahora mismo (el 21/09 eran 14.556):
select count(*) as pendientes
  from (select a.licitacion_id,
               count(*) filter (where a.cif_adjudicatario <> '')                                   as n_gan,
               count(*) filter (where a.cif_adjudicatario =  '' and a.resultado_code =  '3')       as n_des,
               count(*) filter (where a.cif_adjudicatario =  '' and a.resultado_code in ('4','5')) as n_ret
          from public.adjudicaciones a group by a.licitacion_id) c
  join public.licitaciones l on l.licitacion_id = c.licitacion_id
 where l.estado_adjudicacion is distinct from
       (case when c.n_gan = 0 and c.n_des > 0               then 'desierta_total'
             when c.n_gan > 0 and c.n_des > 0               then 'desierta_parcial'
             when c.n_gan = 0 and c.n_des = 0 and c.n_ret > 0 then 'retirada'
             when c.n_gan > 0                               then 'adjudicada'
             else 'sin_ganador' end)
    or l.n_lotes_desiertos is distinct from c.n_des::integer;

-- La tabla de refrescos, vacía hasta que corra el cargador:
select * from public.refrescos order by clave;

-- LA PUESTA AL DÍA de las pendientes NO se lanza desde aquí: se hace con
--   python backfill_catalogo.py --refrescar-desiertas
-- que va por tandas y anota el resultado en public.refrescos.
