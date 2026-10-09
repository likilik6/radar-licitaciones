-- ============================================================================
-- menores_rpc.sql — UNA sola llamada para la vista Menores
--
-- QUÉ SUSTITUYE (medido en menores_api.js antes de esto):
--   una búsqueda con filtros podía costar hasta 14 peticiones HTTP y 33,5 s de
--   presupuesto, en cuatro etapas en serie:
--     1. superaUmbral  -> 1 petición, 4,5 s   (pedir la fila nº 10.001)
--     2. listaClaves   -> hasta 11, 15 s      (todas las claves en tandas de 1.000)
--     3. contar        -> 1 petición, 7 s     (count exact)
--     4. paginaServidor-> 1 petición, 7 s     (las 25 filas que se pintan)
--   Esta función hace lo mismo en UNA petición.
--
-- LA REGLA QUE NO SE PUEDE ROMPER (y que una primera versión de este fichero rompió):
--   cuando el resultado CABE en el tope, la página se saca del subconjunto que ya se ha
--   leído, NUNCA volviendo a la tabla con ORDER BY. Para eso existe listaClaves en el JS.
--   Medido el 09/10/2026 con el nicho real de intereses.yaml (2.420 filas, no topado):
--     · volver a la tabla ordenando por IMPORTE ..... 24.939 ms  (el rol tiene 8 s: error)
--     · volver a la tabla ordenando por FECHA ....... 5.903 ms   (al filo)
--     · sacar la página del subconjunto + hidratar
--       esas 100 filas por clave primaria .......... 414 ms
--   El planner estima 10.084 filas donde hay 2.420, así que elige recorrer el índice de
--   importe de la tabla entera filtrando 1,75 M filas. Por eso el subconjunto se
--   materializa (`as materialized`) y de él salen las tres cosas: el recuento, la lista
--   ligera y las CLAVES de la página.
--
-- POR QUÉ DEVUELVE TAMBIÉN LA LISTA LIGERA:
--   el navegador la cachea 5 minutos (M_CADUCIDAD_ORDEN_MS) y con ella cambia de página y
--   de orden sin volver a pedir nada caro. Medido sobre las 1.752.459 filas de la tabla:
--   la lista ligera son 171 B/fila (1,63 MB por 10.000) y las 13 columnas completas 663 B
--   de media (6,3 MB por 10.000). Por eso la lista va ligera y las filas completas van
--   acotadas por p_filas_completas: con 100 son 66 KB y cubren las cuatro primeras
--   páginas. La lista solo se CONSTRUYE si el resultado no está topado (si lo está no
--   sirve de nada y serían 1,6 MB de JSON para tirarlos).
--
-- EL TOPE Y LAS DOS RAMAS:
--   el recorrido se acota a p_tope + 1 filas. Si caben (<= p_tope), el subconjunto está
--   COMPLETO: recuento exacto, lista válida y página sacada de ahí. Si NO caben, esas
--   p_tope + 1 filas son un subconjunto ARBITRARIO (el LIMIT no lleva ORDER BY), así que
--   no sirven para ordenar: se descartan y la página se pide a la tabla con el índice del
--   orden (OFFSET/LIMIT), que es lo que hacía paginaServidor.
--
-- EL ORDEN POR IMPORTE solo se desactiva si está topado Y HAY FILTROS, igual que el JS
--   (hayFiltros): sin filtros, ordenar por importe es recorrer el índice de importe de
--   cabo a rabo y cuesta 92 ms medidos, así que desactivarlo sería gratuito y dañino —
--   el aviso saldría permanente en el listado general. El rango de importe NO cuenta como
--   filtro, exactamente como en el JS.
--
-- EL ÓRGANO GIGANTE SÍ SE SALVA: con `organo_contratacion = '...'` se entra por un btree
--   y el LIMIT para el escaneo en seco; el clic en el órgano del SAS (230.608 filas) se
--   resuelve en 41 ms. Lo que no se salva es el bitmap de texto + CPV («servicio» + CPV 3:
--   2,3 s). La pista de tamaños por órgano del JS deja de ser imprescindible, pero no
--   estorba: evita la llamada entera cuando ya se sabe que irá topada.
--
-- UN TIMEOUT AQUÍ NO SE PUEDE COMPRAR DESDE DENTRO: el `statement_timeout` se arma al
--   empezar la sentencia de nivel superior, así que un `set local` en el cuerpo no amplía
--   nada. Si una llamada se pasa de los 8 s, el envoltorio debe reintentar con
--   p_filas_completas => 0 o p_con_lista => false, no esperar que la función se defienda.
--
-- IDEMPOTENTE: se puede ejecutar tantas veces como se quiera.
-- SOLO LECTURA: no escribe en ninguna tabla.
--
-- OJO AL SIGUIENTE CAMBIO DE FIRMA: el `drop function if exists` de abajo lleva los 19
--   tipos de AHORA. Si algún día se añade o se quita un parámetro, hay que AÑADIR el drop
--   de la firma vieja (no sustituirlo), porque si no quedan dos sobrecargas y, como todos
--   los argumentos tienen default, PostgREST no sabe elegir: PGRST203 y la vista Menores
--   deja de funcionar. buscador_rpc.sql arrastra tres drops acumulados justo por esto.
-- ============================================================================

drop function if exists public.menores_buscar(
  text, text[], text[], text, jsonb, text[], text, text,
  numeric, numeric, date, date, text, boolean, integer, integer, integer, integer, boolean
);

create or replace function public.menores_buscar(
  p_modo            text    default 'todo',   -- 'todo' | 'nicho' | 'cifs'
  p_cifs            text[]  default null,     -- modo 'cifs': CIF seguidos (GIN &&)
  p_nicho_cpv       text[]  default null,     -- modo 'nicho': prefijos CPV
  p_nicho_kw        text    default null,     -- modo 'nicho': palabras libres (websearch)
  p_nicho_excl      jsonb   default null,     -- modo 'nicho': [{"kw":"...","excluye":["...",...]}]
  p_cpv_prefijo     text[]  default null,     -- filtro CPV por prefijo (además del modo)
  p_texto           text    default null,     -- texto libre (objeto + órgano, vía tsv)
  p_organo          text    default null,     -- órgano comprador EXACTO
  p_importe_min     numeric default null,
  p_importe_max     numeric default null,
  p_fecha_desde     date    default null,
  p_fecha_hasta     date    default null,
  p_orden_campo     text    default 'fecha_adjudicacion',   -- lista blanca
  p_orden_asc       boolean default false,
  p_pagina          integer default 1,
  p_por_pagina      integer default 25,
  p_filas_completas integer default 100,      -- filas completas a devolver desde el inicio
  p_tope            integer default 10000,    -- M_UMBRAL en menores_api.js
  p_con_lista       boolean default true      -- false = no construir la lista ligera
)
returns json
language plpgsql
-- VOLATILE (no STABLE) por el mismo motivo que buscar_licitaciones: deja hacer ajustes
-- con SET LOCAL dentro del cuerpo si algún caso lo pide. Para una función de solo lectura
-- que se llama una vez por petición no tiene coste práctico.
volatile
security invoker
set search_path = extensions, public, pg_catalog
-- work_mem AQUÍ y no con `alter role authenticated`: supautils reserva ese rol y el ALTER
-- puede estar bloqueado. Con los 3,5 MB del plan Micro, un bitmap de texto + CPV se vuelve
-- «lossy» y hay que re-comprobar 502.723 filas (18-28 s medidos). Es USERSET, así que un
-- rol sin privilegios puede aplicarlo, y vale también para los EXECUTE del cuerpo.
set work_mem = '32MB'
as $$
declare
  v_where       text := 'true';
  v_or          text;
  v_trozos      text[];
  v_hay_filtros boolean := false;   -- MISMA definición que hayFiltros() en menores_api.js
  v_campo       text;
  v_dir         text;
  v_sql         text;
  v_tope        integer;
  v_pagina      integer;
  v_porpag      integer;
  v_completas   integer;
  v_offset      integer;
  v_lim         integer;
  v_off         integer;
  v_total       bigint;
  v_topado      boolean := false;
  v_lista       json;
  v_claves      text[];
  v_filas       json;
  v_sin_orden   boolean := false;   -- se pidió importe y no se puede
  v_fuera       boolean := false;   -- la página pedida está más allá del tope
  v_t0          timestamptz := clock_timestamp();
  v_cols        constant text :=
    'm.licitacion_id, m.objeto, m.cpv, m.importe_sin_iva, m.organo_contratacion, '
    'm.adjudicatario, m.cif_adjudicatario, m.cifs_adjudicatarios, m.fecha_adjudicacion, '
    'm.num_expediente, m.enlace, m.n_adjudicatarios, m.fuente';
begin
  -- ---- saneado de los números (nunca confiar en lo que llega del navegador) ----
  -- El tope se limita a 20.000: con 171 B/fila, 20.001 filas ya son 3,4 MB de JSON (y el
  -- doble transitorio al construirlo) en una instancia de 1 GB.
  v_tope      := least(greatest(coalesce(p_tope, 10000), 1), 20000);
  v_porpag    := least(greatest(coalesce(p_por_pagina, 25), 1), 200);
  -- Tope por arriba de la página: (p_pagina - 1) * p_por_pagina es aritmética de integer
  -- y con p_pagina ~ 86 millones desborda y revienta la llamada.
  v_pagina    := least(greatest(coalesce(p_pagina, 1), 1), 1000000);
  v_completas := least(greatest(coalesce(p_filas_completas, 100), 0), 2000);
  v_offset    := (v_pagina - 1) * v_porpag;

  -- ---- orden: LISTA BLANCA, nunca el texto que llegue ----
  if coalesce(p_orden_campo, '') = 'importe_sin_iva' then
    v_campo := 'importe_sin_iva';
  else
    v_campo := 'fecha_adjudicacion';           -- cualquier otra cosa cae aquí
  end if;
  -- DESC con NULLS LAST para que case con los índices existentes (menores_fecha_desc y
  -- menores_importe_desc_id los tienen así).
  v_dir := case when coalesce(p_orden_asc, false) then 'asc' else 'desc nulls last' end;

  -- ---- MODO -------------------------------------------------------------------
  if p_modo = 'cifs' then
    -- array[...] con cada CIF escapado uno a uno, en vez de %L sobre el array entero: así
    -- ni una coma ni una llave dentro de un valor pueden romper el literal.
    select string_agg(format('%L', btrim(c)), ',')
      into v_or
      from unnest(coalesce(p_cifs, '{}')) c
     where btrim(coalesce(c, '')) <> '';
    if coalesce(v_or, '') = '' then
      -- DIVERGENCIA DELIBERADA respecto al JS: allí, modo 'cifs' con la lista vacía no
      -- aplica filtro y devuelve la tabla entera disfrazada de «menores de este
      -- competidor» (el front se protege construyendo el array solo si hay CIF). Aquí se
      -- devuelve nada, que es el lado seguro: enseñar MÁS de lo pedido es el peor fallo.
      v_where := 'false';
    else
      v_where := v_where || format(' and m.cifs_adjudicatarios && array[%s]::text[]', v_or);
      v_hay_filtros := true;
    end if;

  elsif p_modo = 'nicho' then
    v_hay_filtros := true;
    v_trozos := '{}';
    -- Prefijos CPV del nicho. El ' ' delante marca el principio de un código dentro de
    -- cpv_txt (misma convención que menores_api.js: '* <pref>*'). btrim ANTES de validar:
    -- ' 33' no casa '^[0-9]+$' y la rama desaparecería en silencio, enseñando MENOS filas
    -- sin que nadie lo note, porque las otras ramas del OR siguen devolviendo cosas.
    -- intereses.yaml lo edita una persona: un "9073 " con espacio es cuestión de tiempo.
    select array_agg(format('m.cpv_txt ilike %L', '% ' || btrim(pre) || '%'))
      into v_trozos
      from unnest(coalesce(p_nicho_cpv, '{}')) pre
     where btrim(coalesce(pre, '')) ~ '^[0-9]+$';
    v_trozos := coalesce(v_trozos, '{}');
    -- palabras libres del nicho
    if btrim(coalesce(p_nicho_kw, '')) <> '' then
      v_trozos := v_trozos || format(
        'm.tsv @@ websearch_to_tsquery(''spanish'', unaccent(%L))', btrim(p_nicho_kw));
    end if;
    -- Palabras CON EXCLUSIÓN: entra por esa palabra solo si no casa ninguna de sus
    -- exclusiones. Acotada a su palabra, nunca global (ver menores_nicho.py).
    -- Se recorre con jsonb_array_elements y NO con jsonb_to_recordset: éste LANZA si el
    -- array trae algo que no sea un objeto ('[1,2]' -> «must be an array of objects»), y
    -- jsonb_array_elements_text LANZA si 'excluye' no es un array ('cpap' -> «cannot
    -- extract elements from a scalar»). Al convertirse esto en el API, la forma del jsonb
    -- ya no la garantiza menores_nicho.py y un 500 tumbaría la vista entera.
    if p_nicho_excl is not null and jsonb_typeof(p_nicho_excl) = 'array' then
      v_trozos := v_trozos || (
        select coalesce(array_agg(
          '(' || format('m.tsv @@ websearch_to_tsquery(''spanish'', unaccent(%L))',
                        btrim(g->>'kw'))
              || coalesce((
                   select string_agg(format(
                     ' and not m.tsv @@ websearch_to_tsquery(''spanish'', unaccent(%L))',
                     btrim(e)), '')
                   from jsonb_array_elements_text(
                          case when jsonb_typeof(g->'excluye') = 'array'
                               then g->'excluye' else '[]'::jsonb end) e
                  where btrim(coalesce(e, '')) <> ''), '')
              || ')'), '{}')
        from jsonb_array_elements(p_nicho_excl) g
        where jsonb_typeof(g) = 'object' and btrim(coalesce(g->>'kw', '')) <> ''
      );
    end if;
    -- Sin ninguna condición de nicho NO se devuelve toda la tabla como «nicho».
    v_or := array_to_string(v_trozos, ' or ');
    v_where := v_where || ' and (' || coalesce(nullif(v_or, ''), 'false') || ')';
  end if;

  -- ---- filtros sueltos (se suman al modo) -------------------------------------
  if p_cpv_prefijo is not null and cardinality(p_cpv_prefijo) > 0 then
    select string_agg(format('m.cpv_txt ilike %L', '% ' || btrim(pre) || '%'), ' or ')
      into v_or
      from unnest(p_cpv_prefijo) pre
     where btrim(coalesce(pre, '')) ~ '^[0-9]+$';
    -- Si se pidió algún prefijo NO VACÍO y ninguno era válido, no se devuelve nada: dejar
    -- caer el filtro enseñaría MÁS de lo pedido. Pero un array de vacíos o de NULL no es
    -- «he pedido un filtro»: eso no debe dejar la vista en cero.
    if exists (select 1 from unnest(p_cpv_prefijo) x where btrim(coalesce(x, '')) <> '') then
      v_where := v_where || ' and (' || coalesce(nullif(v_or, ''), 'false') || ')';
      v_hay_filtros := true;
    end if;
  end if;

  -- El tsv se guardó con unaccent, así que la consulta también va sin tildes. btrim antes
  -- de decidir si hay texto: con '  ' la tsquery sale VACÍA y no casa con nada, o sea
  -- CERO filas, donde el JS (mTexto recorta y devuelve null) no filtra y devuelve todas.
  if btrim(coalesce(p_texto, '')) <> '' then
    v_where := v_where || format(
      ' and m.tsv @@ websearch_to_tsquery(''spanish'', unaccent(%L))', btrim(p_texto));
    v_hay_filtros := true;
  end if;
  if btrim(coalesce(p_organo, '')) <> '' then
    v_where := v_where || format(' and m.organo_contratacion = %L', btrim(p_organo));
    v_hay_filtros := true;
  end if;
  -- nullif ... 'NaN': PostgREST acepta "NaN" para un numeric y en numeric NaN es el MAYOR
  -- de todos, así que p_importe_min => 'NaN' dejaría la vista en cero sin motivo.
  -- El rango de importe NO pone v_hay_filtros: igual que hayFiltros() en el JS.
  if nullif(p_importe_min, 'NaN') is not null then
    v_where := v_where || format(' and m.importe_sin_iva >= %L::numeric', p_importe_min);
  end if;
  if nullif(p_importe_max, 'NaN') is not null then
    v_where := v_where || format(' and m.importe_sin_iva <= %L::numeric', p_importe_max);
  end if;
  if p_fecha_desde is not null then
    v_where := v_where || format(' and m.fecha_adjudicacion >= %L::date', p_fecha_desde);
    v_hay_filtros := true;
  end if;
  if p_fecha_hasta is not null then
    v_where := v_where || format(' and m.fecha_adjudicacion <= %L::date', p_fecha_hasta);
    v_hay_filtros := true;
  end if;

  -- ---- ventana de filas completas ---------------------------------------------
  -- Si la página pedida CABE en el bloque inicial de p_filas_completas, se devuelve el
  -- bloque entero desde el principio y el navegador cambia de página dentro de él sin
  -- volver a la base. Si no cabe (página profunda), solo esa página: devolver «desde el
  -- principio hasta la página 40» crecería sin límite.
  if (v_offset + v_porpag) <= v_completas then
    v_lim := v_completas; v_off := 0;
  else
    v_lim := v_porpag;    v_off := v_offset;
  end if;
  -- Más allá del tope no hay NADA que enseñar (la vista nunca pasa de p_tope filas: con
  -- 25 por página, el tope de 10.000 es la página 400). Y un OFFSET profundo no es gratis
  -- aunque no devuelva nada: medido con p_pagina = 1.000.000, el OFFSET de 25 millones
  -- sobre la tabla tardó 21,5 s —más del doble del timeout del rol— para devolver cero
  -- filas. Se corta aquí y no se pide la página.
  v_fuera := v_offset >= v_tope;

  -- ---- Q1: UN recorrido materializado -> recuento + lista + claves de la página
  -- `as materialized` es obligatorio: el subconjunto se usa tres veces y sin eso el LIMIT
  -- se recalcularía en cada uso. Las tres cosas salen de subconsultas escalares dentro de
  -- un CASE, así que lo que no haga falta NO se evalúa (ni la lista de 1,6 MB si está
  -- topado, ni las claves).
  v_sql := format($q$
    with acotado as materialized (
      select m.licitacion_id, m.importe_sin_iva, m.fecha_adjudicacion, m.fuente
        from public.menores m
       where %1$s
       limit %2$s
    ), n as (select count(*)::bigint c from acotado)
    select (select c from n),
           case when (select c from n) <= %3$s and %4$s
                then (select coalesce(json_agg(json_build_object(
                        'licitacion_id',      licitacion_id,
                        'importe_sin_iva',    importe_sin_iva,
                        'fecha_adjudicacion', fecha_adjudicacion,
                        'fuente',             fuente)), '[]'::json) from acotado)
           end,
           case when (select c from n) <= %3$s
                then (select array_agg(p.licitacion_id
                               order by p.%5$s %6$s, p.licitacion_id asc)
                        from (select * from acotado
                               order by %5$s %6$s, licitacion_id asc
                               limit %7$s offset %8$s) p)
           end
  $q$, v_where, v_tope + 1, v_tope,
       case when p_con_lista then 'true' else 'false' end,
       v_campo, v_dir, v_lim, v_off);
  execute v_sql into v_total, v_lista, v_claves;

  if v_total > v_tope then
    v_topado := true;
    v_total  := v_tope;
    v_lista  := null;
    v_claves := null;
    v_lim    := v_porpag;
    v_off    := v_offset;
    -- Solo CON FILTROS: con un filtro, ordenar por importe obliga a recorrer el índice de
    -- importe de toda la tabla fila a fila (SAS por importe: más de 30 s). Sin filtros son
    -- 92 ms medidos y desactivarlo sería poner un aviso permanente por nada.
    if v_campo = 'importe_sin_iva' and v_hay_filtros then
      v_campo     := 'fecha_adjudicacion';
      v_dir       := 'desc nulls last';
      v_sin_orden := true;
    end if;
  end if;

  -- ---- Q2: las filas completas -------------------------------------------------
  -- licitacion_id como desempate: sin él, dos filas con la misma fecha pueden salir en
  -- distinto orden en dos peticiones y la paginación se descuadra. El ORDER BY va DENTRO
  -- del json_agg, explícito, y no confiando en el de la subconsulta.
  if v_completas = 0 or v_fuera then
    v_filas := '[]'::json;
  elsif v_claves is not null then
    -- NO topado: las claves ya están elegidas y ordenadas; solo hay que hidratarlas por
    -- clave primaria. Medido: 100 filas en 414 ms, frente a 24.939 ms volviendo a la
    -- tabla con ORDER BY sobre el nicho. Las claves van por parámetro ($1) y no
    -- interpoladas: un licitacion_id es una URL y puede llevar comas.
    v_sql := format(
      'select coalesce(json_agg(t order by t.%s %s, t.licitacion_id asc), ''[]''::json) '
      'from (select %s from public.menores m where m.licitacion_id = any($1)) t',
      v_campo, v_dir, v_cols);
    execute v_sql into v_filas using v_claves;
  else
    -- Topado (o sin lista pedida): el subconjunto es arbitrario, así que la página se pide
    -- a la tabla con el índice del orden. Es lo que hacía paginaServidor en el JS.
    v_sql := format(
      'select coalesce(json_agg(t order by t.%s %s, t.licitacion_id asc), ''[]''::json) '
      'from (select %s from public.menores m where %s '
      'order by m.%s %s, m.licitacion_id asc limit %s offset %s) t',
      v_campo, v_dir, v_cols, v_where, v_campo, v_dir, v_lim, v_off);
    execute v_sql into v_filas;
  end if;

  return json_build_object(
    'filas',            coalesce(v_filas, '[]'::json),
    'desde',            v_off,                   -- a qué fila del orden corresponde filas[0]
    'lista',            v_lista,                 -- null si topado o p_con_lista=false
    'total',            v_total,
    'topado',           v_topado,                -- true = 'total' es el TOPE
    'fuera_de_rango',   v_fuera,                 -- la página pedida pasa del tope
    'hay_filtros',      v_hay_filtros,
    'orden_campo',      v_campo,
    'orden_asc',        coalesce(p_orden_asc, false) and not v_sin_orden,
    'orden_importe_desactivado', v_sin_orden,
    'tope',             v_tope,
    -- epoch y no milliseconds: extract(milliseconds from interval) devuelve los
    -- milisegundos DEL CAMPO segundos (0-59999), así que un minuto largo daría 5.000.
    'ms',               round(extract(epoch from clock_timestamp() - v_t0) * 1000)
  );
end;
$$;

-- PRIVADO: solo 'authenticated'. SECURITY INVOKER + la RLS de la tabla siguen mandando;
-- esto solo evita que 'anon' pueda llamarla. OJO: `revoke ... from public` NO quita el
-- permiso de anon, porque en este proyecto anon tiene un grant EXPLÍCITO por el ALTER
-- DEFAULT PRIVILEGES de Supabase (comprobado: el ACL por defecto de las funciones de
-- public es {postgres=X, anon=X, authenticated=X, service_role=X}). Hay que revocar POR
-- ROL. Es justo lo que le falta hoy a public.buscar_licitaciones, que tiene anon=X en
-- producción.
revoke all on function public.menores_buscar(
  text, text[], text[], text, jsonb, text[], text, text,
  numeric, numeric, date, date, text, boolean, integer, integer, integer, integer, boolean
) from public, anon, authenticated;

grant execute on function public.menores_buscar(
  text, text[], text[], text, jsonb, text[], text, text,
  numeric, numeric, date, date, text, boolean, integer, integer, integer, integer, boolean
) to authenticated;

-- Para que PostgREST vea la función nueva sin esperar a su refresco.
notify pgrst, 'reload schema';

-- ============================================================================
-- CÓMO COMPROBAR QUE HA IDO BIEN (todo SELECT, nada escribe)
--
-- 1) ¿Existe, con la firma esperada, SECURITY INVOKER y work_mem dentro?
--      select p.proname, p.provolatile, p.prosecdef, p.proconfig,
--             pg_get_function_identity_arguments(p.oid) as args
--        from pg_proc p join pg_namespace n on n.oid = p.pronamespace
--       where n.nspname = 'public' and p.proname = 'menores_buscar';
--    Esperado: UNA sola fila (si salen dos, hay dos sobrecargas y PostgREST fallará con
--    PGRST203); provolatile = 'v'; prosecdef = false; y en proconfig las dos entradas,
--    search_path=extensions,public,pg_catalog y work_mem=32MB.
--
-- 2) ¿Permisos como toca? Mirando el ACL crudo, que no engaña:
--      select p.proacl
--        from pg_proc p join pg_namespace n on n.oid = p.pronamespace
--       where n.nspname = 'public' and p.proname = 'menores_buscar';
--    Esperado: aparecen authenticated=X, el dueño (postgres=X) y service_role=X — los dos
--    últimos vienen del ALTER DEFAULT PRIVILEGES de Supabase y son normales. Lo que NO
--    debe aparecer es «anon=X» ni una entrada que empiece por «=X/» (que es PUBLIC).
--    (No uses information_schema.role_routine_grants para esto: ejecutada como postgres
--    lista también al dueño y a service_role, y parece un fallo cuando no lo es.)
--
-- 3) Prueba de humo, con un resultado pequeño y ordenando por IMPORTE, que es el caso que
--    una primera versión de esta función rompía:
--      select (public.menores_buscar(
--                p_organo => 'Servicio Andaluz de Salud',
--                p_texto  => 'ventilacion',
--                p_orden_campo => 'importe_sin_iva',
--                p_filas_completas => 100)::jsonb - 'lista' - 'filas') as resumen;
--    Esperado: topado = false, orden_campo = importe_sin_iva,
--    orden_importe_desactivado = false, y ms de pocos cientos.
--
-- 4) El listado general por importe, que no debe desactivar el orden:
--      select (public.menores_buscar(p_orden_campo => 'importe_sin_iva')::jsonb
--              - 'lista' - 'filas') as resumen;
--    Esperado: topado = true, hay_filtros = false, orden_campo = importe_sin_iva,
--    orden_importe_desactivado = FALSE.
--
-- 5) El caso que importa de verdad («servicio + CPV 3» y el clic en el órgano del SAS) se
--    mide desde el navegador, con sesión. Eso lo hago yo cuando digas que está ejecutado.
-- ============================================================================
