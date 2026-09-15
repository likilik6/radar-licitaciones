-- ccaa_schema.sql  ·  Territorio en el catálogo: poblar ccaa y lugar_ejecucion
-- ============================================================================
-- QUÉ: añade `nuts_codigo` a public.licitaciones y los índices para filtrar y agrupar
-- por comunidad autónoma. Las columnas `ccaa` y `lugar_ejecucion` YA EXISTEN desde
-- BG-3 (buscador_schema.sql:38-39): lo que nunca existió es su contenido.
--
-- CÓMO USARLO: pega TODO en el SQL Editor de Supabase y pulsa Run. Es idempotente.
-- ORDEN OBLIGATORIO: esto va ANTES de desplegar el cambio de backfill_catalogo.py.
-- El pipeline pasa a mandar `nuts_codigo` en cada upsert; si la columna no existe
-- todavía, PostgREST rechaza el lote entero (PGRST204) y se cae la ingesta diaria.
--
-- ----------------------------------------------------------------------------
-- EL PROBLEMA (medido el 15/09/2026)
-- `ccaa` y `lugar_ejecucion` están a NULL en las 624.204 filas del catálogo: el 100%.
-- No es que estén poco pobladas; no se poblaron nunca. El extractor sí saca el dato
-- (feeds.py:414-416), pero backfill_catalogo.py lo descartaba a la espera del mapeo.
--
-- POR QUÉ HACE FALTA UNA COLUMNA NUEVA (nuts_codigo)
-- Misma lección que D1.1 con `sistema_contratacion`: se guarda el código CRUDO del
-- CODICE además del nombre derivado. El código es la clave estable y barata de
-- indexar (5 bytes); los nombres son presentación. Entre NUTS-2016 y NUTS-2021 los
-- códigos de España no cambiaron, pero SÍ los nombres («Comunidad Valenciana» ->
-- «Comunitat Valenciana», «Ciudad Autónoma de Ceuta» -> «Ciudad de Ceuta»). Con el
-- código guardado, reetiquetar son 19 UPDATE; sin él, otro re-backfill de 3 horas.
--
-- COBERTURA QUE SE ESPERA (medida sobre 12.219 entradas reales del feed)
--   · CCAA resuelta: 98,71%
--   · sin resolver:   1,29% -> 1,24% con «ES» a secas (ámbito nacional, no es una
--     comunidad), 0,02% NUTS-1 ambiguos (ES1 Noroeste...), 0,02% extranjeros, 0,01%
--     sin código. Todos ellos se quedan a NULL A PROPÓSITO: un filtro geográfico que
--     miente es peor que uno vacío. Ver las reglas en nuts.py.
--
-- OJO AL LEER LOS DATOS: la columna se poblará al 100% solo para las filas que pase
-- el re-backfill. Mientras ese re-backfill no se lance, `ccaa` estará poblada SOLO en
-- lo que entre por la ingesta diaria a partir de hoy, y un filtro por comunidad
-- ocultará el histórico EN SILENCIO. Ver la consulta de control al final.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) Columna del código NUTS crudo, tal cual lo publica el CODICE ('ES618', 'ES51',
--    'ES'...). Sin restricción de valores: lo que venga se guarda; la decisión de qué
--    es una CCAA la toma nuts.py, no la base.
-- ----------------------------------------------------------------------------
alter table public.licitaciones
  add column if not exists nuts_codigo text;

comment on column public.licitaciones.nuts_codigo is
  'Código territorial CRUDO del CODICE (cbc:CountrySubentityCode, lista NUTS-2021). '
  'Niveles mezclados: ES618 (NUTS-3, provincia), ES51 (NUTS-2, CCAA), ES (país). '
  'ccaa y lugar_ejecucion se derivan de aquí con nuts.py; este es el dato estable.';
comment on column public.licitaciones.ccaa is
  'Nombre de la comunidad autónoma, derivado de nuts_codigo (NUTS-2). NULL cuando el '
  'código no identifica UNA comunidad: ámbito nacional, NUTS-1 ambiguo o extranjero.';
comment on column public.licitaciones.lugar_ejecucion is
  'Provincia o isla (NUTS-3) si el código llega a ese detalle; si no, la comunidad. '
  'NUNCA más fino de lo que dice la fuente.';

-- ----------------------------------------------------------------------------
-- 2) Índices. «Índice antes que consulta» (regla 4), pero solo el mínimo:
--    · el del filtro por comunidad, que es el caso de uso real;
--    · PARCIAL (where ccaa is not null) porque hoy el 100% es NULL y, aun poblada, el
--      1,3% seguirá siéndolo: no tiene sentido indexar lo que nunca se filtra.
--    Medido: un índice de una columna sobre esta tabla ocupa ~14 MB (como
--    licitaciones_fuente_idx); uno compuesto con licitacion_id se va a ~149 MB.
--    NO se crea índice sobre lugar_ejecucion: nadie filtra por provincia todavía.
--    Cuando alguien lo pida, se mide y se crea; no por si acaso (regla 6).
-- ----------------------------------------------------------------------------
create index if not exists licitaciones_ccaa_idx
  on public.licitaciones (ccaa)
  where ccaa is not null;

-- ----------------------------------------------------------------------------
-- 3) Estadísticas. El planner lleva creyendo desde julio que esta columna es 100%
--    NULL (null_frac = 1.0); sin analyze seguiría planificando con esa mentira.
--    RE-EJECUTAR ESTE ANALYZE DESPUÉS DEL RE-BACKFILL, no solo ahora.
-- ----------------------------------------------------------------------------
analyze public.licitaciones;

notify pgrst, 'reload schema';

-- ----------------------------------------------------------------------------
-- CONTROL · cuánto hay poblado de verdad. Mientras el re-backfill no se lance, esto
-- dirá que falta casi todo, y eso es lo correcto: el filtro por comunidad no debe
-- ofrecerse al usuario hasta que el porcentaje sea alto.
--
-- select count(*)                                              as filas,
--        count(nuts_codigo)                                    as con_nuts,
--        count(ccaa)                                           as con_ccaa,
--        round(100.0 * count(ccaa) / nullif(count(*), 0), 2)   as pct_ccaa
-- from public.licitaciones;
--
-- Reparto por comunidad (para comprobar que no hay una dominando por un fallo):
-- select coalesce(ccaa, '(sin resolver)') as ccaa, count(*)
-- from public.licitaciones group by 1 order by 2 desc;
--
-- Qué códigos se quedan sin resolver (debería ser 'ES' y poco más):
-- select nuts_codigo, count(*) from public.licitaciones
-- where ccaa is null and nuts_codigo is not null group by 1 order by 2 desc limit 20;
-- ----------------------------------------------------------------------------
