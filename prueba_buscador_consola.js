// prueba_buscador_consola.js  ·  PASO 5 de BG-3 (verificación en el navegador)
// ============================================================================
// CÓMO USARLO:
//   1) Abre la web del Radar e INICIA SESIÓN (la RLS exige rol authenticated;
//      sin login no devuelve filas, eso es lo correcto).
//   2) Asegúrate de haber creado la RPC: pega buscador_rpc.sql en el SQL Editor.
//   3) Abre la consola del navegador (F12 -> Console).
//   4) Pega TODO este archivo y pulsa Enter. Imprime las 5 pruebas.
//
// NOTA: este snippet crea un cliente supabase TEMPORAL solo para la prueba. NO
// es un "segundo cliente" del producto: reutiliza la sesión ya guardada en el
// navegador (misma URL + misma publishable). El módulo real (buscador_api.js)
// NO crea cliente: recibe el de la web. La función `buscar` de aquí es una COPIA
// fiel de buscador_api.js solo para poder probar sin desplegar nada.
// ============================================================================
(async () => {
  const { createClient } = await import('https://esm.sh/@supabase/supabase-js@2');
  const SUPABASE_URL = 'https://uzktrhpgkyctlnqgdsys.supabase.co';
  const SUPABASE_KEY = 'sb_publishable_3J3pFbMlNzu-NUDs1-740g_lu8YsRv_';
  const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

  const { data: { session } } = await supabase.auth.getSession();
  if (!session) {
    console.error('⛔ No hay sesión. Inicia sesión en la web ANTES de correr la prueba (la RLS bloquea a anon).');
    return;
  }
  console.log('✅ Sesión activa como:', session.user?.email || session.user?.id);

  // ---- COPIA fiel de buscador_api.js (solo para la prueba) -------------------
  const COLUMNAS = [
    'licitacion_id', 'titulo', 'objeto', 'organo_contratacion', 'cpv', 'fuente',
    'presupuesto_con_iva', 'presupuesto_sin_iva', 'valor_estimado',
    'fecha_publicacion', 'fecha_fin_plazo', 'lugar_ejecucion', 'ccaa', 'enlace',
  ].join(',');
  const POR_PAGINA_DEF = 25;
  const UMBRAL_COUNT_EXACTO_DEF = 10000;
  const ORDEN_PERMITIDO = new Set(['fecha_fin_plazo', 'valor_estimado', 'fecha_publicacion']);
  const MODOS_COUNT = new Set(['exact', 'estimated', 'planned']);
  const ESTADOS = new Set(['abierta', 'cerrada', 'todas']);
  const aNumero = (v) => { if (v === '' || v == null) return null; const n = Number(v); return Number.isFinite(n) ? n : null; };
  const aISO = (v) => { if (v === '' || v == null) return null; const d = v instanceof Date ? v : new Date(v); return Number.isNaN(d.getTime()) ? null : d.toISOString(); };
  const aTexto = (v) => { if (typeof v !== 'string') return null; const t = v.trim(); return t.length ? t : null; };

  function crearBuscador(supabase) {
    async function buscar(params = {}) {
      const nPorPagina = aNumero(params.porPagina);
      const porPagina = nPorPagina && nPorPagina > 0 ? Math.floor(nPorPagina) : POR_PAGINA_DEF;
      const nPagina = aNumero(params.pagina);
      const pagina = nPagina && nPagina >= 1 ? Math.floor(nPagina) : 1;
      const desde = (pagina - 1) * porPagina;
      const hasta = desde + porPagina - 1;
      const ok = (data, total, aproximado) => ({ filas: data ?? [], total, pagina, porPagina, aproximado, error: null });
      const fallo = (error) => ({ filas: [], total: 0, pagina, porPagina, aproximado: false, error });

      const texto = aTexto(params.texto);
      const cpvs = Array.isArray(params.cpv) ? params.cpv.map(aTexto).filter(Boolean) : [];
      const fuente = params.fuente === 'estatal' || params.fuente === 'agregadas' ? params.fuente : null;
      const impMin = aNumero(params.importeMin); const impMax = aNumero(params.importeMax);
      const finDesde = aISO(params.fechaFinDesde); const finHasta = aISO(params.fechaFinHasta);
      const pubDesde = aISO(params.fechaPubDesde); const pubHasta = aISO(params.fechaPubHasta);
      const hayOtrosFiltros = !!(texto || cpvs.length || fuente || impMin !== null || impMax !== null || finDesde || finHasta || pubDesde || pubHasta);
      const estado = ESTADOS.has(params.estado) ? params.estado : (hayOtrosFiltros ? 'todas' : 'abierta');
      const ordenCampo = ORDEN_PERMITIDO.has(params.ordenCampo) ? params.ordenCampo : 'fecha_fin_plazo';
      const ordenAsc = params.ordenAsc !== undefined ? !!params.ordenAsc : true;

      // CAMINO 1 · CON TEXTO -> RPC.
      if (texto) {
        const { data, error } = await supabase.rpc('buscar_licitaciones', {
          p_texto: texto, p_cpv: cpvs.length ? cpvs : null, p_fuente: fuente,
          p_importe_min: impMin, p_importe_max: impMax, p_estado: estado,
          p_fin_desde: finDesde, p_fin_hasta: finHasta, p_pub_desde: pubDesde, p_pub_hasta: pubHasta,
          p_orden_campo: ordenCampo, p_orden_asc: ordenAsc, p_pagina: pagina, p_por_pagina: porPagina,
        });
        if (error) return fallo(error);
        return { filas: (data && data.filas) ?? [], total: (data && data.total) ?? 0, pagina, porPagina, aproximado: !!(data && data.aproximado), error: null };
      }

      // CAMINO 2 · SIN TEXTO -> PostgREST + conteo híbrido.
      const ahora = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
      const aplicarFiltros = (q) => {
        if (cpvs.length) q = q.overlaps('cpv', cpvs);
        if (fuente) q = q.eq('fuente', fuente);
        if (impMin !== null) q = q.gte('valor_estimado', impMin);
        if (impMax !== null) q = q.lte('valor_estimado', impMax);
        if (finDesde) q = q.gte('fecha_fin_plazo', finDesde);
        if (finHasta) q = q.lte('fecha_fin_plazo', finHasta);
        if (pubDesde) q = q.gte('fecha_publicacion', pubDesde);
        if (pubHasta) q = q.lte('fecha_publicacion', pubHasta);
        if (estado === 'abierta') q = q.or(`fecha_fin_plazo.gte.${ahora},fecha_fin_plazo.is.null`);
        else if (estado === 'cerrada') q = q.lt('fecha_fin_plazo', ahora);
        return q;
      };
      const construirDatos = () => {
        let q = aplicarFiltros(supabase.from('licitaciones').select(COLUMNAS));
        q = q.order(ordenCampo, { ascending: ordenAsc, nullsFirst: false });
        q = q.order('licitacion_id', { ascending: true }); // desempate ESTABLE
        return q.range(desde, hasta);
      };
      const contar = (modo) => aplicarFiltros(supabase.from('licitaciones').select('licitacion_id', { count: modo, head: true }));

      const modoForzado = MODOS_COUNT.has(params.modoCount) ? params.modoCount : null;
      if (modoForzado) {
        const [rDatos, rCount] = await Promise.all([construirDatos(), contar(modoForzado)]);
        if (rDatos.error) return fallo(rDatos.error);
        return ok(rDatos.data, rCount.error ? 0 : (rCount.count ?? 0), modoForzado !== 'exact');
      }
      const [rDatos, rEstim] = await Promise.all([construirDatos(), contar('planned')]);
      if (rDatos.error) return fallo(rDatos.error);
      const estimado = rEstim.error ? null : (rEstim.count ?? 0);
      const nUmbral = aNumero(params.umbralCountExacto);
      const umbralExacto = nUmbral && nUmbral > 0 ? Math.floor(nUmbral) : UMBRAL_COUNT_EXACTO_DEF;
      if (estimado !== null && estimado < umbralExacto) {
        const rExacto = await contar('exact');
        if (!rExacto.error && rExacto.count != null) return ok(rDatos.data, rExacto.count, false);
        return ok(rDatos.data, estimado, true);
      }
      return ok(rDatos.data, estimado ?? 0, true);
    }
    async function comprobarPoblacion() {
      const salida = {};
      for (const col of ['ccaa', 'lugar_ejecucion']) {
        const { count, error } = await supabase.from('licitaciones').select('licitacion_id', { count: 'exact', head: true }).not(col, 'is', null);
        salida[col] = error ? `error: ${error.message}` : count;
      }
      return salida;
    }
    return { buscar, comprobarPoblacion };
  }
  // ---------------------------------------------------------------------------

  const { buscar, comprobarPoblacion } = crearBuscador(supabase);
  const ms = (t0) => `${Math.round(performance.now() - t0)}ms`;
  const resumen = (etq, r, t0) => console.log(
    `${etq} -> filas=${r.filas.length} total=${r.total}${r.aproximado ? ' (≈ estimado)' : ' (exacto)'} ` +
    `pag=${r.pagina}/${Math.ceil(r.total / r.porPagina) || 1} en ${ms(t0)}` + (r.error ? ` ERROR=${r.error.message}` : ''));

  console.log('\n──────── PRUEBA 1: texto=aire, estado=abierta, importeMax=200000 (RPC) ────────');
  let t = performance.now();
  const p1 = await buscar({ texto: 'aire', estado: 'abierta', importeMax: 200000 });
  resumen('P1', p1, t);
  console.log('→ vía RPC, EXACTO. Cuadra con p1_total del SQL.');

  console.log('\n──────── PRUEBA 2: paginación estable (pág 1 vs pág 2 NO se solapan) ────────');
  t = performance.now();
  const p2a = await buscar({ texto: 'aire', estado: 'abierta', importeMax: 200000, pagina: 1, porPagina: 5 });
  const p2b = await buscar({ texto: 'aire', estado: 'abierta', importeMax: 200000, pagina: 2, porPagina: 5 });
  const ids1 = p2a.filas.map((f) => f.licitacion_id), ids2 = p2b.filas.map((f) => f.licitacion_id);
  const solapan = ids1.filter((id) => ids2.includes(id));
  resumen('P2 pág1', p2a, t); resumen('P2 pág2', p2b, t);
  console.log('IDs pág1:', ids1, '\nIDs pág2:', ids2, '\n¿Solapan?', solapan.length ? '❌ ' + solapan : '✅ no (paginación estable)');

  console.log('\n──────── PRUEBA 3: tildes (climatizacion == climatización), antes daba TIMEOUT ────────');
  t = performance.now();
  const p3a = await buscar({ texto: 'climatizacion' });
  const p3b = await buscar({ texto: 'climatización' });
  resumen('P3 sin tilde', p3a, t); resumen('P3 con tilde', p3b, t);
  console.log(!p3a.error && !p3b.error && p3a.total === p3b.total ? '✅ vía RPC: sin timeout y ambas cuadran (unaccent en la RPC)' : '⚠️ revisar (errores o totales distintos)');

  console.log('\n──────── PRUEBA 4: CPV 90920000 (overlaps, SIN texto -> PostgREST) ────────');
  t = performance.now();
  const p4 = await buscar({ cpv: ['90920000'] });
  resumen('P4', p4, t);
  const llevan = p4.filas.every((f) => Array.isArray(f.cpv) && f.cpv.includes('90920000'));
  console.log('¿Todas las filas llevan 90920000?', p4.filas.length ? (llevan ? '✅ sí' : '❌ no') : '(0 filas)', '· EXACTO, cuadra con p4_total del SQL.');

  console.log('\n──────── PRUEBA 5: orden por valor_estimado DESC (sin filtros), antes daba TIMEOUT ────────');
  t = performance.now();
  const p5 = await buscar({ ordenCampo: 'valor_estimado', ordenAsc: false, estado: 'todas', porPagina: 10 });
  resumen('P5', p5, t);
  const vals = p5.filas.map((f) => f.valor_estimado);
  const ordenado = vals.every((v, i) => i === 0 || v == null || vals[i - 1] == null || vals[i - 1] >= v);
  console.log('valores:', vals, '\n¿Descendente?', ordenado ? '✅ sí' : '❌ no', p5.error ? '❌ ERROR ' + p5.error.message : '✅ sin timeout');

  console.log('\n──────── EXTRA: población ccaa / lugar_ejecucion (debe ser 0 → filtros desactivados) ────────');
  console.log(await comprobarPoblacion());

  console.log('\n✔️ Pruebas terminadas. Cuadra P1/P3/P4 con prueba_buscador.sql.');
})();
