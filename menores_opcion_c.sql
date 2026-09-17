-- ============================================================================
-- menores_opcion_c.sql · PROPUESTA (F0, 17/09/2026) — NO EJECUTAR SIN OK
--
-- QUÉ: cambiar los 2 índices de orden por FECHA de public.menores, que llevan la URL
-- larga de la PLACSP como desempate, por 2 índices de UNA sola clave. La consulta de
-- la web no cambia (sigue ordenando por fecha y luego licitacion_id): Postgres usa el
-- índice fino y ordena cada grupo de la misma fecha con un Incremental Sort.
--
-- POR QUÉ (medido con EXPLAIN y réplicas temporales, y verificado por separado):
--   · menores_fecha_desc_id + menores_fecha_asc_id = 466 MB. Los finos: ~20-25 MB
--     (las fechas se deduplican en el btree). Ahorro: 441-447 MB, y cada fila
--     autonómica nueva pesa ~8 % menos.
--   · Primera página por fecha: 0,07 ms -> 0,6 ms en caliente. OFFSET 2.500:
--     3,7 -> 8,6 ms. Nicho real (8 CPV + 19 frases): 111 -> 125 ms en caliente, mismo
--     plan. Con filtro de fecha, peor caso ~0,8 s en frío (grupos de hasta 2.866 filas).
--   · NO se toca el orden por IMPORTE: con un índice fino, ordenar por importe con un
--     mínimo o máximo redondo (1.500 €, 2.000 €...) obliga a leer el grupo entero
--     (hasta 8.358 filas) y medido en frío tarda 3,5-5,2 s, cerca de los 8 s de límite
--     de la web. Esos 2 índices se quedan como están.
--
-- CÓMO: en el SQL Editor, CADA PASO SOLO (selecciona sus líneas y Run). CONCURRENTLY
-- no puede ir dentro de una transacción, y el editor mete en una todo lo que se lanza
-- junto. CONCURRENTLY no bloquea lecturas ni la ingesta, pero recorre la tabla dos
-- veces: mejor fuera de la franja del cron (10:30-11:45 UTC) y con poco uso.
-- Es reversible: si algo va mal, se recrean los índices antiguos (definición abajo).
-- ============================================================================

-- PASO 1 (solo)
create index concurrently if not exists menores_fecha_desc
  on public.menores (fecha_adjudicacion desc nulls last);

-- PASO 2 (solo)
create index concurrently if not exists menores_fecha_asc
  on public.menores (fecha_adjudicacion asc nulls last);

-- PASO 3: NO borres nada todavía. Claude Code comprueba que los dos índices nuevos
-- están válidos (pg_index.indisvalid) antes del paso 4.

-- PASO 4 (solo)
drop index concurrently if exists public.menores_fecha_desc_id;

-- PASO 5 (solo)
drop index concurrently if exists public.menores_fecha_asc_id;

-- PASO 6 (solo): estadísticas y espacio reutilizable (hoy hay ~244.000 filas muertas).
vacuum (analyze) public.menores;

-- PASO 7: Claude Code repite la línea base (python medir_menores.py) y el EXPLAIN de
-- las 4 ordenaciones de la vista Menores para confirmar el Incremental Sort.

-- ----------------------------------------------------------------------------
-- MARCHA ATRÁS (solo si hiciera falta; cada uno solo):
--   create index concurrently if not exists menores_fecha_desc_id
--     on public.menores (fecha_adjudicacion desc nulls last, licitacion_id asc);
--   create index concurrently if not exists menores_fecha_asc_id
--     on public.menores (fecha_adjudicacion asc nulls last, licitacion_id asc);
-- ----------------------------------------------------------------------------
