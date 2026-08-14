// menores_api.js
// ============================================================================
// FASE M · Explorador de CONTRATOS MENORES — módulo de CONSULTA (solo lectura).
//
// Igual filosofía que buscador_api.js: SIN interfaz (la UI la monta generar_web),
// recibe el cliente supabase YA inicializado (rol authenticated tras login), NUNCA
// service_role. Consulta public.menores (tabla AISLADA; ver menores_schema.sql).
//
// TODO POR PostgREST (sin RPC): a diferencia del Buscador general, aquí NO hace falta
// una RPC. Los tres modos y los filtros se resuelven con PostgREST + índices:
//   · modo 'todo'   -> lista paginada (conteo ESTIMADO del planner; 1,1M no se cuenta exacto).
//   · modo 'cifs'   -> cifs_adjudicatarios && [CIFS_SEGUIDOS]  (GIN; casa CUALQUIER ganador).
//   · modo 'nicho'  -> (cpv_txt ILIKE algún prefijo del nicho) OR (tsv @@ palabras del nicho),
//                      vía .or() con ilike + websearch-fts. El nicho (CPV + palabras) lo
//                      define el CLIENTE (de intereses.yaml), no se guarda en la BD, así
//                      ampliarlo es solo cambiar la config del front (sin re-backfill).
//   · filtros CPV prefijo / importe / órgano / fecha -> PostgREST normal.
//
// TILDES: el tsv se guardó con unaccent. Las palabras del nicho y el texto libre se
// mandan SIN tildes (el front las normaliza) para que casen. cpv_txt lleva un espacio
// por delante de cada código: "empieza por 9073" == contiene ' 9073' (ilike '* 9073*').
//
// PAGINACIÓN ESTABLE: se ordena SIEMPRE además por licitacion_id (PK) como desempate.
// ============================================================================

// Columnas que necesita la UI (sin tsv/cpv_txt, que son de apoyo).
const M_COLUMNAS = [
  'licitacion_id',
  'objeto',
  'cpv',
  'importe_sin_iva',
  'organo_contratacion',
  'adjudicatario',
  'cif_adjudicatario',
  'cifs_adjudicatarios',
  'fecha_adjudicacion',
  'num_expediente',
  'enlace',
  'n_adjudicatarios',
].join(',');

const M_POR_PAGINA_DEF = 25;
const M_UMBRAL_COUNT_EXACTO = 10000; // < a esto: count exacto; >= : estimación del planner
const M_ORDEN_PERMITIDO = new Set(['fecha_adjudicacion', 'importe_sin_iva']);
const M_MODOS = new Set(['todo', 'nicho', 'cifs']);

// Número finito o null.
function mNumero(v) {
  if (v === '' || v === null || v === undefined) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}
// Date | ISO | 'yyyy-mm-dd' -> 'yyyy-mm-dd' (fecha_adjudicacion es DATE), o null.
function mFecha(v) {
  if (v === '' || v === null || v === undefined) return null;
  const d = v instanceof Date ? v : new Date(v);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString().slice(0, 10);
}
// Texto no vacío tras recortar, o null.
function mTexto(v) {
  if (typeof v !== 'string') return null;
  const t = v.trim();
  return t.length ? t : null;
}
// Quita tildes/diacríticos (para casar el tsv, que se guardó con unaccent) y pasa a
// minúsculas. NFD separa la letra de su diacrítico; ̀-ͯ son los diacríticos.
function mSinTildes(s) {
  return String(s == null ? '' : s).normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

// Escapa un valor para meterlo dentro del string de .or() de PostgREST. Las comas y
// los paréntesis separan/agrupan condiciones, así que un valor que los contenga
// rompería el filtro. En nuestros valores (prefijos CPV numéricos, palabras del nicho)
// no aparecen, pero por robustez los quitamos.
function mLimpiaOr(s) {
  return String(s == null ? '' : s).replace(/[(),]/g, ' ').trim();
}

export function crearMenores(supabase) {
  if (!supabase || typeof supabase.from !== 'function') {
    throw new Error('crearMenores: hay que pasarle el cliente supabase ya inicializado.');
  }

  // buscar(params) -> { filas, total, pagina, porPagina, aproximado, error }
  //
  // params (todos opcionales):
  //   modo         'todo' | 'nicho' | 'cifs'         (def. 'todo')
  //   nichoCpv     string[]  prefijos CPV del nicho (para modo 'nicho')
  //   nichoKw      string    consulta websearch YA SIN TILDES de las palabras del
  //                          nicho (p. ej. 'calidad del aire or purificador or ...')
  //   cifsSeguidos string[]  CIFs a cazar en modo 'cifs' (lista curada del front)
  //   cpvPrefijo   string[]  filtro por prefijo CPV (independiente del modo)
  //   texto        string    búsqueda libre en objeto+órgano (se normaliza sin tildes)
  //   organo       string    ÓRGANO comprador EXACTO (igualdad; para "ver todo de este órgano")
  //   importeMin / importeMax   rango sobre importe_sin_iva
  //   fechaDesde / fechaHasta   rango sobre fecha_adjudicacion
  //   ordenCampo   'fecha_adjudicacion' | 'importe_sin_iva'   (def. fecha)
  //   ordenAsc     boolean   (def. false -> más recientes / mayor importe primero)
  //   pagina       number    (1-based, def. 1)
  //   porPagina    number    (def. 25)
  async function buscar(params = {}) {
    const nPorPagina = mNumero(params.porPagina);
    const porPagina = nPorPagina && nPorPagina > 0 ? Math.floor(nPorPagina) : M_POR_PAGINA_DEF;
    const nPagina = mNumero(params.pagina);
    const pagina = nPagina && nPagina >= 1 ? Math.floor(nPagina) : 1;
    const desde = (pagina - 1) * porPagina;
    const hasta = desde + porPagina - 1;

    const ok = (data, total, aproximado) => ({
      filas: data ?? [], total, pagina, porPagina, aproximado, error: null,
    });
    const fallo = (error) => ({ filas: [], total: 0, pagina, porPagina, aproximado: false, error });

    const modo = M_MODOS.has(params.modo) ? params.modo : 'todo';
    const nichoCpv = Array.isArray(params.nichoCpv) ? params.nichoCpv.map(mTexto).filter(Boolean) : [];
    const nichoKw = mTexto(params.nichoKw);
    const cifsSeguidos = Array.isArray(params.cifsSeguidos) ? params.cifsSeguidos.map(mTexto).filter(Boolean) : [];
    const cpvPrefijo = Array.isArray(params.cpvPrefijo) ? params.cpvPrefijo.map(mTexto).filter(Boolean) : [];
    const texto = mTexto(params.texto);
    const organo = mTexto(params.organo);
    const impMin = mNumero(params.importeMin);
    const impMax = mNumero(params.importeMax);
    const fechaDesde = mFecha(params.fechaDesde);
    const fechaHasta = mFecha(params.fechaHasta);
    const ordenCampo = M_ORDEN_PERMITIDO.has(params.ordenCampo) ? params.ordenCampo : 'fecha_adjudicacion';
    const ordenAsc = params.ordenAsc !== undefined ? !!params.ordenAsc : false;

    // --- Aplica TODOS los filtros a una consulta (datos o conteo) -----------
    const aplicar = (q) => {
      // MODO.
      if (modo === 'cifs' && cifsSeguidos.length) {
        q = q.overlaps('cifs_adjudicatarios', cifsSeguidos);   // GIN; casa cualquier ganador
      } else if (modo === 'nicho') {
        // (cpv_txt ILIKE algún prefijo del nicho) OR (tsv @@ palabras del nicho), en UN
        // grupo .or(). Los prefijos van como '* <pref>*' (el * = comodín de PostgREST;
        // el espacio marca el inicio del código en cpv_txt). Las palabras, por wfts.
        const cond = [];
        nichoCpv.forEach((p) => {
          const pref = mLimpiaOr(p);
          if (pref) cond.push('cpv_txt.ilike.* ' + pref + '*');
        });
        if (nichoKw) cond.push('tsv.wfts(spanish).' + mLimpiaOr(nichoKw));
        // Si por lo que sea no hay ninguna condición de nicho, no devolvemos toda la
        // tabla como "nicho": forzamos un imposible (nada casa).
        q = cond.length ? q.or(cond.join(',')) : q.eq('licitacion_id', '__sin_nicho__');
      }
      // FILTRO CPV por prefijo (además del modo). Varios prefijos = OR entre ellos.
      if (cpvPrefijo.length) {
        const cond = cpvPrefijo.map((p) => 'cpv_txt.ilike.* ' + mLimpiaOr(p) + '*').filter(Boolean);
        if (cond.length) q = q.or(cond.join(','));
      }
      // TEXTO libre (objeto + órgano, vía tsv). Sin tildes para casar el tsv unaccent.
      if (texto) q = q.textSearch('tsv', mSinTildes(texto), { type: 'websearch', config: 'spanish' });
      // ÓRGANO comprador EXACTO (btree). Para "ver todo lo que compra este órgano".
      if (organo) q = q.eq('organo_contratacion', organo);
      if (impMin !== null) q = q.gte('importe_sin_iva', impMin);
      if (impMax !== null) q = q.lte('importe_sin_iva', impMax);
      if (fechaDesde) q = q.gte('fecha_adjudicacion', fechaDesde);
      if (fechaHasta) q = q.lte('fecha_adjudicacion', fechaHasta);
      return q;
    };

    const construirDatos = () => {
      let q = aplicar(supabase.from('menores').select(M_COLUMNAS));
      q = q.order(ordenCampo, { ascending: ordenAsc, nullsFirst: false });
      q = q.order('licitacion_id', { ascending: true });      // desempate ESTABLE
      return q.range(desde, hasta);
    };
    const contar = (modoCount) =>
      aplicar(supabase.from('menores').select('licitacion_id', { count: modoCount, head: true }));

    // HÍBRIDO (como el Buscador): datos + estimación del planner en paralelo. Si la
    // estimación es pequeña (o falló), se pide el count EXACTO; si es grande, se
    // devuelve la estimación (no contar 1,1M exactas).
    const [rDatos, rEstim] = await Promise.all([construirDatos(), contar('planned')]);
    if (rDatos.error) return fallo(rDatos.error);
    const estimado = rEstim.error ? null : (rEstim.count ?? 0);

    if (estimado === null || estimado < M_UMBRAL_COUNT_EXACTO) {
      const rExacto = await contar('exact');
      if (!rExacto.error && rExacto.count != null) return ok(rDatos.data, rExacto.count, false);
      return ok(rDatos.data, estimado ?? 0, true);
    }
    return ok(rDatos.data, estimado, true);
  }

  return { buscar };
}
