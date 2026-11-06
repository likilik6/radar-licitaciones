// Pruebas del cableado de la RPC en menores_api.js, con un cliente supabase FALSO.
// Sin red y sin base: se comprueba el CONTRATO y, sobre todo, CUÁNTAS peticiones se hacen.
// Ejecutar:  node test_menores_rpc.mjs
//
// POR QUÉ IMPORTA CONTAR LAS PETICIONES: la vista entera se diseñó alrededor de eso. Una
// búsqueda con filtros costaba hasta 14; la RPC tiene que dejarla en 1, y cambiar de página
// dentro de lo ya traído tiene que costar 0.
import { crearMenores } from './menores_api.js';

let OK = 0;
const FALLOS = [];
function comprueba(nombre, condicion, detalle = '') {
  if (condicion) { OK++; } else { FALLOS.push(`${nombre} ${detalle}`); console.log(`FALLO: ${nombre} ${detalle}`); }
}

// --- cliente falso ----------------------------------------------------------------------
// Registra cada petición. Las de tabla devuelven lo que se le diga; rpc() devuelve el json
// que devolvería menores_buscar.
function clienteFalso({ rpc, filas = [], errorRpc = false, errorTabla = false }) {
  const llamadas = [];
  const constructor = (tipo) => {
    const q = {
      select: () => q, in: (_c, ids) => { q._ids = ids; return q; }, eq: () => q, or: () => q,
      overlaps: () => q, textSearch: () => q, gte: () => q, lte: () => q, order: () => q,
      range: () => q, limit: () => q, abortSignal: () => q,
      then: (res) => {
        llamadas.push({ tipo, ids: q._ids });
        if (errorTabla) return Promise.resolve({ data: null, error: new Error('tabla ko') }).then(res);
        const dev = q._ids ? filas.filter((f) => q._ids.includes(f.licitacion_id)) : filas;
        return Promise.resolve({ data: dev, error: null, count: dev.length }).then(res);
      },
    };
    return q;
  };
  return {
    llamadas,
    from: () => constructor('tabla'),
    rpc: (nombre, params) => {
      const q = {
        abortSignal: () => q,
        then: (res) => {
          llamadas.push({ tipo: 'rpc', nombre, params });
          if (errorRpc) return Promise.resolve({ data: null, error: new Error('rpc ko') }).then(res);
          return Promise.resolve({ data: typeof rpc === 'function' ? rpc(params) : rpc, error: null }).then(res);
        },
      };
      return q;
    },
  };
}

// Datos con la MISMA forma que devuelve la RPC: la fecha baja estrictamente con el índice,
// así que el orden del array ES el orden por fecha descendente — igual que el bloque que
// manda la función, que viene ya ordenado. Si se generan fechas cíclicas, el bloque y el
// orden que calcula el navegador no coinciden y el test miente (me pasó).
// El importe, en cambio, va desordenado a propósito: así cambiar de orden obliga a hidratar
// y se prueba ese camino.
const fila = (i) => ({
  licitacion_id: 'id' + String(i).padStart(3, '0'),
  objeto: 'objeto ' + i,
  importe_sin_iva: 1000 + ((i * 7) % 300),
  fecha_adjudicacion: new Date(Date.UTC(2026, 11, 31) - i * 86400000).toISOString().slice(0, 10),
  fuente: 'estatal',
});
const ligera = (f) => ({
  licitacion_id: f.licitacion_id, importe_sin_iva: f.importe_sin_iva,
  fecha_adjudicacion: f.fecha_adjudicacion, fuente: f.fuente,
});

// === 1. NO topado: una sola petición, y la página sale del bloque ======================
{
  const todas = Array.from({ length: 300 }, (_, i) => fila(i));
  const bloque = todas.slice(0, 100);
  const sb = clienteFalso({
    filas: todas,
    rpc: () => ({
      filas: bloque, desde: 0, lista: todas.map(ligera), total: 300, topado: false,
      hay_filtros: true, orden_campo: 'fecha_adjudicacion', orden_asc: false,
      orden_importe_desactivado: false, fuera_de_rango: false, tope: 10000, ms: 150,
    }),
  });
  const api = crearMenores(sb);
  const r = await api.buscar({ organo: 'SAS', pagina: 1 });
  comprueba('1a. una sola petición en la primera búsqueda', sb.llamadas.length === 1,
            JSON.stringify(sb.llamadas.map((l) => l.tipo)));
  comprueba('1b. y es la RPC', sb.llamadas[0].tipo === 'rpc' && sb.llamadas[0].nombre === 'menores_buscar');
  comprueba('1c. 25 filas', r.filas.length === 25, r.filas.length);
  comprueba('1d. total exacto, sin conteo pendiente',
            r.total === 300 && r.conteoPendiente === false && r.topado === false,
            `${r.total}/${r.conteoPendiente}/${r.topado}`);
  comprueba('1e. sin error y sin tiempoAgotado', !r.error && !r.tiempoAgotado);

  // páginas 2, 3 y 4: dentro del bloque de 100 -> CERO peticiones nuevas
  const antes = sb.llamadas.length;
  const r2 = await api.buscar({ organo: 'SAS', pagina: 2 });
  const r3 = await api.buscar({ organo: 'SAS', pagina: 4 });
  comprueba('1f. cambiar de página dentro del bloque: CERO peticiones',
            sb.llamadas.length === antes, JSON.stringify(sb.llamadas.slice(antes).map((l) => l.tipo)));
  comprueba('1g. la página 2 son las filas 26-50', r2.filas[0].licitacion_id === 'id025', r2.filas[0].licitacion_id);
  comprueba('1h. la página 4 son las filas 76-100', r3.filas[0].licitacion_id === 'id075', r3.filas[0].licitacion_id);
  comprueba('1i. el total se mantiene', r2.total === 300 && r3.total === 300);

  // página 5: ya fuera del bloque -> UNA petición de hidratación (no la RPC otra vez)
  const antes2 = sb.llamadas.length;
  const r5 = await api.buscar({ organo: 'SAS', pagina: 5 });
  const nuevas = sb.llamadas.slice(antes2);
  comprueba('1j. página fuera del bloque: UNA petición', nuevas.length === 1, nuevas.length);
  comprueba('1k. y es de tabla (hidratar), no la RPC', nuevas[0] && nuevas[0].tipo === 'tabla');
  comprueba('1l. pide solo las 25 que faltan', nuevas[0] && nuevas[0].ids.length === 25,
            nuevas[0] && nuevas[0].ids.length);
  comprueba('1m. y devuelve 25 filas', r5.filas.length === 25, r5.filas.length);

  // cambiar el ORDEN: se reordena la lista en memoria; lo ya traído no se vuelve a pedir
  const antes3 = sb.llamadas.length;
  const rOrd = await api.buscar({ organo: 'SAS', pagina: 1, ordenCampo: 'importe_sin_iva', ordenAsc: true });
  comprueba('1n. cambiar de orden no llama a la RPC',
            !sb.llamadas.slice(antes3).some((l) => l.tipo === 'rpc'));
  comprueba('1o. el orden nuevo se aplica (importe ascendente)',
            rOrd.filas.length === 25 && rOrd.filas[0].importe_sin_iva <= rOrd.filas[24].importe_sin_iva,
            rOrd.filas.length && `${rOrd.filas[0].importe_sin_iva}..${rOrd.filas[24].importe_sin_iva}`);
}

// === 2. TOPADO: no hay lista, y cada página es una llamada ==============================
{
  const pagina = Array.from({ length: 25 }, (_, i) => fila(i));
  const sb = clienteFalso({
    filas: pagina,
    rpc: (p) => ({
      filas: pagina, desde: (p.p_pagina - 1) * p.p_por_pagina, lista: null, total: 10000,
      topado: true, hay_filtros: true, orden_campo: 'fecha_adjudicacion', orden_asc: false,
      orden_importe_desactivado: p.p_orden_campo === 'importe_sin_iva',
      fuera_de_rango: false, tope: 10000, ms: 60,
    }),
  });
  const api = crearMenores(sb);
  const r = await api.buscar({ organo: 'SAS', pagina: 3 });
  comprueba('2a. una sola petición', sb.llamadas.length === 1);
  comprueba('2b. topado con el tope como total', r.topado === true && r.total === 10000,
            `${r.topado}/${r.total}`);
  comprueba('2c. 25 filas (el bloque devuelto ES la página)', r.filas.length === 25, r.filas.length);
  const r2 = await api.buscar({ organo: 'SAS', pagina: 3, ordenCampo: 'importe_sin_iva' });
  comprueba('2d. pedir importe con resultado topado: la RPC lo desactiva y se refleja',
            r2.ordenImporteDesactivado === true && r2.motivoOrden === 'grande',
            `${r2.ordenImporteDesactivado}/${r2.motivoOrden}`);
}

// === 3. fuera_de_rango: ni filas ni error ==============================================
{
  const sb = clienteFalso({
    rpc: () => ({ filas: [], desde: 24999975, lista: null, total: 10000, topado: true,
                  hay_filtros: false, orden_campo: 'fecha_adjudicacion', orden_asc: false,
                  orden_importe_desactivado: false, fuera_de_rango: true, tope: 10000, ms: 30 }),
  });
  const r = await crearMenores(sb).buscar({ organo: 'SAS', pagina: 1000000 });
  comprueba('3a. sin filas', r.filas.length === 0);
  comprueba('3b. sin error en pantalla', !r.error, r.error && r.error.message);
  comprueba('3c. el total sigue siendo el tope', r.total === 10000);
}

// === 4. Si la RPC falla, se cae a la ruta de siempre ====================================
{
  const pagina = Array.from({ length: 25 }, (_, i) => fila(i));
  const sb = clienteFalso({ filas: pagina, errorRpc: true });
  const api = crearMenores(sb);
  const r = await api.buscar({ organo: 'SAS', pagina: 1 });
  const tipos = sb.llamadas.map((l) => l.tipo);
  comprueba('4a. se intenta la RPC', tipos[0] === 'rpc');
  comprueba('4b. y después se usa la ruta por etapas (hay peticiones de tabla)',
            tipos.slice(1).includes('tabla'), JSON.stringify(tipos));
  comprueba('4c. el usuario recibe filas igualmente', r.filas.length > 0, r.filas.length);
  comprueba('4d. y ningún error en pantalla', !r.error, r.error && r.error.message);
}

// === 5. Los parámetros que se mandan a la RPC ==========================================
{
  let vistos = null;
  const sb = clienteFalso({
    rpc: (p) => { vistos = p; return { filas: [], desde: 0, lista: [], total: 0, topado: false,
      hay_filtros: true, orden_campo: 'fecha_adjudicacion', orden_asc: false,
      orden_importe_desactivado: false, fuera_de_rango: false, tope: 10000, ms: 5 }; },
  });
  await crearMenores(sb).buscar({
    modo: 'nicho', nichoCpv: ['9073', '3981'], nichoKw: 'purificador or ventilacion',
    nichoExcl: [{ kw: 'ventilacion', excluye: ['cpap', 'no invasiva'] }],
    cpvPrefijo: ['90'], texto: 'climatización', organo: 'SAS',
    importeMin: 1000, importeMax: 5000, fechaDesde: '2026-01-01', fechaHasta: '2026-06-30',
    ordenCampo: 'importe_sin_iva', ordenAsc: true, pagina: 2, porPagina: 50,
  });
  comprueba('5a. modo y nicho', vistos.p_modo === 'nicho'
            && JSON.stringify(vistos.p_nicho_cpv) === '["9073","3981"]'
            && vistos.p_nicho_kw === 'purificador or ventilacion');
  comprueba('5b. las exclusiones van como array de objetos (jsonb)',
            Array.isArray(vistos.p_nicho_excl) && vistos.p_nicho_excl[0].kw === 'ventilacion'
            && vistos.p_nicho_excl[0].excluye.length === 2);
  comprueba('5c. el texto va SIN TILDES, como el tsv', vistos.p_texto === 'climatizacion',
            vistos.p_texto);
  comprueba('5d. rangos y órgano', vistos.p_organo === 'SAS' && vistos.p_importe_min === 1000
            && vistos.p_importe_max === 5000 && vistos.p_fecha_desde === '2026-01-01'
            && vistos.p_fecha_hasta === '2026-06-30');
  comprueba('5e. orden y página', vistos.p_orden_campo === 'importe_sin_iva'
            && vistos.p_orden_asc === true && vistos.p_pagina === 2 && vistos.p_por_pagina === 50);
  comprueba('5f. filas completas pedidas', vistos.p_filas_completas === 100, vistos.p_filas_completas);

  // Sin filtros: los parámetros vacíos van como null, NUNCA como '' (un '' en un date o un
  // numeric lo rechaza PostgREST con un 400).
  let v2 = null;
  const sb2 = clienteFalso({ rpc: (p) => { v2 = p; return { filas: [], desde: 0, lista: [], total: 0,
    topado: false, hay_filtros: false, orden_campo: 'fecha_adjudicacion', orden_asc: false,
    orden_importe_desactivado: false, fuera_de_rango: false, tope: 10000, ms: 5 }; } });
  await crearMenores(sb2).buscar({});
  const vacios = ['p_texto', 'p_organo', 'p_fecha_desde', 'p_fecha_hasta', 'p_cifs',
                  'p_nicho_cpv', 'p_nicho_kw', 'p_nicho_excl', 'p_cpv_prefijo'];
  comprueba('5g. lo vacío va como null, nunca como cadena vacía',
            vacios.every((k) => v2[k] === null), JSON.stringify(vacios.filter((k) => v2[k] !== null)));
  comprueba('5h. el rango de importe vacío va como null',
            v2.p_importe_min === null && v2.p_importe_max === null);
}

console.log(`\n${FALLOS.length ? 'HAY FALLOS ✘' : 'TODO OK ✔'} (${OK} de ${OK + FALLOS.length})`);
process.exit(FALLOS.length ? 1 : 0);
