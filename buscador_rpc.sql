-- buscador_rpc.sql  ·  BG-3/BG-4: RPC de escalada de la búsqueda
-- ============================================================================
-- REQUISITO: ejecutar ANTES buscador_indices.sql (crea public.cpv_texto, el
-- índice GIN trigram y los índices de orden en los que se apoya esta RPC).
--
-- BG-3 (por qué la RPC): con un término poco frecuente, `WHERE tsv @@ q ORDER BY
-- fecha LIMIT 25` hacía que el planner recorriese el índice de fecha filtrando el
-- tsv al vuelo -> 16,9 s -> timeout 57014. Se resolvió con una CTE MATERIALIZED
-- (barrera) que filtra por el GIN(tsv) PRIMERO y ordena el subconjunto pequeño.
--
-- BG-4 inc2 — PROBLEMA NUEVO: con términos/prefijos AMPLIOS ("cai", CPV "9073",
-- "90") volvía el timeout 57014, por TRES motivos:
--   (1) el conteo EXACTO del subconjunto revienta cuando casan cientos de miles;
--   (2) el CPV-prefijo con unnest+LIKE no usaba índice -> escaneo de 588k;
--   (3) MATERIALIZAR un subconjunto enorme para ordenarlo+limitarlo es carísimo
--       (hay que traer del heap TODAS las filas que casan, aunque solo se usen 25).
--
-- BG-4 inc2 — SOLUCIÓN (sin tocar statement_timeout):
--   · CONTEO ACOTADO: un "probe" cuenta hasta un TOPE (v_umbral). Si el resultado
--     es <= tope, es el total EXACTO (barato); si lo supera, se devuelve el tope
--     con aproximado=true y topado=true (la web muestra "más de N"). El probe NO
--     ordena, así que es barato incluso para conjuntos grandes.
--   · PLAN ADAPTATIVO según lo que diga el probe:
--       - SELECTIVO (<= tope): CTE MATERIALIZED (GIN/trigram primero) y se ordena
--         el subconjunto pequeño. Rápido a cualquier profundidad de página.
--       - AMPLIO (> tope): NO se materializa. Se saca la página por INDEX SCAN del
--         índice de ORDEN (con enable_sort=off) filtrando al vuelo: como los
--         resultados son densos, las primeras 25 salen enseguida. Total aproximado.
--   · CPV-PREFIJO por índice: cpv_texto(cpv) (' '+códigos) + LIKE '% <pref>%' se
--     apoya en el GIN trigram (ver buscador_indices.sql). Varios prefijos = OR.
--   · plan_cache_mode=force_custom_plan: para que v_q / v_cpv_pref / prefijos se
--     "plieguen" a constantes en cada llamada y el planner elija los índices (con
--     plan genérico y OR de por medio no los usaría).
--
-- Enrutado desde la web: por AQUÍ van las búsquedas con texto O con prefijo CPV.
-- Las triviales (sin texto ni prefijo) siguen por PostgREST. El CPV EXACTO (p_cpv,
-- overlaps `&&`) se mantiene. PRIVADO: execute solo a authenticated + SECURITY
-- INVOKER (respeta RLS).
--
-- La FIRMA no cambia respecto a inc2 (15 args) -> create or replace basta; se deja
-- el drop de la firma vieja de BG-3 (14 args) por si quedara colgada. Es DDL ligero.
-- ============================================================================

-- (Idempotente) Asegura el GIN sobre tsv por si se corre en un entorno limpio.
create index if not exists licitaciones_tsv_gin on public.licitaciones using gin (tsv);

-- Limpia la firma vieja de BG-3 (14 args, sin p_cpv_prefijo) si aún existiera.
drop function if exists public.buscar_licitaciones(
  text, text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer
);

create or replace function public.buscar_licitaciones(
  p_texto        text        default null,      -- OPCIONAL (búsqueda por prefijo puede ir sin texto)
  p_cpv          text[]      default null,       -- CPV EXACTO (overlaps &&)
  p_cpv_prefijo  text[]      default null,       -- CPV por PREFIJO ("empieza por")
  p_fuente       text        default null,
  p_importe_min  numeric     default null,
  p_importe_max  numeric     default null,
  p_estado       text        default 'todas',    -- 'abierta' | 'cerrada' | 'todas'
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
  v_dir      text;
  v_ord      text;
  v_limit    integer;
  v_offset   integer;
  v_total    bigint;
  v_hit      bigint;
  v_aprox    boolean := false;
  v_topado   boolean := false;
  v_filas    json;
  v_sql      text;
  -- TOPE del conteo exacto y frontera selectivo/amplio. 5000 deja el conteo
  -- EXACTO para casi todo (p.ej. climatización≈3146) y solo lo muy amplio queda
  -- aproximado; además la rama SELECTIVA materializa como mucho ~5000 filas (rápido).
  v_umbral   constant integer := 5000;
begin
  -- Que las constantes/variables se plieguen y el planner elija GIN/trigram.
  set local plan_cache_mode = 'force_custom_plan';

  -- Prefijos CPV -> patrones "CONTIENE '<espacio><prefijo>'" (trigram-friendly).
  -- cpv_texto(cpv) antepone un espacio a cada código, así '% 9073%' casa cualquier
  -- código que EMPIECE por 9073. Se escapan los metacaracteres LIKE (\ % _).
  if p_cpv_prefijo is not null then
    select array_agg('% ' || replace(replace(replace(btrim(px), '\', '\\'), '%', '\%'), '_', '\_') || '%')
      into v_cpv_pref
      from unnest(p_cpv_prefijo) px
     where btrim(px) <> '';
  end if;

  -- Texto (opcional). unaccent para casar tildes (el tsv se guardó con unaccent).
  if p_texto is not null and btrim(p_texto) <> '' then
    v_q := websearch_to_tsquery('spanish', unaccent(p_texto));
  end if;

  -- Solo con texto O con prefijo. Las triviales van por PostgREST.
  if v_q is null and v_cpv_pref is null then
    raise exception 'buscar_licitaciones: requiere p_texto o p_cpv_prefijo (las busquedas triviales van por PostgREST).';
  end if;

  v_limit  := greatest(1, coalesce(p_por_pagina, 25));
  v_offset := greatest(0, (greatest(1, coalesce(p_pagina, 1)) - 1) * v_limit);
  v_campo  := case when p_orden_campo in ('fecha_fin_plazo','valor_estimado','fecha_publicacion')
                   then p_orden_campo else 'fecha_fin_plazo' end;
  v_dir    := case when p_orden_asc then 'asc' else 'desc' end;

  -- =====================================================================
  -- PROBE = conteo ACOTADO (hasta v_umbral+1) y clasificador de selectividad.
  -- NO ordena: usa GIN(tsv)/trigram para casar y se corta pronto -> barato.
  -- =====================================================================
  select count(*) into v_hit
  from (
    select 1
    from public.licitaciones
    where (v_q is null or tsv @@ v_q)
      and (p_cpv is null or array_length(p_cpv, 1) is null or cpv && p_cpv)
      and (v_cpv_pref is null or public.cpv_texto(cpv) like any (v_cpv_pref))
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
    limit v_umbral + 1
  ) s;

  if v_hit <= v_umbral then
    v_total := v_hit;      -- total EXACTO (el probe contó todo el subconjunto)
    v_aprox := false;
  else
    v_total := v_umbral;   -- "más de v_umbral"
    v_aprox := true;
    v_topado := true;
  end if;

  if not v_aprox then
    -- =================================================================
    -- SELECTIVO (<= tope): materializa el subconjunto (GIN/trigram primero)
    -- y ordena solo esas filas. Rápido a cualquier profundidad.
    -- =================================================================
    with filtrado as materialized (
      select licitacion_id, fecha_fin_plazo, valor_estimado, fecha_publicacion
      from public.licitaciones
      where (v_q is null or tsv @@ v_q)
        and (p_cpv is null or array_length(p_cpv, 1) is null or cpv && p_cpv)
        and (v_cpv_pref is null or public.cpv_texto(cpv) like any (v_cpv_pref))
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
    select coalesce(
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
    into v_filas;
  else
    -- =================================================================
    -- AMPLIO (> tope): sin materializar. Página por INDEX SCAN del índice de
    -- ORDEN (enable_sort=off obliga a obtener el orden del índice, no un Sort
    -- masivo) filtrando al vuelo. Denso -> las primeras 25 salen enseguida.
    -- Orden por columna+dirección CONCRETAS (v_campo/v_dir vienen de listas
    -- blancas) para que case EXACTAMENTE con un índice compuesto.
    -- =================================================================
    set local enable_sort = off;
    v_ord := v_campo || ' ' || v_dir || ' nulls last, licitacion_id asc';
    v_sql :=
         'select coalesce(json_agg(row_to_json(x) order by ' || v_ord || '), ''[]''::json) '
      || 'from ( '
      || '  select li.licitacion_id, li.titulo, li.objeto, li.organo_contratacion, li.cpv, li.fuente, '
      || '         li.presupuesto_con_iva, li.presupuesto_sin_iva, li.valor_estimado, '
      || '         li.fecha_publicacion, li.fecha_fin_plazo, li.lugar_ejecucion, li.ccaa, li.enlace '
      || '  from public.licitaciones li '
      || '  where ($1 is null or li.tsv @@ $1) '
      || '    and ($2 is null or array_length($2,1) is null or li.cpv && $2) '
      || '    and ($3 is null or public.cpv_texto(li.cpv) like any ($3)) '
      || '    and ($4 is null or li.fuente = $4) '
      || '    and ($5 is null or li.valor_estimado >= $5) '
      || '    and ($6 is null or li.valor_estimado <= $6) '
      || '    and ($7 is null or li.fecha_fin_plazo >= $7) '
      || '    and ($8 is null or li.fecha_fin_plazo <= $8) '
      || '    and ($9 is null or li.fecha_publicacion >= $9) '
      || '    and ($10 is null or li.fecha_publicacion <= $10) '
      || '    and ($11 = ''todas'' or ($11 = ''abierta'' and (li.fecha_fin_plazo >= now() or li.fecha_fin_plazo is null)) '
      || '         or ($11 = ''cerrada'' and li.fecha_fin_plazo < now())) '
      || '  order by li.' || v_campo || ' ' || v_dir || ' nulls last, li.licitacion_id asc '
      || '  limit ' || v_limit || ' offset ' || v_offset || ' ) x';
    execute v_sql
      into v_filas
      using v_q, p_cpv, v_cpv_pref, p_fuente, p_importe_min, p_importe_max,
            p_fin_desde, p_fin_hasta, p_pub_desde, p_pub_hasta, p_estado;
  end if;

  return json_build_object(
    'total',      v_total,
    'filas',      coalesce(v_filas, '[]'::json),
    'pagina',     greatest(1, coalesce(p_pagina, 1)),
    'porPagina',  v_limit,
    'aproximado', v_aprox,
    'topado',     v_topado   -- true = 'total' es el TOPE; la web muestra "más de N"
  );
end;
$$;

-- PRIVADO: solo authenticated (anon no; y la RLS protege por SECURITY INVOKER).
revoke all on function public.buscar_licitaciones(
  text, text[], text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer
) from public;
grant execute on function public.buscar_licitaciones(
  text, text[], text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer
) to authenticated;

-- Refresca el cache de esquema de PostgREST.
notify pgrst, 'reload schema';

-- ----------------------------------------------------------------------------
-- PRUEBAS / DIAGNÓSTICO: ver prueba_buscador.sql (EXPLAIN ANALYZE de "cai",
-- "9073", "90", "climatizacion" y de las ramas probe/selectiva/amplia).
-- ----------------------------------------------------------------------------
