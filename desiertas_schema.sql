-- ============================================================================
-- DESIERTAS  ·  agregado por licitación del resultado de sus lotes
--
-- OBJETIVO: poder FILTRAR y LISTAR las licitaciones desiertas en el Buscador
-- (detectar nichos donde no se presenta nadie) y marcarlas con un badge visible.
--
-- POR QUÉ UNA COLUMNA AGREGADA (y no un join/EXISTS al vuelo sobre adjudicaciones):
-- medido con EXPLAIN (ANALYZE) contra el catálogo real (596.244 licitaciones,
-- 541.601 adjudicaciones), 14/07/2026:
--   · listado «solo desiertas» + orden + LIMIT 25 .... 4.931 ms
--       Nested Loop Semi Join: recorre el índice de ORDEN y sondea adjudicaciones
--       fila a fila (9.813 sondeos para juntar 25 resultados).
--   · texto ('limpieza') + desiertas ................. 7.277 ms
--       el EXISTS hace que el planner ABANDONE el GIN(tsv) y recorra el índice de
--       fecha filtrando el tsv al vuelo (30.338 filas descartadas) -> exactamente la
--       patología documentada en buscador_rpc.sql (BG-3/BG-4).
--   · COUNT (obligatorio para paginar) .............. 12.959 ms  -> timeout seguro.
-- Causa de fondo: las desiertas están ANTICORRELADAS con el orden por defecto (las
-- recientes aún no tienen resultado), así que "filtrar al vuelo" obliga a recorrer
-- miles de filas. Con la columna, el predicado vive en la MISMA tabla que el resto
-- de filtros del Buscador -> compone con el GIN/trigram y con los índices de orden.
--
-- COSTE DE POBLARLA: NO hace falta re-backfill del feed (~3 h, como el nº de
-- expediente): el dato YA está en public.adjudicaciones, así que el agregado se
-- calcula EN LA BASE con SQL puro. Mismo patrón que refrescar_competidores()
-- (rebuild completo medido en ~3,5 s). Ver refrescar_desiertas() abajo.
--
-- CÓMO USARLO: pega TODO este archivo en el SQL Editor de Supabase y pulsa "Run".
-- Es IDEMPOTENTE (if not exists / create or replace): se puede re-ejecutar.
-- DESPUÉS hay que POBLAR (ver el bloque 5, al final).
--
-- NO toca: el pipeline del Radar, login, decisiones, purga, competencia por CIF ni
-- la ingesta de adjudicaciones (D1/D1.1). Solo AÑADE dos columnas a licitaciones.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) Columnas agregadas (nullable; NULL = la licitación aún no tiene resultado)
-- ----------------------------------------------------------------------------
-- TAXONOMÍA (derivada por licitacion_id sobre sus filas de public.adjudicaciones).
-- Un lote "sin ganador" es cif_adjudicatario = '' (la señal FIABLE; el extractor lo
-- documenta así). El resultado_code NO se usa para saber SI hay ganador, solo para
-- saber POR QUÉ no lo hay — que es lo que separa un nicho vacío de un expediente
-- que el órgano retiró:
--   n_gan = lotes con adjudicatario
--   n_des = lotes sin adjudicatario con resultado_code '3' (Desierto: nadie se
--           presentó / ninguna oferta admisible)            <- la señal de NICHO
--   n_ret = lotes sin adjudicatario con code '4' (Desistimiento) o '5' (Renuncia)
--           -> el ÓRGANO retira la licitación; NO es "no se presentó nadie".
--
--   'desierta_total'   n_gan = 0  y  n_des > 0   ..... 14.593 licitaciones
--   'desierta_parcial' n_gan > 0  y  n_des > 0   .....  5.039
--   'retirada'         n_gan = 0, n_des = 0, n_ret > 0   3.045  (FUERA del filtro)
--   'adjudicada'       n_gan > 0  y  n_des = 0   .... 304.054
--   'sin_ganador'      n_gan = 0, n_des = 0, n_ret = 0 ..... 91  (dato de origen
--                      incompleto: lotes sin ganador con code 8/9/NULL)
--   NULL               sin filas en adjudicaciones .. 269.422  (sin resultado aún)
-- (Recuentos medidos el 14/07/2026; suman las 326.822 con resultado + las NULL.)
--
-- n_lotes_desiertos = n_des. Sirve para el badge «N lotes desiertos» de la tarjeta
-- SIN esperar a la hidratación de adjudicaciones (y sin duplicar su lógica: es el
-- MISMO cálculo, hecho una vez en la BD).
alter table public.licitaciones add column if not exists estado_adjudicacion text;
alter table public.licitaciones add column if not exists n_lotes_desiertos   integer;

comment on column public.licitaciones.estado_adjudicacion is
  'Agregado de public.adjudicaciones por licitacion_id: adjudicada | desierta_total | desierta_parcial | retirada | sin_ganador. NULL = sin resultado publicado. Lo mantiene refrescar_desiertas().';
comment on column public.licitaciones.n_lotes_desiertos is
  'Nº de lotes DESIERTOS de verdad (sin adjudicatario y resultado_code=3). Lo mantiene refrescar_desiertas().';

-- ----------------------------------------------------------------------------
-- 2) Índices · el filtro «solo desiertas» son 19.632 filas de 596.244 (3,3 %)
-- ----------------------------------------------------------------------------
-- (a) PARCIAL sobre el ORDEN por defecto del subapartado «Desiertas»
--     (fecha_fin_plazo DESC = las desiertas más recientes primero). Casa EXACTAMENTE
--     con el v_ord de buscar_licitaciones ('<campo> desc nulls last, licitacion_id asc'),
--     así que el listado es un INDEX SCAN puro de 25 filas: sin sondeos, sin sort.
--     Al ser PARCIAL solo indexa las ~19.6k desiertas (unos pocos cientos de KB).
create index if not exists licitaciones_desiertas_finplazo_desc
  on public.licitaciones (fecha_fin_plazo desc nulls last, licitacion_id asc)
  where estado_adjudicacion in ('desierta_total', 'desierta_parcial');

-- (b) PARCIAL sobre el estado: da el Bitmap Index Scan para el resto de combos
--     (otros órdenes, y el AND con CPV/texto donde manda el GIN). No indexa las
--     269k NULL (que nunca se filtran) ni hace falta más: 'desierta_total' solo
--     también lo usa (Postgres prueba que `= x` implica `in (x, y)`).
create index if not exists licitaciones_estado_adj_idx
  on public.licitaciones (estado_adjudicacion)
  where estado_adjudicacion is not null;

-- ----------------------------------------------------------------------------
-- 3) RPC de refresco · recalcula el agregado desde public.adjudicaciones
-- ----------------------------------------------------------------------------
-- IDEMPOTENTE y RESUMIBLE: solo toca las filas cuyo valor calculado DIFIERE del
-- guardado (`is distinct from`), así que re-ejecutarla no reescribe nada.
--   p_lote = 0  -> de una vez (uso DIARIO: recalcula el agregado y aplica el delta,
--                  que son unas pocas decenas de filas -> milisegundos de escritura).
--   p_lote > 0  -> como mucho p_lote licitaciones por llamada (uso BACKFILL: se llama
--                  en bucle hasta que devuelve 0, sin acercarse al statement_timeout).
-- Devuelve el nº de filas actualizadas.
--
-- SECURITY INVOKER (por defecto): la llama la ingesta con service_role, que ya tiene
-- UPDATE sobre public.licitaciones (es quien la upserta). No hace falta definer.
create or replace function public.refrescar_desiertas(p_lote integer default 0)
returns integer
language plpgsql
volatile
set search_path = public, pg_catalog
as $$
declare
  v_n integer;
begin
  with calc as (
    -- Un solo escaneo de adjudicaciones (mismo patrón que refrescar_competidores).
    select a.licitacion_id,
           count(*) filter (where a.cif_adjudicatario <> '')                                  as n_gan,
           count(*) filter (where a.cif_adjudicatario =  '' and a.resultado_code =  '3')      as n_des,
           count(*) filter (where a.cif_adjudicatario =  '' and a.resultado_code in ('4','5')) as n_ret
      from public.adjudicaciones a
     group by a.licitacion_id
  ), estado as (
    select licitacion_id,
           case when n_gan = 0 and n_des > 0                             then 'desierta_total'
                when n_gan > 0 and n_des > 0                             then 'desierta_parcial'
                when n_gan = 0 and n_des = 0 and n_ret > 0               then 'retirada'
                when n_gan > 0                                           then 'adjudicada'
                else 'sin_ganador' end                                   as est,
           n_des::integer                                                as n_des
      from calc
  ), pendientes as (
    -- Solo lo que CAMBIA. El join descarta además las adjudicaciones cuya licitación
    -- ya no está en el catálogo (purgada): ~40k licitacion_id que no hay que tocar.
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

-- PRIVADA: la invoca la ingesta (backfill_catalogo.py) con service_role. Ni anon ni
-- authenticated la necesitan (la web solo LEE las columnas).
revoke all on function public.refrescar_desiertas(integer) from public, anon, authenticated;
grant execute on function public.refrescar_desiertas(integer) to service_role;

-- Refresca el cache de esquema de PostgREST (columnas nuevas + RPC nueva).
notify pgrst, 'reload schema';

-- ----------------------------------------------------------------------------
-- 4) Estadísticas · sin esto el planner no conoce la selectividad del filtro
--    (3,3 %) y puede elegir un plan malo. Se re-lanza tras poblar (bloque 5).
analyze public.licitaciones;

-- ============================================================================
-- 5) POBLAR (una sola vez; después lo mantiene la ingesta diaria)
-- ============================================================================
-- Hay ~326.822 licitaciones con resultado que pasar de NULL a su estado. NO se puede
-- hacer en una sola sentencia sin arriesgar el statement_timeout, así que va POR LOTES.
--
-- OPCIÓN A (recomendada) · desde el repo, con la service_role del pipeline:
--     python backfill_catalogo.py --refrescar-desiertas
--   Llama a la RPC en bucle (lotes de --lote, por defecto 5.000) hasta que devuelve 0
--   e imprime el progreso. Mismo patrón que --purgar.
--
-- OPCIÓN B · a mano en el SQL Editor: ejecuta esta línea REPETIDAMENTE hasta que
--   devuelva 0 (unas 66 veces con lotes de 5.000; cada llamada tarda pocos segundos):
--     select public.refrescar_desiertas(5000);
--
-- Y AL TERMINAR (una vez, para que el planner tenga la selectividad real):
--     analyze public.licitaciones;
--
-- COMPROBACIÓN (los recuentos deben salir como los de arriba):
--     select estado_adjudicacion, count(*)
--       from public.licitaciones group by 1 order by 2 desc;
-- ============================================================================
