// Pruebas deterministas de menores_api.js con un cliente supabase DE MENTIRA (sin red).
// Ejecutar: node test_menores_api.mjs
//
// El cliente falso registra cada consulta (tabla, select, filtros, orden, rango, límite) y
// la resuelve sobre una tabla en memoria, para comprobar la LÓGICA: cuándo se sondea, cuándo
// se ordena en el navegador, cuándo se desactiva el orden por importe y qué se cuenta.
import { crearMenores } from './menores_api.js';

let ok = 0;
const fallos = [];
function comprueba(nombre, condicion, detalle) {
  if (condicion) ok++;
  else { fallos.push(nombre); console.log('FALLO:', nombre, detalle === undefined ? '' : JSON.stringify(detalle)); }
}

function clienteFalso(filas, opciones = {}) {
  const registro = [];
  function consulta(tabla) {
    const q = { tabla, select: null, filtros: [], orden: [], rango: null, limite: null, head: false, count: null, or: [] };
    const api = {
      select(cols, o = {}) { q.select = cols; q.head = !!o.head; q.count = o.count || null; return api; },
      overlaps(c, v) { q.filtros.push(['ov', c, v]); return api; },
      or(s) { q.or.push(s); return api; },
      eq(c, v) { q.filtros.push(['eq', c, v]); return api; },
      gte(c, v) { q.filtros.push(['gte', c, v]); return api; },
      lte(c, v) { q.filtros.push(['lte', c, v]); return api; },
      gt(c, v) { q.filtros.push(['gt', c, v]); return api; },
      in(c, v) { q.filtros.push(['in', c, v]); return api; },
      textSearch(c, v) { q.filtros.push(['fts', c, v]); return api; },
      order(c, o = {}) { q.orden.push([c, o.ascending !== false]); return api; },
      range(a, b) { q.rango = [a, b]; return api; },
      limit(n) { q.limite = n; return api; },
      then(res, rej) { return Promise.resolve(resuelve(q)).then(res, rej); },
    };
    return api;
  }
  function pasa(f, fila) {
    const [op, c, v] = f;
    if (op === 'eq') return fila[c] === v;
    if (op === 'gte') return fila[c] !== null && fila[c] >= v;
    if (op === 'lte') return fila[c] !== null && fila[c] <= v;
    if (op === 'gt') return fila[c] > v;
    if (op === 'in') return v.includes(fila[c]);
    return true;                       // ov / fts / or: la tabla falsa ya es el resultado filtrado
  }
  function resuelve(q) {
    registro.push(q);
    let r = filas.filter((f) => q.filtros.every((x) => pasa(x, f)));
    if (q.head) {
      if (q.count === 'planned') return { count: opciones.estimado ?? r.length, data: null, error: null };
      return { count: r.length, data: null, error: null };
    }
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

const fila = (i, importe, fecha) => ({ licitacion_id: 'and:' + String(i).padStart(6, '0'), importe_sin_iva: importe, fecha_adjudicacion: fecha, organo_contratacion: 'SAS', fuente: 'andalucia' });

// ---------------------------------------------------------------------------------------
// 1) Orden por importe con filtros y resultado PEQUEÑO: se ordena en el navegador
{
  const filas = [fila(3, 500, '2026-01-03'), fila(1, null, '2026-01-01'), fila(2, 100, '2026-01-02'), fila(4, 100, '2026-01-04')];
  const sb = clienteFalso(filas);
  const api = crearMenores(sb);
  const r = await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', ordenAsc: true });
  comprueba('pequeño: sin error', !r.error, r.error);
  comprueba('pequeño: total exacto sin sondeo extra de conteo', r.total === 4 && !r.topado && !r.conteoPendiente, r);
  comprueba('pequeño: orden asc, empate por clave, nulos al final',
    r.filas.map((f) => f.licitacion_id).join() === 'and:000002,and:000004,and:000003,and:000001', r.filas.map((f) => f.licitacion_id));
  comprueba('pequeño: 1.ª consulta es la sonda de la fila 10.001', JSON.stringify(sb.registro[0].rango) === '[10000,10000]' && sb.registro[0].orden.length === 0, sb.registro[0]);
  const lista = sb.registro.filter((q) => q.select === 'licitacion_id,importe_sin_iva');
  comprueba('pequeño: la lista va por clave con límite 1000', lista.length === 1 && lista[0].orden[0][0] === 'licitacion_id' && lista[0].limite === 1000, lista);
  comprueba('pequeño: la página se pide por clave (in)', sb.registro.some((q) => q.filtros.some((f) => f[0] === 'in')));
  const n = sb.registro.length;
  const r2 = await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', ordenAsc: true, pagina: 1, porPagina: 2 });
  comprueba('pequeño: misma búsqueda, otra página: usa la caché (1 sola consulta)', sb.registro.length === n + 1 && r2.filas.length === 2, sb.registro.length - n);
  const r3 = await api.buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', ordenAsc: false });
  comprueba('pequeño: cambiar el sentido NO reutiliza la caché', r3.filas.map((f) => f.licitacion_id).join() === 'and:000003,and:000002,and:000004,and:000001', r3.filas.map((f) => f.licitacion_id));
}

// 2) Paginación de la lista por clave cuando hay más de 1.000 filas
{
  const filas = Array.from({ length: 2345 }, (_, i) => fila(i, (i * 37) % 1000, '2026-02-01'));
  const sb = clienteFalso(filas);
  const r = await crearMenores(sb).buscar({ fechaDesde: '2026-01-01', ordenCampo: 'importe_sin_iva', ordenAsc: false });
  const tandas = sb.registro.filter((q) => q.select === 'licitacion_id,importe_sin_iva');
  comprueba('2.345 filas: 3 tandas', tandas.length === 3, tandas.length);
  comprueba('tandas 2 y 3 con gt (por clave, no offset)', tandas.slice(1).every((q) => q.filtros.some((f) => f[0] === 'gt')) && tandas.every((q) => q.rango === null), tandas.map((q) => q.filtros));
  comprueba('2.345 filas: total exacto', r.total === 2345, r.total);
  comprueba('2.345 filas: primera página ordenada desc', r.filas.every((f, i) => i === 0 || r.filas[i - 1].importe_sin_iva >= f.importe_sin_iva));
}

// 3) Orden por importe con filtros y resultado GRANDE: se desactiva y va por fecha
{
  const filas = Array.from({ length: 10001 }, (_, i) => fila(i, i, '2026-03-' + String((i % 28) + 1).padStart(2, '0')));
  const sb = clienteFalso(filas);
  const r = await crearMenores(sb).buscar({ organo: 'SAS', ordenCampo: 'importe_sin_iva', ordenAsc: true });
  comprueba('grande: orden desactivado y topado', r.ordenImporteDesactivado === true && r.topado === true && r.total === 10000, r);
  const datos = sb.registro.filter((q) => q.select && q.select.includes('objeto'));
  comprueba('grande: la página va por fecha desc + clave', datos.length === 1 && datos[0].orden[0][0] === 'fecha_adjudicacion' && datos[0].orden[0][1] === false && datos[0].orden[1][0] === 'licitacion_id', datos[0] && datos[0].orden);
  comprueba('grande: no se trae la lista de importes', !sb.registro.some((q) => q.select === 'licitacion_id,importe_sin_iva'));
}

// 4) Orden por importe SIN filtros (o solo con importe): en el servidor, sin sonda
{
  const sb = clienteFalso([fila(1, 5, '2026-01-01')], { estimado: 1700000 });
  const r = await crearMenores(sb).buscar({ ordenCampo: 'importe_sin_iva', ordenAsc: false, importeMin: 1 });
  comprueba('sin filtros: sin sonda', !sb.registro.some((q) => q.rango && q.rango[0] === 10000), sb.registro);
  comprueba('sin filtros: orden por importe en el servidor', sb.registro.some((q) => q.orden[0] && q.orden[0][0] === 'importe_sin_iva'));
  comprueba('estimación grande: conteo pendiente, sin número inventado', r.conteoPendiente === true && r.total === null, r);
}

// 5) Recuento: exacto con estimación pequeña; contar() con tope
{
  const filas = [fila(1, 1, '2026-01-01'), fila(2, 2, '2026-01-02')];
  const sbP = clienteFalso(filas, { estimado: 12 });
  const rP = await crearMenores(sbP).buscar({ organo: 'SAS' });
  comprueba('estimación < 10.000: exacto', rP.total === 2 && !rP.conteoPendiente, rP);
  const sbG = clienteFalso(filas, { estimado: 10074 });
  const apiG = crearMenores(sbG);
  const rG = await apiG.buscar({ organo: 'SAS' });
  comprueba('estimación ≥ 10.000: no se enseña', rG.conteoPendiente === true && rG.total === null, rG);
  const c = await apiG.contar({ organo: 'SAS' });
  comprueba('contar(): sonda vacía → exacto (2, no 10.074)', c.total === 2 && c.topado === false, c);
  const grande = Array.from({ length: 10001 }, (_, i) => fila(i, i, '2026-01-01'));
  const cG = await crearMenores(clienteFalso(grande)).contar({ organo: 'SAS' });
  comprueba('contar(): sonda con fila → «más de 10.000»', cG.total === 10000 && cG.topado === true, cG);
}

// 6) Nicho con exclusiones: la cadena or() exacta, con comillas escapadas
{
  const sb = clienteFalso([]);
  await crearMenores(sb).buscar({
    modo: 'nicho', nichoCpv: ['9073'], nichoKw: 'calidad del aire',
    nichoExcl: [{ kw: 'ventilacion', excluye: ['cpap or "ventilacion no invasiva"'] }, { kw: 'purificador', excluye: ['adn', 'a\\b'] }, { kw: '', excluye: ['x'] }],
  });
  const orDatos = sb.registro[0].or[0];
  comprueba('nicho: cadena or() con and(...) y not entre comillas',
    orDatos === 'cpv_txt.ilike.* 9073*,tsv.wfts(spanish).calidad del aire,and(tsv.wfts(spanish).ventilacion,tsv.not.wfts(spanish)."cpap or \\"ventilacion no invasiva\\""),and(tsv.wfts(spanish).purificador,tsv.not.wfts(spanish)."adn",tsv.not.wfts(spanish)."a\\\\b")',
    orDatos);
  const sb2 = clienteFalso([]);
  await crearMenores(sb2).buscar({ modo: 'nicho', nichoExcl: [{ kw: 'ventilacion', excluye: [] }] });
  comprueba('nicho: palabra con lista vacía entra sin and()', sb2.registro[0].or[0] === 'tsv.wfts(spanish).ventilacion', sb2.registro[0].or[0]);
  const sb3 = clienteFalso([]);
  await crearMenores(sb3).buscar({ modo: 'nicho' });
  comprueba('nicho vacío: imposible, no toda la tabla', sb3.registro[0].filtros.some((f) => f[0] === 'eq' && f[2] === '__sin_nicho__'));
}

console.log(`\n${fallos.length ? 'HAY FALLOS ✘' : 'TODO OK ✔'} (${ok} de ${ok + fallos.length})`);
process.exit(fallos.length ? 1 : 0);
