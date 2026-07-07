-- ============================================================================
-- ADJUDICACIONES (competencia por CIF)  ·  Fase D1: modelo de datos
--
-- Tabla public.adjudicaciones: UNA FILA POR LOTE-ADJUDICATARIO. Normaliza los
-- bloques cac:TenderResult del MISMO feed/CODICE que ya se ingiere (643 estatal +
-- 1044 agregadas), extraídos por feeds.extrae_entrada. Sirve para:
--   (D) auto-marcar 'ganada' cuando el CIF adjudicatario es de LODEPA  [fase D2]
--   (E) inteligencia de competencia: quién gana qué, por CIF          [fase E]
--
-- SEPARADA del Radar y ADITIVA: no toca public.licitaciones, public.decisiones ni
-- public.contratos. Se une a ellas por la clave universal licitacion_id (el atom:id
-- del feed, la MISMA URL larga; nunca una ruta de Storage).
--
-- PRIVADA: lectura solo tras login (rol authenticated). El anónimo no ve nada.
-- Escritura: la ingesta (backfill_catalogo.py) corre con la service_role.
--
-- CÓMO USARLO: pega TODO este archivo en el SQL Editor de Supabase
-- (SQL Editor -> New query) y pulsa "Run". Es IDEMPOTENTE (if not exists /
-- create or replace / drop ... if exists): se puede re-ejecutar sin romper nada.
--
-- NOTA (regla post-mayo-2026): toda tabla nueva en public necesita GRANT explícito
-- al rol Y política RLS; ninguna por separado basta. Aquí están LAS DOS (y además
-- el GRANT a service_role, que salta la RLS pero igualmente necesita privilegios).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) Tabla · una fila por (licitacion_id, lote, cif_adjudicatario)
-- ----------------------------------------------------------------------------
-- Convenios (documentados y usados por el extractor y el upsert):
--   · lote = ''  -> contrato SIN lotes (el CODICE no trae ProcurementProjectLotID).
--   · cif_adjudicatario = ''  -> lote DESIERTO (TenderResult sin WinningParty).
--   · El CIF se guarda NORMALIZADO (MAYÚSCULAS, sin espacios/'/'/'.'/'-'), para que
--     el cruce D2 (CIF de LODEPA) y la agregación por CIF de la fase E casen.
-- lote y cif son NOT NULL DEFAULT '' A PROPÓSITO: el on_conflict de PostgREST solo
-- acepta constraints sobre COLUMNAS PLANAS (no índices de expresión con coalesce),
-- así que la clave de idempotencia se apoya en columnas que nunca son NULL.
create table if not exists public.adjudicaciones (
  id                  bigint generated always as identity primary key,
  licitacion_id       text  not null,                 -- clave universal (une con licitaciones/decisiones)
  lote                text  not null default '',       -- ProcurementProjectLotID ('' si el contrato no tiene lotes)
  resultado_code      text,                            -- cbc:ResultCode (code list TenderResultCode-2.09)
  resultado           text,                            -- etiqueta legible del code (Adjudicado/Formalizado/Desierto/…)
  cif_adjudicatario   text  not null default '',       -- WinningParty/ID NORMALIZADO ('' si desierto)
  id_scheme           text,                            -- schemeName del ID (NIF / UTE / OTROS …); NULL si desierto
  adjudicatario       text,                            -- WinningParty/PartyName/Name
  es_pyme             boolean,                          -- cbc:SMEAwardedIndicator (true/false); NULL si no viene
  importe_sin_iva     numeric,                          -- AwardedTenderedProject/…/TaxExclusiveAmount
  importe_con_iva     numeric,                          -- AwardedTenderedProject/…/PayableAmount (falta a menudo)
  n_ofertas           integer,                          -- cbc:ReceivedTenderQuantity (si viene)
  fecha_adjudicacion  date,                             -- cbc:AwardDate (falta a menudo)
  estado_expediente   text,                             -- ContractFolderStatusCode del expediente (ADJ/RES/…); dato de contexto
  updated_at          timestamptz default now(),
  -- Clave de idempotencia del upsert (columnas planas, ver arriba).
  constraint adjudicaciones_uniq unique (licitacion_id, lote, cif_adjudicatario)
);

-- Si la tabla ya existía de una versión previa, añade las columnas "extra" de forma
-- idempotente (no rompe si ya están). Aditivo; no toca datos existentes.
alter table public.adjudicaciones add column if not exists id_scheme         text;
alter table public.adjudicaciones add column if not exists estado_expediente text;

-- ----------------------------------------------------------------------------
-- 2) Índices · competencia por CIF y filtro por fecha.
--    NO hace falta un índice suelto sobre licitacion_id: el índice de la UNIQUE
--    (licitacion_id, lote, cif_adjudicatario) ya lo cubre como COLUMNA LÍDER
--    (regla del prefijo izquierdo), así que el join con licitaciones y el DELETE por
--    licitacion_id de la ingesta diaria ya usan ese índice. Un índice extra solo
--    sumaría coste de escritura y disco (relevante con el límite de ~500 MB del free).
-- ----------------------------------------------------------------------------
create index if not exists adjudicaciones_cif_idx    on public.adjudicaciones (cif_adjudicatario);
create index if not exists adjudicaciones_fecha_idx  on public.adjudicaciones (fecha_adjudicacion);

-- ----------------------------------------------------------------------------
-- 3) Permisos + RLS  ·  PRIVADO (solo authenticated lee)
-- ----------------------------------------------------------------------------
-- Nada para el anónimo (defensa en profundidad; por defecto ya no hay grants).
revoke all on public.adjudicaciones from anon;

-- Lectura para usuarios con sesión (fases D2/E).
grant select on public.adjudicaciones to authenticated;

alter table public.adjudicaciones enable row level security;
drop policy if exists "adjudicaciones_select_authenticated" on public.adjudicaciones;
create policy "adjudicaciones_select_authenticated" on public.adjudicaciones
  for select to authenticated using (true);

-- ----------------------------------------------------------------------------
-- 4) Escritura (INGESTA) — corre con la service_role (backfill_catalogo.py: el
--    re-backfill y el cron diario). service_role SALTA la RLS, pero (regla
--    post-mayo-2026) necesita GRANT EXPLÍCITO de privilegios; el bypass no basta.
--    Incluye DELETE: la ingesta diaria REEMPLAZA las filas de un expediente
--    re-publicado (delete+insert por licitacion_id) para no dejar huérfanas si
--    cambia el adjudicatario de un lote. Idempotente: re-ejecutar no rompe nada.
grant select, insert, update, delete on public.adjudicaciones to service_role;

-- ----------------------------------------------------------------------------
-- 5) Estadísticas al día (para buenos planes en las consultas de la fase E).
analyze public.adjudicaciones;
-- ----------------------------------------------------------------------------
