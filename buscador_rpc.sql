-- buscador_rpc.sql  ·  BG-3: RPC de escalada para la búsqueda CON TEXTO
-- ============================================================================
-- PROBLEMA (prueba P3): `WHERE tsv @@ q ORDER BY fecha_fin_plazo LIMIT 25` hace
-- que el planner recorra el índice de fecha y filtre el tsv AL VUELO; con un
-- término poco frecuente escanea miles de filas por fecha antes de juntar 25
-- aciertos -> 16,9 s -> timeout 57014. El GIN(tsv) existe, pero el planner no lo
-- elige. Los btree compuestos no lo arreglan.
--
-- SOLUCIÓN: CTE MATERIALIZED = barrera de optimización. Fuerza a FILTRAR por el
-- GIN(tsv) PRIMERO (materializa el subconjunto que casa), y SÓLO DESPUÉS ordena
-- y pagina ese subconjunto (pequeño). No se toca statement_timeout.
--
-- USO: la web enruta por aquí SOLO las búsquedas con texto; las búsquedas SIN
-- texto siguen yendo por PostgREST (ya van bien). PRIVADO: execute solo a
-- authenticated (y, además, la RLS de la tabla sigue aplicando porque la función
-- es SECURITY INVOKER).
--
-- Es DDL LIGERO (no construye índices, el GIN ya existe): no debería dar
-- "Failed to fetch". Si el editor se queja, ejecuta antes: set statement_timeout='120s';
-- ============================================================================

-- (Idempotente) Asegura el GIN sobre tsv por si se corre en un entorno limpio.
create index if not exists licitaciones_tsv_gin on public.licitaciones using gin (tsv);

create or replace function public.buscar_licitaciones(
  p_texto        text,
  p_cpv          text[]      default null,
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
  v_q      tsquery;
  v_campo  text;
  v_limit  integer;
  v_offset integer;
  v_total  bigint;
  v_filas  json;
begin
  -- Esta RPC es SOLO para búsquedas con texto (las demás van por PostgREST).
  if p_texto is null or btrim(p_texto) = '' then
    raise exception 'buscar_licitaciones: p_texto es obligatorio (las busquedas sin texto van por PostgREST).';
  end if;

  -- websearch + unaccent: el tsv se guardó como to_tsvector('spanish', unaccent(..)).
  -- Aplicamos unaccent aquí también para casar tildes con independencia del cliente.
  v_q := websearch_to_tsquery('spanish', unaccent(p_texto));

  v_limit  := greatest(1, coalesce(p_por_pagina, 25));
  v_offset := greatest(0, (greatest(1, coalesce(p_pagina, 1)) - 1) * v_limit);
  v_campo  := case when p_orden_campo in ('fecha_fin_plazo','valor_estimado','fecha_publicacion')
                   then p_orden_campo else 'fecha_fin_plazo' end;

  with filtrado as materialized (
    -- Solo id + claves de orden: materializar es barato aunque el match sea amplio.
    select licitacion_id, fecha_fin_plazo, valor_estimado, fecha_publicacion
    from public.licitaciones
    where tsv @@ v_q                                            -- <- GIN primero
      and (p_cpv is null or array_length(p_cpv, 1) is null or cpv && p_cpv)
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
revoke all on function public.buscar_licitaciones(
  text, text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer
) from public;
grant execute on function public.buscar_licitaciones(
  text, text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer
) to authenticated;

-- Refresca el cache de esquema de PostgREST para que la RPC sea visible ya.
notify pgrst, 'reload schema';

-- ----------------------------------------------------------------------------
-- PRUEBA RÁPIDA (descomenta para probar en el editor; 'climatizacion' es ligero):
-- select public.buscar_licitaciones('climatizacion');
-- select public.buscar_licitaciones(p_texto => 'aire', p_estado => 'abierta', p_importe_max => 200000);
-- ----------------------------------------------------------------------------
