-- ============================================================================
-- PRUEBA de public.automarcar_ganadas_lodepa (Fase D2) — SIN TOCAR DATOS REALES.
--
-- Es un ÚNICO bloque DO: siembra filas SINTÉTICAS en public.adjudicaciones y
-- public.decisiones (CIF y licitacion_id de prueba, prefijo 'ZZTEST'), ejecuta la
-- función, comprueba las 5 ramas de la regla + idempotencia + que no toca favorita
-- ni las discrepancias, y AL FINAL FUERZA UN ROLLBACK: no persiste NADA.
--   · Si todo va bien: NOTICE "PRUEBA D2 OK ..." y la transacción se revierte sola.
--   · Si una aserción falla: la excepción se propaga (verás el mensaje) y también
--     se revierte todo. En ningún caso quedan filas de prueba.
--
-- Requisito: haber ejecutado antes automarcar_ganadas.sql (crea la función).
-- CÓMO USARLO: pega TODO en el SQL Editor de Supabase y pulsa Run. Es seguro
-- re-ejecutarlo (no deja rastro). Usa un CIF ('ZZTESTLODEPA0') que NO es de nadie.
-- ============================================================================
do $$
declare
  cifs_test text[] := array['ZZTESTLODEPA0'];
  r1 jsonb;
  r2 jsonb;
begin
  -- --- Semilla: 5 expedientes, uno por rama de la regla ---------------------
  -- Todos adjudicados al CIF de prueba (una fila en adjudicaciones cada uno).
  insert into public.adjudicaciones (licitacion_id, lote, cif_adjudicatario, adjudicatario, importe_sin_iva) values
    ('ZZTEST-sinfila',    '', 'ZZTESTLODEPA0', 'LODEPA (prueba)', 100),
    ('ZZTEST-activa',     '', 'ZZTESTLODEPA0', 'LODEPA (prueba)', 200),
    ('ZZTEST-presentada', '', 'ZZTESTLODEPA0', 'LODEPA (prueba)', 300),
    ('ZZTEST-perdida',    '', 'ZZTESTLODEPA0', 'LODEPA (prueba)', 400),
    ('ZZTEST-yaganada',   '', 'ZZTESTLODEPA0', 'LODEPA (prueba)', 500),
    -- lote DESIERTO del mismo expediente 'activa' (cif=''): NO debe contar como candidato
    ('ZZTEST-activa',   'X9', '',              null,             null);

  -- Estado de partida en decisiones (a 'sinfila' NO le ponemos fila, a propósito).
  -- updated_at explícito por si la columna fuese NOT NULL sin default (el front lo escribe).
  insert into public.decisiones (licitacion_id, estado, favorita, updated_at) values
    ('ZZTEST-activa',     'activa',     true,  now()),   -- favorita true: debe conservarse
    ('ZZTEST-presentada', 'presentada', false, now()),
    ('ZZTEST-perdida',    'perdida',    false, now()),   -- manual: NO se toca -> discrepancia
    ('ZZTEST-yaganada',   'ganada',     true,  now());   -- ya ganada: no-op

  -- --- 1ª PASADA (aplica) ---------------------------------------------------
  r1 := public.automarcar_ganadas_lodepa(cifs_test, false);

  -- Recuentos del informe
  assert (r1->>'n_marcadas')::int = 3,
    format('esperaba 3 marcadas (sinfila+activa+presentada), informe: %s', r1);
  assert (r1->>'ya_ganadas')::int = 1,
    format('esperaba 1 ya_ganada, informe: %s', r1);
  assert (r1->>'n_discrepancias')::int = 1,
    format('esperaba 1 discrepancia (perdida), informe: %s', r1);

  -- Estado REAL de decisiones tras aplicar (rama por rama)
  assert (select estado from public.decisiones where licitacion_id = 'ZZTEST-sinfila') = 'ganada',
    'rama sin-fila: debía INSERTARse como ganada';
  assert (select favorita from public.decisiones where licitacion_id = 'ZZTEST-sinfila') = false,
    'rama sin-fila: favorita del alta debe ser false';
  assert (select estado from public.decisiones where licitacion_id = 'ZZTEST-activa') = 'ganada',
    'rama activa: debía pasar a ganada';
  assert (select favorita from public.decisiones where licitacion_id = 'ZZTEST-activa') = true,
    'rama activa: favorita=true debía CONSERVARSE (no la tocamos)';
  assert (select estado from public.decisiones where licitacion_id = 'ZZTEST-presentada') = 'ganada',
    'rama presentada: debía pasar a ganada (transición natural)';
  assert (select estado from public.decisiones where licitacion_id = 'ZZTEST-perdida') = 'perdida',
    'rama perdida: NO se toca (discrepancia)';
  assert (select estado from public.decisiones where licitacion_id = 'ZZTEST-yaganada') = 'ganada',
    'rama ya-ganada: sigue ganada (no-op)';

  -- La discrepancia sale listada en el informe
  assert r1->'discrepancias'->0->>'licitacion_id' = 'ZZTEST-perdida',
    format('la discrepancia debía ser ZZTEST-perdida, informe: %s', r1);

  -- El lote desierto del expediente 'activa' no debe duplicar candidato (sigue 1 fila)
  assert (select count(*) from public.decisiones where licitacion_id = 'ZZTEST-activa') = 1,
    'el lote desierto (cif='''') no debe crear candidato ni fila extra';

  -- --- 2ª PASADA (idempotencia): 0 cambios ----------------------------------
  r2 := public.automarcar_ganadas_lodepa(cifs_test, false);
  assert (r2->>'n_marcadas')::int = 0,
    format('idempotencia: 2ª pasada debía marcar 0, informe: %s', r2);
  assert (r2->>'ya_ganadas')::int = 4,
    format('idempotencia: las 4 (sinfila+activa+presentada+yaganada) ya son ganadas, informe: %s', r2);
  assert (r2->>'n_discrepancias')::int = 1,
    format('idempotencia: la discrepancia (perdida) sigue ahí, informe: %s', r2);

  -- --- SIMULAR no escribe ---------------------------------------------------
  -- (sobre un CIF de prueba distinto sin candidatos reales: solo comprobamos que
  --  simular=true sobre 'perdida' seguiría sin tocarla — ya es discrepancia).
  perform public.automarcar_ganadas_lodepa(cifs_test, true);
  assert (select estado from public.decisiones where licitacion_id = 'ZZTEST-perdida') = 'perdida',
    'simular no debe cambiar nada';

  raise notice 'PRUEBA D2 OK — 5 ramas + idempotencia + favorita preservada + discrepancia intacta.';
  raise notice 'Informe 1ª pasada: %', r1;

  -- Forzar rollback limpio: nada de lo sembrado/modificado persiste.
  raise exception using errcode = 'ZZ999', message = 'ROLLBACK_LIMPIO_PRUEBA_D2';
exception
  when sqlstate 'ZZ999' then
    raise notice 'Datos de prueba revertidos (no se ha persistido nada). ✔';
  -- Cualquier otro error (p. ej. una aserción fallida) se propaga: lo verás y
  -- también revierte todo lo del bloque (mismo savepoint).
end $$;
