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
--   · CONTEO ACOTADO + PÁGINA EN UNA CONSULTA: se materializa el subconjunto CAP-ado
--     a v_umbral+1 (barrera; GIN/trigram primero) y de ahí salen conteo y página. Si
--     NO se trunca (hit<=tope) -> SELECTIVO: total EXACTO y página correcta en UN
--     escaneo. Si se trunca (hit=tope+1) -> AMPLIO: total = tope con topado=true (la
--     web pone "más de N") y la página se recalcula por INDEX SCAN del índice de
--     ORDEN (enable_sort=off) filtrando al vuelo (sin materializar el conjunto enorme).
--   · CPV-PREFIJO por índice: cpv_texto(cpv) (' '+códigos) + LIKE '% <pref>%' se
--     apoya en el GIN trigram (ver buscador_indices.sql). Varios prefijos = OR.
--
-- BG-4 inc2 (fix 0A000 + REGRESIÓN de sargabilidad) — ESTA VERSIÓN:
--   · 0A000 + plan de la SELECTIVA: la función es VOLATILE y enable_sort=off se fija
--     con `SET LOCAL` DENTRO de la rama AMPLIA (no a nivel de función). Fijarlo a
--     nivel de función afectaba a la SELECTIVA (CTE materializada + join): los
--     disable_cost del Sort se propagaban y elegía un plan pésimo -> timeout. Con
--     SET LOCAL acotado, la SELECTIVA planifica normal (Bitmap Index Scan + Sort de
--     un subconjunto pequeño). VOLATILE permite SET en el cuerpo (STABLE daría 0A000).
--   · SARGABILIDAD: el patrón `(v_q is null OR tsv @@ v_q)` es NO-SARGABLE -> el
--     planner NO usa licitaciones_tsv_gin -> Seq Scan de 588k -> hasta un término
--     SELECTIVO (climatización) hacía que el probe (limit 5001) escanease toda la
--     tabla -> timeout. FIX: el WHERE se CONSTRUYE dinámicamente e INLINEADO con
--     quote_literal (sin params, sin IS NULL OR): `tsv @@ '...'::tsquery` solo si
--     hay texto y `cpv_texto(cpv) like any('...'::text[])` solo si hay prefijo.
--     Así el predicato que dirige el índice es una constante -> Bitmap Index Scan.
--     Las tres consultas (probe/selectiva/amplia) comparten ese WHERE.
--
-- Enrutado desde la web: por AQUÍ van las búsquedas con texto, prefijo CPV O Nº de
-- expediente. Las triviales (sin nada de eso) siguen por PostgREST. El CPV EXACTO
-- (p_cpv, overlaps `&&`) se mantiene. PRIVADO: execute solo a authenticated +
-- SECURITY INVOKER (respeta RLS).
--
-- EXPEDIENTE (nuevo): p_expediente filtra por public.norm_expediente(num_expediente)
-- LIKE '%<frag normalizado>%' (contiene, ignora / . - y mayúsculas). Se apoya en el
-- índice GIN trigram licitaciones_expediente_trgm (ver expediente_schema.sql, correr
-- ANTES). Solo dispara con >=3 chars normalizados. Añadir p_expediente cambia la firma
-- (15 -> 16 args): hay que DROPear la de 15 antes de crear (create or replace no basta).
-- ============================================================================

-- (Idempotente) Asegura el GIN sobre tsv por si se corre en un entorno limpio.
create index if not exists licitaciones_tsv_gin on public.licitaciones using gin (tsv);

-- Limpia la firma vieja de BG-3 (14 args, sin p_cpv_prefijo) si aún existiera.
drop function if exists public.buscar_licitaciones(
  text, text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer
);
-- Limpia la firma de BG-4 (15 args, sin p_expediente): añadir el arg crea un OVERLOAD
-- que haría ambigua la llamada; hay que quitarla antes de recrear con 16 args.
drop function if exists public.buscar_licitaciones(
  text, text[], text[], text, numeric, numeric, text,
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
  p_por_pagina   integer     default 25,
  p_expediente   text        default null        -- Nº de expediente (contiene, normalizado; >=3 chars)
)
returns json
language plpgsql
-- VOLATILE (no STABLE): necesario para poder hacer `SET LOCAL enable_sort = off`
-- DENTRO del cuerpo, ACOTADO a la rama AMPLIA. Fijarlo a nivel de función afectaba
-- también a la SELECTIVA y le arruinaba el plan (los disable_cost=1e10 del Sort se
-- propagaban -> elegía p.ej. Seq Scan de 588k en el join -> timeout). Para un RPC
-- de solo lectura llamado una vez por request, VOLATILE no tiene coste práctico.
volatile
security invoker
set search_path = extensions, public, pg_catalog
as $$
declare
  v_q        tsquery;
  v_cpv_pref text[];
  v_exp      text;
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
  v_combo    json;
  -- Columnas de salida (mismas que COLUMNAS en buscador_api.js). Sin qualificar:
  -- resuelven contra la única tabla del FROM / el join por licitacion_id.
  v_cols     constant text :=
    'licitacion_id, titulo, objeto, organo_contratacion, cpv, fuente, '
    'presupuesto_con_iva, presupuesto_sin_iva, valor_estimado, '
    'fecha_publicacion, fecha_fin_plazo, lugar_ejecucion, ccaa, enlace, num_expediente';
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

  -- Nº de expediente (opcional): se normaliza IGUAL que el índice; solo dispara con
  -- >=3 chars normalizados (un fragmento más corto casaría cientos de miles de filas).
  if p_expediente is not null and length(public.norm_expediente(p_expediente)) >= 3 then
    v_exp := public.norm_expediente(p_expediente);
  end if;

  -- Solo con texto O prefijo CPV O expediente. Las triviales van por PostgREST.
  if v_q is null and v_cpv_pref is null and v_exp is null then
    raise exception 'buscar_licitaciones: requiere p_texto, p_cpv_prefijo o p_expediente (>=3 chars) (las busquedas triviales van por PostgREST).';
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
  if v_exp is not null then
    -- CONTIENE el fragmento normalizado; predicado CONSTANTE -> Bitmap Index Scan del
    -- índice GIN trigram sobre public.norm_expediente(num_expediente). Se escapan los
    -- metacaracteres LIKE (\ % _). La expresión del LIKE coincide EXACTAMENTE con la del
    -- índice (misma función cualificada) para que el planner lo use.
    v_where := v_where || ' and public.norm_expediente(num_expediente) like '
      || quote_literal('%' || replace(replace(replace(v_exp, '\', '\\'), '%', '\%'), '_', '\_') || '%');
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
  -- UNA SOLA CONSULTA (conteo + página) para el caso SELECTIVO, evitando el doble
  -- escaneo. Materializa el subconjunto CAP-ado a v_umbral+1 (barrera; filtra por
  -- GIN/trigram PRIMERO) y de ahí saca el CONTEO ACOTADO y la PÁGINA.
  --   · Si NO se trunca (hit <= tope): el subconjunto está COMPLETO -> conteo
  --     EXACTO y página CORRECTA. Es la rama SELECTIVA, en UN único escaneo.
  --   · Si se trunca (hit = tope+1): es AMPLIO -> esta página NO vale (orden sobre
  --     un subconjunto ARBITRARIO de tope+1 filas); se recalcula por índice abajo.
  -- Corre con enable_sort NORMAL (el SET LOCAL de la rama amplia va después): así
  -- el Sort del subconjunto pequeño es barato y NO se distorsiona el plan.
  -- =====================================================================
  v_sql :=
       'with filtrado as materialized ('
    || ' select licitacion_id, fecha_fin_plazo, valor_estimado, fecha_publicacion'
    || ' from public.licitaciones where ' || v_where
    || ' limit ' || (v_umbral + 1)
    || '), pagina as ('
    || ' select licitacion_id from filtrado order by ' || v_ord
    || ' limit ' || v_limit || ' offset ' || v_offset
    || ') select json_build_object('
    || '''hit'', (select count(*) from filtrado),'
    || '''filas'', coalesce((select json_agg(row_to_json(x) order by ' || v_ord || ')'
    || ' from (select ' || v_cols || ' from pagina pg join public.licitaciones li using (licitacion_id)) x), ''[]''::json))';
  execute v_sql into v_combo;
  v_hit := (v_combo->>'hit')::bigint;

  if v_hit <= v_umbral then
    -- SELECTIVO: subconjunto completo -> total EXACTO y la página del combo YA vale.
    v_total := v_hit;
    v_filas := v_combo->'filas';
  else
    -- =================================================================
    -- AMPLIO (> tope): la página del combo NO vale (orden sobre subconjunto
    -- arbitrario). Se recalcula SIN materializar: INDEX SCAN del índice de ORDEN
    -- filtrando al vuelo (denso -> primeras 25 enseguida). v_ord = columna+dirección
    -- concretas (listas blancas) que casan con un índice compuesto. enable_sort=off
    -- (SET LOCAL, SOLO aquí; requiere función VOLATILE) obliga a orden por índice.
    -- =================================================================
    v_total  := v_umbral;   -- "más de v_umbral"
    v_aprox  := true;
    v_topado := true;
    set local enable_sort = off;
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
-- Firma de 16 args (con p_expediente al final).
revoke all on function public.buscar_licitaciones(
  text, text[], text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer, text
) from public;
grant execute on function public.buscar_licitaciones(
  text, text[], text[], text, numeric, numeric, text,
  timestamptz, timestamptz, timestamptz, timestamptz, text, boolean, integer, integer, text
) to authenticated;

-- Refresca el cache de esquema de PostgREST.
notify pgrst, 'reload schema';

-- ----------------------------------------------------------------------------
-- PRUEBAS / DIAGNÓSTICO: ver prueba_buscador.sql (EXPLAIN del probe -> debe ser
-- Bitmap Index Scan y NO Seq Scan; y los 4 casos cai/9073/90/climatizacion).
-- ----------------------------------------------------------------------------
