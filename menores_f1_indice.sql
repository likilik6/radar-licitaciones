-- ============================================================================
-- menores_f1_indice.sql · F1 — índice (organo_contratacion, fuente)
--
-- QUÉ: sustituye menores_organo_idx (organo_contratacion) por un índice con la fuente
-- detrás. Mismo uso de antes (igualdad por órgano: la columna de delante es la misma)
-- y, además, deja contestar SOLO CON EL ÍNDICE «¿tiene este órgano filas de esta
-- fuente?», que es lo que hace public.menores_organos (regla de duplicados).
--
-- POR QUÉ: con el índice actual, preguntar por los órganos ESTATALES obliga a leer en
-- la tabla todas las filas de cada órgano para mirar su fuente. Tras cargar Andalucía,
-- el órgano «Servicio Andaluz de Salud» tendrá ~300.000 filas andaluzas y ninguna
-- estatal: el recorrido las leería todas en cada ejecución semanal.
-- Tamaño: el actual ocupa 16,8 MB; este, parecido (la fuente se deduplica con el órgano).
--
-- CÓMO: en el SQL Editor, CADA PASO SOLO. Si sale «cannot run inside a transaction
-- block», quita `concurrently` y repítelo (bloquea escrituras en menores un momento).
-- ============================================================================

-- PASO 1 (solo)
create index concurrently if not exists menores_organo_fuente_idx
  on public.menores (organo_contratacion, fuente);

-- PASO 2: Claude Code comprueba que el índice nuevo es válido antes del paso 3.

-- PASO 3 (solo)
drop index concurrently if exists public.menores_organo_idx;

-- ----------------------------------------------------------------------------
-- MARCHA ATRÁS (solo si hiciera falta):
--   create index concurrently if not exists menores_organo_idx
--     on public.menores (organo_contratacion);
-- ----------------------------------------------------------------------------
