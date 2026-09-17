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
//   · modo 'todo'   -> lista paginada.
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
// RECUENTO Y ORDEN POR IMPORTE: ver M_UMBRAL.
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
  'fuente',            // estatal | andalucia | … (etiqueta en data/menores_fuentes.json)
].join(',');

const M_POR_PAGINA_DEF = 25;
// UMBRAL de 10.000 filas. Decide DOS cosas (medido el 17/09/2026, tras cargar Andalucía):
//  · EL RECUENTO. Si la estimación del planner queda por debajo, count exacto. Si queda
//    por encima, la estimación NO se enseña, porque se equivoca mucho (el nicho estimaba
//    10.074 y eran 2.947): se mira si existe la fila nº 10.001 y se da el número exacto
//    o «más de 10.000».
//  · EL ORDEN POR IMPORTE CON FILTROS. Con un filtro, Postgres recorre el índice de
//    importe de TODA la tabla mirando fila a fila (SAS por importe: más de 30 s; nicho
//    por importe ascendente: 20,6 s en frío). Si el resultado tiene 10.000 filas o menos,
//    se traen solo clave + importe (en tandas de 1.000, paginando por clave: medido de 4 a
//    340 ms por tanda) y se ordena aquí. Si tiene más, el orden por importe se desactiva y
//    la UI avisa para acotar. Decide el tamaño del RESULTADO, no el del órgano: «SAS +
//    nicho» (432) o «SAS en marzo» (7.407) sí se ordenan.
const M_UMBRAL = 10000;
const M_TANDA = 1000;                  // tope de filas por petición de PostgREST en Supabase
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

// Valor de una EXCLUSIÓN del nicho dentro de or()/and(): entre comillas y con las
// comillas y barras internas escapadas. Dentro de comillas las comas y los paréntesis ya
// no rompen nada. Sin las comillas, PostgREST quita las de un valor que es SOLO una frase
// y el websearch pierde la frase (medido: 429 filas en vez de 431).
function mValorOr(s) {
  return '"' + String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
}

// Orden por importe hecho en el navegador: sin importe al final (NULLS LAST en los dos
// sentidos, como el servidor) y desempate por clave ascendente.
function mComparaImporte(ascendente) {
  const signo = ascendente ? 1 : -1;
  return (a, b) => {
    const ia = mNumero(a.importe_sin_iva);
    const ib = mNumero(b.importe_sin_iva);
    if (ia === null && ib !== null) return 1;
    if (ib === null && ia !== null) return -1;
    if (ia !== null && ib !== null && ia !== ib) return signo * (ia - ib);
    if (a.licitacion_id < b.licitacion_id) return -1;
    return a.licitacion_id > b.licitacion_id ? 1 : 0;
  };
}

export function crearMenores(supabase) {
  if (!supabase || typeof supabase.from !== 'function') {
    throw new Error('crearMenores: hay que pasarle el cliente supabase ya inicializado.');
  }

  // Lista (clave + importe) ya ordenada de la ÚLTIMA búsqueda pequeña por importe. Cambiar
  // de página no vuelve a traerla: solo se piden las 25 filas de la página.
  let mCacheOrden = { clave: null, lista: null };

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
  function normaliza(params = {}) {
    const nPorPagina = mNumero(params.porPagina);
    const nPagina = mNumero(params.pagina);
    return {
      porPagina: nPorPagina && nPorPagina > 0 ? Math.floor(nPorPagina) : M_POR_PAGINA_DEF,
      pagina: nPagina && nPagina >= 1 ? Math.floor(nPagina) : 1,
      modo: M_MODOS.has(params.modo) ? params.modo : 'todo',
      nichoCpv: Array.isArray(params.nichoCpv) ? params.nichoCpv.map(mTexto).filter(Boolean) : [],
      nichoKw: mTexto(params.nichoKw),
      // [{kw, excluye: [websearch, ...]}]: palabras del nicho que llevan exclusión.
      nichoExcl: Array.isArray(params.nichoExcl)
        ? params.nichoExcl.filter((x) => x && mTexto(x.kw)).map((x) => ({
          kw: mTexto(x.kw),
          excluye: (Array.isArray(x.excluye) ? x.excluye : []).map(mTexto).filter(Boolean),
        }))
        : [],
      cifsSeguidos: Array.isArray(params.cifsSeguidos) ? params.cifsSeguidos.map(mTexto).filter(Boolean) : [],
      cpvPrefijo: Array.isArray(params.cpvPrefijo) ? params.cpvPrefijo.map(mTexto).filter(Boolean) : [],
      texto: mTexto(params.texto),
      organo: mTexto(params.organo),
      impMin: mNumero(params.importeMin),
      impMax: mNumero(params.importeMax),
      fechaDesde: mFecha(params.fechaDesde),
      fechaHasta: mFecha(params.fechaHasta),
      ordenCampo: M_ORDEN_PERMITIDO.has(params.ordenCampo) ? params.ordenCampo : 'fecha_adjudicacion',
      ordenAsc: params.ordenAsc !== undefined ? !!params.ordenAsc : false,
    };
  }

  // --- Aplica TODOS los filtros a una consulta (datos, sonda o conteo) -------
  function aplicar(q, n) {
    // MODO.
    if (n.modo === 'cifs' && n.cifsSeguidos.length) {
      q = q.overlaps('cifs_adjudicatarios', n.cifsSeguidos);   // GIN; casa cualquier ganador
    } else if (n.modo === 'nicho') {
      // (cpv_txt ILIKE algún prefijo del nicho) OR (tsv @@ palabras del nicho), en UN
      // grupo .or(). Los prefijos van como '* <pref>*' (el * = comodín de PostgREST;
      // el espacio marca el inicio del código en cpv_txt). Las palabras, por wfts.
      const cond = [];
      n.nichoCpv.forEach((p) => {
        const pref = mLimpiaOr(p);
        if (pref) cond.push('cpv_txt.ilike.* ' + pref + '*');
      });
      if (n.nichoKw) cond.push('tsv.wfts(spanish).' + mLimpiaOr(n.nichoKw));
      // Palabras con EXCLUSIÓN (ver menores_nicho.py): la fila entra por esa palabra solo
      // si no casa ninguna de sus exclusiones. Lo que entra por CPV o por otra palabra no
      // se toca (exclusión acotada, no global).
      n.nichoExcl.forEach((x) => {
        const positiva = 'tsv.wfts(spanish).' + mLimpiaOr(x.kw);
        const negativas = x.excluye.map((e) => 'tsv.not.wfts(spanish).' + mValorOr(e));
        cond.push(negativas.length ? 'and(' + positiva + ',' + negativas.join(',') + ')' : positiva);
      });
      // Si por lo que sea no hay ninguna condición de nicho, no devolvemos toda la
      // tabla como "nicho": forzamos un imposible (nada casa).
      q = cond.length ? q.or(cond.join(',')) : q.eq('licitacion_id', '__sin_nicho__');
    }
    // FILTRO CPV por prefijo (además del modo). Varios prefijos = OR entre ellos.
    if (n.cpvPrefijo.length) {
      const cond = n.cpvPrefijo.map((p) => 'cpv_txt.ilike.* ' + mLimpiaOr(p) + '*').filter(Boolean);
      if (cond.length) q = q.or(cond.join(','));
    }
    // TEXTO libre (objeto + órgano, vía tsv). Sin tildes para casar el tsv unaccent.
    if (n.texto) q = q.textSearch('tsv', mSinTildes(n.texto), { type: 'websearch', config: 'spanish' });
    // ÓRGANO comprador EXACTO (índices órgano + fecha). Para "ver todo lo que compra este órgano".
    if (n.organo) q = q.eq('organo_contratacion', n.organo);
    if (n.impMin !== null) q = q.gte('importe_sin_iva', n.impMin);
    if (n.impMax !== null) q = q.lte('importe_sin_iva', n.impMax);
    if (n.fechaDesde) q = q.gte('fecha_adjudicacion', n.fechaDesde);
    if (n.fechaHasta) q = q.lte('fecha_adjudicacion', n.fechaHasta);
    return q;
  }

  // ¿Hay algún filtro que NO sea el propio importe? Sin ninguno (o solo con importe
  // mínimo y máximo), ordenar por importe es recorrer directamente el índice de importe
  // (0,1 ms medido): se deja en el servidor, sin sonda ni tope.
  function hayFiltros(n) {
    return n.modo === 'nicho' || (n.modo === 'cifs' && n.cifsSeguidos.length > 0)
      || n.cpvPrefijo.length > 0 || !!n.texto || !!n.organo || !!n.fechaDesde || !!n.fechaHasta;
  }

  // ¿Hay MÁS de M_UMBRAL filas con estos filtros? Se pide la fila nº M_UMBRAL + 1 SIN
  // ORDER BY, para que Postgres use el plan más barato (medido: 60 ms-1,9 s en frío).
  async function superaUmbral(n) {
    const r = await aplicar(supabase.from('menores').select('licitacion_id'), n).range(M_UMBRAL, M_UMBRAL);
    if (r.error) return { error: r.error };
    return { supera: Array.isArray(r.data) && r.data.length > 0 };
  }

  // contar(params) -> { total, topado, error }
  // Se pide DESPUÉS de pintar la página: la sonda puede tardar ~2 s en frío y no debe
  // retrasar la lista.
  async function contar(params = {}) {
    const n = normaliza(params);
    const s = await superaUmbral(n);
    if (s.error) return { total: null, topado: false, error: s.error };
    if (s.supera) return { total: M_UMBRAL, topado: true, error: null };
    const r = await aplicar(supabase.from('menores').select('licitacion_id', { count: 'exact', head: true }), n);
    if (r.error || r.count == null) return { total: null, topado: false, error: r.error || new Error('sin conteo') };
    return { total: r.count, topado: false, error: null };
  }

  function paginaServidor(n, desde, hasta) {
    let q = aplicar(supabase.from('menores').select(M_COLUMNAS), n);
    q = q.order(n.ordenCampo, { ascending: n.ordenAsc, nullsFirst: false });
    q = q.order('licitacion_id', { ascending: true });      // desempate ESTABLE
    return q.range(desde, hasta);
  }

  // buscar(params) -> { filas, total, pagina, porPagina, topado, conteoPendiente,
  //                     ordenImporteDesactivado, umbral, error }
  //   total           número exacto, o M_UMBRAL con topado = true («más de 10.000»),
  //                   o null con conteoPendiente = true (la UI llama a contar()).
  //   ordenImporteDesactivado = true: se pidió orden por importe pero con estos filtros
  //                   hay más de `umbral` menores; las filas llegan por fecha (más
  //                   recientes primero) y la UI avisa para acotar.
  async function buscar(params = {}) {
    const n = normaliza(params);
    const { pagina, porPagina } = n;
    const desde = (pagina - 1) * porPagina;
    const hasta = desde + porPagina - 1;
    const resultado = (data, extra) => Object.assign({
      filas: data ?? [], total: null, pagina, porPagina, topado: false,
      conteoPendiente: false, ordenImporteDesactivado: false, umbral: M_UMBRAL, error: null,
    }, extra || {});
    const fallo = (error) => resultado([], { total: 0, error });

    // ---- ORDEN POR IMPORTE CON FILTROS: lo decide el TAMAÑO del resultado ------
    if (n.ordenCampo === 'importe_sin_iva' && hayFiltros(n)) {
      const claveFiltros = JSON.stringify(Object.assign({}, n, { pagina: 0, porPagina: 0 }));
      let lista = mCacheOrden.clave === claveFiltros ? mCacheOrden.lista : null;
      if (!lista) {
        const s = await superaUmbral(n);
        if (s.error) return fallo(s.error);
        if (s.supera) {
          // GRANDE: ordenar por importe obligaría a recorrer la tabla. Página por fecha.
          const nFecha = Object.assign({}, n, { ordenCampo: 'fecha_adjudicacion', ordenAsc: false });
          const r = await paginaServidor(nFecha, desde, hasta);
          if (r.error) return fallo(r.error);
          return resultado(r.data, { total: M_UMBRAL, topado: true, ordenImporteDesactivado: true });
        }
        // PEQUEÑO: clave + importe de todas, paginando por CLAVE y no por offset. Medido:
        // Postgres entra por el índice del filtro y ordena en memoria; nunca recorre la
        // tabla entera (paginar por fecha con offset sí podría, en la última tanda).
        lista = [];
        let ultimo = null;
        for (let vuelta = 0; vuelta <= Math.ceil(M_UMBRAL / M_TANDA); vuelta++) {
          let q = aplicar(supabase.from('menores').select('licitacion_id,importe_sin_iva'), n);
          if (ultimo !== null) q = q.gt('licitacion_id', ultimo);
          const r = await q.order('licitacion_id', { ascending: true }).limit(M_TANDA);
          if (r.error) return fallo(r.error);
          const tanda = r.data || [];
          lista.push(...tanda);
          if (tanda.length < M_TANDA) break;
          ultimo = tanda[tanda.length - 1].licitacion_id;
        }
        lista.sort(mComparaImporte(n.ordenAsc));
        mCacheOrden = { clave: claveFiltros, lista };
      }
      const ids = lista.slice(desde, hasta + 1).map((x) => x.licitacion_id);
      if (!ids.length) return resultado([], { total: lista.length });
      const r = await supabase.from('menores').select(M_COLUMNAS).in('licitacion_id', ids);
      if (r.error) return fallo(r.error);
      const porId = new Map((r.data || []).map((f) => [f.licitacion_id, f]));
      return resultado(ids.map((id) => porId.get(id)).filter(Boolean), { total: lista.length });
    }

    // ---- RESTO: orden en el servidor; recuento exacto o con tope --------------
    const [rDatos, rEstim] = await Promise.all([
      paginaServidor(n, desde, hasta),
      aplicar(supabase.from('menores').select('licitacion_id', { count: 'planned', head: true }), n),
    ]);
    if (rDatos.error) return fallo(rDatos.error);
    const estimado = rEstim.error ? null : (rEstim.count ?? 0);
    if (estimado !== null && estimado < M_UMBRAL) {
      const rExacto = await aplicar(supabase.from('menores').select('licitacion_id', { count: 'exact', head: true }), n);
      if (!rExacto.error && rExacto.count != null) return resultado(rDatos.data, { total: rExacto.count });
    }
    // Estimación grande (o fallida): NO se enseña; la UI llama a contar() tras pintar.
    return resultado(rDatos.data, { conteoPendiente: true });
  }

  return { buscar, contar };
}
