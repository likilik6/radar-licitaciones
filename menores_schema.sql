-- ============================================================================
-- FASE M · public.menores — contratos menores estatales (sindicación 1143)
--
-- QUÉ ES: los contratos MENORES son un conjunto SEPARADO en Datos Abiertos PLACSP
-- (sindicación 1143 contratosMenoresPerfilesContratantes, CODICE, mismo extractor
-- que 643/1044). NO están en el catálogo del Buscador (que los excluye: la fuente
-- agregada se llama literalmente "PlataformasAgregadasSinMenores"). Vienen ya
-- ADJUDICADOS (adjudicatario + CIF + importe), porque el menor se adjudica directo.
--
-- POR QUÉ TABLA APARTE (decisión de producto v2, PASO T medido): se ingieren TODOS
-- los menores del estatal (~1,1M en 2 años) SIN filtrar, y el nicho/competidor se
-- calcula AL CONSULTAR. El requisito duro es que esto NO ralentice el Buscador.
-- Vía elegida = tabla Postgres SEPARADA (medido: la BD ya está en Pro con holgura;
-- +~1 GB es calderilla). El aislamiento es real: el Buscador nunca hace JOIN con
-- menores, así que sus planes de consulta NO cambian. (La alternativa Parquet+DuckDB
-- daba aislamiento total pero metía un motor de consulta nuevo en un sitio sin build;
-- descartada por complejidad frente a un riesgo de contención despreciable.)
--
-- UNA FILA POR MENOR (PK licitacion_id = atom:id). Un menor se adjudica directo:
-- solo el 0,22% tiene >1 ganador distinto (medido en 2 meses); se guarda el
-- adjudicatario PRINCIPAL y n_adjudicatarios marca el caso raro.
--
-- CÓMO USARLO: pega TODO en el SQL Editor de Supabase y pulsa "Run". Idempotente
-- (if not exists / create or replace). Requisito: buscador_indices.sql ejecutado
-- ANTES (reutilizamos public.cpv_texto y la extensión unaccent/pg_trgm de allí).
--
-- NO toca: el Radar, el catálogo de licitaciones/Buscador, adjudicaciones, desiertas,
-- login, decisiones ni cartera. Solo AÑADE la tabla nueva.
-- ============================================================================

-- Extensiones (idempotente; ya las crea buscador_indices.sql, aquí por si acaso).
create extension if not exists unaccent with schema extensions;
create extension if not exists pg_trgm  with schema extensions;

-- ----------------------------------------------------------------------------
-- 1) Tabla · una fila por menor (licitacion_id)
-- ----------------------------------------------------------------------------
create table if not exists public.menores (
  licitacion_id        text  primary key,            -- atom:id (clave universal, une con Competencia por CIF)
  objeto               text,                          -- objeto del contrato (el menor no trae "título" separado útil)
  cpv                  text[] not null default '{}',  -- códigos CPV (solo ~54% de los menores traen alguno)
  importe_sin_iva      numeric,                       -- importe adjudicado sin IVA
  importe_con_iva      numeric,                       -- importe con IVA (si viene)
  organo_contratacion  text,                          -- ÓRGANO COMPRADOR (vertiente "oportunidad": quién compra)
  adjudicatario        text,                          -- razón social del ganador
  cif_adjudicatario    text,                          -- CIF NORMALIZADO del ganador PRINCIPAL (el de mayor importe)
  -- TODOS los CIFs ganadores distintos (incl. el principal). Un menor se adjudica
  -- directo y casi siempre tiene 1 ganador, pero el 0,22% tiene >1 (medido en PASO 0).
  -- Guardarlos TODOS evita perder CIFs de competidor: «solo CIFS_SEGUIDOS» y los cruces
  -- casan con CUALQUIER ganador (`&&`), no solo el principal.
  cifs_adjudicatarios  text[] not null default '{}',
  fecha_adjudicacion   date,                          -- cbc:AwardDate
  num_expediente       text,                          -- nº de expediente del órgano (dato de contexto)
  enlace               text,                          -- enlace al detalle en la PLACSP
  fuente               text  not null default 'estatal',
  n_adjudicatarios     integer,                       -- nº de ganadores distintos (normalmente 1; marca el multi-lote raro)
  tsv                  tsvector,                      -- índice de texto del objeto+órgano (lo rellena el trigger de abajo)
  -- Representación textual del CPV para filtrar POR PREFIJO desde PostgREST (columna
  -- REAL, no una función, para que el front pueda hacer cpv_txt=ilike.'% 9073%'). Un
  -- espacio antepuesto a cada código: "empieza por 9073" == la cadena CONTIENE ' 9073'.
  -- array_to_string es IMMUTABLE -> vale para columna generada. Se apoya en el trigram.
  cpv_txt              text generated always as (' ' || array_to_string(cpv, ' ')) stored,
  updated_at           timestamptz default now()
);

-- ----------------------------------------------------------------------------
-- 2) tsv por TRIGGER (mismo patrón que licitaciones: unaccent es STABLE, no vale
--    columna generada). Config 'spanish' + unaccent para casar "formaldehído" con o
--    sin tilde. Pesa el objeto (A) por encima del órgano (C). search_path fijo.
-- ----------------------------------------------------------------------------
create or replace function public.menores_tsv_update()
returns trigger
language plpgsql
set search_path = extensions, public, pg_catalog
as $$
begin
  new.tsv :=
      setweight(to_tsvector('spanish', unaccent(coalesce(new.objeto,              ''))), 'A')
   || setweight(to_tsvector('spanish', unaccent(coalesce(new.organo_contratacion, ''))), 'C');
  return new;
end;
$$;

drop trigger if exists menores_tsv_trg on public.menores;
create trigger menores_tsv_trg
  before insert or update on public.menores
  for each row execute function public.menores_tsv_update();

-- ----------------------------------------------------------------------------
-- 3) Índices · para los filtros del explorador (justificación de cada uno)
-- ----------------------------------------------------------------------------
-- (a) NICHO por palabra clave -> GIN(tsv). El filtro "nicho" busca ~19 términos en
--     el objeto sobre 1,1M filas; sin esto sería Seq Scan. Mismo patrón que el Buscador.
create index if not exists menores_tsv_gin
  on public.menores using gin (tsv);

-- (b) NICHO/filtro por CPV PREFIJO -> GIN trigram sobre la columna generada cpv_txt.
--     El front filtra por PostgREST: cpv_txt=ilike.'% 9073%' (acelerado por el trigram).
create index if not exists menores_cpv_txt_trgm
  on public.menores using gin (cpv_txt extensions.gin_trgm_ops);

-- (c) CIFS_SEGUIDOS y cruces por competidor -> btree sobre el CIF PRINCIPAL (igualdad
--     / IN) para el caso común, y GIN sobre el ARRAY de todos los CIFs para que
--     «solo CIFS_SEGUIDOS» (`cifs_adjudicatarios && ARRAY[...]`) case también con el
--     ganador secundario del 0,22% multi-ganador (sin perder competidores).
create index if not exists menores_cif_idx
  on public.menores (cif_adjudicatario);
create index if not exists menores_cifs_gin
  on public.menores using gin (cifs_adjudicatarios);

-- (d) ÓRGANO comprador -> btree para la agregación "quién compra" (GROUP BY) y el
--     filtro por órgano exacto. (El "contiene" por texto libre, si se pide, tira del
--     tsv que ya incluye el órgano con peso C.)
create index if not exists menores_organo_idx
  on public.menores (organo_contratacion);

-- (e) ORDEN de la lista (fecha e importe, en las dos direcciones) con desempate por
--     PK, para que la página salga por Index Scan y no por Sort masivo de 1,1M. Mismo
--     criterio que buscador_indices.sql. El DEFAULT del explorador es fecha desc.
create index if not exists menores_fecha_desc_id
  on public.menores (fecha_adjudicacion desc nulls last, licitacion_id asc);
create index if not exists menores_fecha_asc_id
  on public.menores (fecha_adjudicacion asc nulls last, licitacion_id asc);
create index if not exists menores_importe_desc_id
  on public.menores (importe_sin_iva desc nulls last, licitacion_id asc);
create index if not exists menores_importe_asc_id
  on public.menores (importe_sin_iva asc nulls last, licitacion_id asc);

-- ----------------------------------------------------------------------------
-- 4) Permisos + RLS · PRIVADO (solo authenticated lee). Regla post-mayo-2026: GRANT
--    explícito Y política RLS; ninguna por separado basta.
-- ----------------------------------------------------------------------------
revoke all on public.menores from anon;
grant select on public.menores to authenticated;

alter table public.menores enable row level security;
drop policy if exists "menores_select_authenticated" on public.menores;
create policy "menores_select_authenticated" on public.menores
  for select to authenticated using (true);

-- Escritura: la ingesta (backfill_catalogo.py) corre con service_role. Incluye DELETE
-- por si un menor re-publicado cambia de adjudicatario (delete+insert por licitacion_id).
grant select, insert, update, delete on public.menores to service_role;

-- ----------------------------------------------------------------------------
-- 5) Estadísticas al día (buenos planes desde el principio; se re-lanza tras poblar).
-- ----------------------------------------------------------------------------
analyze public.menores;

-- ----------------------------------------------------------------------------
-- POBLAR: el backfill lo hace python backfill_catalogo.py --cargar-menores (ventana
-- de 2 años; ~1,1M filas; ~30-60 min; necesita service_role). AVISA del coste antes.
-- Tras poblar, re-lanzar:  analyze public.menores;
-- ----------------------------------------------------------------------------

notify pgrst, 'reload schema';
