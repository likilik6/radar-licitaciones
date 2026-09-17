-- ============================================================================
-- menores_autonomicos_schema.sql · F0 de «Menores autonómicos» (17/09/2026)
--
-- QUÉ: prepara public.menores para recibir los contratos menores de las plataformas
-- autonómicas (F1 Andalucía; luego País Vasco, Cataluña, La Rioja, Galicia, Asturias)
-- SIN reescribir las ~1,39 M filas estatales que ya hay. NO carga datos.
--
-- DECISIONES (Cowork, 17/09/2026):
--   · Tabla única public.menores. La procedencia va en la columna `fuente`, que YA
--     existe (default 'estatal'; hoy la tienen todas las filas). No hay columna `origen`.
--   · Clave primaria con id propio por fuente (p. ej. 'and:766139'), que no choca con
--     las URL de la PLACSP que usan las filas estatales.
--   · Retención: 3 años móviles por fecha_adjudicacion, igual para todas las fuentes.
--
-- CÓMO USARLO: pega TODO en el SQL Editor de Supabase y pulsa Run. Idempotente.
-- ORDEN: este SQL ANTES que cualquier código que escriba filas autonómicas (lección de
-- ccaa_schema.sql: si PostgREST no ve una columna, rechaza el lote entero).
--
-- NO toca: el Radar, public.licitaciones (catálogo del Buscador), adjudicaciones,
-- competidores, ni los datos de public.menores. No crea índices.
--
-- COLUMNAS NUEVAS: NINGUNA. Cada candidata se midió en F0 (17/09/2026) y ninguna es
-- imprescindible:
--   · Identificador del órgano (DIR3/NIF) para duplicados: el CSV de Andalucía no lo
--     trae, las filas estatales no lo guardan (habría que reescribir 1,39 M) y, sobre
--     2025 completo, NO hay ningún duplicado Andalucía <-> estatal: ninguno de los 248
--     órganos de la Junta aparece entre los 7.672 órganos de la sindicación 1143. La
--     regla de duplicados se aplica en el cargador con columnas que ya existen.
--   · Id de la fuente: va en la clave ('and:<ID_EXPEDIENTE>'; 0 ID repetidos entre los
--     ficheros 2023-2026, así que no hace falta el año).
--   · Fecha de publicación / formalización: en Andalucía coincide con la de
--     adjudicación en 124.730 de 124.730 filas.
--   · Enlace: ya existe (la ficha del portal de la Junta se monta con el ID).
--   · Provincia (NUTS): útil para el SAS, que viene como un único órgano sin hospital,
--     pero no es imprescindible para F1. Queda como decisión aparte.
-- ============================================================================

-- Si algo (la ingesta diaria, un informe) tiene la tabla ocupada, mejor fallar en 5 s
-- que quedarse en cola con un bloqueo exclusivo pedido y parar las lecturas de la web.
set lock_timeout = '5s';

-- ----------------------------------------------------------------------------
-- 1) Valores permitidos de `fuente`
--    Todo el diseño descansa en esta columna: el desglose por fuente de la ficha y
--    del informe, y los controles de siempre (Hardolass, LODEPA) medidos SOLO sobre
--    fuente = 'estatal'. Un 'Andalucia' o 'andalucía' escrito a mano partiría el
--    desglose en silencio.
--    NOT VALID: no recorre las filas existentes (todas son 'estatal', las escribe el
--    cargador) y el bloqueo dura un instante; solo se comprueba en filas nuevas o
--    modificadas, que es donde puede entrar el error.
-- ----------------------------------------------------------------------------
do $$
begin
  if not exists (select 1 from pg_constraint
                 where conrelid = 'public.menores'::regclass
                   and conname = 'menores_fuente_valida') then
    alter table public.menores
      add constraint menores_fuente_valida
      check (fuente in ('estatal', 'andalucia', 'euskadi', 'cataluna',
                        'larioja', 'galicia', 'asturias'))
      not valid;
  end if;
end $$;

comment on column public.menores.fuente is
  'Procedencia del menor: estatal (PLACSP, sindicación 1143) | andalucia | euskadi | '
  'cataluna | larioja | galicia | asturias. La clave de las autonómicas lleva prefijo '
  'propio (and:, eus:, cat:...).';

-- ----------------------------------------------------------------------------
-- 2) Retención: public.purga_menores(anios, simular, lote)
--    Hoy public.menores NO se purga nunca (purga_catalogo solo toca licitaciones).
--    Mismo patrón que purga_catalogo: por lotes, bucle en el cliente, SECURITY
--    DEFINER, solo service_role.
--
--    Borra las filas con fecha_adjudicacion anterior a (hoy − anios), de TODAS las
--    fuentes, EXCEPTO:
--      · sin fecha (NULL): no se pueden datar; se conservan.
--      · fecha imposible, anterior a 2018-01-01 (basura del origen: 2002, 2006...):
--        se conservan y se cuentan aparte. Son las mismas que la ficha de competidor
--        ya separa como «fecha rara» (menores_ficha.sql).
--
--    simular = true NO borra: devuelve cuántas borraría (total y por fuente) y cuántas
--    quedan fuera por no tener fecha o tenerla imposible.
--    simular = false borra COMO MUCHO `lote` filas y devuelve cuántas. El cliente la
--    llama en bucle hasta que devuelve 0 (un único DELETE grande vía RPC da 57014).
--
--    Previsualizar:      select public.purga_menores(3, true);
--    Una tanda de 5.000: select public.purga_menores(3, false, 5000);
--
--    NO está en ningún job. Se activa solo con OK expreso, y tras un borrado grande
--    toca VACUUM (ANALYZE) public.menores.
-- ----------------------------------------------------------------------------
create or replace function public.purga_menores(
  anios   int     default 3,
  simular boolean default false,
  lote    int     default 5000
)
returns json
language plpgsql
security definer
set search_path = public, pg_catalog
as $$
declare
  v_corte constant date := (current_date - make_interval(years => anios))::date;
  v_suelo constant date := date '2018-01-01';
  v_n     integer;
begin
  if anios is null or anios < 1 then
    raise exception 'purga_menores: anios tiene que ser 1 o más (recibido %)', anios;
  end if;
  if lote is null or lote < 1 or lote > 50000 then
    raise exception 'purga_menores: lote entre 1 y 50000 (recibido %)', lote;
  end if;

  if simular then
    return json_build_object(
      'simulacion', true,
      'anios',      anios,
      'corte',      v_corte,
      'borraria',   (select count(*) from public.menores m
                     where m.fecha_adjudicacion >= v_suelo
                       and m.fecha_adjudicacion <  v_corte),
      'borraria_por_fuente',
                    (select coalesce(json_object_agg(s.fuente, s.n), '{}'::json)
                     from (select m.fuente, count(*) as n from public.menores m
                           where m.fecha_adjudicacion >= v_suelo
                             and m.fecha_adjudicacion <  v_corte
                           group by m.fuente) s),
      'se_conservan_fecha_imposible',
                    (select count(*) from public.menores m where m.fecha_adjudicacion < v_suelo),
      'se_conservan_sin_fecha',
                    (select count(*) from public.menores m where m.fecha_adjudicacion is null)
    );
  end if;

  delete from public.menores
  where ctid in (
    select m.ctid
    from public.menores m
    where m.fecha_adjudicacion >= v_suelo
      and m.fecha_adjudicacion <  v_corte
    limit lote
  );
  get diagnostics v_n = row_count;
  return json_build_object('simulacion', false, 'corte', v_corte, 'borradas', v_n);
end;
$$;

revoke all on function public.purga_menores(int, boolean, int) from public, anon, authenticated;
grant execute on function public.purga_menores(int, boolean, int) to service_role;

-- ----------------------------------------------------------------------------
-- 3) PostgREST: que vea la función y el comentario nuevos.
-- ----------------------------------------------------------------------------
notify pgrst, 'reload schema';

reset lock_timeout;

-- ----------------------------------------------------------------------------
-- COMPROBACIÓN (después de ejecutar; lo verifica Claude Code con claude_ro):
--   select conname, convalidated from pg_constraint
--    where conrelid = 'public.menores'::regclass and conname = 'menores_fuente_valida';
--      -> una fila, convalidated = false (NOT VALID, a propósito)
--   select public.purga_menores(3, true);
--      -> medido el 17/09/2026 con la función en una sesión temporal:
--         borraria 41.261 (todas 'estatal'), corte 2023-09-17,
--         se_conservan_fecha_imposible 141, se_conservan_sin_fecha 4.879 (14,7 s)
--   Y al día siguiente, en el log de radar.yml (job catalogo), la línea
--   «Menores (diario): N upsertados» sin AVISO nuevo (17/09: 9.580).
-- ----------------------------------------------------------------------------
