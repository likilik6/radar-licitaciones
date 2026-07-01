-- buscador_rpc.sql  ·  BG-3/BG-4: RPC de escalada de la búsqueda
-- ============================================================================
-- REQUISITO: ejecutar ANTES buscador_indices.sql (crea public.cpv_texto, el
-- índice GIN trigram y los índices de orden en los que se apoya esta RPC).
--
-- BG-3 (por qué la RPC): con un término poco frecuente, `WHERE tsv @@ q ORDER BY
-- fecha LIMIT 25` hacía que el planner recorriese el índice de fecha filtrando el
-- tsv al vuelo -> 16,9 s -> timeout 57014. Se resolvió filtrando por el GIN(tsv)
-- PRIMERO y ordenando el subconjunto pequeño.
--
-- BG-4 inc2 — timeout con términos/prefijos AMPLIOS ("cai", CPV "9073"/"90").
-- Causas: (1) conteo EXACTO de cientos de miles; (2) CPV-prefijo sin índice;
-- (3) materializar un subconjunto enorme para ordenar+limitar (trae del heap
-- TODAS las filas que casan aunque solo se usen 25).
--
-- BG-4 inc2 (fix rendimiento) — SOLUCIÓN (sin tocar statement_timeout):
--   · CONTEO ACOTADO: un "probe" cuenta hasta un TOPE (v_umbral) y clasifica. Si
--     <= tope -> total EXACTO; si lo supera -> tope con topado=true (la web pone
--     "más de N"). El probe NO ordena -> barato.
--   · PLAN ADAPTATIVO: SELECTIVO(<=tope) -> CTE MATERIALIZED (GIN/trigram primero)
--     + orden del subconjunto pequeño. AMPLIO(>tope) -> sin materializar, página
--     por INDEX SCAN del índice de ORDEN (enable_sort=off) filtrando al vuelo.
--   · CPV-PREFIJO por índice: cpv_texto(cpv) (' '+códigos) + LIKE '% <pref>%' se
--     apoya en el GIN trigram (ver buscador_indices.sql). Varios prefijos = OR.
--
-- BG-4 inc2 (fix 0A000 + REGRESIÓN de sargabilidad) — ESTA VERSIÓN:
--   · 0A000: los GUC (enable_sort) van en la cláusula SET de la función, NO en el
--     cuerpo (SET en el cuerpo está prohibido en funciones STABLE).
--   · SARGABILIDAD: el patrón `(v_q is null OR tsv @@ v_q)` es NO-SARGABLE -> el
--     planner NO usa licitaciones_tsv_gin -> Seq Scan de 588k -> hasta un término
--     SELECTIVO (climatización) hacía que el probe (limit 5001) escanease toda la
--     tabla -> timeout. FIX: el WHERE se CONSTRUYE dinámicamente e INLINEADO con
--     quote_literal (sin params, sin IS NULL OR): `tsv @@ '...'::tsquery` solo si
--     hay texto y `cpv_texto(cpv) like any('...'::text[])` solo si hay prefijo.
--     Así el predicato que dirige el índice es una constante -> Bitmap Index Scan.
--     Las tres consultas (probe/selectiva/amplia) comparten ese WHERE.
--
-- Enrutado desde la web: por AQUÍ van las búsquedas con texto O con prefijo CPV.
-- Las triviales (sin texto ni prefijo) siguen por PostgREST. El CPV EXACTO (p_cpv,
-- overlaps `&&`) se mantiene. PRIVADO: execute solo a authenticated + SECURITY
-- INVOKER (respeta RLS). Firma sin cambios (15 args) -> create or replace basta.
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
-- GUC a nivel de FUNCIÓN (NO en el cuerpo: `SET` en el cuerpo daría 0A000 al ser
-- STABLE). enable_sort=off: la rama AMPLIA obtiene el orden del ÍNDICE (Index
-- Scan) en vez de un Sort masivo; en la SELECTIVA el sort del subconjunto (<=tope)
-- es un soft-disable (se hace igual, pocas filas).
set enable_sort = off
as $$
declare
  v_q        tsquery;
  v_cpv_pref text[];
  v_campo    text;
  v_dir      text;
  v_ord      text;
  v_where    text;
  v_sql      text;
  v_limit    integer;
  v_offset   integer;
  v_total    bigint;
  v_hit      bigint;
  v_aprox    boolean := false;
  v_topado   boolean := false;
  v_filas    json;
  -- Columnas de salida (mismas que COLUMNAS en buscador_api.js). Sin qualificar:
  -- resuelven contra la única tabla del FROM / el join por licitacion_id.
  v_cols     constant text :=
    'licitacion_id, titulo, objeto, organo_contratacion, cpv, fuente, '
    'presupuesto_con_iva, presupuesto_sin_iva, valor_estimado, '
    'fecha_publicacion, fecha_fin_plazo, lugar_ejecucion, ccaa, enlace';
  -- TOPE del conteo exacto y frontera selectivo/amplio. 5000 deja el conteo EXACTO
  -- para casi todo (climatización≈3146) y solo lo muy amplio queda aproximado;
  -- la rama SELECTIVA materializa como mucho ~5000 filas (rápido).
  v_umbral   constant integer := 5000;
begin
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
                   then p_orden_campo else 'fecha_fin_plazo' end;   -- lista blanca
  v_dir    := case when p_orden_asc then 'asc' else 'desc' end;     -- lista blanca
  v_ord    := v_campo || ' ' || v_dir || ' nulls last, licitacion_id asc';

  -- =====================================================================
  -- WHERE dinámico e INLINEADO (quote_literal -> seguro; SARGABLE: el predicado
  -- que dirige el índice (tsv / cpv_texto) es una CONSTANTE, no `param IS NULL OR`).
  -- Solo se añaden las condiciones presentes. Se comparte en las 3 consultas.
  -- =====================================================================
  v_where := 'true';
  if v_q is not null then
    v_where := v_where || ' and tsv @@ ' || quote_literal(v_q::text) || '::tsquery';
  end if;
  if v_cpv_pref is not null then
    v_where := v_where || ' and public.cpv_texto(cpv) like any (' || quote_literal(v_cpv_pref::text) || '::text[])';
  end if;
  if p_cpv is not null and array_length(p_cpv, 1) is not null then
    v_where := v_where || ' and cpv && ' || quote_literal(p_cpv::text) || '::text[]';
  end if;
  if p_fuente is not null then
    v_where := v_where || ' and fuente = ' || quote_literal(p_fuente);
  end if;
  if p_importe_min is not null then
    v_where := v_where || ' and valor_estimado >= ' || p_importe_min::text;   -- numeric: sin comillas
  end if;
  if p_importe_max is not null then
    v_where := v_where || ' and valor_estimado <= ' || p_importe_max::text;
  end if;
  if p_fin_desde is not null then
    v_where := v_where || ' and fecha_fin_plazo >= ' || quote_literal(p_fin_desde::text) || '::timestamptz';
  end if;
  if p_fin_hasta is not null then
    v_where := v_where || ' and fecha_fin_plazo <= ' || quote_literal(p_fin_hasta::text) || '::timestamptz';
  end if;
  if p_pub_desde is not null then
    v_where := v_where || ' and fecha_publicacion >= ' || quote_literal(p_pub_desde::text) || '::timestamptz';
  end if;
  if p_pub_hasta is not null then
    v_where := v_where || ' and fecha_publicacion <= ' || quote_literal(p_pub_hasta::text) || '::timestamptz';
  end if;
  if p_estado = 'abierta' then
    v_where := v_where || ' and (fecha_fin_plazo >= now() or fecha_fin_plazo is null)';
  elsif p_estado = 'cerrada' then
    v_where := v_where || ' and fecha_fin_plazo < now()';
  end if;  -- 'todas' (o cualquier otro): sin filtro de estado

  -- =====================================================================
  -- PROBE = conteo ACOTADO (hasta v_umbral+1) y clasificador. NO ordena; el
  -- predicado sargable -> Bitmap Index Scan (GIN/trigram) y se corta pronto.
  -- =====================================================================
  execute 'select count(*) from (select 1 from public.licitaciones where '
       || v_where || ' limit ' || (v_umbral + 1) || ') s'
    into v_hit;

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
    -- SELECTIVO (<= tope): CTE MATERIALIZED (filtra por GIN/trigram PRIMERO,
    -- barrera de optimización) y ordena solo ese subconjunto pequeño. Rápido a
    -- cualquier profundidad de página.
    -- =================================================================
    v_sql :=
         'with filtrado as materialized ('
      || ' select licitacion_id, fecha_fin_plazo, valor_estimado, fecha_publicacion'
      || ' from public.licitaciones where ' || v_where
      || '), pagina as ('
      || ' select licitacion_id from filtrado order by ' || v_ord
      || ' limit ' || v_limit || ' offset ' || v_offset
      || ') select coalesce((select json_agg(row_to_json(x) order by ' || v_ord || ')'
      || ' from (select ' || v_cols || ' from pagina pg join public.licitaciones li using (licitacion_id)) x), ''[]''::json)';
    execute v_sql into v_filas;
  else
    -- =================================================================
    -- AMPLIO (> tope): sin materializar. Página por INDEX SCAN del índice de
    -- ORDEN (enable_sort=off, a nivel de función) filtrando al vuelo. Denso ->
    -- las primeras 25 salen enseguida. v_ord usa columna+dirección concretas
    -- (listas blancas) que casan EXACTAMENTE con un índice compuesto.
    -- =================================================================
    v_sql :=
         'select coalesce(json_agg(row_to_json(x) order by ' || v_ord || '), ''[]''::json)'
      || ' from (select ' || v_cols || ' from public.licitaciones where ' || v_where
      || ' order by ' || v_ord || ' limit ' || v_limit || ' offset ' || v_offset || ') x';
    execute v_sql into v_filas;
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
-- PRUEBAS / DIAGNÓSTICO: ver prueba_buscador.sql (EXPLAIN del probe -> debe ser
-- Bitmap Index Scan y NO Seq Scan; y los 4 casos cai/9073/90/climatizacion).
-- ----------------------------------------------------------------------------
