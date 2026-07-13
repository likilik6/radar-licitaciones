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
--   · BASURA DE ORIGEN en el CIF: una minoría de filas (~5.800) trae en el ID del
--     adjudicatario texto que NO es un CIF (p. ej. "VERRESOLUCIÓN...", DNIs de persona
--     con asteriscos). NO se limpia (el dato de origen manda y no se puede reconstruir):
--     la VISTA DE COMPETENCIA (fase E) la filtrará, p. ej. con
--        where cif_adjudicatario ~ '^[A-Z0-9]+$'
--     al agregar por CIF. El cruce D2 no se ve afectado (los CIF de LODEPA son limpios).
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
  -- D1.1 (todas nullable): datos por LOTE y de EXPEDIENTE. Ver el bloque de notas al final.
  presupuesto_lote_sin_iva numeric,                     -- BudgetAmount/TaxExclusiveAmount del ProcurementProjectLot de ESE lote
  sistema_contratacion     text,                        -- ContractingSystemCode CRUDO '0'..'4' (0=normal, 1=AM, 3=basado en AM…)
  tipo_contrato            text,                        -- ProcurementProject/TypeCode CRUDO ('2'=Servicios, '3'=Obras, '1'=Suministros…)
  updated_at          timestamptz default now(),
  -- Clave de idempotencia del upsert (columnas planas, ver arriba).
  constraint adjudicaciones_uniq unique (licitacion_id, lote, cif_adjudicatario)
);

-- Si la tabla ya existía de una versión previa, añade las columnas "extra" de forma
-- idempotente (no rompe si ya están). Aditivo; no toca datos existentes.
alter table public.adjudicaciones add column if not exists id_scheme         text;
alter table public.adjudicaciones add column if not exists estado_expediente text;

-- ----------------------------------------------------------------------------
-- D1.1 · datos POR LOTE y de EXPEDIENTE (aditivo; las rellena el re-backfill).
-- Rutas VERIFICADAS contra el CODICE real (no supuestas), 08/07/2026:
--
--   presupuesto_lote_sin_iva:
--     place:ContractFolderStatus/cac:ProcurementProjectLot/
--       cbc:ID (schemeName='ID_LOTE')  ← casa con AwardedTenderedProject/ProcurementProjectLotID
--       cac:ProcurementProject/cac:BudgetAmount/cbc:TaxExclusiveAmount
--     -> con esto el % de baja por LOTE es EXACTO (antes solo se podía en contratos sin lotes).
--
--   sistema_contratacion (CÓDIGO CRUDO, no un booleano — a propósito):
--     place:ContractFolderStatus/cac:TenderingProcess/cbc:ContractingSystemCode
--     code list ContractingSystemTypeCode-2.08:
--       0 = No aplica (contrato normal)
--       1 = Establecimiento del ACUERDO MARCO      -> sus importes adjudicados son PRECIOS
--           UNITARIOS/tarifas: NO se debe calcular el % de baja contra el presupuesto.
--       2 = Establecimiento del Sistema Dinámico (SDA)
--       3 = Contrato BASADO en un Acuerdo Marco    -> importe REAL de contrato.
--       4 = Contrato basado en un Sistema Dinámico
--     Un booleano "es_acuerdo_marco" PERDERÍA el matiz 1 vs 3 (por eso se guarda el código).
--     Sustituye a la heurística de E.5 (importe < 1.000 €), que queda solo de fallback.
--
--   tipo_contrato (CÓDIGO CRUDO):
--     place:ContractFolderStatus/cac:ProcurementProject/cbc:TypeCode
--     code list ContractCode-2.08: 1=Suministros, 2=Servicios, 3=Obras, 8=Privado,
--     21=Gestión Servicios Públicos, 22=Concesión Servicios, 31=Concesión Obras, 50=Patrimonial.
--
-- ÍNDICES: NO se añade ninguno. Justificación con datos: la web SIEMPRE consulta
-- adjudicaciones por licitacion_id (bloque Adjudicación, ver expediente) o por
-- cif_adjudicatario (ficha, cruces) — ambos ya indexados; estas tres columnas son
-- carga útil que se lee con la fila, nunca criterio de filtrado por sí solo.
-- La clave de idempotencia del upsert NO cambia.
-- ----------------------------------------------------------------------------
alter table public.adjudicaciones add column if not exists presupuesto_lote_sin_iva numeric;
alter table public.adjudicaciones add column if not exists sistema_contratacion     text;
alter table public.adjudicaciones add column if not exists tipo_contrato            text;

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
-- 5) Saneo de fechas IMPLAUSIBLES ya cargadas (basura de origen del feed: años
--    '0001', fechas absurdamente futuras). El extractor ya acota al insertar de
--    aquí en adelante (feeds.a_fecha: fuera de [2000-01-01, hoy+2 años] -> NULL);
--    este UPDATE limpia lo que entró ANTES de ese cambio. Es IDEMPOTENTE: una vez
--    saneado, el WHERE ya no casa. MISMA ventana que el extractor (coherencia).
-- ----------------------------------------------------------------------------
update public.adjudicaciones
   set fecha_adjudicacion = null
 where fecha_adjudicacion is not null
   and (fecha_adjudicacion < date '2000-01-01'
        or fecha_adjudicacion > current_date + interval '2 years');

-- ----------------------------------------------------------------------------
-- 6) Estadísticas al día (para buenos planes en las consultas de la fase E).
analyze public.adjudicaciones;
-- ----------------------------------------------------------------------------
