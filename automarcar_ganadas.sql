-- ============================================================================
-- Fase D2 · AUTO-MARCAR GANADAS por CIF (competencia LODEPA)
--
-- Crea la función public.automarcar_ganadas_lodepa(cifs, simular). Cruza
-- public.adjudicaciones (poblada en D1) con public.decisiones: para cada
-- expediente (licitacion_id) con ALGUNA adjudicación a un CIF de LODEPA, marca su
-- decisión como estado='ganada' según esta regla (cerrada con Alejandro 07/07/2026):
--   · sin fila en decisiones            -> INSERT con estado='ganada' (favorita=false)
--   · estado null / '' / 'activa'       -> UPDATE a 'ganada'
--   · estado 'presentada'               -> UPDATE a 'ganada'  (transición natural)
--   · estado 'ganada'                   -> no-op
--   · 'perdida' / 'descartada' / otro   -> NO se toca; se reporta como DISCREPANCIA
-- NO toca favorita ni ningún otro campo de decisiones, ni la tabla contratos.
--
-- IDEMPOTENTE: en la 2ª pasada el UPDATE ya no casa (excluye 'ganada') y el INSERT
-- no encuentra filas nuevas -> 0 cambios. Puede correr a diario sin efectos.
--
-- DEVUELVE un jsonb con el informe (lo imprime backfill_catalogo.py):
--   { "simular": bool,
--     "n_marcadas": int,  "marcadas": [ {licitacion_id, accion, estado_anterior,
--                                        adjudicatario, importe_sin_iva}, ... ],
--     "ya_ganadas": int,
--     "n_discrepancias": int, "discrepancias": [ {licitacion_id, estado_actual,
--                                        adjudicatario, importe_sin_iva}, ... ] }
-- Con simular=true NO escribe: devuelve el MISMO informe de lo que HARÍA.
--
-- CÓMO USARLO: pega TODO en el SQL Editor de Supabase y pulsa Run (idempotente,
-- create or replace). La lista de CIF de LODEPA vive en el pipeline
-- (backfill_catalogo.CIFS_LODEPA), no aquí: la función recibe los CIF por parámetro.
--   Previsualizar (no escribe):  select public.automarcar_ganadas_lodepa(array['B86833753'], true);
--   Aplicar:                     select public.automarcar_ganadas_lodepa(array['B86833753']);
--
-- SECURITY DEFINER: corre con los privilegios del propietario para ESCRIBIR en
-- public.decisiones saltando su RLS de forma controlada (igual que purga_catalogo lo
-- hace para leer/borrar). GRACIAS A ESTO el rol del pipeline (service_role) NO necesita
-- GRANT de escritura directo sobre decisiones: solo 'execute' sobre esta función.
-- search_path fijo por seguridad. Solo service_role puede ejecutarla.
-- ============================================================================

-- El cruce filtra adjudicaciones por cif_adjudicatario: el índice
-- adjudicaciones_cif_idx (adjudicaciones_schema.sql) ya lo cubre, así que el
-- candidato (un puñado de expedientes) sale sin recorrer la tabla entera.

create or replace function public.automarcar_ganadas_lodepa(
  cifs    text[],
  simular boolean default false
)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_catalog
as $$
declare
  resultado jsonb;
begin
  -- Un statement único: clasifica cada candidato y, si no es simulación, aplica el
  -- INSERT/UPDATE. Todas las sub-consultas de un WITH ven el MISMO snapshot, así que
  -- la clasificación (clasif) y lo aplicado (ins/upd) son coherentes; los data-CTE
  -- ins/upd se ejecutan SIEMPRE (regla de Postgres) aunque el SELECT no los lea.
  with cand as materialized (
    -- Un licitacion_id por expediente ganado por LODEPA. adjudicatario/importe = los
    -- del lote de MAYOR importe (solo para el informe; da igual para el cruce).
    select distinct on (a.licitacion_id)
           a.licitacion_id,
           a.adjudicatario,
           a.importe_sin_iva
    from public.adjudicaciones a
    where a.cif_adjudicatario = any(cifs)
      and a.cif_adjudicatario <> ''            -- '' = lote desierto; nunca casa un CIF real
    order by a.licitacion_id, a.importe_sin_iva desc nulls last
  ),
  clasif as materialized (
    select c.licitacion_id, c.adjudicatario, c.importe_sin_iva,
           d.estado as estado_actual,
           case
             when d.licitacion_id is null                                     then 'insert'
             when d.estado is null or d.estado in ('', 'activa', 'presentada') then 'update'
             when d.estado = 'ganada'                                         then 'ya_ganada'
             else 'discrepancia'
           end as accion
    from cand c
    left join public.decisiones d on d.licitacion_id = c.licitacion_id
  ),
  ins as (
    -- Sin fila previa -> alta como 'ganada'. favorita=false (no la ponemos "en
    -- observación"; es un dato nuevo, no había favorita que preservar). El
    -- 'do nothing' es cinturón anti-carrera (si el front insertó entre el snapshot
    -- y aquí): esa quedaría para el siguiente cruce, no se pisa.
    insert into public.decisiones (licitacion_id, estado, favorita, updated_at)
    select licitacion_id, 'ganada', false, now()
    from clasif
    where accion = 'insert' and not simular
    on conflict (licitacion_id) do nothing
    returning 1
  ),
  upd as (
    -- Fila pisable (null/''/activa/presentada) -> a 'ganada'. NO toca favorita.
    update public.decisiones d
    set estado = 'ganada', updated_at = now()
    from clasif x
    where d.licitacion_id = x.licitacion_id
      and x.accion = 'update' and not simular
    returning 1
  )
  select jsonb_build_object(
    'simular', simular,
    'n_marcadas', count(*) filter (where accion in ('insert', 'update')),
    'marcadas', coalesce(jsonb_agg(
        jsonb_build_object(
          'licitacion_id',   licitacion_id,
          'accion',          accion,
          'estado_anterior', estado_actual,
          'adjudicatario',   adjudicatario,
          'importe_sin_iva', importe_sin_iva)
        order by importe_sin_iva desc nulls last)
        filter (where accion in ('insert', 'update')), '[]'::jsonb),
    'ya_ganadas', count(*) filter (where accion = 'ya_ganada'),
    'n_discrepancias', count(*) filter (where accion = 'discrepancia'),
    'discrepancias', coalesce(jsonb_agg(
        jsonb_build_object(
          'licitacion_id',  licitacion_id,
          'estado_actual',  estado_actual,
          'adjudicatario',  adjudicatario,
          'importe_sin_iva', importe_sin_iva))
        filter (where accion = 'discrepancia'), '[]'::jsonb)
  ) into resultado
  from clasif;

  return resultado;
end;
$$;

-- Solo el backend (service_role) puede ejecutarla. Nada para anon/public.
revoke all on function public.automarcar_ganadas_lodepa(text[], boolean) from public, anon;
grant execute on function public.automarcar_ganadas_lodepa(text[], boolean) to service_role;

-- ----------------------------------------------------------------------------
-- NOTAS (léelas antes de activar):
--   · La lista de CIF de LODEPA NO se hardcodea aquí: la pasa el pipeline por
--     parámetro (backfill_catalogo.CIFS_LODEPA, único sitio documentado). Así crece
--     sin tocar SQL (otras razones sociales o UTEs con CIF propio).
--   · Contratos ganados en UTE llevan el CIF de la UTE, no el de LODEPA -> no se
--     auto-marcan hasta añadir ese CIF a CIFS_LODEPA (limitación conocida, fase E).
--   · Depende de que existan public.adjudicaciones (D1) y public.decisiones. La
--     función es aditiva: no crea ni altera columnas de decisiones.
--   · Para verificarla sin tocar datos reales: prueba_automarcar.sql (transacción
--     con auto-rollback; cubre las 5 ramas de la regla + idempotencia).
-- ----------------------------------------------------------------------------
