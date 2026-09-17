// Pruebas deterministas de menores_api.js con un cliente supabase DE MENTIRA (sin red).
// Ejecutar: node test_menores_api.mjs
//
// El cliente falso registra cada consulta (tabla, select, filtros, or, orden, rango, límite)
// y la resuelve sobre una tabla en memoria, para comprobar la LÓGICA: cuándo se sondea,
// cuándo se ordena en el navegador, cuándo se desactiva el orden por importe, qué pasa si
// una consulta falla o agota su tiempo y qué se cuenta.
import { crearMenores } from './menores_api.js';

let ok = 0;
const fallos = [];
function comprueba(nombre, condicion, detalle) {
  if (condicion) ok++;
  else { fallos.push(nombre); console.log('FALLO:', nombre, detalle === undefined ? '' : JSON.stringify(detalle)); }
}

// opciones: { estimado, falla(q) -> bool, cuelga(q) -> bool }
function clienteFalso(filas, opciones = {}) {
  const registro = [];
  function consulta(tabla) {
    const q = { tabla, select: null, filtros: [], orden: [], rango: null, limite: null, head: false, count: null, or: [], abortable: false };
    const api = {
      select(cols, o = {}) { q.select = cols; q.head = !!o.head; q.count = o.count || null; return api; },
      overlaps(c, v) { q.filtros.push(['ov', c, v]); return api; },
      or(s) { q.or.push(s); return api; },
      eq(c, v) { q.filtros.push(['eq', c, v]); return api; },
      gte(c, v) { q.filtros.push(['gte', c, v]); return api; },
      lte(c, v) { q.filtros.push(['lte', c, v]); return api; },
      in(c, v) { q.filtros.push(['in', c, v]); return api; },
      textSearch(c, v) { q.filtros.push(['fts', c, v]); return api; },
      order(c, o = {}) { q.orden.push([c, o.ascending !== false]); return api; },
      range(a, b) { q.rango = [a, b]; return api; },
      limit(n) { q.limite = n; return api; },
      abortSignal() { q.abortable = true; return api; },
      then(res, rej) {
        registro.push(q);
        if (opciones.cuelga && opciones.cuelga(q)) return new Promise(() => {}).then(res, rej);   // nunca contesta
        return Promise.resolve(resuelve(q)).then(res, rej);
      },
    };
    return api;
  }
  const CLAVE = /^fuente\.gt\."(.*)",and\(fuente\.eq\."(.*)",licitacion_id\.gt\."(.*)"\)$/;
  function pasa(q, fila) {
    for (const [op, c, v] of q.filtros) {
      if (op === 'eq' && fila[c] !== v) return false;
      if (op === 'gte' && !(fila[c] !== null && fila[c] >= v)) return false;
      if (op === 'lte' && !(fila[c] !== null && fila[c] <= v)) return false;
      if (op === 'in' && !v.includes(fila[c])) return false;
    }
    for (const s of q.or) {                           // solo se interpreta la paginación por clave
      const m = CLAVE.exec(s);
      if (m && !(fila.fuente > m[1] || (fila.fuente === m[2] && fila.licitacion_id > m[3]))) return false;
    }
    return true;
  }
  function resuelve(q) {
    if (opciones.falla && opciones.falla(q)) return { data: null, count: null, error: { message: 'canceling statement due to statement timeout' } };
    let r = filas.filter((f) => pasa(q, f));
    if (q.head) return { count: q.count === 'planned' ? (opciones.estimado ?? r.length) : r.length, data: null, error: null };
    if (q.orden.length) {
      r = r.slice().sort((a, b) => {
        for (const [c, asc] of q.orden) {
          const va = a[c], vb = b[c];
          if (va === vb) continue;
          if (va === null) return 1;
          if (vb === null) return -1;
          return (va < vb ? -1 : 1) * (asc ? 1 : -1);
        }
        return 0;
      });
    }
    if (q.rango) r = r.slice(q.rango[0], q.rango[1] + 1);
    if (q.limite !== null) r = r.slice(0, q.limite);
    return { data: r, error: null };
  }
  return { from: consulta, registro };
}

const fila = (i, importe, fecha, fuente = 'andalucia') => ({ licitacion_id: (fuente === 'andalucia' ? 'and:' : 'https://x/') + String(i).padStart(6, '0'), importe_sin_iva: importe, fecha_adjudicacion: fecha, organo_contratacion: 'SAS', fuente });
const esLista = (q) => q.select === 'licitacion_id,importe_sin_iva,fecha_adjudicacion,fuente';
const esPaginaServidor = (q) => q.select && q.select.includes('objeto') && !q.filtros.some((f) => f[0] === 'in');
const esSonda = (q) => q.rango && q.rango[0] === 10000;

// ---------------------------------------------------------------------------------------
// 1) Orden por importe con filtros y resultado PEQUEÑO: se ordena en el navegador
{
  const filas = [fila(3, 500, '2026-01-03'), fila(1, null, '2026-01-01'), fila(2, 100, '2026-01-02'), fila(4, 100, '2026-01-04', 'estatal')];
  const sb = clienteFalso(filas);
  const api = crearMenores(sb);
  const r = await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', ordenAsc: true });
  comprueba('pequeño: sin error ni aviso', !r.error && !r.ordenImporteDesactivado, r);
  comprueba('pequeño: total exacto', r.total === 4 && !r.topado && !r.conteoPendiente, r);
  comprueba('pequeño: orden asc, empate por clave, nulos al final',
    r.filas.map((f) => f.licitacion_id).join() === 'and:000002,https://x/000004,and:000003,and:000001', r.filas.map((f) => f.licitacion_id));
  const sonda = sb.registro.find(esSonda);
  comprueba('pequeño: la sonda va sin ORDER BY y con presupuesto', sonda && sonda.orden.length === 0 && sonda.abortable, sonda);
  comprueba('pequeño: por importe, la página por fecha y la sonda salen a la vez (antes de la lista)',
    sb.registro.findIndex(esLista) > sb.registro.findIndex(esSonda), sb.registro.map((q) => q.select));
  const lista = sb.registro.filter(esLista);
  comprueba('pequeño: la lista va por (fuente, clave), con límite y presupuesto',
    lista.length === 1 && lista[0].orden.map((o) => o[0]).join() === 'fuente,licitacion_id' && lista[0].limite === 1000 && lista[0].abortable, lista);
  comprueba('pequeño: la página se pide por clave (in)', sb.registro.some((q) => q.filtros.some((f) => f[0] === 'in')));
  const n = sb.registro.length;
  const r2 = await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', ordenAsc: true, pagina: 2, porPagina: 2 });
  comprueba('pequeño: otra página: caché (1 sola consulta)', sb.registro.length === n + 1 && r2.filas.length === 2, sb.registro.length - n);
  const r3 = await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', ordenAsc: false });
  comprueba('pequeño: el otro sentido reutiliza la lista y ordena bien',
    sb.registro.length === n + 2 && r3.filas.map((f) => f.licitacion_id).join() === 'and:000003,and:000002,https://x/000004,and:000001', r3.filas.map((f) => f.licitacion_id));
  const r4 = await api.buscar({ organo: 'OTRO', ordenCampo: 'importe_sin_iva', ordenAsc: false });
  comprueba('pequeño: filtros distintos no reutilizan la caché', r4.total === 0 && sb.registro.slice(n + 2).some(esSonda), r4);
}

// 2) Caducidad de la caché: a los 5 minutos se vuelve a sondear
{
  const sb = clienteFalso([fila(1, 5, '2026-01-01')]);
  const api = crearMenores(sb);
  const ahora = Date.now;
  let t = 1_000_000;
  Date.now = () => t;
  try {
    await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva' });
    const n = sb.registro.filter(esSonda).length;
    t += 4 * 60 * 1000;
    await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', pagina: 2 });
    comprueba('caché: a los 4 min se reutiliza', sb.registro.filter(esSonda).length === n);
    t += 2 * 60 * 1000;
    await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva' });
    comprueba('caché: a los 6 min caduca y se vuelve a sondear', sb.registro.filter(esSonda).length === n + 1);
  } finally { Date.now = ahora; }
}

// 3) Paginación de la lista por (fuente, clave) con más de 1.000 filas y dos fuentes
{
  const filas = Array.from({ length: 2345 }, (_, i) => fila(i, (i * 37) % 1000, '2026-02-01', i % 3 ? 'andalucia' : 'estatal'));
  const sb = clienteFalso(filas);
  const r = await crearMenores(sb).buscar({ fechaDesde: '2026-01-01', ordenCampo: 'importe_sin_iva', ordenAsc: false });
  const tandas = sb.registro.filter(esLista);
  comprueba('2.345 filas: 3 tandas', tandas.length === 3, tandas.length);
  comprueba('tandas 2 y 3 paginan por (fuente, clave), sin offset', tandas.slice(1).every((q) => q.or.some((s) => s.startsWith('fuente.gt.'))) && tandas.every((q) => q.rango === null), tandas.map((q) => q.or));
  comprueba('2.345 filas: total exacto (ni repetidas ni perdidas)', r.total === 2345, r.total);
  comprueba('2.345 filas: primera página ordenada desc', r.filas.every((f, i) => i === 0 || r.filas[i - 1].importe_sin_iva >= f.importe_sin_iva));
}

// 4) Resultado GRANDE: orden desactivado, página por fecha
{
  const filas = Array.from({ length: 10001 }, (_, i) => fila(i, i, '2026-03-' + String((i % 28) + 1).padStart(2, '0')));
  const sb = clienteFalso(filas);
  const r = await crearMenores(sb).buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', ordenAsc: true });
  comprueba('grande: desactivado, motivo grande, topado', r.ordenImporteDesactivado && r.motivoOrden === 'grande' && r.topado && r.total === 10000, r);
  const datos = sb.registro.filter((q) => q.select && q.select.includes('objeto'));
  comprueba('grande: página por fecha desc + clave', datos.length === 1 && datos[0].orden[0][0] === 'fecha_adjudicacion' && datos[0].orden[0][1] === false, datos[0] && datos[0].orden);
  const antes = sb.registro.filter(esSonda).length;
  comprueba('grande: no se trae la lista', !sb.registro.some(esLista));
}

// 5) La SONDA falla o se cuelga: nunca un error; página por fecha con aviso 'costosa'
{
  const filas = [fila(1, 5, '2026-01-01'), fila(2, 6, '2026-01-02')];
  const sbF = clienteFalso(filas, { falla: esSonda });
  const rF = await crearMenores(sbF).buscar({ texto: 'material', importeMin: 1, ordenCampo: 'importe_sin_iva' });
  comprueba('sonda con error: aviso costosa, sin error, filas por fecha, sin lanzar recuento', !rF.error && rF.ordenImporteDesactivado && rF.motivoOrden === 'costosa' && rF.filas.length === 2 && rF.sinRecuento && !rF.conteoPendiente, rF);
  comprueba('sonda y página por fecha salen A LA VEZ', sbF.registro.length >= 2 && sbF.registro.some(esSonda) && sbF.registro.some(esPaginaServidor), sbF.registro.map((q) => q.select));
  const apiF = crearMenores(sbF);
  await apiF.buscar({ texto: 'material', importeMin: 1, ordenCampo: 'importe_sin_iva' });
  const nF = sbF.registro.filter(esSonda).length;
  const rF2 = await apiF.buscar({ texto: 'material', importeMin: 1, ordenCampo: 'importe_sin_iva', pagina: 2, porPagina: 1 });
  comprueba('costosa: la página siguiente NO repite la sonda', sbF.registro.filter(esSonda).length === nF && rF2.motivoOrden === 'costosa', rF2);
  const sbL = clienteFalso(filas, { falla: esLista });
  const rL = await crearMenores(sbL).buscar({ texto: 'salud', fechaDesde: '2026-01-01', ordenCampo: 'importe_sin_iva' });
  comprueba('lista con error: aviso costosa, sin error, recuento pendiente', !rL.error && rL.ordenImporteDesactivado && rL.motivoOrden === 'costosa' && rL.filas.length === 2 && rL.conteoPendiente, rL);
  const sbC = clienteFalso(filas, { cuelga: esSonda });
  const t0 = Date.now();
  const rC = await crearMenores(sbC).buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva' });
  const ms = Date.now() - t0;
  comprueba('sonda colgada: se corta por presupuesto (~4,5 s) y sale con aviso', !rC.error && rC.motivoOrden === 'costosa' && ms >= 4400 && ms < 6000, { ms, rC });
}

// 6) Orden por importe SIN filtros (o solo importe): en el servidor, sin sonda
{
  const sb = clienteFalso([fila(1, 5, '2026-01-01')], { estimado: 1700000 });
  const r = await crearMenores(sb).buscar({ ordenCampo: 'importe_sin_iva', ordenAsc: false, importeMin: 1 });
  comprueba('sin filtros: sin sonda', !sb.registro.some(esSonda), sb.registro);
  comprueba('sin filtros: orden por importe en el servidor', sb.registro.some((q) => q.orden[0] && q.orden[0][0] === 'importe_sin_iva'));
  comprueba('servidor: el recuento nunca bloquea la página', r.conteoPendiente === true && r.total === null && r.estimado === 1700000, r);
  comprueba('servidor: no se pide count exacto en buscar', !sb.registro.some((q) => q.head && q.count === 'exact'));
}

// 7) contar(): directo si la estimación es pequeña; con sonda si es grande; con tope
{
  const filas = [fila(1, 1, '2026-01-01'), fila(2, 2, '2026-01-02')];
  const sbP = clienteFalso(filas);
  const cP = await crearMenores(sbP).contar({ organo: 'SAS' }, 12);
  comprueba('contar(estimado 12): exacto sin sonda', cP.total === 2 && !cP.topado && !sbP.registro.some(esSonda), cP);
  const sbG = clienteFalso(filas);
  const cG = await crearMenores(sbG).contar({ organo: 'SAS' }, 10074);
  comprueba('contar(estimado 10.074): sonda vacía y exacto (2, no 10.074)', cG.total === 2 && sbG.registro.some(esSonda), cG);
  const grande = Array.from({ length: 10001 }, (_, i) => fila(i, i, '2026-01-01'));
  const cT = await crearMenores(clienteFalso(grande)).contar({ organo: 'SAS' }, null);
  comprueba('contar(): sonda con fila → «más de 10.000»', cT.total === 10000 && cT.topado === true, cT);
  const cS = await crearMenores(clienteFalso(grande)).contar({ organo: 'SAS' }, 5);
  comprueba('contar(): estimación corta pero exacto > 10.000 → topado', cS.total === 10000 && cS.topado === true, cS);
  const cE = await crearMenores(clienteFalso(filas, { falla: esSonda })).contar({ organo: 'SAS' }, 20000);
  comprueba('contar(): sonda con error → error (la UI dice «No se pudo contar»)', cE.error && cE.total === null, cE);
}

// 8) Nicho con exclusiones: la cadena or() exacta, con comillas escapadas
{
  const sb = clienteFalso([]);
  await crearMenores(sb).buscar({
    modo: 'nicho', nichoCpv: ['9073'], nichoKw: 'calidad del aire',
    nichoExcl: [{ kw: 'ventilacion', excluye: ['"cpap" or "ventilacion no invasiva"'] }, { kw: 'purificador', excluye: ['"adn"', 'a\\b'] }, { kw: '', excluye: ['x'] }],
  });
  const orDatos = sb.registro[0].or[0];
  comprueba('nicho: cadena or() con and(...) y not entre comillas',
    orDatos === 'cpv_txt.ilike.* 9073*,tsv.wfts(spanish).calidad del aire,and(tsv.wfts(spanish).ventilacion,tsv.not.wfts(spanish)."\\"cpap\\" or \\"ventilacion no invasiva\\""),and(tsv.wfts(spanish).purificador,tsv.not.wfts(spanish)."\\"adn\\"",tsv.not.wfts(spanish)."a\\\\b")',
    orDatos);
  const sb3 = clienteFalso([]);
  await crearMenores(sb3).buscar({ modo: 'nicho' });
  comprueba('nicho vacío: imposible, no toda la tabla', sb3.registro[0].filtros.some((f) => f[0] === 'eq' && f[2] === '__sin_nicho__'));
}

// 9) La clave de filtros no depende de página ni orden (la UI guarda el recuento con ella)
{
  const api = crearMenores(clienteFalso([fila(1, 1, '2026-01-01')]));
  const a = await api.buscar({ organo: 'SAS', pagina: 1 });
  const b = await api.buscar({ organo: 'SAS', pagina: 3, ordenCampo: 'importe_sin_iva', ordenAsc: true });
  const c = await api.buscar({ organo: 'OTRO' });
  comprueba('clave igual con otra página u orden, distinta con otro filtro', a.clave === b.clave && a.clave !== c.clave, [a.clave, b.clave, c.clave]);
}

// 10) Por FECHA con filtros: gana la página del servidor si contesta antes que la sonda
{
  const filas = [fila(1, 5, '2026-01-01'), fila(2, 6, '2026-01-02')];
  const sb = clienteFalso(filas, { cuelga: (q) => esSonda(q) });
  const api = crearMenores(sb);
  const r = await api.buscar({ organo: 'SAS' });
  comprueba('fecha: página del servidor antes que la sonda: se usa', !r.error && r.filas.length === 2 && r.conteoPendiente && !sb.registro.some(esLista), r);
  const n = sb.registro.filter(esSonda).length;
  await api.buscar({ organo: 'SAS', pagina: 2, porPagina: 1 });
  comprueba('fecha: página siguiente sale del servidor sin otra sonda', sb.registro.filter(esSonda).length === n);
}

// 11) Por FECHA con filtros: la página se cuelga y la sonda dice «pequeño» -> se ordena aquí
{
  const filas = [fila(1, 5, '2026-01-01'), fila(2, 6, '2026-03-02'), fila(3, 7, null), fila(4, 8, '2026-03-02')];
  const sb = clienteFalso(filas, { cuelga: esPaginaServidor });
  const t0 = Date.now();
  const r = await crearMenores(sb).buscar({ organo: 'SAS', modo: 'nicho', nichoKw: 'x', ordenCampo: 'fecha_adjudicacion', ordenAsc: false });
  comprueba('fecha: página colgada + sonda pequeña: lista ordenada aquí sin esperar a la página',
    !r.error && Date.now() - t0 < 2000 && r.total === 4 && r.filas.map((f) => f.licitacion_id).join() === 'and:000002,and:000004,and:000001,and:000003', { ms: Date.now() - t0, ids: r.filas.map((f) => f.licitacion_id) });
  const r2 = await crearMenores(sb).buscar({ organo: 'SAS', modo: 'nicho', nichoKw: 'x', ordenCampo: 'fecha_adjudicacion', ordenAsc: true });
  comprueba('fecha ascendente aquí: nulos al final', r2.filas.map((f) => f.licitacion_id).join() === 'and:000001,and:000002,and:000004,and:000003', r2.filas.map((f) => f.licitacion_id));
}

// 12) contar() espera la sonda ya lanzada por buscar() en vez de lanzar otra
{
  const filas = [fila(1, 5, '2026-01-01')];
  const sb = clienteFalso(filas);
  const api = crearMenores(sb);
  const r = await api.buscar({ organo: 'SAS' });
  const c = await api.contar({ organo: 'SAS' }, r.estimado);
  comprueba('contar(): una sola sonda en total y recuento correcto', sb.registro.filter(esSonda).length === 1 && c.total === 1, { sondas: sb.registro.filter(esSonda).length, c });
}

// === REGRESIÓN (17/09/2026) ==============================================================
// 13) FALLO: la lista de claves se pedía ordenada SOLO por licitacion_id. Cuando el planner
//     sobrestimaba el filtro («salud» desde abril: estimaba 26.737 y había 4.636), Postgres
//     recorría la clave primaria entera y la consulta se iba a los 8 s del rol: peor que
//     antes del cambio. ARREGLO: ordenar por (fuente, licitacion_id) —ningún índice empieza
//     por fuente, así que entra por el índice del filtro— y paginar por clave, sin offset.
{
  const muchas = [];
  for (let i = 1; i <= 1500; i++) muchas.push(fila(i, i, '2026-01-01', i % 2 ? 'andalucia' : 'estatal'));
  const sb = clienteFalso(muchas);
  const r = await crearMenores(sb).buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', ordenAsc: true });
  const listas = sb.registro.filter(esLista);
  comprueba('regresión: ninguna tanda de la lista ordena solo por clave',
    listas.length >= 2 && listas.every((q) => q.orden.length === 2 && q.orden[0][0] === 'fuente' && q.orden[1][0] === 'licitacion_id'),
    listas.map((q) => q.orden));
  comprueba('regresión: ninguna tanda de la lista usa offset', listas.every((q) => q.rango === null), listas.map((q) => q.rango));
  comprueba('regresión: la lista completa se ordena aquí', !r.error && r.total === 1500 && r.filas[0].importe_sin_iva === 1, r.total);
}

// 14) FALLO: si la sonda de la fila 10.001 se eternizaba, se llevaba por delante la búsqueda
//     entera (error en pantalla). ARREGLO: presupuesto de tiempo y plan B por fecha con
//     aviso; la decisión se guarda para no repetir la consulta cara en cada página.
{
  const filas = [fila(1, 5, '2026-01-01'), fila(2, 6, '2026-01-02')];
  const sb = clienteFalso(filas, { cuelga: esSonda });
  const api = crearMenores(sb);
  const r = await api.buscar({ organo: 'SAS', texto: 'salud', ordenCampo: 'importe_sin_iva' });
  comprueba('regresión: sonda eterna -> resultado por fecha, con aviso y SIN error',
    !r.error && r.filas.length === 2 && r.ordenImporteDesactivado && r.motivoOrden === 'costosa' && r.sinRecuento, r);
  const sondas = sb.registro.filter(esSonda).length;
  await api.buscar({ organo: 'SAS', texto: 'salud', ordenCampo: 'importe_sin_iva', pagina: 2 });
  const c = await api.contar({ organo: 'SAS', texto: 'salud' });
  comprueba('regresión: la sonda cara no se repite ni al pasar página ni al contar',
    sb.registro.filter(esSonda).length === sondas && c.error, { sondas, c });
}

// 15) Tamaño del órgano ya sabido (public.menores_cobertura): el aviso sale SIN sonda.
{
  const filas = [fila(1, 5, '2026-01-01'), fila(2, 6, '2026-01-02')];
  const sb = clienteFalso(filas);
  const api = crearMenores(sb);
  api.pistas({ SAS: 230610 });
  const r = await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva' });
  comprueba('pista: órgano grande -> aviso al instante, sin sonda ni lista',
    !r.error && r.ordenImporteDesactivado && r.motivoOrden === 'grande' && r.topado && r.total === 10000
    && !sb.registro.some(esSonda) && !sb.registro.some(esLista), { registro: sb.registro.map((q) => q.select), r });
  const c = await api.contar({ organo: 'SAS' });
  comprueba('pista: contar() tampoco pregunta', c.topado && c.total === 10000 && !sb.registro.some(esSonda), c);
}

// 16) La pista NO se usa si hay cualquier otro filtro: el resultado ya no es el órgano entero.
{
  const filas = [fila(1, 5, '2026-01-01'), fila(2, 6, '2026-01-02')];
  const sb = clienteFalso(filas);
  const api = crearMenores(sb);
  api.pistas({ SAS: 230610 });
  const r = await api.buscar({ organo: 'SAS', texto: 'mascarilla', ordenCampo: 'importe_sin_iva' });
  comprueba('pista: con otro filtro se sondea igual y sale el total exacto',
    !r.error && !r.ordenImporteDesactivado && r.total === 2 && sb.registro.some(esSonda), r);
  // Cualquier otro filtro recorta el resultado: la pista NO puede valer con ninguno.
  const otros = [
    ['fechas', { fechaDesde: '2026-01-02' }],
    ['importe mínimo', { importeMin: 6 }],
    ['importe máximo', { importeMax: 5 }],
    ['CPV', { cpvPrefijo: ['33'] }],
    ['nicho', { modo: 'nicho', nichoKw: 'x' }],
    ['CIF', { modo: 'cifs', cifsSeguidos: ['B86833753'] }],
  ];
  for (const [nombre, extra] of otros) {
    const sbX = clienteFalso(filas);
    const apiX = crearMenores(sbX);
    apiX.pistas({ SAS: 230610 });
    const rX = await apiX.buscar(Object.assign({ organo: 'SAS', ordenCampo: 'importe_sin_iva' }, extra));
    comprueba('pista: con ' + nombre + ' tampoco vale (se sondea)', sbX.registro.some(esSonda) && !rX.topado,
      { nombre, selects: sbX.registro.map((q) => q.select), rX });
  }
}

// 16b) Coherencia buscar/contar: lo MEDIDO manda sobre la pista.
{
  const filas = [fila(1, 5, '2026-01-01'), fila(2, 6, '2026-01-02')];
  const sb = clienteFalso(filas);
  const api = crearMenores(sb);
  const r1 = await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva' });   // mide: 2 filas
  api.pistas({ SAS: 230610 });                                                     // pista vieja y grande
  const c = await api.contar({ organo: 'SAS' });
  comprueba('pista: contar() respeta la lista ya medida en vez de la pista',
    r1.total === 2 && c.total === 2 && !c.topado, { r1: r1.total, c });
}

// 17) Por IMPORTE con resultado pequeño no se pide la página del servidor: esa consulta es
//     la cara (ordena por fecha el filtro entero) y cortarla en el navegador no la para en
//     el servidor, donde seguía viva hasta los 8 s del rol.
{
  const filas = [fila(1, 5, '2026-01-01'), fila(2, 6, '2026-01-02')];
  const sb = clienteFalso(filas);
  const r = await crearMenores(sb).buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', ordenAsc: true });
  comprueba('por importe pequeño: cero páginas del servidor',
    !r.error && r.total === 2 && !sb.registro.some(esPaginaServidor), sb.registro.map((q) => q.select));
}

console.log(`\n${fallos.length ? 'HAY FALLOS ✘' : 'TODO OK ✔'} (${ok} de ${ok + fallos.length})`);
process.exit(fallos.length ? 1 : 0);
