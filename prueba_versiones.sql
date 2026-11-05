-- ============================================================================
-- PRUEBA de public.carga_versiones (versiones y bajas) — SIN TOCAR DATOS REALES.
--
-- Es un ÚNICO bloque DO: escribe expedientes SINTÉTICOS (licitacion_id 'ZZTEST-...')
-- a través de la función, comprueba cada rama de la regla y AL FINAL FUERZA UN ROLLBACK:
-- no persiste NADA.
--   · Si todo va bien: NOTICE «PRUEBA VERSIONES OK ...» y la transacción se revierte sola.
--   · Si una comprobación falla: verás el mensaje del assert y también se revierte todo.
--
-- Requisito: haber ejecutado antes versiones_schema.sql.
-- CÓMO USARLO: pega TODO en el SQL Editor de Supabase y pulsa Run. Se puede repetir.
--
-- Los casos imitan expedientes reales del PASO 0 (15/10/2026):
--   19366614 (plazo ampliado), 19554568 (lotes renumerados), 20329062 (republicación
--   con la misma marca), 19116501 (anulada: la adjudicación tiene que desaparecer),
--   16577712 (baja ANULADA) y 19979771 (baja seguida de una nueva versión).
-- ============================================================================
do $$
declare
  id1 constant text := 'ZZTEST-versiones-1';
  id2 constant text := 'ZZTEST-versiones-2';
  id3 constant text := 'ZZTEST-versiones-3';
  id4 constant text := 'ZZTEST-versiones-4';
  id9 constant text := 'ZZTEST-versiones-no-existe';
  -- Plantilla de una licitación: cada caso pisa lo que necesita con «||».
  base jsonb := jsonb_build_object('licitacion_id', 'ZZTEST-versiones-1', 'titulo', 'Prueba de versiones',
                                   'fuente', 'estatal', 'presupuesto_sin_iva', 100,
                                   'cpv', jsonb_build_array('90920000'), 'adjudicaciones', '[]'::jsonb);
  r jsonb;
  f record;
begin
  -- A) ALTA: el expediente no existe -> se escribe.
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'atom_updated', '2026-05-04T14:09:58+02:00', 'version_snapshot', '2026-05-05T04:00:56+02:00',
         'version_huella', 'h1', 'estado_expediente', 'PUB', 'fecha_fin_plazo', '2026-05-08'))));
  assert (r ->> 'aplicadas')::int = 1 and (r ->> 'descartadas')::int = 0, format('A alta: %s', r);
  select * into f from public.licitaciones where licitacion_id = id1;
  assert f.fecha_fin_plazo = '2026-05-08'::timestamptz and f.estado_expediente = 'PUB'
     and f.atom_updated = '2026-05-04T14:09:58+02:00'::timestamptz and f.cpv = array['90920000'],
     format('A alta: fila %s', row_to_json(f));

  -- B) VERSIÓN VIEJA (30/03) que llega después -> rechazada, no cambia nada.
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'atom_updated', '2026-03-30T10:04:36+02:00', 'version_snapshot', '2026-03-31T04:00:56+02:00',
         'version_huella', 'h0', 'estado_expediente', 'PUB', 'fecha_fin_plazo', '2026-05-04'))));
  assert (r ->> 'rechazadas')::int = 1 and (r ->> 'aplicadas')::int = 0, format('B vieja: %s', r);
  assert (select fecha_fin_plazo from public.licitaciones where licitacion_id = id1) = '2026-05-08'::timestamptz,
     'B vieja: el plazo no debe cambiar';

  -- C) VERSIÓN NUEVA (08/05: plazo ampliado al 21/05) con 2 adjudicaciones -> escrita.
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'atom_updated', '2026-05-08T10:05:19+02:00', 'version_snapshot', '2026-05-09T04:01:08+02:00',
         'version_huella', 'h2', 'estado_expediente', 'ADJ', 'fecha_fin_plazo', '2026-05-21',
         'adjudicaciones', jsonb_build_array(
           jsonb_build_object('lote', '610363', 'cif_adjudicatario', 'ZZTESTA1', 'importe_sin_iva', 100, 'resultado_code', '8'),
           jsonb_build_object('lote', '610364', 'cif_adjudicatario', 'ZZTESTB2', 'importe_sin_iva', 200, 'resultado_code', '8'))))));
  assert (r ->> 'aplicadas')::int = 1 and (r ->> 'adj_insertadas')::int = 2, format('C nueva: %s', r);
  assert (select fecha_fin_plazo from public.licitaciones where licitacion_id = id1) = '2026-05-21'::timestamptz,
     'C nueva: el plazo ampliado tiene que quedar';
  assert (select bool_and(atom_updated = '2026-05-08T10:05:19+02:00'::timestamptz) from public.adjudicaciones
           where licitacion_id = id1), 'C: las adjudicaciones llevan la marca de su versión';

  -- D) MISMA MARCA, snapshot ANTERIOR y contenido distinto -> rechazada.
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'atom_updated', '2026-05-08T10:05:19+02:00', 'version_snapshot', '2026-05-08T20:00:00+02:00',
         'version_huella', 'hD', 'estado_expediente', 'PUB', 'fecha_fin_plazo', '2026-05-30'))));
  assert (r ->> 'rechazadas')::int = 1, format('D empate con snapshot anterior: %s', r);
  assert (select count(*) from public.adjudicaciones where licitacion_id = id1) = 2, 'D: adjudicaciones intactas';

  -- D2) MISMA MARCA, MISMO snapshot y otra huella (releer el mismo .atom con el extractor
  --     mejorado, p. ej. una columna nueva) -> se escribe.
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'atom_updated', '2026-05-08T10:05:19+02:00', 'version_snapshot', '2026-05-09T04:01:08+02:00',
         'version_huella', 'h2-con-nuts', 'estado_expediente', 'ADJ', 'fecha_fin_plazo', '2026-05-21',
         'nuts_codigo', 'ES511',
         'adjudicaciones', jsonb_build_array(
           jsonb_build_object('lote', '610363', 'cif_adjudicatario', 'ZZTESTA1', 'importe_sin_iva', 100, 'resultado_code', '8'),
           jsonb_build_object('lote', '610364', 'cif_adjudicatario', 'ZZTESTB2', 'importe_sin_iva', 200, 'resultado_code', '8'))))));
  assert (r ->> 'aplicadas')::int = 1, format('D2 mismo snapshot, otra huella: %s', r);
  assert (select nuts_codigo from public.licitaciones where licitacion_id = id1) = 'ES511', 'D2: columna nueva escrita';

  -- E) MISMA MARCA, snapshot POSTERIOR y contenido distinto (republicación de septiembre:
  --    ADJ -> RES y lotes renumerados) -> escrita, y las adjudicaciones se SUSTITUYEN.
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'atom_updated', '2026-05-08T10:05:19+02:00', 'version_snapshot', '2026-09-23T09:27:41+02:00',
         'version_huella', 'h2b', 'estado_expediente', 'RES', 'fecha_fin_plazo', '2026-05-21',
         'adjudicaciones', jsonb_build_array(
           -- (el atom_updated que traiga la fila se IGNORA: manda el de su licitación)
           jsonb_build_object('lote', '618417', 'cif_adjudicatario', 'ZZTESTA1', 'importe_sin_iva', 100,
                              'resultado_code', '9', 'atom_updated', '1999-01-01T00:00:00Z'),
           jsonb_build_object('lote', '618418', 'cif_adjudicatario', 'ZZTESTB2', 'importe_sin_iva', 200, 'resultado_code', '9'))))));
  assert (r ->> 'aplicadas')::int = 1 and (r ->> 'adj_borradas')::int = 2 and (r ->> 'adj_insertadas')::int = 2,
     format('E republicación: %s', r);
  assert (select array_agg(lote order by lote) from public.adjudicaciones where licitacion_id = id1) = array['618417','618418'],
     'E republicación: solo los lotes nuevos, sin huérfanas';
  assert (select bool_and(atom_updated = '2026-05-08T10:05:19+02:00'::timestamptz) from public.adjudicaciones
           where licitacion_id = id1), 'E: la marca de las adjudicaciones sale de la licitación escrita';
  assert (select estado_expediente from public.licitaciones where licitacion_id = id1) = 'RES', 'E: estado RES';

  -- F) TODO IGUAL (misma marca, mismo snapshot, misma huella) -> no se toca nada.
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'atom_updated', '2026-05-08T10:05:19+02:00', 'version_snapshot', '2026-09-23T09:27:41+02:00',
         'version_huella', 'h2b', 'estado_expediente', 'RES', 'fecha_fin_plazo', '2026-05-21'))));
  assert (r ->> 'rechazadas')::int = 1 and (r ->> 'adj_borradas')::int = 0, format('F idéntica: %s', r);
  assert (select count(*) from public.adjudicaciones where licitacion_id = id1) = 2, 'F: adjudicaciones intactas';

  -- G) VERSIÓN NUEVA SIN ADJUDICACIONES (anulada) -> se borran las filas y se limpia el
  --    agregado de desiertas que dependía de ellas.
  update public.licitaciones set estado_adjudicacion = 'adjudicada', n_lotes_desiertos = 0 where licitacion_id = id1;
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'atom_updated', '2026-09-10T10:00:00+02:00', 'version_snapshot', '2026-09-11T03:00:00+02:00',
         'version_huella', 'h3', 'estado_expediente', 'ANUL', 'fecha_fin_plazo', '2026-05-21'))));
  assert (r ->> 'aplicadas')::int = 1 and (r ->> 'adj_borradas')::int = 2 and (r ->> 'sin_adjudicacion_recalculadas')::int = 1,
     format('G anulada: %s', r);
  assert not exists (select 1 from public.adjudicaciones where licitacion_id = id1), 'G: sin filas de adjudicación';
  select * into f from public.licitaciones where licitacion_id = id1;
  assert f.estado_adjudicacion is null and f.n_lotes_desiertos is null, 'G: estado_adjudicacion y n_lotes_desiertos a NULL';

  -- H) BAJAS sobre id1 (su versión guardada es la del 10/09 10:00).
  --    H1: baja con la MISMA hora que la versión -> no es posterior: no marca.
  r := public.carga_versiones(jsonb_build_object('bajas', jsonb_build_array(
         jsonb_build_object('licitacion_id', id1, 'baja_en', '2026-09-10T10:00:00+02:00', 'baja_tipo', 'ANULADA'))));
  assert (r ->> 'bajas_marcadas')::int = 0, format('H1 baja igual a la versión: %s', r);
  --    H2: baja anterior -> no marca.
  r := public.carga_versiones(jsonb_build_object('bajas', jsonb_build_array(
         jsonb_build_object('licitacion_id', id1, 'baja_en', '2026-09-01T10:00:00+02:00', 'baja_tipo', 'ANULADA'))));
  assert (r ->> 'bajas_marcadas')::int = 0, format('H2 baja anterior: %s', r);
  --    H3: baja posterior -> marca.
  r := public.carga_versiones(jsonb_build_object('bajas', jsonb_build_array(
         jsonb_build_object('licitacion_id', id1, 'baja_en', '2026-09-10T12:05:00+02:00', 'baja_tipo', 'ANULADA'))));
  assert (r ->> 'bajas_marcadas')::int = 1, format('H3 baja posterior: %s', r);
  select * into f from public.licitaciones where licitacion_id = id1;
  assert f.baja_en = '2026-09-10T12:05:00+02:00'::timestamptz and f.baja_tipo = 'ANULADA', 'H3: baja marcada';
  --    H4: otra baja posterior a la versión pero MÁS VIEJA que la marcada -> no la pisa.
  r := public.carga_versiones(jsonb_build_object('bajas', jsonb_build_array(
         jsonb_build_object('licitacion_id', id1, 'baja_en', '2026-09-10T11:00:00+02:00', 'baja_tipo', 'CERRADA'))));
  assert (r ->> 'bajas_marcadas')::int = 0
     and (select baja_en from public.licitaciones where licitacion_id = id1) = '2026-09-10T12:05:00+02:00'::timestamptz,
     format('H4 baja más vieja que la marcada: %s', r);
  --    H5: versión INTERMEDIA (más nueva que la guardada, anterior a la baja) -> se escribe
  --        y CONSERVA la baja.
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'atom_updated', '2026-09-10T11:30:00+02:00', 'version_snapshot', '2026-09-11T03:00:00+02:00',
         'version_huella', 'h3b', 'estado_expediente', 'ANUL', 'fecha_fin_plazo', '2026-05-21'))));
  select * into f from public.licitaciones where licitacion_id = id1;
  assert (r ->> 'aplicadas')::int = 1 and f.baja_en = '2026-09-10T12:05:00+02:00'::timestamptz and f.baja_tipo = 'ANULADA',
     format('H5 versión intermedia conserva la baja: %s / %s', r, row_to_json(f));
  --    H6: baja SIN tipo y más reciente -> marca, con baja_tipo NULL.
  r := public.carga_versiones(jsonb_build_object('bajas', jsonb_build_array(
         jsonb_build_object('licitacion_id', id1, 'baja_en', '2026-09-10T13:00:00+02:00'))));
  select * into f from public.licitaciones where licitacion_id = id1;
  assert (r ->> 'bajas_marcadas')::int = 1 and f.baja_en = '2026-09-10T13:00:00+02:00'::timestamptz and f.baja_tipo is null,
     format('H6 baja sin tipo: %s', r);
  --    H7: versión POSTERIOR a la baja (republicación) -> la desmarca.
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'atom_updated', '2026-10-07T09:00:00+02:00', 'version_snapshot', '2026-10-08T03:00:00+02:00',
         'version_huella', 'h4', 'estado_expediente', 'PUB', 'fecha_fin_plazo', '2026-10-30'))));
  select * into f from public.licitaciones where licitacion_id = id1;
  assert f.baja_en is null and f.baja_tipo is null and f.estado_expediente = 'PUB', 'H7: la republicación desmarca la baja';
  --    H8: baja de un id que no está en la base -> no crea nada y se cuenta.
  r := public.carga_versiones(jsonb_build_object('bajas', jsonb_build_array(
         jsonb_build_object('licitacion_id', id9, 'baja_en', '2026-10-15T00:00:47+02:00', 'baja_tipo', 'CERRADA'))));
  assert (r ->> 'bajas_sin_fila')::int = 1 and not exists (select 1 from public.licitaciones where licitacion_id = id9),
     format('H8 baja de un id inexistente: %s', r);
  --    H9: baja sobre una fila de HOY (sin versión) -> se marca; luego la reparación escribe
  --        una versión ANTERIOR a la baja (se conserva) y después una POSTERIOR (se limpia).
  insert into public.licitaciones (licitacion_id, titulo, fuente) values (id4, 'legado con baja', 'estatal');
  r := public.carga_versiones(jsonb_build_object('bajas', jsonb_build_array(
         jsonb_build_object('licitacion_id', id4, 'baja_en', '2026-10-02T12:05:00+02:00', 'baja_tipo', 'ANULADA'))));
  assert (r ->> 'bajas_marcadas')::int = 1, format('H9 baja sobre fila sin versión: %s', r);
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'licitacion_id', id4, 'atom_updated', '2026-10-02T11:24:55+02:00', 'version_snapshot', '2026-10-03T03:00:00+02:00',
         'version_huella', 'l1', 'estado_expediente', 'ANUL'))));
  assert (select baja_en from public.licitaciones where licitacion_id = id4) = '2026-10-02T12:05:00+02:00'::timestamptz,
     'H9: la versión anterior a la baja la conserva';
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'licitacion_id', id4, 'atom_updated', '2026-10-07T09:00:00+02:00', 'version_snapshot', '2026-10-08T03:00:00+02:00',
         'version_huella', 'l2', 'estado_expediente', 'PUB'))));
  assert (select baja_en from public.licitaciones where licitacion_id = id4) is null, 'H9: la versión posterior la limpia';

  -- I) EL MISMO ID DOS VECES EN UN LOTE (la nueva PRIMERO) -> sin error, se queda la más
  --    nueva CON SUS adjudicaciones (las de la vieja no entran).
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(
         base || jsonb_build_object('licitacion_id', id2, 'atom_updated', '2026-10-05T10:00:00+02:00',
                                    'version_snapshot', '2026-10-06T03:00:00+02:00', 'version_huella', 'b', 'presupuesto_sin_iva', 2,
                                    'adjudicaciones', jsonb_build_array(jsonb_build_object('lote', '1', 'cif_adjudicatario', 'ZZTESTNUEVO', 'importe_sin_iva', 443))),
         base || jsonb_build_object('licitacion_id', id2, 'atom_updated', '2026-10-01T10:00:00+02:00',
                                    'version_snapshot', '2026-10-02T03:00:00+02:00', 'version_huella', 'a', 'presupuesto_sin_iva', 1,
                                    'adjudicaciones', jsonb_build_array(jsonb_build_object('lote', '1', 'cif_adjudicatario', 'ZZTESTVIEJO', 'importe_sin_iva', 418))))));
  assert (r ->> 'recibidas')::int = 2 and (r ->> 'duplicadas')::int = 1 and (r ->> 'aplicadas')::int = 1, format('I duplicado: %s', r);
  assert (select presupuesto_sin_iva from public.licitaciones where licitacion_id = id2) = 2, 'I: queda la del 05/10';
  assert (select array_agg(cif_adjudicatario) from public.adjudicaciones where licitacion_id = id2) = array['ZZTESTNUEVO'],
     'I: solo las adjudicaciones de la versión elegida';

  -- J) DESCARTES: sin lista "adjudicaciones", sin marca, sin snapshot o sin huella -> no se
  --    escribe NADA de ellas (ni se borran sus adjudicaciones) y se cuentan.
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(
         (base - 'adjudicaciones') || jsonb_build_object('licitacion_id', id2, 'atom_updated', '2026-10-09T10:00:00+02:00',
                                    'version_snapshot', '2026-10-10T03:00:00+02:00', 'version_huella', 'c'),
         base || jsonb_build_object('licitacion_id', id2, 'version_snapshot', '2026-10-10T03:00:00+02:00', 'version_huella', 'c'),
         base || jsonb_build_object('licitacion_id', id2, 'atom_updated', '2026-10-09T10:00:00+02:00', 'version_huella', 'c'),
         base || jsonb_build_object('licitacion_id', id2, 'atom_updated', '2026-10-09T10:00:00+02:00',
                                    'version_snapshot', '2026-10-10T03:00:00+02:00'))));
  assert (r ->> 'descartadas')::int = 4 and (r ->> 'aplicadas')::int = 0, format('J descartes: %s', r);
  assert (select count(*) from public.adjudicaciones where licitacion_id = id2) = 1
     and (select presupuesto_sin_iva from public.licitaciones where licitacion_id = id2) = 2,
     'J: nada cambia en lo descartado';
  --    El formato viejo (adjudicaciones o con_adjudicaciones de primer nivel) da ERROR.
  begin
    perform public.carga_versiones(jsonb_build_object('con_adjudicaciones', false));
    raise exception 'J: con_adjudicaciones de primer nivel tenía que fallar';
  exception when raise_exception then
    if sqlerrm not like 'carga_versiones:%' then raise; end if;
  end;
  begin
    perform public.carga_versiones(jsonb_build_object('adjudicaciones', '[]'::jsonb));
    raise exception 'J: adjudicaciones de primer nivel tenía que fallar';
  exception when raise_exception then
    if sqlerrm not like 'carga_versiones:%' then raise; end if;
  end;

  -- K) FILA DE HOY SIN VERSIÓN (atom_updated NULL): acepta la primera versión que llegue.
  insert into public.licitaciones (licitacion_id, titulo, fuente, presupuesto_sin_iva) values (id3, 'legado', 'estatal', 1);
  r := public.carga_versiones(jsonb_build_object('licitaciones', jsonb_build_array(base || jsonb_build_object(
         'licitacion_id', id3, 'atom_updated', '2024-01-10T10:00:00+01:00', 'version_snapshot', '2024-01-11T03:00:00+01:00',
         'version_huella', 'k', 'presupuesto_sin_iva', 7))));
  assert (r ->> 'aplicadas')::int = 1, format('K legado: %s', r);
  assert (select presupuesto_sin_iva from public.licitaciones where licitacion_id = id3) = 7, 'K: escrita';

  -- L) PERMISOS: solo service_role puede ejecutarla.
  assert not has_function_privilege('anon', 'public.carga_versiones(jsonb)', 'execute'), 'L: anon NO debe poder';
  assert not has_function_privilege('authenticated', 'public.carga_versiones(jsonb)', 'execute'), 'L: authenticated NO debe poder';
  assert has_function_privilege('service_role', 'public.carga_versiones(jsonb)', 'execute'), 'L: service_role SÍ';

  raise notice 'PRUEBA VERSIONES OK — alta, vieja, nueva, empates (snapshot anterior / igual con otra huella / posterior / idéntico), anulada sin filas, 9 casos de bajas, duplicado con adjudicaciones, descartes, formato viejo rechazado, legado y permisos.';

  -- Forzar rollback limpio: nada de lo escrito persiste.
  raise exception using errcode = 'ZZ999', message = 'ROLLBACK_LIMPIO_PRUEBA_VERSIONES';
exception
  when sqlstate 'ZZ999' then
    raise notice 'Datos de prueba revertidos (no se ha persistido nada). ✔';
end $$;
