-- ============================================================================
-- menores_f1.sql · F1 de «Menores autonómicos» (Andalucía) — 17/09/2026
--
-- QUÉ: lo que necesita la base ANTES de cargar la primera fuente autonómica.
--   1) menores_resumen_cif: añade `por_fuente` (nº e importe por fuente) a lo que ya
--      devolvía. Cambio ADITIVO: la web actual ignora la clave nueva.
--   2) menores_carga_lote(p_filas): la carga de las fuentes autonómicas.
--   3) menores_organos(p_fuente): lista de órganos distintos de una fuente (regla de
--      duplicados).
--   4) Índice (organo_contratacion, fuente) en lugar de (organo_contratacion): en
--      menores_f1_indice.sql, aparte, porque va con CONCURRENTLY.
--
-- REQUISITOS: menores_ficha.sql y menores_autonomicos_schema.sql ya ejecutados.
-- CÓMO: pega TODO en el SQL Editor y Run. Idempotente (create or replace).
-- NO toca: Radar, licitaciones, adjudicaciones, competidores, ni la ingesta estatal
-- (que sigue con su upsert de PostgREST de siempre).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) Resumen de menores de UN CIF, ahora con el desglose por fuente.
--    Misma pasada por el GIN que antes (CTE materializada): el desglose sale de las
--    mismas filas, no de otra consulta. Todo lo demás, idéntico a menores_ficha.sql.
-- ----------------------------------------------------------------------------
create or replace function public.menores_resumen_cif(p_cif text)
returns json
language plpgsql
stable
security invoker
set search_path = public, pg_catalog
as $$
declare
  v_cif   text;
  v_desde constant date := date '2018-01-01';
  v_r     record;
begin
  v_cif := upper(regexp_replace(coalesce(p_cif, ''), '[[:space:]./-]', '', 'g'));

  if v_cif = '' then
    return json_build_object(
      'cif', null, 'n', 0, 'importe_total', null, 'ultimo', null, 'primero', null,
      'n_sin_fecha', 0, 'n_fecha_rara', 0, 'n_sin_importe', 0,
      'n_compartidos', 0, 'importe_compartido', 0, 'desde', v_desde,
      'por_fuente', '[]'::json
    );
  end if;

  with m as materialized (
    select x.fuente, x.importe_sin_iva, x.fecha_adjudicacion, x.n_adjudicatarios, x.cif_adjudicatario
    from public.menores x
    where x.cifs_adjudicatarios && array[v_cif]
  )
  select
    count(*)                                                              as n,
    sum(m.importe_sin_iva)                                                as importe_total,
    max(m.fecha_adjudicacion) filter (where m.fecha_adjudicacion >= v_desde) as ultimo,
    min(m.fecha_adjudicacion) filter (where m.fecha_adjudicacion >= v_desde) as primero,
    count(*) filter (where m.fecha_adjudicacion is null)                   as n_sin_fecha,
    count(*) filter (where m.fecha_adjudicacion < v_desde)                 as n_fecha_rara,
    count(*) filter (where m.importe_sin_iva is null)                      as n_sin_importe,
    count(*) filter (where m.n_adjudicatarios > 1
                       and m.cif_adjudicatario is distinct from v_cif)     as n_compartidos,
    coalesce(sum(m.importe_sin_iva) filter (where m.n_adjudicatarios > 1
                       and m.cif_adjudicatario is distinct from v_cif), 0) as importe_compartido,
    (select coalesce(json_agg(json_build_object('fuente', f.fuente, 'n', f.n,
                                                'importe_total', f.importe_total)
                              order by f.n desc, f.fuente), '[]'::json)
     from (select fuente, count(*) as n, sum(importe_sin_iva) as importe_total
           from m group by fuente) f)                                    as por_fuente
  into v_r
  from m;

  return json_build_object(
    'cif',           v_cif,
    'n',             coalesce(v_r.n, 0),
    'importe_total', v_r.importe_total,
    'ultimo',        v_r.ultimo,
    'primero',       v_r.primero,
    'n_sin_fecha',   coalesce(v_r.n_sin_fecha, 0),
    'n_fecha_rara',  coalesce(v_r.n_fecha_rara, 0),
    'n_sin_importe', coalesce(v_r.n_sin_importe, 0),
    'n_compartidos', coalesce(v_r.n_compartidos, 0),
    'importe_compartido', coalesce(v_r.importe_compartido, 0),
    'desde',         v_desde,
    -- [{fuente, n, importe_total}] de más a menos contratos. importe_total es null si
    -- ninguna fila de esa fuente trae importe.
    'por_fuente',    coalesce(v_r.por_fuente, '[]'::json)
  );
end;
$$;

revoke all on function public.menores_resumen_cif(text) from public, anon;
grant execute on function public.menores_resumen_cif(text) to authenticated;

-- ----------------------------------------------------------------------------
-- 2) Carga de menores AUTONÓMICOS por lotes: public.menores_carga_lote(p_filas jsonb)
--
--    POR QUÉ UNA FUNCIÓN Y NO EL UPSERT DE PostgREST (el que usa la estatal):
--    · El upsert de PostgREST reescribe la fila AUNQUE NO HAYA CAMBIADO. Así nacen las
--      ~244.000 filas muertas que hay hoy en menores (la ingesta estatal reescribe
--      ~9.500 filas al día). Los ficheros de Andalucía se republican enteros cada
--      pocos meses: repasar 125.000 filas iguales dejaría 125.000 filas muertas más.
--      Aquí solo se escribe lo NUEVO o lo que CAMBIA.
--    · Devuelve cuántas filas son nuevas, cuántas cambian y cuántas estaban igual,
--      que es lo que pide el informe de la carga.
--    · El CPV se puede rellenar después (ficha del portal). `cpv` null en la entrada =
--      «no lo sé»: en una fila que ya existe se CONSERVA el que tenga; en una nueva
--      entra vacío. `cpv` = [] sí lo vacía.
--
--    BLINDAJE: nunca escribe filas estatales. Si el lote trae fuente 'estatal', una
--    clave con forma de URL de la PLACSP o claves repetidas, falla entero (mejor parar
--    que pisar). Si la clave ya existe con OTRA fuente, no la toca y la cuenta como
--    conflicto.
--
--    p_filas: array json de objetos con las columnas de menores (sin tsv, cpv_txt ni
--    updated_at, que pone el trigger / la función). Lotes de ~500 filas.
--    Devuelve: {recibidas, insertadas, actualizadas, iguales, conflictos}
-- ----------------------------------------------------------------------------
create or replace function public.menores_carga_lote(p_filas jsonb)
returns json
language plpgsql
volatile
security invoker
set search_path = public, pg_catalog
as $$
declare
  v_recibidas    integer;
  v_distintas    integer;
  v_malas        integer;
  v_insertadas   integer;
  v_actualizadas integer;
  v_conflictos   integer;
begin
  if p_filas is null or jsonb_typeof(p_filas) <> 'array' then
    raise exception 'menores_carga_lote: p_filas tiene que ser un array json';
  end if;

  -- Sin tabla temporal a propósito: crear y borrar una por lote (~600 por carga) deja
  -- basura en el catálogo. El json se lee en cada sentencia (500 filas, barato).
  select count(*), count(distinct r.licitacion_id),
         count(*) filter (where r.licitacion_id is null or r.licitacion_id ~* '^https?://'
                             or r.fuente is null or r.fuente = 'estatal')
  into v_recibidas, v_distintas, v_malas
  from jsonb_to_recordset(p_filas) as r(licitacion_id text, fuente text);

  if v_malas > 0 then
    raise exception 'menores_carga_lote: el lote trae % fila(s) estatales o sin clave; no se carga nada', v_malas;
  end if;
  if v_distintas <> v_recibidas then
    raise exception 'menores_carga_lote: el lote trae claves repetidas; no se carga nada';
  end if;

  select count(*) into v_conflictos
  from jsonb_to_recordset(p_filas) as s(licitacion_id text, fuente text)
  join public.menores m on m.licitacion_id = s.licitacion_id
  where m.fuente is distinct from s.fuente;

  with s as (
    select * from jsonb_to_recordset(p_filas) as r(
      licitacion_id text, objeto text, cpv text[], importe_sin_iva numeric,
      importe_con_iva numeric, organo_contratacion text, adjudicatario text,
      cif_adjudicatario text, cifs_adjudicatarios text[], fecha_adjudicacion date,
      num_expediente text, enlace text, fuente text, n_adjudicatarios integer)
  ), cambiadas as (
    update public.menores m set
      objeto              = s.objeto,
      cpv                 = coalesce(s.cpv, m.cpv),
      importe_sin_iva     = s.importe_sin_iva,
      importe_con_iva     = s.importe_con_iva,
      organo_contratacion = s.organo_contratacion,
      adjudicatario       = s.adjudicatario,
      cif_adjudicatario   = s.cif_adjudicatario,
      cifs_adjudicatarios = coalesce(s.cifs_adjudicatarios, '{}'),
      fecha_adjudicacion  = s.fecha_adjudicacion,
      num_expediente      = s.num_expediente,
      enlace              = s.enlace,
      n_adjudicatarios    = s.n_adjudicatarios,
      updated_at          = now()
    from s
    where m.licitacion_id = s.licitacion_id
      and m.fuente = s.fuente
      and (m.objeto, m.cpv, m.importe_sin_iva, m.importe_con_iva, m.organo_contratacion,
           m.adjudicatario, m.cif_adjudicatario, m.cifs_adjudicatarios,
           m.fecha_adjudicacion, m.num_expediente, m.enlace, m.n_adjudicatarios)
          is distinct from
          (s.objeto, coalesce(s.cpv, m.cpv), s.importe_sin_iva, s.importe_con_iva,
           s.organo_contratacion, s.adjudicatario, s.cif_adjudicatario,
           coalesce(s.cifs_adjudicatarios, '{}'), s.fecha_adjudicacion, s.num_expediente,
           s.enlace, s.n_adjudicatarios)
    returning 1
  )
  select count(*) into v_actualizadas from cambiadas;

  with s as (
    select * from jsonb_to_recordset(p_filas) as r(
      licitacion_id text, objeto text, cpv text[], importe_sin_iva numeric,
      importe_con_iva numeric, organo_contratacion text, adjudicatario text,
      cif_adjudicatario text, cifs_adjudicatarios text[], fecha_adjudicacion date,
      num_expediente text, enlace text, fuente text, n_adjudicatarios integer)
  ), nuevas as (
    insert into public.menores (
      licitacion_id, objeto, cpv, importe_sin_iva, importe_con_iva, organo_contratacion,
      adjudicatario, cif_adjudicatario, cifs_adjudicatarios, fecha_adjudicacion,
      num_expediente, enlace, fuente, n_adjudicatarios, updated_at)
    select s.licitacion_id, s.objeto, coalesce(s.cpv, '{}'), s.importe_sin_iva,
           s.importe_con_iva, s.organo_contratacion, s.adjudicatario, s.cif_adjudicatario,
           coalesce(s.cifs_adjudicatarios, '{}'), s.fecha_adjudicacion, s.num_expediente,
           s.enlace, s.fuente, s.n_adjudicatarios, now()
    from s
    where not exists (select 1 from public.menores m where m.licitacion_id = s.licitacion_id)
    on conflict (licitacion_id) do nothing
    returning 1
  )
  select count(*) into v_insertadas from nuevas;

  return json_build_object(
    'recibidas',    v_recibidas,
    'insertadas',   v_insertadas,
    'actualizadas', v_actualizadas,
    'iguales',      v_recibidas - v_insertadas - v_actualizadas - v_conflictos,
    'conflictos',   v_conflictos
  );
end;
$$;

revoke all on function public.menores_carga_lote(jsonb) from public, anon, authenticated;
grant execute on function public.menores_carga_lote(jsonb) to service_role;

-- ----------------------------------------------------------------------------
-- 3) Órganos distintos de UNA fuente: public.menores_organos(p_fuente)
--    Para la regla de duplicados (F0): una fila autonómica solo puede ser gemela de
--    una estatal si el órgano estatal es uno de los de esa fuente. Si las dos listas no
--    comparten ningún órgano, no hace falta buscar gemelos por CIF (hoy: 0 en común).
--    Recorrido por SALTOS (una búsqueda por órgano distinto, no una lectura de la
--    tabla): con el índice (organo_contratacion, fuente) de menores_f1_indice.sql no
--    pasa por el heap. Sin ese índice funciona igual, pero con una fuente grande (el
--    SAS son ~300.000 filas con el mismo órgano) leería todas sus filas.
-- ----------------------------------------------------------------------------
create or replace function public.menores_organos(p_fuente text)
returns setof text
language sql
stable
security invoker
set search_path = public, pg_catalog
as $$
  with recursive o(organo) as (
    (select m.organo_contratacion from public.menores m
     where m.fuente = p_fuente and m.organo_contratacion is not null
     order by m.organo_contratacion limit 1)
    union all
    select (select m.organo_contratacion from public.menores m
            where m.fuente = p_fuente and m.organo_contratacion > o.organo
            order by m.organo_contratacion limit 1)
    from o
    where o.organo is not null
  )
  select organo from o where organo is not null;
$$;

revoke all on function public.menores_organos(text) from public, anon, authenticated;
grant execute on function public.menores_organos(text) to service_role;

notify pgrst, 'reload schema';
