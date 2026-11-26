-- ============================================================================
-- VERSIONES Y BAJAS  ·  que la base guarde SIEMPRE la versión más nueva de cada
-- expediente y marque las bajas del feed
--
-- EL PROBLEMA (PASO 0 del 15/10/2026, medido contra la base y los ZIP completos):
--   La Plataforma republica cada expediente (mismo atom:id, distinto atom:updated) cada
--   vez que cambia. La ingesta nunca comparaba versiones: escribía la que le tocaba por
--   el orden de lectura, y ese orden favorecía casi siempre a la VIEJA.
--     · 234.104 licitaciones tenían una versión más vieja que la última;
--     · 196.732 expedientes se habían quedado SIN adjudicaciones (entre ellos, 4
--       ganadas por LODEPA que el auto-marcado nunca vio);
--     · 2.817 bajas del feed seguían como vivas.
--   Además, el upsert de PostgREST (merge-duplicates) escribe A CIEGAS: un re-backfill
--   lanzado después pisa lo nuevo con lo viejo, que es lo que pasó el 15/09.
--
-- LA REGLA (especificación de sindicación PCSP v1.3, §3.3.2, más un desempate):
--   1. Gana la versión de MAYOR atom_updated (timestamptz: compara instantes, no texto).
--   2. Con el MISMO atom_updated gana la leída en un snapshot IGUAL O MÁS RECIENTE
--      (version_snapshot = el <updated> del propio .atom) SI su contenido es distinto
--      (version_huella). Hace falta por dos motivos:
--        · en septiembre la Plataforma republicó 6.491 expedientes con la marca vieja y
--          el contenido nuevo (ADJ→RES, EV→ADJ...): snapshot MÁS reciente. Acierta aunque
--          traiga menos avisos o un estado «hacia atrás» (anulada, adjudicación caída):
--          8 de 8 comprobados en la web el 30/10. No desempatar por avisos ni por estado;
--        · releer el MISMO .atom con un extractor mejorado (p. ej. una columna nueva,
--          como los re-backfills de nº de expediente o de ccaa): snapshot IGUAL.
--   3. Si todo es igual (marca, snapshot y contenido), no se toca nada.
--
-- QUÉ HACE ESTE FICHERO (solo DDL ligero; NO mueve datos):
--   1) 6 columnas nuevas en public.licitaciones y 1 en public.adjudicaciones. Todas
--      admiten NULL y no llevan valor por defecto: añadirlas es instantáneo (no reescribe
--      la tabla). Las filas actuales quedan con NULL hasta la reparación.
--   2) La función public.carga_versiones(jsonb): la ÚNICA vía de escritura de la ingesta
--      a partir de ahora. Escribe una licitación solo si la versión que llega gana según
--      la regla y, en ese caso, SUSTITUYE sus adjudicaciones en la misma transacción.
--      También marca las bajas.
--   3) Permisos: solo service_role puede ejecutarla.
--
-- PERMISOS (cambio de Supabase del 30/10/2026): este fichero NO crea tablas ni vistas.
--   Las columnas nuevas heredan los permisos de su tabla, así que no hace falta GRANT.
--   La función nueva SÍ: por defecto nacería ejecutable por anon y authenticated, y por
--   eso se le quitan explícitamente (bloque 3).
--
-- CÓMO USARLO:
--   · Pega TODO en el SQL Editor de Supabase y pulsa Run. Es IDEMPOTENTE (if not exists /
--     create or replace): se puede re-ejecutar. Para cambiar SOLO la función más adelante,
--     basta con pegar los bloques 2 y 3.
--   · ANTES, mira en GitHub → Actions que no haya nada en marcha. Horas MEDIDAS del robot:
--     radar.yml de lunes a viernes entre ~10:00 y ~14:00 UTC (el cron dice 05:23, pero
--     GitHub lo retrasa) y mantenimiento.yml los domingos a las 06:17 UTC.
--   · El SQL Editor ejecuta el fichero entero en UNA transacción: el bloqueo exclusivo que
--     pide «alter table» (un instante) dura hasta el final. Por eso el lock_timeout corto:
--     si en 3 s no lo consigue, FALLA sin tocar nada y basta con repetirlo un rato después.
--     Mientras espera, las lecturas de la web pueden quedarse en cola esos segundos.
--   · Al final sale una TABLA DE COMPROBACIÓN (bloque 5): las 6 filas tienen que decir «sí»
--     en la columna «ok». Si sale un error en vez de la tabla, no se ha aplicado nada.
--   · Después, ejecuta prueba_versiones.sql (comprueba cada rama y no deja rastro).
--
-- CONTRATO DEL CLIENTE (lo que tiene que hacer backfill_catalogo.py; lo fijan sus tests):
--   · ELEGIR antes de mandar: de cada atom:id, solo la versión ganadora de lo que ha leído
--     (feeds.elige_versiones). Si un id viene dos veces en un lote, la función se queda
--     con la más nueva y lo cuenta como «duplicadas».
--   · HUELLA (version_huella): md5 de un JSON con las claves ORDENADAS y separadores fijos
--     que contiene SOLO el contenido: las columnas de la licitación que se escriben MENOS
--     ultima_vez, atom_updated, version_snapshot y version_huella, más sus adjudicaciones
--     (ordenadas por lote y CIF, sin updated_at ni atom_updated). Si la huella incluyera
--     la hora de carga, cambiaría en cada pasada y todo se reescribiría cada día.
--   · CADA licitación lleva DENTRO su lista "adjudicaciones" (vacía si la versión no trae
--     ninguna). Una licitación sin esa lista se DESCARTA (no se escribe, no se borra nada)
--     y se cuenta en «descartadas»: así un fallo del cliente nunca vacía adjudicaciones.
--     Lo mismo si le falta licitacion_id, atom_updated, version_snapshot o version_huella
--     (p. ej. una fecha sin zona horaria: feeds.instante devuelve None y no se adivina).
--     El cliente debe pararse si «descartadas» > 0.
--   · BAJAS AL FINAL DE LA EJECUCIÓN, después de todas las versiones. La carga recorre los
--     ZIP de lo más nuevo a lo más viejo y la baja suele estar en un fichero MÁS NUEVO que
--     la última versión de su expediente: si se mandara en el mismo lote que su fichero,
--     el expediente aún no existiría y la baja se perdería («bajas_sin_fila»). Las bajas
--     pendientes se guardan en el checkpoint para no perderlas al reanudar.
--   · REINTENTOS: ante 55P03 (bloqueo), 57014 (tiempo) o 40P01 (interbloqueo), reintentar
--     partiendo el lote en dos. La llamada hereda el lock_timeout de 8 s de authenticator
--     y el statement_timeout de 120 s de service_role. Una versión rechazada también
--     bloquea su fila hasta el final de la llamada.
--   · ultima_vez CAMBIA DE SIGNIFICADO: antes era «la última vez que la vimos» (se pisaba
--     en cada pasada); ahora es «la última vez que se escribió una versión».
--
-- ORDEN DE DESPLIEGUE (no saltárselo):
--   1. Este fichero + prueba_versiones.sql. El código ACTUAL sigue funcionando después
--      (manda sus columnas de siempre; las nuevas quedan NULL).
--   2. El código nuevo de la ingesta (feeds.py, backfill_catalogo.py, filtrar.py), con
--      test_versiones.py en verde. Si el código fuera ANTES que este fichero, el diario
--      llamaría a una función que no existe y fallaría.
--   3. Lanzar radar.yml a mano (no se dispara con push) y comprobar el log.
--   4. La reparación dirigida, con su propio OK: reparar_versiones.py (en Actions,
--      reparacion.yml; primero sin marcar «escribir» y leer el ensayo). Mientras dure:
--        · NO lanzar backfill.yml (las filas con atom_updated NULL aceptan cualquier
--          versión, aunque sea vieja; solo la reparación elige sobre TODOS los ZIP);
--        · parar la purga del job catalogo (reintroduce y borra a la vez lo purgado);
--        · nunca a la vez que el robot ni que el mantenimiento del domingo.
--   5. Refrescos: refrescar_competidores, refrescar_desiertas completo (p_dias=0),
--      automarcar_ganadas en simulación. OJO: el auto-marcado nunca DESMARCA. Si una
--      versión nueva quita a LODEPA (anulación), la decisión «ganada» se queda: listar con
--      la consulta del bloque 4 y decidir a mano. Después, VACUUM ANALYZE de las dos tablas
--      (por separado, en el editor) y mirar el disco: la reparación reescribe mucho.
--
-- MARCHA ATRÁS:
--   · Freno de emergencia: «revoke execute on function public.carga_versiones(jsonb)
--     from service_role;» (el diario fallará al llamarla: mejor que escribir mal).
--   · Si se vuelve al código ANTERIOR aunque sea un día, ese código escribe a ciegas sin
--     tocar las columnas de versión. ANTES de volver a poner el código nuevo:
--     «update public.licitaciones set atom_updated = null, version_snapshot = null,
--      version_huella = null where ultima_vez >= '<hora UTC en que volvió el código viejo>';»
--     (si no, la guarda protegería contenido viejo con la marca de una versión nueva).
--   · No hace falta borrar las columnas: nadie más las lee. Borrarlas y crearlas de nuevo
--     obligaría a repetir la reparación entera.
--
-- PARA QUIEN LEA ESTAS TABLAS (Radar Farma, informes): las bajas siguen siendo filas
--   (filtrar «baja_en is null» si no se quieren), y las adjudicaciones de un expediente
--   pueden DESAPARECER si una versión nueva lo anula.
--
-- NO toca: la web, el login, decisiones, cartera, contratos, la purga, menores, el
--   trigger del tsv ni las RPC del Buscador.
-- ============================================================================

-- Si no consigue el bloqueo en 3 s, falla en vez de quedarse esperando (ver arriba).
set lock_timeout = '3s';

-- ----------------------------------------------------------------------------
-- 1) Columnas nuevas
-- ----------------------------------------------------------------------------
-- licitaciones:
--   atom_updated       atom:updated de la versión guardada (la marca de la Plataforma).
--   version_snapshot   <updated> del .atom donde se leyó esa versión (el desempate).
--   version_huella     md5 del contenido escrito (ver «HUELLA» arriba).
--   estado_expediente  ContractFolderStatusCode de esa versión: PRE, PUB, EV, ADJ, RES,
--                      ANUL... Hoy solo lo tenían las adjudicaciones; sin él, la web no
--                      puede saber si algo está anulado o ya resuelto.
--   baja_en / baja_tipo  fecha y tipo (ANULADA en el estatal, CERRADA en agregadas, a veces
--                      ninguno) de la <at:deleted-entry> si es POSTERIOR a la versión
--                      guardada. Una versión posterior a la baja la borra (republicación).
--                      NUNCA se borra la licitación: solo se marca.
alter table public.licitaciones
  add column if not exists atom_updated      timestamptz,
  add column if not exists version_snapshot  timestamptz,
  add column if not exists version_huella    text,
  add column if not exists estado_expediente text,
  add column if not exists baja_en           timestamptz,
  add column if not exists baja_tipo         text;

-- adjudicaciones: de qué versión del expediente sale cada fila (para auditar).
alter table public.adjudicaciones
  add column if not exists atom_updated timestamptz;

reset lock_timeout;

-- ----------------------------------------------------------------------------
-- 2) public.carga_versiones(p_lote jsonb) -> jsonb
-- ----------------------------------------------------------------------------
-- ENTRADA (lo que manda backfill_catalogo.py, en lotes de ~500 expedientes):
--   {
--     "licitaciones": [
--       { licitacion_id, titulo, objeto, num_expediente, organo_contratacion, cpv, fuente,
--         presupuesto_con_iva, presupuesto_sin_iva, valor_estimado, fecha_publicacion,
--         fecha_fin_plazo, enlace, ccaa, lugar_ejecucion, nuts_codigo,
--         atom_updated, version_snapshot, version_huella, estado_expediente,
--         "adjudicaciones": [ { lote, cif_adjudicatario, resultado_code, resultado,
--                               id_scheme, adjudicatario, es_pyme, importe_sin_iva,
--                               importe_con_iva, n_ofertas, fecha_adjudicacion,
--                               estado_expediente, presupuesto_lote_sin_iva,
--                               sistema_contratacion, tipo_contrato }, ... ] },
--       ... ],
--     "bajas": [ { licitacion_id, baja_en, baja_tipo }, ... ]
--   }
--   Las dos listas son opcionales (una llamada puede traer solo bajas).
--   Una columna de la lista que no venga en el JSON se escribe como NULL.
--
-- SALIDA:
--   recibidas      licitaciones en el JSON
--   descartadas    sin licitacion_id / atom_updated / version_snapshot / version_huella
--                  o sin su lista "adjudicaciones" (no se escribe nada de ellas)
--   duplicadas     ids repetidos dentro del lote (se queda la versión más nueva)
--   aplicadas      licitaciones escritas (nuevas o con una versión que gana)
--   rechazadas     versiones válidas que NO ganan (más viejas o idénticas)
--   adj_borradas / adj_insertadas   filas de adjudicaciones sustituidas
--   sin_adjudicacion_recalculadas   licitaciones escritas que se quedan sin filas y a las
--                  que se les ha puesto estado_adjudicacion / n_lotes_desiertos a NULL
--   bajas_recibidas / bajas_marcadas / bajas_sin_fila (id que no está en la base)
--
-- QUÉ HACE, en UNA transacción (si algo falla, no queda nada a medias):
--   a) Licitaciones: inserta las nuevas y actualiza las existentes SOLO si la versión gana.
--      Las filas de hoy (atom_updated NULL) aceptan cualquier versión: así entran en el
--      sistema en la reparación.
--   b) Adjudicaciones: SOLO para las licitaciones escritas, borra todas sus filas e inserta
--      las de ESA versión (las que van dentro de su objeto), aunque sean CERO: una
--      anulación borra el ganador. Una versión que no gana no toca nada.
--   c) Si una licitación escrita se queda sin adjudicaciones, pone a NULL su
--      estado_adjudicacion y n_lotes_desiertos: refrescar_desiertas solo recalcula las
--      que tienen filas, así que si no se hace aquí se quedarían con el valor antiguo.
--   d) Bajas: marca baja_en/baja_tipo si la baja es posterior a la versión guardada (o la
--      fila aún no tiene versión: la reparación la confirmará o la limpiará al escribir la
--      versión real) y posterior a la baja ya marcada.
--
-- SECURITY INVOKER: se ejecuta con los permisos de quien llama (service_role, que ya puede
-- escribir en las dos tablas y se salta la RLS). No necesita más.
create or replace function public.carga_versiones(p_lote jsonb)
returns jsonb
language plpgsql
volatile
security invoker
set search_path = public, pg_temp
as $$
declare
  v_lics        jsonb := coalesce(p_lote -> 'licitaciones', '[]'::jsonb);
  v_bajas       jsonb := coalesce(p_lote -> 'bajas', '[]'::jsonb);
  v_recibidas   integer;
  v_validas     integer := 0;
  v_unicas      integer := 0;
  v_ids         text[]  := '{}';
  v_ganadoras   jsonb   := '[]'::jsonb;
  v_adj_borr    integer := 0;
  v_adj_ins     integer := 0;
  v_recalc      integer := 0;
  v_bajas_rec   integer := 0;
  v_bajas_marc  integer := 0;
  v_bajas_sin   integer := 0;
begin
  if jsonb_typeof(v_lics) <> 'array' or jsonb_typeof(v_bajas) <> 'array' then
    raise exception 'carga_versiones: "licitaciones" y "bajas" tienen que ser listas JSON';
  end if;
  if p_lote ? 'con_adjudicaciones' or p_lote ? 'adjudicaciones' then
    raise exception 'carga_versiones: las adjudicaciones van DENTRO de cada licitación '
                    '(no hay "adjudicaciones" ni "con_adjudicaciones" de primer nivel)';
  end if;
  v_recibidas := jsonb_array_length(v_lics);

  -- ---- a) licitaciones ------------------------------------------------------
  -- jsonb_populate_record con el tipo de la propia tabla: los tipos (text[], numeric,
  -- timestamptz) salen de la definición real, sin repetirlos aquí.
  with entrada as (
    select r.*, e.j -> 'adjudicaciones' as adjs, e.n
      from jsonb_array_elements(v_lics) with ordinality as e(j, n)
     cross join lateral jsonb_populate_record(null::public.licitaciones, e.j) as r
     where r.licitacion_id    is not null
       and r.atom_updated     is not null
       and r.version_snapshot is not null
       and r.version_huella   is not null
       and jsonb_typeof(e.j -> 'adjudicaciones') = 'array'
  ), unica as (
    -- Un mismo id dos veces en el lote: se queda la más nueva (y, a igualdad, la última);
    -- sus adjudicaciones van con ella.
    select distinct on (licitacion_id) *
      from entrada
     order by licitacion_id, atom_updated desc, version_snapshot desc, n desc
  ), escritas as (
    insert into public.licitaciones as t (
      licitacion_id, titulo, objeto, num_expediente, organo_contratacion, cpv, fuente,
      presupuesto_con_iva, presupuesto_sin_iva, valor_estimado,
      fecha_publicacion, fecha_fin_plazo, enlace, ccaa, lugar_ejecucion, nuts_codigo,
      atom_updated, version_snapshot, version_huella, estado_expediente, ultima_vez)
    select
      licitacion_id, titulo, objeto, num_expediente, organo_contratacion, cpv, fuente,
      presupuesto_con_iva, presupuesto_sin_iva, valor_estimado,
      fecha_publicacion, fecha_fin_plazo, enlace, ccaa, lugar_ejecucion, nuts_codigo,
      atom_updated, version_snapshot, version_huella, estado_expediente, now()
      from unica
    on conflict (licitacion_id) do update set
      titulo              = excluded.titulo,
      objeto              = excluded.objeto,
      num_expediente      = excluded.num_expediente,
      organo_contratacion = excluded.organo_contratacion,
      cpv                 = excluded.cpv,
      fuente              = excluded.fuente,
      presupuesto_con_iva = excluded.presupuesto_con_iva,
      presupuesto_sin_iva = excluded.presupuesto_sin_iva,
      valor_estimado      = excluded.valor_estimado,
      fecha_publicacion   = excluded.fecha_publicacion,
      fecha_fin_plazo     = excluded.fecha_fin_plazo,
      enlace              = excluded.enlace,
      ccaa                = excluded.ccaa,
      lugar_ejecucion     = excluded.lugar_ejecucion,
      nuts_codigo         = excluded.nuts_codigo,
      atom_updated        = excluded.atom_updated,
      version_snapshot    = excluded.version_snapshot,
      version_huella      = excluded.version_huella,
      estado_expediente   = excluded.estado_expediente,
      ultima_vez          = excluded.ultima_vez,
      -- Una baja posterior a la versión que se escribe se conserva; una anterior deja de
      -- valer (se republicó). Las dos expresiones leen el baja_en ANTIGUO (t.*).
      baja_en   = case when t.baja_en > excluded.atom_updated then t.baja_en   end,
      baja_tipo = case when t.baja_en > excluded.atom_updated then t.baja_tipo end
    where t.atom_updated is null
       or excluded.atom_updated > t.atom_updated
       or (    excluded.atom_updated = t.atom_updated
           and excluded.version_snapshot >= coalesce(t.version_snapshot, '-infinity'::timestamptz)
           and excluded.version_huella is distinct from t.version_huella)
    returning t.licitacion_id, t.atom_updated
  )
  select coalesce(array_agg(w.licitacion_id), '{}'),
         coalesce(jsonb_agg(jsonb_build_object('id', w.licitacion_id, 'au', w.atom_updated,
                                               'adjs', u.adjs)), '[]'::jsonb),
         (select count(*) from entrada),
         (select count(*) from unica)
    into v_ids, v_ganadoras, v_validas, v_unicas
    from escritas w
    join unica u using (licitacion_id);

  -- ---- b) adjudicaciones de las licitaciones escritas -----------------------
  -- (dos sentencias separadas: borrar e insertar en la MISMA sentencia chocaría con la
  --  clave única, porque Postgres no garantiza el orden entre sub-sentencias)
  if cardinality(v_ids) > 0 then
    delete from public.adjudicaciones a
     where a.licitacion_id = any (v_ids);
    get diagnostics v_adj_borr = row_count;

    insert into public.adjudicaciones (
      licitacion_id, lote, resultado_code, resultado, cif_adjudicatario, id_scheme,
      adjudicatario, es_pyme, importe_sin_iva, importe_con_iva, n_ofertas,
      fecha_adjudicacion, estado_expediente, presupuesto_lote_sin_iva,
      sistema_contratacion, tipo_contrato, atom_updated, updated_at)
    select distinct on (g.id, coalesce(r.lote, ''), coalesce(r.cif_adjudicatario, ''))
           g.id, coalesce(r.lote, ''), r.resultado_code, r.resultado,
           coalesce(r.cif_adjudicatario, ''), r.id_scheme, r.adjudicatario, r.es_pyme,
           r.importe_sin_iva, r.importe_con_iva, r.n_ofertas, r.fecha_adjudicacion,
           r.estado_expediente, r.presupuesto_lote_sin_iva, r.sistema_contratacion,
           r.tipo_contrato,
           g.au,          -- la versión ESCRITA, no lo que diga el JSON de la fila
           now()
      from jsonb_to_recordset(v_ganadoras) as g(id text, au timestamptz, adjs jsonb)
     cross join lateral jsonb_array_elements(g.adjs) with ordinality as e(j, n)
     cross join lateral jsonb_populate_record(null::public.adjudicaciones, e.j) as r
     -- misma clave repetida: gana la última, como hacía _dedup_adj en Python
     order by g.id, coalesce(r.lote, ''), coalesce(r.cif_adjudicatario, ''), e.n desc;
    get diagnostics v_adj_ins = row_count;

    -- ---- c) las que se han quedado sin adjudicaciones ---------------------
    update public.licitaciones l
       set estado_adjudicacion = null,
           n_lotes_desiertos   = null
     where l.licitacion_id = any (v_ids)
       and (l.estado_adjudicacion is not null or l.n_lotes_desiertos is not null)
       and not exists (select 1 from public.adjudicaciones a
                        where a.licitacion_id = l.licitacion_id);
    get diagnostics v_recalc = row_count;
  end if;

  -- ---- d) bajas -------------------------------------------------------------
  with b as (
    select distinct on (z.licitacion_id) z.licitacion_id, z.baja_en, z.baja_tipo
      from jsonb_to_recordset(v_bajas) as z(licitacion_id text, baja_en timestamptz, baja_tipo text)
     where z.licitacion_id is not null
       and z.baja_en is not null
     order by z.licitacion_id, z.baja_en desc
  ), marcadas as (
    update public.licitaciones t
       set baja_en   = b.baja_en,
           baja_tipo = b.baja_tipo
      from b
     where t.licitacion_id = b.licitacion_id
       and (t.atom_updated is null or b.baja_en > t.atom_updated)
       and (t.baja_en is null or b.baja_en > t.baja_en)
    returning 1
  )
  select (select count(*) from b),
         (select count(*) from marcadas),
         (select count(*) from b
           where not exists (select 1 from public.licitaciones l
                              where l.licitacion_id = b.licitacion_id))
    into v_bajas_rec, v_bajas_marc, v_bajas_sin;

  return jsonb_build_object(
    'recibidas',                     v_recibidas,
    'descartadas',                   v_recibidas - v_validas,
    'duplicadas',                    v_validas - v_unicas,
    'aplicadas',                     cardinality(v_ids),
    'rechazadas',                    v_unicas - cardinality(v_ids),
    'adj_borradas',                  v_adj_borr,
    'adj_insertadas',                v_adj_ins,
    'sin_adjudicacion_recalculadas', v_recalc,
    'bajas_recibidas',               v_bajas_rec,
    'bajas_marcadas',                v_bajas_marc,
    'bajas_sin_fila',                v_bajas_sin);
end;
$$;

-- ----------------------------------------------------------------------------
-- 3) Permisos: SOLO service_role (el robot). Por defecto, una función nueva de public
--    nacería ejecutable por anon y authenticated: se les quita aquí.
-- ----------------------------------------------------------------------------
revoke all on function public.carga_versiones(jsonb) from public, anon, authenticated;
grant execute on function public.carga_versiones(jsonb) to service_role;

-- Que PostgREST vea la función nueva sin esperar.
notify pgrst, 'reload schema';

-- ----------------------------------------------------------------------------
-- 4) Consulta para DESPUÉS de la reparación (SOLO LECTURA; pégala aparte cuando toque)
-- ----------------------------------------------------------------------------
-- Las columnas y los permisos de este fichero ya los comprueba la tabla del bloque 5.
--
-- Tras la reparación: «ganadas» de LODEPA que se han quedado sin su adjudicación (el
-- auto-marcado nunca desmarca; se deciden a mano).
-- select d.licitacion_id, d.estado, l.estado_expediente, l.baja_en
--   from public.decisiones d
--   left join public.licitaciones l using (licitacion_id)
--  where d.perfil = 'lodepa' and d.estado = 'ganada'
--    and not exists (select 1 from public.adjudicaciones a
--                     where a.licitacion_id = d.licitacion_id
--                       and a.cif_adjudicatario = 'B86833753');

-- ----------------------------------------------------------------------------
-- 5) COMPROBACIÓN: lo único que enseña el SQL Editor es esta tabla. Las 6 filas tienen
--    que decir «sí» en «ok».
-- ----------------------------------------------------------------------------
select comprobacion, resultado, esperado,
       case when resultado = esperado then 'sí' else 'NO' end as ok
  from (values
    (1, 'columnas nuevas en licitaciones (de 6)',
        (select count(*)::text from information_schema.columns
          where table_schema = 'public' and table_name = 'licitaciones'
            and column_name in ('atom_updated', 'version_snapshot', 'version_huella',
                                'estado_expediente', 'baja_en', 'baja_tipo')), '6'),
    (2, 'columna atom_updated en adjudicaciones',
        (select count(*)::text from information_schema.columns
          where table_schema = 'public' and table_name = 'adjudicaciones'
            and column_name = 'atom_updated'), '1'),
    (3, 'existe la función carga_versiones(jsonb)',
        (to_regprocedure('public.carga_versiones(jsonb)') is not null)::text, 'true'),
    (4, 'anon puede ejecutarla',
        has_function_privilege('anon', 'public.carga_versiones(jsonb)', 'execute')::text, 'false'),
    (5, 'authenticated puede ejecutarla',
        has_function_privilege('authenticated', 'public.carga_versiones(jsonb)', 'execute')::text, 'false'),
    (6, 'service_role puede ejecutarla',
        has_function_privilege('service_role', 'public.carga_versiones(jsonb)', 'execute')::text, 'true')
  ) as t(n, comprobacion, resultado, esperado)
 order by n;
