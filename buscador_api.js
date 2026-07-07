// buscador_api.js
// ============================================================================
// BG-3 · Buscador General de Licitaciones — módulo de CONSULTA (solo lectura).
//
// SIN interfaz (la UI es BG-4). NO crea cliente supabase: recibe el cliente YA
// inicializado (el mismo `const supabase` de la web, rol authenticated tras el
// login). NUNCA service_role ni claves nuevas. NO toca el Radar estático
// (data/licitaciones.json) ni el pipeline de ingesta: esto es solo lectura.
//
// USO:
//   import { crearBuscador } from './buscador_api.js';
//   const { buscar } = crearBuscador(supabase);   // <- el supabase ya existente
//   const r = await buscar({ ... });               // { filas, total, pagina,
//                                                  //   porPagina, aproximado, error }
//
// DOS CAMINOS según los filtros:
//   · CON texto  O  CON CPV por PREFIJO -> RPC public.buscar_licitaciones (ver
//     buscador_rpc.sql). Usa una CTE MATERIALIZED que filtra PRIMERO (por el
//     GIN(tsv) si hay texto; por el prefijo CPV si no) y luego ordena+pagina.
//     Con texto, esto evita el plan malo de PostgREST (ordenar por el índice de
//     fecha y filtrar el tsv al vuelo -> 16 s -> timeout 57014). El término va
//     tal cual; la RPC le aplica unaccent('spanish') para casar tildes.
//     El PREFIJO CPV ("empieza por", como el Radar) no lo sabe hacer PostgREST
//     sobre un text[], por eso también pasa por la RPC (con o sin texto).
//   · SIN texto y SIN prefijo -> PostgREST normal (filtros + orden + paginación)
//     con conteo HÍBRIDO (estimación del planner; exacto solo si el conjunto es
//     pequeño). El CPV EXACTO (overlaps) también va por aquí.
//
// PAGINACIÓN ESTABLE: ambos caminos ordenan ADEMÁS por licitacion_id (PK) como
// desempate, para que páginas consecutivas no se solapen en empates de fecha/NULL.
// ============================================================================

// Columnas que la UI necesitará. NO usamos select('*') para no arrastrar el tsv.
const COLUMNAS = [
  'licitacion_id',
  'titulo',
  'objeto',
  'num_expediente',
  'organo_contratacion',
  'cpv',
  'fuente',
  'presupuesto_con_iva',
  'presupuesto_sin_iva',
  'valor_estimado',
  'fecha_publicacion',
  'fecha_fin_plazo',
  'lugar_ejecucion',
  'ccaa',
  'enlace',
].join(',');

const POR_PAGINA_DEF = 25;
const UMBRAL_COUNT_EXACTO_DEF = 10000; // < a esto: count exacto; >= : estimación
const ORDEN_PERMITIDO = new Set(['fecha_fin_plazo', 'valor_estimado', 'fecha_publicacion']);
const MODOS_COUNT = new Set(['exact', 'estimated', 'planned']);
const ESTADOS = new Set(['abierta', 'cerrada', 'todas']);

// Número finito o null (ignora '', null, undefined, NaN).
function aNumero(v) {
  if (v === '' || v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

// Date | ISO | ms -> ISO, o null si no es una fecha válida.
function aISO(v) {
  if (v === '' || v === null || v === undefined) return null;
  const d = v instanceof Date ? v : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

// Texto no vacío tras recortar, o null.
function aTexto(v) {
  if (typeof v !== 'string') return null;
  const t = v.trim();
  return t.length ? t : null;
}

export function crearBuscador(supabase) {
  if (!supabase || typeof supabase.from !== 'function') {
    throw new Error('crearBuscador: hay que pasarle el cliente supabase ya inicializado.');
  }

  // buscar(params) -> { filas, total, pagina, porPagina, aproximado, topado, error }
  //   topado=true (solo vía RPC) -> el conjunto supera el TOPE del conteo; `total`
  //                      es ese tope y la UI muestra "más de N" (no "≈ N").
  //   aproximado=true  -> `total` es una estimación del planner (conjunto grande,
  //                       solo en el camino SIN texto). El camino CON texto da
  //                       siempre conteo exacto del subconjunto que casa.
  //
  // params admitidos (todos opcionales; los vacíos/undefined se ignoran):
  //   texto        string   -> búsqueda full-text (vía RPC)
  //   cpv          string[] -> overlaps (OR): filas que comparten AL MENOS UN código
  //                            EXACTO (código completo). Va por PostgREST/RPC según haya texto.
  //   cpvPrefijo   string[] -> PREFIJO de CPV (OR): casa la familia "empieza por"
  //                            (p. ej. "9073" -> 9073xxxx), como el Radar. FUERZA la
  //                            RPC (con o sin texto), porque PostgREST no hace prefijo
  //                            sobre text[]. Cada prefijo se usa trim() tal cual.
  //   expediente   string   -> Nº de expediente del órgano. Casa por CONTENIDO e
  //                            ignorando / . - y mayúsculas (normalizado). Solo dispara
  //                            con >=3 chars normalizados; FUERZA la RPC (con o sin texto).
  //   fuente       'estatal' | 'agregadas'
  //   importeMin / importeMax  -> rango sobre valor_estimado
  //   estado       'abierta' | 'cerrada' | 'todas'   (ver default abajo)
  //   fechaFinDesde / fechaFinHasta   -> rango sobre fecha_fin_plazo (ISO/Date)
  //   fechaPubDesde / fechaPubHasta   -> rango sobre fecha_publicacion (ISO/Date)
  //   ordenCampo   'fecha_fin_plazo' | 'valor_estimado' | 'fecha_publicacion'
  //   ordenAsc     boolean  (def. true)
  //   pagina       number   (1-based, def. 1)
  //   porPagina    number   (def. 25)
  //   modoCount    'exact' | 'estimated' | 'planned'  -> FUERZA el modo en el
  //                camino SIN texto (salta el híbrido). Sin pasarlo, híbrido.
  //   umbralCountExacto number -> umbral del híbrido (def. 10000)
  //
  //   NOTA: ccaa y lugar_ejecucion NO se filtran: el catálogo los tiene a 0
  //   (verificado 2026-06-30). Extraer la región del CODICE es tarea futura del
  //   pipeline; cuando se pueblen, reactivar aquí (ver comprobarPoblacion()).
  async function buscar(params = {}) {
    // --- Paginación saneada.
    const nPorPagina = aNumero(params.porPagina);
    const porPagina = nPorPagina && nPorPagina > 0 ? Math.floor(nPorPagina) : POR_PAGINA_DEF;
    const nPagina = aNumero(params.pagina);
    const pagina = nPagina && nPagina >= 1 ? Math.floor(nPagina) : 1;
    const desde = (pagina - 1) * porPagina;
    const hasta = desde + porPagina - 1;

    const ok = (data, total, aproximado) => ({
      filas: data ?? [], total, pagina, porPagina, aproximado, topado: false, error: null,
    });
    const fallo = (error) => ({ filas: [], total: 0, pagina, porPagina, aproximado: false, topado: false, error });

    // --- Normalizar filtros UNA vez (se reutilizan en ambos caminos).
    const texto = aTexto(params.texto);
    const cpvs = Array.isArray(params.cpv) ? params.cpv.map(aTexto).filter(Boolean) : [];
    const cpvPrefijos = Array.isArray(params.cpvPrefijo) ? params.cpvPrefijo.map(aTexto).filter(Boolean) : [];
    // Nº de expediente: normalizamos (MAYÚSCULAS, sin espacios / . -) SOLO para decidir
    // si dispara el filtro (>=3 chars; un fragmento más corto casaría cientos de miles).
    // A la RPC va el término TAL CUAL: ella re-normaliza igual (public.norm_expediente).
    const expedienteRaw = aTexto(params.expediente);
    const expedienteNorm = expedienteRaw ? expedienteRaw.toUpperCase().replace(/[\s./-]/g, '') : '';
    const expediente = expedienteNorm.length >= 3 ? expedienteRaw : null;
    const fuente = params.fuente === 'estatal' || params.fuente === 'agregadas' ? params.fuente : null;
    const impMin = aNumero(params.importeMin);
    const impMax = aNumero(params.importeMax);
    const finDesde = aISO(params.fechaFinDesde);
    const finHasta = aISO(params.fechaFinHasta);
    const pubDesde = aISO(params.fechaPubDesde);
    const pubHasta = aISO(params.fechaPubHasta);

    // Estado (DERIVADO, no se guarda).
    //   'abierta' -> fin de plazo en el FUTURO (fecha_fin_plazo >= ahora). Las que NO
    //               tienen fecha de fin NO cuentan como abiertas (aparecen solo en 'todas').
    //               (Antes 'abierta' incluía las NULL, pero ese OR-null no es sargable y
    //                con el orden fin_plazo ASC provocaba timeout 57014 en la vista inicial.)
    //   'cerrada' -> fin de plazo ya pasado (los NULL NO cuentan como cerrada).
    //   'todas'   -> no filtra por estado (aquí caen también las de fin de plazo NULL).
    // DEFAULT: 'abierta' SOLO si no hay ningún otro filtro (pantalla de entrada =
    // "primeras N abiertas"); si hay otro filtro (incluido texto), 'todas'.
    const hayOtrosFiltros = !!(
      texto || cpvs.length || cpvPrefijos.length || expediente || fuente ||
      impMin !== null || impMax !== null ||
      finDesde || finHasta || pubDesde || pubHasta
    );
    const estado = ESTADOS.has(params.estado) ? params.estado : (hayOtrosFiltros ? 'todas' : 'abierta');

    const ordenCampo = ORDEN_PERMITIDO.has(params.ordenCampo) ? params.ordenCampo : 'fecha_fin_plazo';
    const ordenAsc = params.ordenAsc !== undefined ? !!params.ordenAsc : true;

    // ======================================================================
    // CAMINO 1 · CON TEXTO  O  CON PREFIJO CPV -> RPC (filtra primero; conteo
    // exacto sobre el subconjunto que casa).
    // ======================================================================
    if (texto || cpvPrefijos.length || expediente) {
      const rpcParams = {
        p_texto: texto,                                 // puede ir null (solo prefijo CPV / expediente)
        p_cpv: cpvs.length ? cpvs : null,               // CPV exacto (overlaps)
        p_fuente: fuente,
        p_importe_min: impMin,
        p_importe_max: impMax,
        p_estado: estado,
        p_fin_desde: finDesde,
        p_fin_hasta: finHasta,
        p_pub_desde: pubDesde,
        p_pub_hasta: pubHasta,
        p_orden_campo: ordenCampo,
        p_orden_asc: ordenAsc,
        p_pagina: pagina,
        p_por_pagina: porPagina,
      };
      // Solo enviamos p_cpv_prefijo cuando HAY prefijos: así una búsqueda de solo
      // TEXTO sigue resolviendo contra la RPC ANTIGUA (BG-3, sin ese parámetro) si
      // aún no se re-desplegó buscador_rpc.sql. Degradación segura: lo único que
      // exige la RPC nueva es el propio filtro CPV por prefijo.
      if (cpvPrefijos.length) rpcParams.p_cpv_prefijo = cpvPrefijos;
      // Ídem con p_expediente: solo se envía si hay expediente válido (>=3 chars
      // normalizados). Texto/CPV-solo no lo mandan, así que siguen casando la RPC
      // anterior si aún no se re-desplegó el SQL; el filtro de expediente exige la
      // RPC nueva (16 args) + expediente_schema.sql.
      if (expediente) rpcParams.p_expediente = expediente;
      const { data, error } = await supabase.rpc('buscar_licitaciones', rpcParams);
      if (error) return fallo(error);
      return {
        filas: (data && data.filas) ?? [],
        total: (data && data.total) ?? 0,
        pagina,
        porPagina,
        aproximado: !!(data && data.aproximado),
        topado: !!(data && data.topado),   // total es el TOPE -> la UI muestra "más de N"
        error: null,
      };
    }

    // ======================================================================
    // CAMINO 2 · SIN TEXTO -> PostgREST + conteo híbrido.
    // ======================================================================
    // ISO sin milisegundos para no meter puntos extra en el filtro .or().
    const ahora = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');

    const aplicarFiltros = (q) => {
      if (cpvs.length) q = q.overlaps('cpv', cpvs); // OR; código COMPLETO (el prefijo va por la RPC, ver arriba)
      if (fuente) q = q.eq('fuente', fuente);
      if (impMin !== null) q = q.gte('valor_estimado', impMin);
      if (impMax !== null) q = q.lte('valor_estimado', impMax);
      if (finDesde) q = q.gte('fecha_fin_plazo', finDesde);
      if (finHasta) q = q.lte('fecha_fin_plazo', finHasta);
      if (pubDesde) q = q.gte('fecha_publicacion', pubDesde);
      if (pubHasta) q = q.lte('fecha_publicacion', pubHasta);
      // 'abierta' = fecha de fin FUTURA. SARGABLE: es un rango por el índice
      // (fecha_fin_plazo, licitacion_id) desde 'ahora' -> instantáneo. El antiguo
      // 'OR fecha_fin_plazo IS NULL' rompía el rango y, con orden fin_plazo ASC, el
      // índice recorría TODAS las cerradas (pasadas) antes de las abiertas -> timeout.
      if (estado === 'abierta') q = q.gte('fecha_fin_plazo', ahora);
      else if (estado === 'cerrada') q = q.lt('fecha_fin_plazo', ahora);
      return q;
    };

    // Consulta de DATOS: filtros + orden + desempate por PK + paginación.
    const construirDatos = () => {
      let q = aplicarFiltros(supabase.from('licitaciones').select(COLUMNAS));
      q = q.order(ordenCampo, { ascending: ordenAsc, nullsFirst: false });
      q = q.order('licitacion_id', { ascending: true }); // desempate ESTABLE
      return q.range(desde, hasta);
    };
    // Consulta de CONTEO (head: sin datos) en el modo indicado.
    const contar = (modo) =>
      aplicarFiltros(supabase.from('licitaciones').select('licitacion_id', { count: modo, head: true }));

    // Modo de conteo FORZADO por el usuario: datos + ese count, en paralelo.
    const modoForzado = MODOS_COUNT.has(params.modoCount) ? params.modoCount : null;
    if (modoForzado) {
      const [rDatos, rCount] = await Promise.all([construirDatos(), contar(modoForzado)]);
      if (rDatos.error) return fallo(rDatos.error);
      const total = rCount.error ? 0 : (rCount.count ?? 0);
      return ok(rDatos.data, total, modoForzado !== 'exact');
    }

    // HÍBRIDO (por defecto): datos + estimación del planner en paralelo (1 RTT).
    const [rDatos, rEstim] = await Promise.all([construirDatos(), contar('planned')]);
    if (rDatos.error) return fallo(rDatos.error);
    const estimado = rEstim.error ? null : (rEstim.count ?? 0);

    // Count EXACTO cuando la estimación es PEQUEÑA (barato) O cuando NO hay estimación
    // (el probe 'planned' falló/503): así un fallo del probe de estimación NO deja el
    // contador en 0. Solo si la estimación es GRANDE devolvemos la estimación aproximada
    // (para no contar cientos de miles de filas exactas).
    const nUmbral = aNumero(params.umbralCountExacto);
    const umbralExacto = nUmbral && nUmbral > 0 ? Math.floor(nUmbral) : UMBRAL_COUNT_EXACTO_DEF;
    if (estimado === null || estimado < umbralExacto) {
      const rExacto = await contar('exact');
      if (!rExacto.error && rExacto.count != null) return ok(rDatos.data, rExacto.count, false);
      // El exacto también falló: usa la estimación si la había; si no, 0 aproximado.
      return ok(rDatos.data, estimado ?? 0, true);
    }

    // Conjunto grande: devolvemos la estimación, aproximada.
    return ok(rDatos.data, estimado, true);
  }

  // Utilidad para re-comprobar (en el futuro) si ccaa / lugar_ejecucion ya se
  // pueblan en el catálogo. Hoy ambos dan 0 (verificado 2026-06-30) y por eso
  // sus filtros están desactivados en buscar().
  async function comprobarPoblacion() {
    const salida = {};
    for (const col of ['ccaa', 'lugar_ejecucion']) {
      const { count, error } = await supabase
        .from('licitaciones')
        .select('licitacion_id', { count: 'exact', head: true })
        .not(col, 'is', null);
      salida[col] = error ? `error: ${error.message}` : count;
    }
    return salida;
  }

  return { buscar, comprobarPoblacion };
}
