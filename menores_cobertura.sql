-- ============================================================================
-- menores_cobertura.sql · Hasta qué fecha llega cada fuente de menores
-- 17/09/2026 — punto 4 de F1b, opción sencilla
--
-- PARA QUÉ
--   1) Decir en la web hasta qué fecha llega cada fuente, con la advertencia de que lo
--      más reciente está incompleto. Hoy (17/09/2026):
--        estatal   último menor 16/09/2026   ·  andalucía  último menor 30/06/2026
--      Y los menores por mes de los últimos 6, que enseñan el retraso mejor que la fecha:
--        estatal    abr 37.978 · may 37.653 · jun 35.460 · jul 28.776 · ago 11.067 · sep 4.681
--        andalucía  abr  2.181 · may  2.059 · jun  1.965 · jul      0 · ago      0 · sep     0
--      (agosto y septiembre del estatal no son una caída real: son los menores que aún no
--      ha publicado nadie. Por eso la nota.)
--   2) Saber SIN PREGUNTAR A LA BASE cuántos menores tiene un órgano grande. Con eso, la
--      web enseña al instante el aviso de «este órgano no se puede ordenar por importe»
--      (hasta ahora tardaba 3 s en decidirlo: tenía que ir a por la fila nº 10.001).
--
-- QUÉ NO HACE
--   No toca `menores` ni ninguna tabla del Radar o del Buscador: solo LEE.
--   No guarda la fecha de cada órgano: esa se pregunta en vivo al filtrar (49 ms por el
--   índice (órgano, fecha) creado esta misma semana), así siempre está al día.
--
-- COSTE DEL REFRESCO (medido en caliente el 17/09/2026 sobre las 1.713.937 filas)
--   filas por órgano y fuente (un recorrido del índice órgano+fuente) ....... 0,40 s
--   menores por mes de los últimos 6 (índice de fecha) ...................... 2,53 s
--   última fecha de cada fuente (índice de fecha) ........................... 1,36 s
--   TOTAL 4-10 s según caché (medido entero con datos reales: 9,6 s en frío). Lo llama el
--   cargador al terminar (semanal). NO se ha tocado la ingesta diaria del Buscador.
--
-- CÓMO: en el SQL Editor, CADA PASO SOLO, en una pestaña LIMPIA (el editor ejecuta todo
-- lo que hay en la pestaña). Es idempotente: se puede repetir sin miedo.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- PASO 1 · La tabla (26 órganos + 2 fuentes = 28 filas hoy; cabe en nada)
-- ---------------------------------------------------------------------------
create table if not exists public.menores_cobertura (
  ambito      text        not null check (ambito in ('fuente', 'organo')),
  clave       text        not null,
  fuente      text        not null,
  ultima      date,
  filas       bigint      not null default 0,
  meses       jsonb       not null default '[]'::jsonb,
  actualizado timestamptz not null default now(),
  primary key (ambito, clave)
);

comment on table public.menores_cobertura is
  'Hasta dónde llega cada fuente de menores y tamaño de los órganos grandes. La rellena menores_cobertura_refresca(); solo se lee.';
comment on column public.menores_cobertura.ambito is
  'fuente: una fila por fuente de datos. organo: una fila por órgano con muchos menores (ver p_umbral_organo).';
comment on column public.menores_cobertura.clave is
  'Nombre de la fuente o del órgano de contratación, tal y como está en menores.';
comment on column public.menores_cobertura.ultima is
  'Fecha del último menor publicado por esa fuente. Solo en las filas de ámbito fuente: la del órgano se pregunta en vivo.';
comment on column public.menores_cobertura.filas is
  'Menores cargados de esa fuente o de ese órgano.';
comment on column public.menores_cobertura.meses is
  'Menores por mes de los últimos 6, [{"mes":"2026-04","n":37978}, ...]. Solo en las filas de ámbito fuente.';

alter table public.menores_cobertura enable row level security;

drop policy if exists menores_cobertura_select_authenticated on public.menores_cobertura;
create policy menores_cobertura_select_authenticated
  on public.menores_cobertura for select to authenticated using (true);

-- OJO: en Supabase, las tablas nuevas de `public` nacen con TODOS los permisos para anon y
-- authenticated (default ACL), y ahí solo las tapa la RLS. Se revoca por ROL, no a `public`.
revoke all on public.menores_cobertura from public;
revoke all on public.menores_cobertura from anon, authenticated;
grant select on public.menores_cobertura to authenticated;
grant select, insert, update, delete on public.menores_cobertura to service_role;


-- ---------------------------------------------------------------------------
-- PASO 2 · La función que la rellena (la llama el cargador; nadie más puede)
-- ---------------------------------------------------------------------------
create or replace function public.menores_cobertura_refresca(p_umbral_organo int default 5000)
returns json
language plpgsql
security definer
set search_path = public, pg_catalog, pg_temp
as $$
declare
  v_marca   timestamptz := clock_timestamp();   -- marca las filas de ESTA pasada
  v_desde   date := (date_trunc('month', current_date) - interval '5 months')::date;
  v_hasta   date := (date_trunc('month', current_date) + interval '1 month')::date;
  v_fuentes int;
  v_organos int;
  v_sobran  int;
begin
  -- Un umbral bajo llenaría la tabla de órganos y no aporta: por debajo de 10.000 menores
  -- la web ya puede ordenar por importe sin preguntar nada.
  if p_umbral_organo is null or p_umbral_organo < 1000 then
    raise exception 'p_umbral_organo debe ser 1000 o más (recibido: %)', p_umbral_organo;
  end if;

  -- (a) Filas por órgano y fuente: un solo recorrido del índice (órgano, fuente).
  -- Cualificadas con pg_temp a propósito: con pg_temp al final del search_path, un
  -- `_cob_n` a secas podría apuntar a public (y esto corre como postgres).
  drop table if exists pg_temp._cob_n;   -- por si la función corre dos veces en la misma transacción
  drop table if exists pg_temp._cob_mes;
  create temporary table _cob_n on commit drop as
    select organo_contratacion as organo, fuente, count(*)::bigint as n
      from public.menores
     group by 1, 2;

  -- (b) Menores por mes de los últimos 6, por el índice de fecha.
  create temporary table _cob_mes on commit drop as
    select fuente,
           to_char(date_trunc('month', fecha_adjudicacion), 'YYYY-MM') as mes,
           count(*)::bigint as n
      from public.menores
     where fecha_adjudicacion >= v_desde
       and fecha_adjudicacion <  v_hasta
     group by 1, 2;

  -- (c) Una fila por fuente. Los meses sin menores salen a 0 (que se vea el hueco).
  insert into public.menores_cobertura as c (ambito, clave, fuente, ultima, filas, meses, actualizado)
  select 'fuente', f.fuente, f.fuente,
         -- Sin fechas futuras: una sola fila con fecha basura pondría «Datos hasta 2126».
         (select max(m.fecha_adjudicacion) from public.menores m
           where m.fuente = f.fuente and m.fecha_adjudicacion <= current_date),
         f.filas,
         (select coalesce(jsonb_agg(jsonb_build_object('mes', g.mes, 'n', coalesce(x.n, 0)) order by g.mes), '[]'::jsonb)
            from (select to_char(generate_series(v_desde, v_hasta - interval '1 day', interval '1 month'), 'YYYY-MM') as mes) g
            left join pg_temp._cob_mes x on x.fuente = f.fuente and x.mes = g.mes),
         v_marca
    from (select fuente, sum(n)::bigint as filas from pg_temp._cob_n group by 1) f
  on conflict (ambito, clave) do update
     set fuente = excluded.fuente, ultima = excluded.ultima, filas = excluded.filas,
         meses = excluded.meses, actualizado = excluded.actualizado;
  get diagnostics v_fuentes = row_count;

  -- (d) Una fila por órgano grande, SUMANDO fuentes: la web filtra por órgano sin fuente, y
  --     dos fuentes con el mismo nombre de órgano (posible con Cataluña o Euskadi) romperían
  --     el ON CONFLICT (21000: la misma fila afectada dos veces).
  insert into public.menores_cobertura as c (ambito, clave, fuente, ultima, filas, meses, actualizado)
  select 'organo', n.organo,
         case when count(distinct n.fuente) > 1 then 'varias' else min(n.fuente) end,
         null, sum(n.n), '[]'::jsonb, v_marca
    from pg_temp._cob_n n
   where n.organo is not null
   group by n.organo
  having sum(n.n) >= p_umbral_organo
  on conflict (ambito, clave) do update
     set fuente = excluded.fuente, ultima = excluded.ultima, filas = excluded.filas,
         meses = excluded.meses, actualizado = excluded.actualizado;
  get diagnostics v_organos = row_count;

  -- (e) Fuera lo que ya no toca (un órgano que se queda por debajo del umbral, una fuente
  --     que se vacía). Todo lo vivo lleva la marca de esta pasada. Si no ha entrado NINGUNA
  --     fuente, algo ha ido mal: se aborta y no se borra nada (la función va en transacción).
  if v_fuentes = 0 then
    raise exception 'menores no devolvió ninguna fila: no se toca la cobertura';
  end if;
  delete from public.menores_cobertura where actualizado is distinct from v_marca;
  get diagnostics v_sobran = row_count;

  return json_build_object(
    'fuentes', v_fuentes,
    'organos', v_organos,
    'retiradas', v_sobran,
    'umbral_organo', p_umbral_organo,
    'meses_desde', v_desde,
    'ms', round(extract(epoch from clock_timestamp() - v_marca) * 1000)
  );
end;
$$;

comment on function public.menores_cobertura_refresca(int) is
  'Recalcula public.menores_cobertura leyendo menores (~4 s). La llama el cargador de menores; no la puede llamar la web.';

-- Igual que con la tabla: las funciones nuevas de `public` nacen EJECUTABLES por anon y
-- authenticated. Sin este revoke por rol, la web podría lanzar el refresco (4 s de lectura)
-- tantas veces como quisiera.
revoke all on function public.menores_cobertura_refresca(int) from public;
revoke all on function public.menores_cobertura_refresca(int) from anon, authenticated;
grant execute on function public.menores_cobertura_refresca(int) to service_role;

notify pgrst, 'reload schema';


-- ---------------------------------------------------------------------------
-- PASO 3 · Primer llenado y comprobación (esto sí devuelve datos)
-- ---------------------------------------------------------------------------
select public.menores_cobertura_refresca();

select ambito, clave, fuente, ultima, filas, meses
  from public.menores_cobertura
 where ambito = 'fuente'
 order by filas desc;

-- Los órganos grandes (26 el 17/09/2026; el SAS con 230.610 menores es el primero).
select clave, fuente, filas
  from public.menores_cobertura
 where ambito = 'organo'
 order by filas desc
 limit 10;

-- Y que la web (rol authenticated) la ve:
--   select * from public.menores_cobertura;  -- desde la app, no desde aquí
