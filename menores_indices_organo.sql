-- ============================================================================
-- menores_indices_organo.sql · Timeouts de la vista Menores con filtro de ÓRGANO
-- 17/09/2026 — Alejandro decide crear SOLO los dos de (órgano, fecha)
--
-- EL PROBLEMA (medido en frío con EXPLAIN (ANALYZE, BUFFERS), tope de 30 s por consulta):
--   Filtro órgano = 'Servicio Andaluz de Salud' (230.608 menores desde la carga de F1):
--     · por importe ascendente ................... más de 30 s (con y sin desempate)
--     · por importe descendente .................. 1.854 ms en frío
--     · por fecha descendente (medido en F1) ..... 5.057 ms en frío
--     · desde el 01/04/2026, por importe ......... más de 30 s (asc y desc, con y sin desempate)
--   Causa: Postgres recorre el índice de importe (o de fecha) de TODA la tabla y va a la
--   fila para mirar el órgano. Los 25 menores más baratos del SAS llegan a 350,41 € y hay
--   356.399 menores por debajo de ese importe. Quitar el desempate por licitacion_id no
--   cambia el plan.
--
-- LO QUE SE HA PROBADO ANTES DE PROPONER (réplica TEMPORAL con las 1.713.937 filas,
-- solo las columnas necesarias y los mismos índices que compiten hoy):
--   · (órgano, importe asc) y (órgano, importe desc): SAS por importe pasa a 0,1 ms sin
--     desempate y ~5 ms con él (Incremental Sort sobre 26 filas). Postgres los elige.
--   · (órgano, fecha desc) y (órgano, fecha asc): SAS por fecha pasa a 0,1-3 ms.
--   · Un índice con licitacion_id detrás NO compensa: +63 B por fila (la URL) para ahorrar
--     un Incremental Sort de 26 filas.
--   · SAS DESDE ABRIL por importe NO LO ARREGLA NINGUNO: Postgres estima ~22.000 menores
--     del SAS desde abril (hay 2: la fuente solo publica el SAS hasta el 27/04/2026) y
--     elige recorrer las 230.608 del SAS por importe. Eso lo resuelve la opción (b), en
--     la web, no un índice.
--
-- TAMAÑO (réplica recién construida; con las cargas crecen ~1,3x):
--     menores_organo_importe_asc   ~138 MB     menores_organo_fecha_desc   ~54 MB
--     menores_organo_importe_desc  ~138 MB     menores_organo_fecha_asc    ~54 MB
--   Total ~384 MB (hoy la base ocupa 4.747 MB y el disco 5,82 GB de 8). El órgano es texto
--   de ~52 B de media y casi no se deduplica con el importe; con la fecha sí.
--   Si el espacio pesa más que ordenar un órgano grande por importe, los dos de importe se
--   pueden sustituir por la opción (b) (no dejar ordenar por importe un órgano de más de
--   10.000 menores): se ahorran ~276 MB.
--
-- CÓMO: en el SQL Editor, CADA PASO SOLO, en una pestaña LIMPIA (el editor ejecuta todo lo
-- que hay en la pestaña). OJO: el 17/09/2026 el editor respondió dos veces
-- «25001: CREATE INDEX CONCURRENTLY cannot run inside a transaction block». Si vuelve a
-- pasar, quita `concurrently`: el índice se construye igual, pero bloquea las ESCRITURAS
-- en menores mientras dura (las lecturas de la web siguen). Unos segundos por índice en la
-- réplica; en producción, contar minutos. Fuera de la franja del cron (10:30-11:45 UTC L-V)
-- y del workflow de menores autonómicos (sábado y domingo 03:15 UTC).
-- ============================================================================

-- PASO 1 (solo) — órgano + fecha descendente (orden por defecto de la vista)
create index concurrently if not exists menores_organo_fecha_desc
  on public.menores (organo_contratacion, fecha_adjudicacion desc nulls last);

-- PASO 2 (solo)
create index concurrently if not exists menores_organo_fecha_asc
  on public.menores (organo_contratacion, fecha_adjudicacion asc nulls last);

-- (Los dos de órgano + importe NO se crean por ahora: decisión de Alejandro del 17/09/2026.
-- El orden por importe con filtros lo resuelve la web según el tamaño del resultado.)

-- PASO 3: Claude Code comprueba que los índices son válidos (pg_index.indisvalid), repite
-- en frío las consultas de arriba y la línea base (python medir_menores.py).
-- Ninguno sustituye a menores_organo_fuente_idx (lo usa la regla de duplicados).

-- ----------------------------------------------------------------------------
-- MARCHA ATRÁS (cada uno solo):
--   drop index concurrently if exists public.menores_organo_fecha_desc;
--   drop index concurrently if exists public.menores_organo_fecha_asc;
-- ----------------------------------------------------------------------------
