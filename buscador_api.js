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
//   const { filas, total, pagina, porPagina, error } = await buscar({ ... });
//
// Tabla: public.licitaciones (BG-1). El texto va sobre la columna `tsv`, que se
// construyó con config 'spanish' + unaccent() aplicado AL GUARDAR (no es una
// config propia con unaccent integrado). Por eso aquí quitamos las tildes del
// término en cliente, para que "climatizacion" case con "climatización".
// ============================================================================

// Columnas que la UI necesitará. NO usamos select('*') para no arrastrar el tsv.
const COLUMNAS = [
  'licitacion_id',
  'titulo',
  'objeto',
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
const ORDEN_PERMITIDO = new Set(['fecha_fin_plazo', 'valor_estimado', 'fecha_publicacion']);
const MODOS_COUNT = new Set(['exact', 'estimated', 'planned']);
const ESTADOS = new Set(['abierta', 'cerrada', 'todas']);

// Quita tildes/diacríticos para casar con el tsv (guardado con unaccent()).
// NFD descompone los acentos en marcas combinantes y las elimina; además ñ -> n,
// igual que hace unaccent() en Postgres, así que el comportamiento coincide.
function quitarTildes(s) {
  return s.normalize('NFD').replace(/\p{Diacritic}/gu, '');
}

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

  // buscar(params) -> { filas, total, pagina, porPagina, error }
  //
  // params admitidos (todos opcionales; los vacíos/undefined se ignoran):
  //   texto        string   -> búsqueda full-text (websearch) sobre tsv
  //   cpv          string[] -> overlaps (OR): filas que comparten AL MENOS UN código
  //   fuente       'estatal' | 'agregadas'
  //   importeMin   number   -> valor_estimado >= importeMin
  //   importeMax   number   -> valor_estimado <= importeMax
  //   estado       'abierta' | 'cerrada' | 'todas'   (ver default abajo)
  //   fechaFinDesde / fechaFinHasta   -> rango sobre fecha_fin_plazo (ISO/Date)
  //   fechaPubDesde / fechaPubHasta   -> rango sobre fecha_publicacion (ISO/Date)
  //   ccaa         string   -> igualdad (PENDIENTE: confirmar que está poblado)
  //   lugar        string   -> ilike sobre lugar_ejecucion (PENDIENTE: idem)
  //   ordenCampo   'fecha_fin_plazo' | 'valor_estimado' | 'fecha_publicacion'
  //   ordenAsc     boolean  (def. true)
  //   pagina       number   (1-based, def. 1)
  //   porPagina    number   (def. 25)
  //   modoCount    'exact' | 'estimated' | 'planned'  (def. 'exact')
  async function buscar(params = {}) {
    // --- Paginación saneada.
    const nPorPagina = aNumero(params.porPagina);
    const porPagina = nPorPagina && nPorPagina > 0 ? Math.floor(nPorPagina) : POR_PAGINA_DEF;
    const nPagina = aNumero(params.pagina);
    const pagina = nPagina && nPagina >= 1 ? Math.floor(nPagina) : 1;

    // --- Modo de conteo. 'exact' sobre ~587k puede ir lento en consultas MUY
    //     amplias; entonces conviene pasar modoCount:'estimated' (más barato).
    const modoCount = MODOS_COUNT.has(params.modoCount) ? params.modoCount : 'exact';

    let q = supabase.from('licitaciones').select(COLUMNAS, { count: modoCount });

    // --- Texto: websearch sobre tsv, config 'spanish', SIN tildes (ver arriba).
    const texto = aTexto(params.texto);
    if (texto) {
      q = q.textSearch('tsv', quitarTildes(texto), { type: 'websearch', config: 'spanish' });
    }

    // --- CPV: OR dentro de la lista (overlaps = comparte AL MENOS UNO).
    //     v1: igualdad de código COMPLETO. La coincidencia por PREFIJO del Radar
    //     ("empieza por") NO la da overlaps; queda como iteración futura
    //     (requeriría una RPC o una columna auxiliar de prefijos).
    let cpvs = [];
    if (Array.isArray(params.cpv)) {
      cpvs = params.cpv.map(aTexto).filter(Boolean);
      if (cpvs.length) q = q.overlaps('cpv', cpvs);
    }

    // --- Fuente.
    const fuente = params.fuente === 'estatal' || params.fuente === 'agregadas' ? params.fuente : null;
    if (fuente) q = q.eq('fuente', fuente);

    // --- Importe (valor_estimado).
    const impMin = aNumero(params.importeMin);
    const impMax = aNumero(params.importeMax);
    if (impMin !== null) q = q.gte('valor_estimado', impMin);
    if (impMax !== null) q = q.lte('valor_estimado', impMax);

    // --- Rangos de fecha explícitos (AND con todo lo demás).
    const finDesde = aISO(params.fechaFinDesde);
    const finHasta = aISO(params.fechaFinHasta);
    if (finDesde) q = q.gte('fecha_fin_plazo', finDesde);
    if (finHasta) q = q.lte('fecha_fin_plazo', finHasta);
    const pubDesde = aISO(params.fechaPubDesde);
    const pubHasta = aISO(params.fechaPubHasta);
    if (pubDesde) q = q.gte('fecha_publicacion', pubDesde);
    if (pubHasta) q = q.lte('fecha_publicacion', pubHasta);

    // --- ccaa / lugar_ejecucion: PENDIENTES de confirmar que el catálogo los
    //     tiene poblados (BG-1 no garantizó que CODICE traiga estos campos). Si
    //     están casi todo NULL no filtran nada útil. Quedan escritos pero solo
    //     se aplican si se pasan; usa comprobarPoblacion() para decidir.
    const ccaa = aTexto(params.ccaa);
    if (ccaa) q = q.eq('ccaa', ccaa);
    const lugar = aTexto(params.lugar);
    if (lugar) q = q.ilike('lugar_ejecucion', `%${lugar}%`);

    // --- Estado (DERIVADO, no se guarda).
    //     'abierta' -> fin de plazo en el futuro  O  sin plazo (NULL).
    //     'cerrada' -> fin de plazo ya pasado (los NULL NO cuentan como cerrada).
    //     'todas'   -> no filtra por estado.
    //   fecha_fin_plazo NULL = "sin plazo" (coherente con el Radar: NULL no es
    //   caducada). En 'abierta' las INCLUIMOS (no están cerradas) con el OR is.null.
    //
    //   DEFAULT: si no se pasa estado, vale 'abierta' SOLO cuando no hay ningún
    //   otro filtro (la pantalla de entrada = "primeras N abiertas"); si hay otro
    //   filtro, default 'todas' (para no ocultar resultados que el usuario pidió).
    const hayOtrosFiltros = !!(
      texto || cpvs.length || fuente ||
      impMin !== null || impMax !== null ||
      finDesde || finHasta || pubDesde || pubHasta || ccaa || lugar
    );
    const estado = ESTADOS.has(params.estado)
      ? params.estado
      : (hayOtrosFiltros ? 'todas' : 'abierta');
    // ISO sin milisegundos para no meter puntos extra en el filtro .or().
    const ahora = new Date().toISOString().replace(/\.\d{3}Z$/, 'Z');
    if (estado === 'abierta') {
      q = q.or(`fecha_fin_plazo.gte.${ahora},fecha_fin_plazo.is.null`);
    } else if (estado === 'cerrada') {
      q = q.lt('fecha_fin_plazo', ahora);
    }

    // --- Orden. Por defecto fin de plazo ascendente (lo que vence antes, primero).
    const ordenCampo = ORDEN_PERMITIDO.has(params.ordenCampo) ? params.ordenCampo : 'fecha_fin_plazo';
    const ordenAsc = params.ordenAsc !== undefined ? !!params.ordenAsc : true;
    q = q.order(ordenCampo, { ascending: ordenAsc, nullsFirst: false });

    // --- Paginación: range es inclusivo en ambos extremos.
    const desde = (pagina - 1) * porPagina;
    const hasta = desde + porPagina - 1;
    q = q.range(desde, hasta);

    // --- Ejecutar. El error de PostgREST se DEVUELVE (no se lanza) para no
    //     tumbar la página.
    const { data, count, error } = await q;
    if (error) {
      return { filas: [], total: 0, pagina, porPagina, error };
    }
    return { filas: data ?? [], total: count ?? 0, pagina, porPagina, error: null };
  }

  // Utilidad opcional para BG-3: cuántas filas tienen ccaa / lugar_ejecucion
  // poblados. Úsala UNA vez para decidir si esos filtros valen la pena en la UI.
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
