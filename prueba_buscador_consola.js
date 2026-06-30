// prueba_buscador_consola.js  ·  PASO 5 de BG-3 (verificación en el navegador)
// ============================================================================
// CÓMO USARLO:
//   1) Abre la web del Radar e INICIA SESIÓN (la RLS exige rol authenticated;
//      sin login no devuelve filas, eso es lo correcto).
//   2) Abre la consola del navegador (F12 -> Console).
//   3) Pega TODO este archivo y pulsa Enter. Imprime las 5 pruebas.
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
  const ORDEN_PERMITIDO = new Set(['fecha_fin_plazo', 'valor_estimado', 'fecha_publicacion']);
  const MODOS_COUNT = new Set(['exact', 'estimated', 'planned']);
  const ESTADOS = new Set(['abierta', 'cerrada', 'todas']);
  const quitarTildes = (s) => s.normalize('NFD').replace(/\p{Diacritic}/gu, '');
  const aNumero = (v) => { if (v === '' || v == null) return null; const n = Number(v); return Number.isFinite(n) ? n : null; };
  const aISO = (v) => { if (v === '' || v == null) return null; const d = v instanceof Date ? v : new Date(v); return Number.isNaN(d.getTime()) ? null : d.toISOString(); };
  const aTexto = (v) => { if (typeof v !== 'string') return null; const t = v.trim(); return t.length ? t : null; };

  function crearBuscador(supabase) {
    async function buscar(params = {}) {
      const nPorPagina = aNumero(params.porPagina);
      const porPagina = nPorPagina && nPorPagina > 0 ? Math.floor(nPorPagina) : 25;
      const nPagina = aNumero(params.pagina);
      const pagina = nPagina && nPagina >= 1 ? Math.floor(nPagina) : 1;
      const modoCount = MODOS_COUNT.has(params.modoCount) ? params.modoCount : 'exact';
      let q = supabase.from('licitaciones').select(COLUMNAS, { count: modoCount });
      const texto = aTexto(params.texto);
      if (texto) q = q.textSearch('tsv', quitarTildes(texto), { type: 'websearch', config: 'spanish' });
      let cpvs = [];
      if (Array.isArray(params.cpv)) { cpvs = params.cpv.map(aTexto).filter(Boolean); if (cpvs.length) q = q.overlaps('cpv', cpvs); }
      const fuente = params.fuente === 'estatal' || params.fuente === 'agregadas' ? params.fuente : null;
      if (fuente) q = q.eq('fuente', fuente);
      const impMin = aNumero(params.importeMin); const impMax = aNumero(params.importeMax);
      if (impMin !== null) q = q.gte('valor_estimado', impMin);
      if (impMax !== null) q = q.lte('valor_estimado', impMax);
      const finDesde = aISO(params.fechaFinDesde); const finHasta = aISO(params.fechaFinHasta);
      if (finDesde) q = q.gte('fecha_fin_plazo', finDesde);
      if (finHasta) q = q.lte('fecha_fin_plazo', finHasta);
      const pubDesde = aISO(params.fechaPubDesde); const pubHasta = aISO(params.fechaPubHasta);
      if (pubDesde) q = q.gte('fecha_publicacion', pubDesde);
      if (pubHasta) q = q.lte('fecha_publicacion', pubHasta);
      const ccaa = aTexto(params.ccaa); if (ccaa) q = q.eq('ccaa', ccaa);
      const lugar = aTexto(params.lugar); if (lugar) q = q.ilike('lugar_ejecucion', `%${lugar}%`);
      const hayOtrosFiltros = !!(texto || cpvs.length || fuente || impMin !== null || impMax !== null || finDesde || finHasta || pubDesde || pubHasta || ccaa || lugar);
      const estado = ESTADOS.has(params.estado) ? params.estado : (hayOtrosFiltros ? 'todas' : 'abierta');
      const ahora = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
      if (estado === 'abierta') q = q.or(`fecha_fin_plazo.gte.${ahora},fecha_fin_plazo.is.null`);
      else if (estado === 'cerrada') q = q.lt('fecha_fin_plazo', ahora);
      const ordenCampo = ORDEN_PERMITIDO.has(params.ordenCampo) ? params.ordenCampo : 'fecha_fin_plazo';
      const ordenAsc = params.ordenAsc !== undefined ? !!params.ordenAsc : true;
      q = q.order(ordenCampo, { ascending: ordenAsc, nullsFirst: false });
      const desde = (pagina - 1) * porPagina; const hasta = desde + porPagina - 1;
      q = q.range(desde, hasta);
      const { data, count, error } = await q;
      if (error) return { filas: [], total: 0, pagina, porPagina, error };
      return { filas: data ?? [], total: count ?? 0, pagina, porPagina, error: null };
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
  const resumen = (etq, r) => console.log(`${etq} -> filas=${r.filas.length} total=${r.total} pag=${r.pagina}/${Math.ceil(r.total / r.porPagina) || 1}` + (r.error ? ` ERROR=${r.error.message}` : ''));

  console.log('\n──────── PRUEBA 1: texto=aire, estado=abierta, importeMax=200000 ────────');
  const p1 = await buscar({ texto: 'aire', estado: 'abierta', importeMax: 200000 });
  resumen('P1', p1); console.table(p1.filas.map(f => ({ titulo: f.titulo, valor: f.valor_estimado, fin: f.fecha_fin_plazo })));

  console.log('\n──────── PRUEBA 2: paginación (pág 1 vs pág 2 no se solapan) ────────');
  const p2a = await buscar({ texto: 'aire', estado: 'abierta', importeMax: 200000, pagina: 1, porPagina: 5 });
  const p2b = await buscar({ texto: 'aire', estado: 'abierta', importeMax: 200000, pagina: 2, porPagina: 5 });
  const ids1 = p2a.filas.map(f => f.licitacion_id), ids2 = p2b.filas.map(f => f.licitacion_id);
  const solapan = ids1.filter(id => ids2.includes(id));
  resumen('P2 pág1', p2a); resumen('P2 pág2', p2b);
  console.log('IDs pág1:', ids1, '\nIDs pág2:', ids2, '\n¿Solapan?', solapan.length ? '❌ ' + solapan : '✅ no');

  console.log('\n──────── PRUEBA 3: tildes (climatizacion encuentra climatización) ────────');
  const p3a = await buscar({ texto: 'climatizacion' });
  const p3b = await buscar({ texto: 'climatización' });
  resumen('P3 sin tilde', p3a); resumen('P3 con tilde', p3b);
  console.log(p3a.total > 0 && p3a.total === p3b.total ? '✅ ambas devuelven lo mismo y > 0' : '⚠️ revisar: totales ' + p3a.total + ' vs ' + p3b.total);

  console.log('\n──────── PRUEBA 4: CPV 90920000 (overlaps) ────────');
  const p4 = await buscar({ cpv: ['90920000'] });
  resumen('P4', p4);
  const llevan = p4.filas.every(f => Array.isArray(f.cpv) && f.cpv.includes('90920000'));
  console.log('¿Todas las filas llevan 90920000?', p4.filas.length ? (llevan ? '✅ sí' : '❌ no') : '(0 filas)');
  console.table(p4.filas.slice(0, 5).map(f => ({ titulo: f.titulo, cpv: (f.cpv || []).join(' ') })));

  console.log('\n──────── PRUEBA 5: orden por valor_estimado DESC ────────');
  const p5 = await buscar({ ordenCampo: 'valor_estimado', ordenAsc: false, estado: 'todas', porPagina: 10 });
  resumen('P5', p5);
  const vals = p5.filas.map(f => f.valor_estimado);
  const ordenado = vals.every((v, i) => i === 0 || v == null || vals[i - 1] == null || vals[i - 1] >= v);
  console.log('valores:', vals, '\n¿Descendente?', ordenado ? '✅ sí' : '❌ no');

  console.log('\n──────── EXTRA: población de ccaa / lugar_ejecucion ────────');
  console.log(await comprobarPoblacion(), '(si salen ~0, esos filtros no sirven aún)');

  console.log('\n✔️ Pruebas terminadas. Compara los "total" con los SQL de prueba_buscador.sql.');
})();
