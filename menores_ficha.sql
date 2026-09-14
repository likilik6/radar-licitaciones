-- menores_ficha.sql  ·  «Menores en la ficha de competidor»
-- ============================================================================
-- QUÉ: una RPC de SOLO LECTURA que resume, para UN CIF, lo que ha ganado por la vía
-- de los CONTRATOS MENORES (public.menores, sindicación 1143). La ficha de competidor
-- sale hoy de public.adjudicaciones (feeds 643+1044), que NO incluye menores: esta
-- función es la que rellena el bloque «Contratos menores» de esa ficha.
--
-- REQUISITO: ejecutar ANTES menores_schema.sql (crea public.menores y el índice
-- GIN menores_cifs_gin sobre cifs_adjudicatarios, del que depende todo esto).
--
-- POR QUÉ UNA RPC Y NO PostgREST (medido el 14/09/2026):
--   El bloque necesita TRES cosas a la vez: nº de menores, SUMA de importes y fecha
--   del último. PostgREST no agrega, así que sumar en el navegador obligaría a
--   traerse todas las filas del CIF: el CIF más grande de la tabla tiene 4.844
--   menores -> 5 viajes de red y ~5.000 filas por el cable para pintar un número.
--   La RPC lo resuelve en UN viaje y en una sola pasada por el índice.
--
-- RENDIMIENTO — EXPLAIN (ANALYZE) con el CIF más pesado de los seguidos (Anticimex
-- A82850611, 479 menores), sobre 1.398.245 filas:
--     Aggregate (actual time=13.940..13.942 rows=1 loops=1)
--       ->  Bitmap Heap Scan on menores (actual rows=478)
--             Recheck Cond: (cifs_adjudicatarios && '{A82850611}'::text[])
--             ->  Bitmap Index Scan on menores_cifs_gin      <-- por el GIN, sin Seq Scan
--     Execution Time: 14.161 ms
--   Es decir: NO hay count exacto sobre 1,4M. Se cuenta SOLO el subconjunto que el
--   GIN devuelve para ese CIF (cientos de filas), que es la regla 3 de rendimiento
--   («nunca count exacto en tablas grandes») bien aplicada: lo caro es contar sin
--   índice, no contar 479 filas ya localizadas.
--
--   PLAN GENÉRICO (el caso malo de plpgsql, que cachea el plan y a partir de la 6ª
--   llamada puede dejar de mirar el valor concreto): comprobado con
--   `set plan_cache_mode = force_generic_plan` -> SIGUE entrando por el índice:
--     ->  Bitmap Index Scan on menores_cifs_gin
--           Index Cond: (cifs_adjudicatarios && ARRAY[$1])      <-- parametrizado
--     Execution Time: 10.702 ms
--   No hay riesgo de que la RPC degenere en Seq Scan tras unas cuantas llamadas.
--
--   PEOR CASO REAL de toda la tabla: el CIF con más menores (B84498955, 4.844 filas)
--   tarda ~1 s. Es OTRA razón para que el bloque cargue APARTE y EN PARALELO con su
--   propio «cargando…» (regla 2): un rival así no puede congelar la ficha.
--
-- MULTI-GANADOR: se filtra por cifs_adjudicatarios[] (TODOS los ganadores distintos
-- del menor), no por cif_adjudicatario (solo el principal). Así un menor repartido
-- entre varias empresas cuenta para todas ellas, que es lo que se quiere al estudiar
-- a un rival. Es además el operador que usa el índice GIN.
--
-- FECHAS SUCIAS (medido el 14/09/2026 sobre 1.398.245 filas): 140 filas tienen
-- fecha_adjudicacion anterior a 2018 (imposibles: 4 en 2002, 1 en 2003...; basura del
-- origen) y 4.879 no tienen fecha. Si no se filtran, UNA fila basura se convierte en
-- «el último menor» de ese competidor. Por eso:
--   · `ultimo`  = MAX(fecha) IGNORANDO las anteriores a MENORES_FICHA_DESDE (2018) y
--                 los NULL. Es el dato que se pinta.
--   · `n`       = TODAS las filas del CIF (contar es contar; no se esconden menores
--                 por tener la fecha mal: lo que está mal es la fecha, no el contrato).
--   · `importe` = suma de TODAS (el importe de una fila con fecha basura sigue siendo
--                 dinero que ese rival se ha llevado).
--   · `n_sin_fecha` / `n_fecha_rara` se devuelven aparte para que la web pueda ser
--     honesta («3 sin fecha») en vez de fingir que el dato está limpio.
--
-- MENORES REPARTIDOS — EL IMPORTE NO ES SUYO ENTERO (medido el 14/09/2026): public.menores
-- guarda UNA fila por menor, con `importe_sin_iva` = importe del contrato COMPLETO y
-- `cif_adjudicatario` = el adjudicatario PRINCIPAL (el de mayor importe). Como aquí se
-- filtra por el array de TODOS los ganadores (que es lo que se quiere), a un ganador
-- SECUNDARIO se le imputaría el dinero entero del contrato. Caso real: EXGONVAL
-- (B97367080) tiene 2.300 € de negocio propio en menores, pero figura como ganador
-- secundario de una obra de emergencia de 1.467.903,65 € cuyo principal es otro -> sin
-- avisar, su ficha inflaría la cifra x640. Alcance: 3.111 filas multi-adjudicatario y
-- 4.438 CIF que aparecen como ganador no principal (de los competidores seguidos solo roza
-- a SGS: 2 filas / 6.200 € de 1,7 M€). Por eso se devuelven `n_compartidos` e
-- `importe_compartido`: el total se sigue dando entero (es lo que dice la fuente), pero la
-- web DICE cuánto de ese total es de un contrato repartido.
--
-- UNIVERSOS DISTINTOS — AVISO QUE LA WEB DEBE RESPETAR: menores = ESTATAL ONLY
-- (sindicación 1143); adjudicaciones = 643 + 1044 (estatal + agregadas). Los dos
-- importes NO se suman en un mismo total sin decir qué incluye cada uno.
--
-- PRIVADO: SECURITY INVOKER (respeta la RLS de public.menores) + execute SOLO a
-- authenticated. anon no puede ni ejecutarla ni leer la tabla.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) Resumen de menores de UN CIF.
--    Devuelve SIEMPRE un objeto json (nunca null), para que el front no tenga que
--    distinguir «sin datos» de «error»: un CIF sin menores devuelve n=0.
-- ----------------------------------------------------------------------------
create or replace function public.menores_resumen_cif(p_cif text)
returns json
language plpgsql
stable                     -- solo lectura; sin SET dentro del cuerpo (no hace falta VOLATILE)
security invoker           -- respeta RLS: quien llama tiene que poder leer public.menores
set search_path = public, pg_catalog
as $$
declare
  v_cif   text;
  v_desde constant date := date '2018-01-01';   -- suelo anti-basura (ver cabecera)
  v_r     record;
begin
  -- Normaliza IGUAL que el pipeline de adjudicaciones/competidores: MAYÚSCULAS y sin
  -- espacios ni . / -. (Medido: en public.menores el 100% de los CIF ya están así, sin
  -- minúsculas ni signos; esto es solo blindaje contra un llamante descuidado.)
  v_cif := upper(regexp_replace(coalesce(p_cif, ''), '[[:space:]./-]', '', 'g'));

  -- Sin CIF no se escanea nada: se responde el objeto vacío.
  if v_cif = '' then
    return json_build_object(
      'cif', null, 'n', 0, 'importe_total', null, 'ultimo', null, 'primero', null,
      'n_sin_fecha', 0, 'n_fecha_rara', 0, 'n_sin_importe', 0,
      'n_compartidos', 0, 'importe_compartido', 0, 'desde', v_desde
    );
  end if;

  -- UNA sola pasada por el GIN: de ahí salen el conteo, la suma y las fechas.
  -- El FILTER de `ultimo`/`primero` es el que deja fuera la basura documentada arriba.
  select
    count(*)                                                              as n,
    sum(m.importe_sin_iva)                                                as importe_total,
    max(m.fecha_adjudicacion) filter (where m.fecha_adjudicacion >= v_desde) as ultimo,
    min(m.fecha_adjudicacion) filter (where m.fecha_adjudicacion >= v_desde) as primero,
    count(*) filter (where m.fecha_adjudicacion is null)                   as n_sin_fecha,
    count(*) filter (where m.fecha_adjudicacion < v_desde)                 as n_fecha_rara,
    count(*) filter (where m.importe_sin_iva is null)                      as n_sin_importe,
    -- MENORES REPARTIDOS (ver cabecera): filas en las que este CIF figura como ganador
    -- pero NO es el principal, así que el importe de la fila es el del contrato entero.
    count(*) filter (where m.n_adjudicatarios > 1
                       and m.cif_adjudicatario is distinct from v_cif)     as n_compartidos,
    coalesce(sum(m.importe_sin_iva) filter (where m.n_adjudicatarios > 1
                       and m.cif_adjudicatario is distinct from v_cif), 0) as importe_compartido
  into v_r
  from public.menores m
  where m.cifs_adjudicatarios && array[v_cif];

  return json_build_object(
    'cif',           v_cif,
    'n',             coalesce(v_r.n, 0),
    'importe_total', v_r.importe_total,          -- null si ninguna fila trae importe
    'ultimo',        v_r.ultimo,                 -- null si TODAS las fechas son basura/null
    'primero',       v_r.primero,
    'n_sin_fecha',   coalesce(v_r.n_sin_fecha, 0),
    'n_fecha_rara',  coalesce(v_r.n_fecha_rara, 0),
    'n_sin_importe', coalesce(v_r.n_sin_importe, 0),
    'n_compartidos', coalesce(v_r.n_compartidos, 0),      -- menores en los que NO es el principal
    'importe_compartido', coalesce(v_r.importe_compartido, 0),
    'desde',         v_desde                     -- el suelo aplicado, para poder explicarlo
  );
end;
$$;

-- ----------------------------------------------------------------------------
-- 2) Permisos · PRIVADO (igual que buscar_licitaciones): solo authenticated.
-- ----------------------------------------------------------------------------
revoke all on function public.menores_resumen_cif(text) from public, anon;
grant execute on function public.menores_resumen_cif(text) to authenticated;

-- Refresca el cache de esquema de PostgREST (si no, la web recibe 404 al llamarla).
notify pgrst, 'reload schema';

-- ----------------------------------------------------------------------------
-- COMPROBACIÓN RÁPIDA (pegar en el SQL Editor después de crearla). Contraste medido
-- el 14/09/2026 con la conexión de solo lectura:
--   A82850611 (Anticimex) -> n=479, importe=1.246.395,32 €, último=2026-08-24
--   A28345577 (SGS)       -> n=374, importe=1.721.676,88 €, último=2026-09-09
--   A03637899 (Labaqua)   -> n=129, importe=  437.575,19 €, último=2026-08-13
--   B85578573 (Crioges)   -> n= 15, importe=  149.285,50 €, último=2026-07-21
--   B86833753 (LODEPA)    -> n=  6, importe=   56.208,00 €, último=2025-08-19
--
-- select public.menores_resumen_cif('A82850611');
-- select public.menores_resumen_cif('a82850611');   -- normaliza -> mismo resultado
-- select public.menores_resumen_cif('Z99999999');   -- sin menores -> n=0, ultimo=null
--   (OJO: 'B00000000' NO sirve de ejemplo vacío; es un CIF de relleno que algún órgano
--    publica de verdad y devuelve 1 menor de 976 €.)
-- select public.menores_resumen_cif('');            -- vacío -> n=0, sin escanear
-- select public.menores_resumen_cif('B97367080');   -- EXGONVAL: n=2, importe 1.470.203,65 €
--   PERO n_compartidos=1 e importe_compartido=1.467.903,65 -> lo suyo son 2.300 €.
--
-- Y el plan (debe salir Bitmap Index Scan on menores_cifs_gin, NUNCA Seq Scan):
-- explain (analyze, costs off)
-- select count(*), sum(importe_sin_iva), max(fecha_adjudicacion)
-- from public.menores where cifs_adjudicatarios && array['A82850611'];
-- ----------------------------------------------------------------------------
