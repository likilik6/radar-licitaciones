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
//                      vía .or() con ilike + websearch-fts. El nicho (CPV + palabras +
//                      exclusiones) lo construye menores_nicho.py y lo inyecta la web.
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
//  · EL RECUENTO. Nunca se enseña la estimación del planner: se equivoca mucho (el nicho
//    estimaba 10.074 y eran 2.947; «Consejo Superior de Investigaciones Científicas»
//    estimaba 1 y eran 20.838). Se pide DESPUÉS de pintar la página (contar()): exacto, o
//    «más de 10.000» mirando si existe la fila nº 10.001.
//  · EL ORDEN POR IMPORTE CON FILTROS. Con un filtro, Postgres recorre el índice de importe
//    de TODA la tabla mirando fila a fila (SAS por importe: más de 30 s; nicho por importe
//    ascendente: 20,6 s en frío). Si el resultado tiene 10.000 filas o menos, se traen solo
//    clave + importe y se ordena aquí; si tiene más, el orden por importe se desactiva y la
//    UI avisa para acotar. Decide el tamaño del RESULTADO, no el del órgano: «SAS + nicho»
//    (3) o «SAS en marzo» (7.407) sí se ordenan.
const M_UMBRAL = 10000;
const M_TANDA = 1000;                  // tope de filas por petición de PostgREST en Supabase
// PRESUPUESTOS DE TIEMPO (la web corre con statement_timeout de 8 s). Medido en la revisión:
// la sonda de la fila 10.001 con un texto común y un rango de importe («material» y
// ≥ 10.000 €) lee decenas de miles de páginas y pasa de 8 s, y la lista de un filtro que el
// planner sobrestima también puede. Si una petición agota su presupuesto, NO se devuelve un
// error: la página sale por fecha (medido: 54-177 ms en esos casos) con el aviso.
const M_PRESUPUESTO_SONDA_MS = 4500;   // sondas medidas: 60 ms-3,7 s (en frío, 17/09/2026)
// OJO con bajarlo: con 3.000 ms, «Servicio Andaluz de Salud» por importe salía unas veces
// con «más de 10.000» (sonda 2,1 s) y otras con «No se pudo contar» (sonda 3,0 s), según
// la caché del servidor. Contar exacto no sirve de atajo general: mide 894 ms con el SAS
// pero 14,9 s con el texto «salud», donde la sonda tarda 195 ms.
const M_PRESUPUESTO_TANDA_MS = 7000;   // por petición de la lista (bajo los 8 s del rol)
const M_PRESUPUESTO_LISTA_MS = 15000;  // la lista entera (hasta 11 tandas)
const M_PRESUPUESTO_CONTEO_MS = 7000;
const M_PRESUPUESTO_PAGINA_MS = 7000;  // la página del servidor (bajo los 8 s del rol)
// Ordenando por FECHA, la sonda solo sale si la página del servidor tarda más de esto: la
// mayoría de las búsquedas las resuelve el índice del filtro en menos (un CIF, 276 ms), y
// así no se lanza una consulta que no hace falta.
const M_RETRASO_SONDA_MS = 400;
const M_CADUCIDAD_ORDEN_MS = 5 * 60 * 1000;   // la lista ordenada se reutiliza 5 min como mucho
// TAMAÑOS YA SABIDOS (tabla public.menores_cobertura, ver menores_cobertura.sql): cuántos
// menores tiene cada órgano grande. Con eso, «Servicio Andaluz de Salud» ordenado por
// importe enseña el aviso al instante, en vez de tardar 3 s en ir a por la fila nº 10.001.
// Solo sirve para decir «pasa del tope», NUNCA «cabe»: la tabla se refresca al cargar (no
// al minuto) y dar por buena una lista completa con un dato viejo la dejaría coja. El
// margen evita que un órgano justo en el filo baile entre aviso y no aviso.
const M_PISTA_MARGEN = 1.1;
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

// Valor entre comillas para or()/and(), con comillas y barras internas escapadas. Dentro
// de comillas las comas, los paréntesis, los puntos y los dos puntos ya no rompen nada.
// Se usa para las EXCLUSIONES del nicho (sin comillas, PostgREST quita las de un valor
// que es SOLO una frase y el websearch pierde la frase: medido 429 filas en vez de 431) y
// para las claves de la paginación (llevan ':' y '/').
function mValorOr(s) {
  return '"' + String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
}

// Orden hecho en el navegador (resultados pequeños), igual que el del servidor: sin valor al
// final en los dos sentidos (NULLS LAST) y desempate por clave ascendente.
function mComparador(campo, ascendente) {
  const signo = ascendente ? 1 : -1;
  const valor = campo === 'importe_sin_iva' ? (f) => mNumero(f.importe_sin_iva) : (f) => (f.fecha_adjudicacion || null);
  return (a, b) => {
    const va = valor(a);
    const vb = valor(b);
    if (va === null && vb !== null) return 1;
    if (vb === null && va !== null) return -1;
    if (va !== null && vb !== null && va !== vb) return signo * (va < vb ? -1 : 1);
    if (a.licitacion_id < b.licitacion_id) return -1;
    return a.licitacion_id > b.licitacion_id ? 1 : 0;
  };
}

// Ejecuta una consulta de PostgREST con un presupuesto de tiempo. Nunca lanza: devuelve
// { data, count, error, agotado }.
async function mConPresupuesto(consulta, ms) {
  const ctl = typeof AbortController === 'function' ? new AbortController() : null;
  const q = ctl && typeof consulta.abortSignal === 'function' ? consulta.abortSignal(ctl.signal) : consulta;
  let reloj = null;
  const tope = new Promise((resolver) => {
    reloj = setTimeout(() => { if (ctl) ctl.abort(); resolver({ data: null, count: null, error: new Error('tiempo agotado'), agotado: true }); }, ms);
  });
  try {
    const r = await Promise.race([Promise.resolve(q).then((x) => x, (e) => ({ data: null, error: e })), tope]);
    return { data: r.data ?? null, count: r.count ?? null, error: r.error || null, agotado: !!r.agotado };
  } finally {
    clearTimeout(reloj);
  }
}

export function crearMenores(supabase) {
  if (!supabase || typeof supabase.from !== 'function') {
    throw new Error('crearMenores: hay que pasarle el cliente supabase ya inicializado.');
  }

  // DECISIÓN de la ÚLTIMA búsqueda CON FILTROS (por clave de filtros; caduca a los 5 min,
  // porque la ingesta diaria cambia datos). Cambiar de página u orden la reutiliza:
  //   lista        [{licitacion_id, importe_sin_iva, fecha_adjudicacion, fuente}] de un
  //                resultado de 10.000 filas o menos: sus páginas se ordenan aquí.
  //   supera       true: más de M_UMBRAL; false: 10.000 o menos; null: aún no se sabe.
  //   costosa      la sonda agotó su tiempo: no se vuelve a lanzar ni ella ni el recuento
  //                (cortarla en el navegador no la para en el servidor; repetirla lo cargaría).
  //   listaCostosa la lista agotó su tiempo: esas páginas salen del servidor.
  //   servidor     la página del servidor contestó antes que la sonda: las siguientes también
  //                salen de allí, para que el orden de los empates no cambie a mitad.
  //   sonda        la promesa de la sonda (contar() la espera en vez de lanzar otra).
  let mDecision = { clave: null, hora: 0 };
  function decisionDe(clave) {
    const vigente = mDecision.clave === clave && (Date.now() - mDecision.hora) < M_CADUCIDAD_ORDEN_MS;
    if (!vigente) {
      mDecision = { clave, hora: Date.now(), lista: null, supera: null, costosa: false, listaCostosa: false, servidor: false, sonda: null };
    }
    return mDecision;
  }

  // params (todos opcionales):
  //   modo         'todo' | 'nicho' | 'cifs'         (def. 'todo')
  //   nichoCpv     string[]  prefijos CPV del nicho (para modo 'nicho')
  //   nichoKw      string    consulta websearch YA SIN TILDES de las palabras del
  //                          nicho SIN exclusión (p. ej. 'calidad del aire or purificador')
  //   nichoExcl    [{kw, excluye: [websearch, ...]}]  palabras del nicho CON exclusión
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

  // Clave de los FILTROS (sin página, tamaño ni orden): identifica un mismo resultado.
  function claveFiltros(n) {
    return JSON.stringify(Object.assign({}, n, { pagina: 0, porPagina: 0, ordenCampo: '', ordenAsc: false }));
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
  // (medido: 50-850 ms incluso en la página 2.000): se deja en el servidor.
  function hayFiltros(n) {
    return n.modo === 'nicho' || (n.modo === 'cifs' && n.cifsSeguidos.length > 0)
      || n.cpvPrefijo.length > 0 || !!n.texto || !!n.organo || !!n.fechaDesde || !!n.fechaHasta;
  }

  // ¿Hay MÁS de M_UMBRAL filas con estos filtros? Se pide la fila nº M_UMBRAL + 1 SIN
  // ORDER BY, para que Postgres use el plan más barato. -> { supera } o { error, agotado }.
  // Tamaños por órgano que la UI trae de menores_cobertura al abrir la vista.
  let mTamanosOrgano = null;
  function pistas(tamanosPorOrgano) {
    mTamanosOrgano = (tamanosPorOrgano && typeof tamanosPorOrgano === 'object' && !Array.isArray(tamanosPorOrgano))
      ? tamanosPorOrgano : null;
    return !!mTamanosOrgano;
  }

  // ¿Se SABE, sin preguntar a la base, que esto da más de M_UMBRAL menores? Solo cuando el
  // ÚNICO filtro es el órgano: cualquier otro (texto, nicho, CPV, fechas, importe) recorta el
  // resultado y el tamaño del órgano dejaría de valer. En vez de enumerar los filtros a mano
  // —que se quedaría viejo en cuanto se añada uno nuevo— se comprueba que TODO lo demás está
  // vacío: un filtro nuevo apaga la pista solo, que es el lado seguro.
  const M_NO_FILTRAN = new Set(['porPagina', 'pagina', 'ordenCampo', 'ordenAsc', 'organo', 'modo']);
  function soloFiltraOrgano(n) {
    if (n.modo !== 'todo' || !n.organo) return false;
    return Object.keys(n).every((k) => {
      if (M_NO_FILTRAN.has(k)) return true;
      const v = n[k];
      return Array.isArray(v) ? v.length === 0 : (v === null || v === undefined || v === '');
    });
  }
  function pistaGrande(n) {
    if (!mTamanosOrgano || !soloFiltraOrgano(n)) return false;
    if (!Object.prototype.hasOwnProperty.call(mTamanosOrgano, n.organo)) return false;
    const filas = mTamanosOrgano[n.organo];
    return typeof filas === 'number' && filas > M_UMBRAL * M_PISTA_MARGEN;
  }

  async function superaUmbral(n) {
    const r = await mConPresupuesto(
      aplicar(supabase.from('menores').select('licitacion_id'), n).range(M_UMBRAL, M_UMBRAL),
      M_PRESUPUESTO_SONDA_MS);
    if (r.error) return { error: r.error, agotado: r.agotado };
    return { supera: Array.isArray(r.data) && r.data.length > 0 };
  }

  // Clave + importe + fecha de TODAS las filas (≤ M_UMBRAL). Se ordena por (fuente, clave): ningún
  // índice empieza por fuente, así que Postgres NO puede recorrer un índice de orden
  // mirando fila a fila; entra por el índice del filtro y ordena en memoria. Ordenar solo
  // por clave dejaba recorrer la clave primaria entera cuando el planner sobrestimaba
  // («salud» desde abril: estimaba 26.737, había 4.636, y agotaba los 8 s). Se pagina por
  // (fuente, clave), nunca por offset. -> { lista } o { error }.
  async function listaClaves(n) {
    const inicio = Date.now();
    const lista = [];
    let ultimo = null;
    for (let vuelta = 0; vuelta <= Math.ceil(M_UMBRAL / M_TANDA); vuelta++) {
      if (Date.now() - inicio > M_PRESUPUESTO_LISTA_MS) return { error: new Error('tiempo agotado'), agotado: true };
      let q = aplicar(supabase.from('menores').select('licitacion_id,importe_sin_iva,fecha_adjudicacion,fuente'), n);
      if (ultimo !== null) {
        q = q.or('fuente.gt.' + mValorOr(ultimo.fuente) + ',and(fuente.eq.' + mValorOr(ultimo.fuente)
          + ',licitacion_id.gt.' + mValorOr(ultimo.licitacion_id) + ')');
      }
      // Cada tanda se acota a lo que QUEDA de la lista entera: si no, una tanda que arranca
      // en el segundo 14,9 corre sus 7 s por encima y la espera real se va a 22 s.
      const queda = M_PRESUPUESTO_LISTA_MS - (Date.now() - inicio);
      const r = await mConPresupuesto(
        q.order('fuente', { ascending: true }).order('licitacion_id', { ascending: true }).limit(M_TANDA),
        Math.min(M_PRESUPUESTO_TANDA_MS, queda));
      if (r.error) return { error: r.error, agotado: r.agotado };
      const tanda = r.data || [];
      lista.push(...tanda);
      if (tanda.length < M_TANDA) return { lista };
      ultimo = tanda[tanda.length - 1];
    }
    return { lista };
  }

  // contar(params, estimado) -> { total, topado, error }
  // Se pide DESPUÉS de pintar la página. Usa lo que ya sepa la decisión de esos filtros (la
  // lista, la sonda o su coste; si la sonda está en curso, la espera). Si no sabe nada: con
  // la estimación del planner por debajo del umbral, count exacto directo; si no, primero
  // la fila nº 10.001.
  async function contar(params = {}, estimado = null) {
    const n = normaliza(params);
    const d = mDecision.clave === claveFiltros(n) ? mDecision : null;
    if (d && d.sonda && d.supera === null && !d.costosa) await d.sonda;
    // Lo MEDIDO manda sobre la pista, igual que en buscar(): si ya hay lista o sonda de estos
    // filtros, ese número es de ahora; la pista viene de la última carga.
    if (d && d.lista) return { total: d.lista.length, topado: false, error: null };
    if (!d || (d.supera === null && !d.costosa)) {
      if (pistaGrande(n)) return { total: M_UMBRAL, topado: true, error: null };
    }
    if (d && d.costosa) return { total: null, topado: false, error: new Error('recuento demasiado costoso') };
    if (d && d.supera === true) return { total: M_UMBRAL, topado: true, error: null };
    const pequeno = (d && d.supera === false) || (estimado !== null && estimado < M_UMBRAL);
    if (!pequeno) {
      const s = await superaUmbral(n);
      if (s.error) return { total: null, topado: false, error: s.error };
      if (s.supera) return { total: M_UMBRAL, topado: true, error: null };
    }
    const r = await mConPresupuesto(
      aplicar(supabase.from('menores').select('licitacion_id', { count: 'exact', head: true }), n),
      M_PRESUPUESTO_CONTEO_MS);
    if (r.error || r.count == null) return { total: null, topado: false, error: r.error || new Error('sin conteo') };
    if (r.count > M_UMBRAL) return { total: M_UMBRAL, topado: true, error: null };
    return { total: r.count, topado: false, error: null };
  }

  function paginaServidor(n, desde, hasta) {
    let q = aplicar(supabase.from('menores').select(M_COLUMNAS), n);
    q = q.order(n.ordenCampo, { ascending: n.ordenAsc, nullsFirst: false });
    q = q.order('licitacion_id', { ascending: true });      // desempate ESTABLE
    return q.range(desde, hasta);
  }

  // buscar(params) -> { filas, total, pagina, porPagina, topado, conteoPendiente, sinRecuento,
  //                     estimado, ordenImporteDesactivado, motivoOrden, umbral, clave, error }
  //   total             número exacto, o M_UMBRAL con topado = true («más de 10.000»), o
  //                     null con conteoPendiente = true (la UI llama a contar(params, estimado)).
  //   sinRecuento       true si la sonda agotó su tiempo: la UI dice «No se pudo contar» sin
  //                     lanzar otra consulta pesada.
  //   ordenImporteDesactivado = true: se pidió orden por importe y no se puede; las filas
  //                     llegan por fecha (más recientes primero). motivoOrden: 'grande' (más
  //                     de 10.000 menores) o 'costosa' (la sonda o la lista agotaron su tiempo).
  //   clave             identifica los filtros (sin página ni orden): la UI guarda con ella
  //                     el recuento para no repetirlo al cambiar de página.
  //
  // COSTE de una búsqueda con filtros: hasta 13 peticiones y ~1 MB por el cable en el peor
  // caso (página + sonda + 10 tandas de 1.000 claves + hidratación de 25 filas). Cambiar de
  // página con la lista ya en memoria son 1-2 peticiones.
  //
  // CON FILTROS:
  //   · por FECHA la sonda solo sale si la página del servidor tarda más de 400 ms. Si la
  //     sonda dice «10.000 o menos», se trae la lista y se ordena aquí: el servidor podía
  //     tardar mucho («SAS + nicho» por fecha: 12 s en frío recorriendo las 230.608 filas del
  //     SAS por el índice órgano+fecha para encontrar 3).
  //   · por IMPORTE manda la sonda: 10.000 o menos -> lista ordenada aquí; si no, se pide
  //     entonces la página por fecha y sale el aviso.
  async function buscar(params = {}) {
    const n = normaliza(params);
    const { pagina, porPagina } = n;
    const desde = (pagina - 1) * porPagina;
    const hasta = desde + porPagina - 1;
    const clave = claveFiltros(n);
    const resultado = (data, extra) => Object.assign({
      filas: data ?? [], total: null, pagina, porPagina, topado: false, conteoPendiente: false,
      sinRecuento: false, estimado: null, ordenImporteDesactivado: false, motivoOrden: null,
      umbral: M_UMBRAL, clave, error: null,
    }, extra || {});
    const fallo = (error) => resultado([], { total: 0, error });

    // ---- SIN FILTROS (o solo importe): el servidor recorre directamente el índice del orden.
    if (!hayFiltros(n)) {
      const [rDatos, rEstim] = await Promise.all([
        paginaServidor(n, desde, hasta),
        aplicar(supabase.from('menores').select('licitacion_id', { count: 'planned', head: true }), n),
      ]);
      if (rDatos.error) return fallo(rDatos.error);
      return resultado(rDatos.data, { conteoPendiente: true, estimado: rEstim.error ? null : (rEstim.count ?? null) });
    }

    // ---- CON FILTROS ---------------------------------------------------------
    const d = decisionDe(clave);
    // Tamaño ya sabido del órgano: aviso al instante, sin sonda (ver pistaGrande).
    if (d.supera === null && !d.costosa && pistaGrande(n)) d.supera = true;
    const porImporte = n.ordenCampo === 'importe_sin_iva';
    const nServidor = porImporte ? Object.assign({}, n, { ordenCampo: 'fecha_adjudicacion', ordenAsc: false }) : n;

    const paginaAqui = async () => {
      const ids = d.lista.slice().sort(mComparador(n.ordenCampo, n.ordenAsc)).slice(desde, hasta + 1).map((x) => x.licitacion_id);
      if (!ids.length) return resultado([], { total: d.lista.length });
      const r = await supabase.from('menores').select(M_COLUMNAS).in('licitacion_id', ids);
      if (r.error) return fallo(r.error);
      const porId = new Map((r.data || []).map((f) => [f.licitacion_id, f]));
      return resultado(ids.map((id) => porId.get(id)).filter(Boolean), { total: d.lista.length });
    };
    const deServidor = (r) => {
      if (r.error) return fallo(r.error);
      const extra = d.supera === true ? { total: M_UMBRAL, topado: true }
        : d.costosa ? { sinRecuento: true } : { conteoPendiente: true };
      if (porImporte) Object.assign(extra, { ordenImporteDesactivado: true, motivoOrden: d.supera === true ? 'grande' : 'costosa' });
      return resultado(r.data, extra);
    };
    const pideServidor = () => mConPresupuesto(paginaServidor(nServidor, desde, hasta), M_PRESUPUESTO_PAGINA_MS);
    const trataLista = async () => {
      const l = await listaClaves(n);
      if (l.error) { d.listaCostosa = true; return false; }
      d.lista = l.lista;
      return true;
    };

    // 1) Lo ya decidido para estos filtros.
    if (d.lista) return paginaAqui();
    if (d.supera === true || d.costosa || d.listaCostosa || (d.servidor && !porImporte)) return deServidor(await pideServidor());
    if (d.supera === false && porImporte) {
      if (await trataLista()) return paginaAqui();
      return deServidor(await pideServidor());
    }

    // 2) Primera vez. La página del servidor se pide UNA vez y solo cuando hace falta:
    // cortarla en el navegador no la para en el servidor (seguía viva hasta los 8 s del rol),
    // y ordenando por importe casi siempre se acaba usando la lista.
    let pPaginaUna = null;
    const pPaginaFn = () => (pPaginaUna || (pPaginaUna = pideServidor().then((p) => ({ tipo: 'pagina', p }))));
    if (!porImporte) {
      // Por fecha, primero se le deja un momento a la página del servidor: si contesta, ya
      // está (y no se lanza la sonda). El recuento lo pedirá luego la UI con contar().
      const primero = await Promise.race([pPaginaFn(), new Promise((res) => setTimeout(res, M_RETRASO_SONDA_MS, { tipo: 'espera' }))]);
      if (primero.tipo === 'pagina') {
        if (!primero.p.error) { d.servidor = true; return deServidor(primero.p); }
      }
    }
    if (!d.sonda) {
      d.sonda = superaUmbral(n).then((s) => {
        if (mDecision === d) { if (s.error) d.costosa = true; else d.supera = s.supera; }
        return s;
      });
    }
    const pSonda = d.sonda.then((s) => ({ tipo: 'sonda', s }));

    const { s } = await pSonda;
    if (!s.error && !s.supera && await trataLista()) return paginaAqui();
    return deServidor((await pPaginaFn()).p);
  }

  return { buscar, contar, pistas };
}
