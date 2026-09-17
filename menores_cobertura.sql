-- ============================================================================
-- menores_cobertura.sql · Hasta qué fecha llega cada fuente de menores
-- 17/09/2026 — punto 4 de F1b, opción sencilla
--
-- PARA QUÉ
--   1) Decir en la web hasta qué fecha llega cada fuente, con la advertencia de que lo
--      más reciente está incompleto. Hoy (17/09/2026):
--        estatal   último menor 16/09/2026   ·  andalucía  último menor 30/06/2026
--      Y los menores por mes de los últimos 6 CON EL MISMO MES DEL AÑO ANTERIOR al lado,
--      que es lo que deja ver el retraso (el % es de esta pasada, 17/09/2026):
--        estatal    abr 37.978 (92%) · may 37.653 (78%) · jun 35.460 (77%) · jul 28.776 (60%)
--                   · ago 11.067 (41%) · sep 4.681 (11%)
--        andalucía  abr 2.181 (21%) · may 2.059 (19%) · jun 1.965 (19%) · jul 0 (0%) · ago 0 (0%)
--                   · sep 0 (0%)
--      O sea: NINGÚN mes reciente está cerrado, ni siquiera abril. En Andalucía el escalón es
--      otra cosa: el SAS (79% de la fuente) dejó de publicar el 27/04/2026.
--      NO se guarda ninguna regla de «último mes completo» (descartada el 17/09): se guarda
--      el dato y quien lo lea decide.
--   2) Saber SIN PREGUNTAR A LA BASE cuántos menores tiene un órgano grande. Con eso, la
--      web enseña al instante el aviso de «este órgano no se puede ordenar por importe»
--      (hasta ahora tardaba 3 s en decidirlo: tenía que ir a por la fila nº 10.001).
--      OJO: se guardan los órganos desde 5.000 menores (margen para que crezcan), pero la
--      web solo usa la pista por encima de 10.000 x 1,1 = 11.000 (M_PISTA_MARGEN en
--      menores_api.js). Hoy eso son 10 de los 26 órganos guardados; los otros 16 son
--      informativos. El CSIC-Gerencias (10.834) sigue pagando la sonda: está en el margen.
--
-- QUÉ NO HACE
--   No toca `menores` ni ninguna tabla del Radar o del Buscador: solo LEE.
--   No guarda la fecha de cada órgano: esa se pregunta en vivo al filtrar (49 ms por el
--   índice (órgano, fecha) creado esta misma semana), así siempre está al día.
--
-- COSTE DEL REFRESCO (medido en caliente el 17/09/2026 sobre las 1.713.937 filas)
--   filas por órgano y fuente (un recorrido del índice órgano+fuente) ....... 0,40 s
--   menores por mes de los últimos 6 (índice de fecha) .................. 0,24-3,71 s
--   los mismos 6 meses del año anterior ................ 0 s (reaprovechados de la foto
--       anterior). Se recalcula el tramo que falte: ~25 s la PRIMERA vez y cuando entra una
--       FUENTE nueva (le faltan los 6 meses), ~9 s el primer refresco de cada mes (un mes).
--       Un previo a 0 se recalcula siempre (ver el bloque b2): así se cura solo cuando llega
--       el histórico de una fuente nueva. Para forzarlo todo a mano, con service_role:
--         update public.menores_cobertura set meses = '[]'::jsonb where ambito = 'fuente';
--       y volver a llamar a la función.
--   última fecha de cada fuente (índice de fecha) ........................... 1,36 s
--   TOTAL 4-10 s según caché (medido entero con datos reales: 9,6 s en frío; con los meses
--   del año anterior, ver abajo). La llaman DOS sitios: la ingesta diaria de menores
--   (backfill_catalogo.py, L-V, junto a los refrescos de competidores y desiertas) y la carga
--   autonómica de los fines de semana. Hace falta a diario: el estatal entra todos los días y
--   una foto del domingo diría «Estatal 26/08/2026 · sep 0» con la base ya en el 16/09.
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
  'Menores por mes de los últimos 6 con el MISMO mes del año anterior al lado, [{"mes":"2026-04","n":37978,"previo":41364}, ...]. Solo en las filas de ámbito fuente.';
comment on column public.menores_cobertura.fuente is
  'CANAL por el que llega el dato (estatal = sindicación 1143 de la PLACSP, que también trae órganos autonómicos y locales), NO el territorio del órgano. En las filas de ámbito organo: su única fuente, o ''varias'' si publica por más de un canal (ojo entonces a los menores gemelos).';
comment on column public.menores_cobertura.actualizado is
  'Cuándo se calculó esta foto. La rellena menores_cobertura_refresca(): la ingesta diaria (backfill_catalogo.py) y la carga autonómica semanal.';

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
  v_falta_d date;                               -- primer mes sin «año anterior» guardado
  v_falta_h date;                               -- último
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
  drop table if exists pg_temp._cob_prev;
  create temporary table _cob_n on commit drop as
    select organo_contratacion as organo, fuente, count(*)::bigint as n
      from public.menores
     group by 1, 2;

  -- (b) Menores por mes de los últimos 6 (0,2-3,7 s por el índice de fecha).
  create temporary table _cob_mes on commit drop as
    select fuente,
           to_char(date_trunc('month', fecha_adjudicacion), 'YYYY-MM') as mes,
           count(*)::bigint as n
      from public.menores
     where fecha_adjudicacion >= v_desde
       and fecha_adjudicacion <  v_hasta
     group by 1, 2;

  -- (b2) El MISMO MES DEL AÑO ANTERIOR de cada uno de esos 6. Es lo único que hace legible
  --      la cifra suelta: «jun 35.460» parece un mes cerrado y va por el 76% de lo que fue
  --      junio del año pasado. Son meses CERRADOS, así que se reaprovecha lo ya guardado:
  --      calcular los 6 cuesta 23 s medidos, y reaprovechando sale gratis salvo cuando entra
  --      un mes nuevo (o una fuente nueva), que cuesta el tramo que falte.
  --      OJO con los CEROS: un previo a 0 (fuente cuyo histórico aún no estaba cargado) NO se
  --      reaprovecha. Si se reaprovechara, ese 0 bloquearía su propio recálculo y esa fuente
  --      se quedaría sin % hasta que el mes se cayera de la ventana (6 meses). Recalcular un
  --      mes a 0 es barato y así se auto-cura en cuanto llega el histórico.
  create temporary table _cob_prev on commit drop as
    select c.clave as fuente, (m->>'mes') as mes, ((m->>'previo')::bigint) as n
      from public.menores_cobertura c, jsonb_array_elements(c.meses) m
     where c.ambito = 'fuente' and (m ? 'previo') and (m->>'previo')::bigint > 0;

  select min(g.m), max(g.m) into v_falta_d, v_falta_h
    from (select distinct fuente from pg_temp._cob_n) f
    cross join (select generate_series(v_desde, v_hasta - interval '1 day', interval '1 month')::date as m) g
   where not exists (select 1 from pg_temp._cob_prev p
                      where p.fuente = f.fuente and p.mes = to_char(g.m, 'YYYY-MM'));

  if v_falta_d is not null then
    delete from pg_temp._cob_prev
     where mes >= to_char(v_falta_d, 'YYYY-MM') and mes <= to_char(v_falta_h, 'YYYY-MM');
    insert into pg_temp._cob_prev (fuente, mes, n)
    select fuente,
           to_char(date_trunc('month', fecha_adjudicacion) + interval '1 year', 'YYYY-MM'),
           count(*)::bigint
      from public.menores
     where fecha_adjudicacion >= (v_falta_d - interval '1 year')
       and fecha_adjudicacion <  (v_falta_h - interval '1 year' + interval '1 month')
     group by 1, 2;
  end if;

  -- (c) Una fila por fuente. Los meses sin menores salen a 0 (que se vea el hueco).
  insert into public.menores_cobertura as c (ambito, clave, fuente, ultima, filas, meses, actualizado)
  select 'fuente', f.fuente, f.fuente,
         -- Sin fechas futuras: una sola fila con fecha basura pondría «Datos hasta 2126».
         (select max(m.fecha_adjudicacion) from public.menores m
           where m.fuente = f.fuente and m.fecha_adjudicacion <= current_date),
         f.filas,
         (select coalesce(jsonb_agg(jsonb_build_object(
                     'mes', to_char(g.m, 'YYYY-MM'),
                     'n', coalesce(x.n, 0),
                     'previo', coalesce(y.n, 0)) order by g.m), '[]'::jsonb)
            from (select generate_series(v_desde, v_hasta - interval '1 day', interval '1 month') as m) g
            left join pg_temp._cob_mes  x on x.fuente = f.fuente and x.mes = to_char(g.m, 'YYYY-MM')
            left join pg_temp._cob_prev y on y.fuente = f.fuente and y.mes = to_char(g.m, 'YYYY-MM')),
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
