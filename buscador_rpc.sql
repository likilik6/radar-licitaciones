-- buscador_rpc.sql  ·  BG-3/BG-4: RPC de escalada de la búsqueda
-- ============================================================================
-- BG-3 (por qué la RPC): `WHERE tsv @@ q ORDER BY fecha_fin_plazo LIMIT 25` hace
-- que el planner recorra el índice de fecha y filtre el tsv AL VUELO; con un
-- término poco frecuente escanea miles de filas por fecha antes de juntar 25
-- aciertos -> 16,9 s -> timeout 57014. El GIN(tsv) existe, pero el planner no lo
-- elige. SOLUCIÓN: CTE MATERIALIZED = barrera de optimización. Fuerza a FILTRAR
-- primero (materializa el subconjunto que casa) y SÓLO DESPUÉS ordena y pagina
-- ese subconjunto (pequeño). No se toca statement_timeout.
--
-- BG-4 incremento 2 (NOVEDADES en esta RPC):
--   1) p_texto pasa a ser OPCIONAL. La RPC ya no es solo "con texto".
--   2) NUEVO p_cpv_prefijo text[]: casa por PREFIJO de CPV ("empieza por", igual
--      que el Radar con intereses.yaml). PostgREST no sabe hacer prefijo sobre un
--      text[], así que se resuelve aquí con EXISTS + LIKE ANY sobre unnest(cpv).
--      Varios prefijos = OR entre ellos.
--   La web enruta por AQUÍ cuando hay texto O cuando hay filtro CPV por prefijo
--   (con o sin texto). Las búsquedas TRIVIALES (sin texto ni prefijo) siguen por
--   PostgREST. El CPV EXACTO (p_cpv, overlaps `&&`) se mantiene tal cual.
--
-- PRIVADO: execute solo a authenticated; además la función es SECURITY INVOKER,
-- así que la RLS de la tabla sigue aplicando.
--
-- CÓMO USARLO: pega TODO este archivo en el SQL Editor de Supabase y pulsa "Run".
-- OJO (BG-4): esta versión AÑADE un parámetro (p_cpv_prefijo) -> cambia la FIRMA
-- de la función; por eso se hace DROP de la firma vieja (14 args) antes de crear
-- la nueva (15 args). Es DDL LIGERO (el GIN ya existe): no debería dar "Failed to
-- fetch". Si el editor se queja, ejecuta antes: set statement_timeout='120s';
-- ============================================================================

-- (Idempotente) Asegura el GIN sobre tsv por si se corre en un entorno limpio.
create index if not exists licitaciones_tsv_gin on public.licitaciones using gin (tsv);

-- BG-4: la firma cambia (nuevo p_cpv_prefijo). `create or replace` NO permite
-- añadir parámetros -> hay que dropear la firma ANTERIOR (14 args) primero. Es
-- idempotente: en re-ejecuciones no encuentra la vieja y no pasa nada.
drop function if exists public.buscar_licitaciones(
  text, text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer
);

create or replace function public.buscar_licitaciones(
  p_texto        text        default null,     -- BG-4: ahora OPCIONAL
  p_cpv          text[]      default null,      -- CPV EXACTO (overlaps &&)
  p_cpv_prefijo  text[]      default null,      -- BG-4: CPV por PREFIJO (empieza por)
  p_fuente       text        default null,
  p_importe_min  numeric     default null,
  p_importe_max  numeric     default null,
  p_estado       text        default 'todas',   -- 'abierta' | 'cerrada' | 'todas'
  p_fin_desde    timestamptz default null,
  p_fin_hasta    timestamptz default null,
  p_pub_desde    timestamptz default null,
  p_pub_hasta    timestamptz default null,
  p_orden_campo  text        default 'fecha_fin_plazo',
  p_orden_asc    boolean     default true,
  p_pagina       integer     default 1,
  p_por_pagina   integer     default 25
)
returns json
language plpgsql
stable
security invoker
set search_path = extensions, public, pg_catalog
as $$
declare
  v_q        tsquery;
  v_cpv_pref text[];
  v_campo    text;
  v_limit    integer;
  v_offset   integer;
  v_total    bigint;
  v_filas    json;
begin
  -- Prefijos CPV -> patrones LIKE, escapando los metacaracteres (\ % _) para que
  -- solo actúen como prefijo literal + '%'. Empties fuera. Varios = OR (LIKE ANY).
  if p_cpv_prefijo is not null then
    select array_agg(
             replace(replace(replace(btrim(px), '\', '\\'), '%', '\%'), '_', '\_') || '%'
           )
      into v_cpv_pref
      from unnest(p_cpv_prefijo) px
     where btrim(px) <> '';
  end if;

  -- Texto (opcional). websearch + unaccent: el tsv se guardó como
  -- to_tsvector('spanish', unaccent(..)); aplicamos unaccent aquí también.
  if p_texto is not null and btrim(p_texto) <> '' then
    v_q := websearch_to_tsquery('spanish', unaccent(p_texto));
  end if;

  -- Esta RPC es para búsquedas CON texto O CON prefijo CPV. Las triviales (sin
  -- ninguno de los dos) van por PostgREST; si llegan aquí es un bug de enrutado.
  if v_q is null and v_cpv_pref is null then
    raise exception 'buscar_licitaciones: requiere p_texto o p_cpv_prefijo (las busquedas triviales van por PostgREST).';
  end if;

  v_limit  := greatest(1, coalesce(p_por_pagina, 25));
  v_offset := greatest(0, (greatest(1, coalesce(p_pagina, 1)) - 1) * v_limit);
  v_campo  := case when p_orden_campo in ('fecha_fin_plazo','valor_estimado','fecha_publicacion')
                   then p_orden_campo else 'fecha_fin_plazo' end;

  with filtrado as materialized (
    -- Solo id + claves de orden: materializar es barato aunque el match sea amplio.
    -- Con texto, el GIN(tsv) filtra primero; sin texto manda el prefijo CPV.
    select licitacion_id, fecha_fin_plazo, valor_estimado, fecha_publicacion
    from public.licitaciones
    where (v_q is null or tsv @@ v_q)                           -- <- GIN si hay texto
      and (p_cpv is null or array_length(p_cpv, 1) is null or cpv && p_cpv)
      and (v_cpv_pref is null
           or exists (select 1 from unnest(cpv) c where c like any (v_cpv_pref)))
      and (p_fuente is null or fuente = p_fuente)
      and (p_importe_min is null or valor_estimado >= p_importe_min)
      and (p_importe_max is null or valor_estimado <= p_importe_max)
      and (p_fin_desde  is null or fecha_fin_plazo  >= p_fin_desde)
      and (p_fin_hasta  is null or fecha_fin_plazo  <= p_fin_hasta)
      and (p_pub_desde  is null or fecha_publicacion >= p_pub_desde)
      and (p_pub_hasta  is null or fecha_publicacion <= p_pub_hasta)
      and (
            p_estado = 'todas'
        or (p_estado = 'abierta' and (fecha_fin_plazo >= now() or fecha_fin_plazo is null))
        or (p_estado = 'cerrada' and fecha_fin_plazo < now())
      )
  ),
  pagina as (
    -- Elige QUÉ 25 filas (orden dinámico por CASE; el desempate va por la PK).
    select licitacion_id
    from filtrado
    order by
      case when v_campo='fecha_fin_plazo'   and p_orden_asc     then fecha_fin_plazo   end asc  nulls last,
      case when v_campo='fecha_fin_plazo'   and not p_orden_asc then fecha_fin_plazo   end desc nulls last,
      case when v_campo='valor_estimado'    and p_orden_asc     then valor_estimado    end asc  nulls last,
      case when v_campo='valor_estimado'    and not p_orden_asc then valor_estimado    end desc nulls last,
      case when v_campo='fecha_publicacion' and p_orden_asc     then fecha_publicacion end asc  nulls last,
      case when v_campo='fecha_publicacion' and not p_orden_asc then fecha_publicacion end desc nulls last,
      licitacion_id asc
    limit v_limit offset v_offset
  )
  select
    (select count(*) from filtrado),
    coalesce(
      (select json_agg(row_to_json(x) order by
          case when v_campo='fecha_fin_plazo'   and p_orden_asc     then x.fecha_fin_plazo   end asc  nulls last,
          case when v_campo='fecha_fin_plazo'   and not p_orden_asc then x.fecha_fin_plazo   end desc nulls last,
          case when v_campo='valor_estimado'    and p_orden_asc     then x.valor_estimado    end asc  nulls last,
          case when v_campo='valor_estimado'    and not p_orden_asc then x.valor_estimado    end desc nulls last,
          case when v_campo='fecha_publicacion' and p_orden_asc     then x.fecha_publicacion end asc  nulls last,
          case when v_campo='fecha_publicacion' and not p_orden_asc then x.fecha_publicacion end desc nulls last,
          x.licitacion_id asc)
       from (
         select li.licitacion_id, li.titulo, li.objeto, li.organo_contratacion, li.cpv, li.fuente,
                li.presupuesto_con_iva, li.presupuesto_sin_iva, li.valor_estimado,
                li.fecha_publicacion, li.fecha_fin_plazo, li.lugar_ejecucion, li.ccaa, li.enlace
         from pagina pg
         join public.licitaciones li using (licitacion_id)
       ) x),
      '[]'::json)
  into v_total, v_filas;

  return json_build_object(
    'total',      v_total,
    'filas',      v_filas,
    'pagina',     greatest(1, coalesce(p_pagina, 1)),
    'porPagina',  v_limit,
    'aproximado', false      -- el conteo es exacto sobre el subconjunto que casa
  );
end;
$$;

-- PRIVADO: por defecto CREATE FUNCTION concede EXECUTE a PUBLIC; lo quitamos y
-- dejamos solo authenticated (anon no puede llamarla; además la RLS protege).
-- La firma incluye ya el nuevo p_cpv_prefijo (15 args).
revoke all on function public.buscar_licitaciones(
  text, text[], text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer
) from public;
grant execute on function public.buscar_licitaciones(
  text, text[], text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer
) to authenticated;

-- Refresca el cache de esquema de PostgREST para que la nueva firma sea visible ya.
notify pgrst, 'reload schema';

-- ----------------------------------------------------------------------------
-- PRUEBA RÁPIDA (descomenta para probar en el editor):
-- select public.buscar_licitaciones(p_texto => 'climatizacion');
-- select public.buscar_licitaciones(p_cpv_prefijo => array['9073']);          -- familia 9073xxxx
-- select public.buscar_licitaciones(p_cpv_prefijo => array['90731100']);      -- código completo
-- select public.buscar_licitaciones(p_texto => 'aire', p_cpv_prefijo => array['9073'], p_importe_max => 200000);
--
-- RENDIMIENTO (BG-4): el prefijo CPV usa unnest(cpv)+LIKE, que NO aprovecha el
-- GIN(cpv) (ese índice sirve a `&&`/`@>`, no a LIKE sobre elementos). Con texto o
-- con otro filtro (fuente/importe/fechas) el subconjunto es pequeño y va sobrado.
-- Un prefijo MUY corto (p.ej. 2 dígitos) SIN ningún otro filtro obliga a recorrer
-- la tabla (~588k) con unnest+LIKE: puede ir lento. Si molesta, la salida NO es
-- subir statement_timeout, sino acotar (pedir >=4 dígitos o combinar con filtro) o,
-- si hiciera falta, un índice de expresión con pg_trgm sobre una representación
-- textual del array. De momento se deja sin índice extra (aviso a Alejandro).
-- ----------------------------------------------------------------------------
