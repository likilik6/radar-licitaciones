# Genera una página web (HTML) con las licitaciones que el filtro ya ha guardado.
# Este script NO descarga ni filtra nada: solo "pinta" lo que hay en
# data/licitaciones.json. Usa solo librería estándar: json, html, pathlib y datetime.
import sys
import json
import html
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo   # para mostrar la hora en la zona horaria de España

# Hacemos que la consola muestre acentos y "ñ" correctamente en Windows.
sys.stdout.reconfigure(encoding="utf-8")

# --- Constantes que puedes cambiar a tu gusto -------------------------------
TITULO_PAGINA = "Radar de licitaciones — LODEPA"
DIAS_NUEVO = 7   # se marca como "NUEVO" lo detectado en los últimos 7 días

# Opciones del menú lateral (el "desplegable" de la izquierda). Iremos añadiendo
# más opciones en el futuro; basta con añadir más diccionarios a esta lista.
OPCIONES_MENU = [
    {"nombre": "Listado de Licitaciones", "enlace": "#listado", "icono": "📋"},
]


# CSS de la página. Va en una cadena NORMAL (no f-string) para que las llaves { }
# de CSS no choquen con las llaves de las f-strings de Python.
CSS = """
  :root {
    --bg:#f1f5f9; --panel:#fff; --texto:#0f172a; --suave:#64748b; --borde:#e2e8f0;
    --acento:#6366f1; --acento-2:#4f46e5;
    --sidebar-bg:#0f172a; --sidebar-tx:#cbd5e1; --sidebar-activo:#6366f1;
  }
  * { box-sizing:border-box; }
  html, body { margin:0; }
  body {
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:var(--bg); color:var(--texto); line-height:1.5;
  }

  /* ---- Menú lateral ---- */
  .sidebar {
    position:fixed; top:0; left:0; bottom:0; width:264px; z-index:50;
    background:var(--sidebar-bg); color:var(--sidebar-tx);
    padding:20px 14px; overflow-y:auto; display:flex; flex-direction:column; gap:16px;
  }
  .brand { display:flex; align-items:center; gap:10px; padding:6px 8px; font-weight:700; color:#fff; }
  .brand .logo {
    width:36px; height:36px; border-radius:10px; flex:0 0 auto;
    display:flex; align-items:center; justify-content:center; font-size:1.15rem;
    background:linear-gradient(135deg,var(--acento),#22d3ee);
  }
  .brand span:last-child { font-size:.98rem; line-height:1.25; }
  .nav-group { border:0; }
  .nav-group > summary {
    list-style:none; cursor:pointer; display:flex; align-items:center; justify-content:space-between;
    padding:8px 10px; border-radius:8px; color:#94a3b8;
    font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.06em;
  }
  .nav-group > summary::-webkit-details-marker { display:none; }
  .nav-group > summary:hover { color:#e2e8f0; }
  .chevron { transition:transform .2s; }
  .nav-group[open] > summary .chevron { transform:rotate(90deg); }
  .nav-lista { display:flex; flex-direction:column; gap:4px; margin-top:6px; }
  .nav-item {
    display:flex; align-items:center; gap:10px; padding:10px 12px; border-radius:9px;
    text-decoration:none; color:var(--sidebar-tx); font-size:.92rem; font-weight:500;
    transition:background .15s, color .15s;
  }
  .nav-item:hover { background:rgba(255,255,255,.06); color:#fff; }
  .nav-item.activo { background:var(--sidebar-activo); color:#fff; box-shadow:0 6px 16px rgba(99,102,241,.35); }
  .nav-icono { font-size:1rem; }

  /* ---- Contenido ---- */
  .contenido { margin-left:264px; min-height:100vh; }
  .wrap { max-width:1100px; margin:0 auto; padding:22px; }
  .barra-superior {
    position:sticky; top:0; z-index:30; display:flex; align-items:center; gap:14px;
    padding:16px 22px; margin:-22px -22px 22px;
    background:rgba(241,245,249,.85); backdrop-filter:blur(8px);
    border-bottom:1px solid var(--borde);
  }
  .menu-toggle {
    display:none; width:42px; height:42px; border-radius:10px; cursor:pointer; font-size:1.2rem;
    align-items:center; justify-content:center;
    border:1px solid var(--borde); background:var(--panel); color:var(--texto);
  }
  .titulos h1 { margin:0; font-size:1.4rem; }
  .meta { margin:2px 0 0; color:var(--suave); font-size:.9rem; }
  .badge {
    display:inline-block; background:var(--acento); color:#fff; font-weight:700;
    font-size:.75rem; padding:1px 9px; border-radius:999px; margin-left:4px;
  }

  /* ---- Tarjetas ---- */
  .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fill,minmax(330px,1fr)); }
  .card {
    background:var(--panel); border:1px solid var(--borde); border-radius:14px;
    padding:18px; display:flex; flex-direction:column; gap:10px;
    box-shadow:0 1px 2px rgba(15,23,42,.04);
    transition:transform .15s, box-shadow .15s, border-color .15s;
  }
  .card:hover { transform:translateY(-3px); box-shadow:0 10px 24px rgba(15,23,42,.10); border-color:#c7d2fe; }
  .card-title { margin:0; font-size:1.05rem; line-height:1.35; }
  .card-title a { color:var(--texto); text-decoration:none; }
  .card-title a:hover { color:var(--acento-2); }
  .tags { display:flex; flex-wrap:wrap; gap:6px; }
  .tag {
    display:inline-block; padding:3px 11px; border-radius:999px;
    font-size:.7rem; font-weight:700; text-transform:uppercase; letter-spacing:.03em;
  }
  .tag.cat { background:#e5e7eb; color:#374151; }      /* color por defecto */
  .tag.cat-criticas { background:#fee2e2; color:#b91c1c; }
  .tag.cat-a-revisar { background:#fef3c7; color:#b45309; }
  .tag.cat-pruebas { background:#e0e7ff; color:#4338ca; }
  .tag.nuevo { background:#dcfce7; color:#15803d; }
  .cpv { font-size:.82rem; color:var(--suave); display:flex; flex-wrap:wrap; gap:5px; align-items:center; }
  .cpv .et { font-weight:600; color:var(--texto); }
  .cpv code { background:#f1f5f9; color:#334155; padding:2px 7px; border-radius:6px; font-size:.78rem; }

  /* ---- Datos económicos y fechas ---- */
  .datos { display:flex; flex-direction:column; gap:5px; font-size:.84rem; margin-top:2px;
           padding-top:10px; border-top:1px solid var(--borde); }
  .dato { display:flex; justify-content:space-between; gap:12px; align-items:baseline; }
  .dato .et-dato { color:var(--suave); }
  .dato .val-dato { color:var(--texto); font-weight:600; text-align:right; }
  .dato .quedan { color:#15803d; font-weight:600; }     /* plazo abierto: verde */
  .dato .vence-hoy { color:#b45309; font-weight:600; }  /* vence hoy: ámbar (urgente) */
  .dato .cerrado { color:#b91c1c; font-weight:600; }    /* plazo cerrado: rojo */

  /* ---- Barra para ordenar las tarjetas ---- */
  .orden-barra { display:flex; align-items:center; gap:10px; margin-bottom:16px; flex-wrap:wrap; }
  .orden-barra label { font-size:.85rem; color:var(--suave); font-weight:600; }
  .orden-barra select {
    font:inherit; font-size:.9rem; padding:8px 12px; border-radius:9px; cursor:pointer;
    border:1px solid var(--borde); background:var(--panel); color:var(--texto);
  }
  .orden-barra select:focus { outline:2px solid var(--acento); outline-offset:1px; }

  .fecha { font-size:.8rem; color:var(--suave); margin-top:auto; }
  .fecha b { color:var(--texto); font-weight:600; }

  /* ---- Mensaje cuando no hay nada ---- */
  .vacio {
    grid-column:1/-1; text-align:center; color:var(--suave);
    background:var(--panel); border:1px dashed var(--borde); border-radius:14px; padding:56px 20px;
  }
  .vacio .emoji { font-size:2.4rem; display:block; margin-bottom:10px; }

  /* ---- Móvil: el menú se oculta y se abre con el botón ☰ ---- */
  @media (max-width:860px) {
    .sidebar { transform:translateX(-100%); transition:transform .25s ease; box-shadow:0 0 40px rgba(0,0,0,.4); }
    .sidebar.abierta { transform:translateX(0); }
    .contenido { margin-left:0; }
    .menu-toggle { display:flex; }
  }
"""


# JavaScript mínimo para abrir/cerrar el menú en móvil. También en cadena normal
# (no f-string) para que sus llaves { } no molesten.
JS = """
  const toggle = document.querySelector('.menu-toggle');
  const sidebar = document.getElementById('sidebar');
  if (toggle && sidebar) {
    // El botón ☰ abre y cierra el menú.
    toggle.addEventListener('click', function (e) {
      e.stopPropagation();
      sidebar.classList.toggle('abierta');
    });
    // Al elegir una opción (en móvil), cerramos el menú.
    sidebar.querySelectorAll('.nav-item').forEach(function (a) {
      a.addEventListener('click', function () { sidebar.classList.remove('abierta'); });
    });
    // Tocar fuera del menú (en móvil) también lo cierra.
    document.addEventListener('click', function (e) {
      if (window.innerWidth <= 860 && sidebar.classList.contains('abierta') &&
          !sidebar.contains(e.target) && !toggle.contains(e.target)) {
        sidebar.classList.remove('abierta');
      }
    });
  }

  // ---- Ordenar las tarjetas en el navegador (no guarda ni envía nada) -------
  const grid = document.getElementById('listado');
  const selectOrden = document.getElementById('orden');
  if (grid && selectOrden) {
    // "Hoy" a medianoche, para contar los días que faltan hasta el fin de plazo.
    const hoy = new Date();
    hoy.setHours(0, 0, 0, 0);

    // Días desde hoy hasta una fecha ISO 'YYYY-MM-DD'. NaN si está vacía o mal.
    function diasHasta(iso) {
      if (!iso) return NaN;
      const f = new Date(iso + 'T00:00:00');
      return isNaN(f) ? NaN : Math.round((f - hoy) / 86400000);
    }
    // Milisegundos de una fecha ISO (para ordenar por fecha). NaN si está vacía.
    function tiempo(iso) {
      if (!iso) return NaN;
      const f = new Date(iso + 'T00:00:00');
      return isNaN(f) ? NaN : f.getTime();
    }

    // Cada criterio devuelve, para una tarjeta: la 'clave' de orden, si está 'vacio'
    // (va al final) y la 'dir'ección (1 ascendente, -1 descendente). 'texto'=comparar
    // como texto (para el nombre).
    const criterios = {
      dias: function (c) {
        const d = diasHasta(c.dataset.finPlazo);
        if (isNaN(d) || d < 0) return { vacio: true };   // sin fecha o cerrada: al final
        return { clave: d, dir: 1 };                     // menos días = más urgente = primero
      },
      pub:     function (c) { const t = tiempo(c.dataset.fechaPub);    return { clave: t, vacio: isNaN(t), dir: -1 }; },
      subida:  function (c) { const t = tiempo(c.dataset.fechaSubida); return { clave: t, vacio: isNaN(t), dir: -1 }; },
      importe: function (c) { const v = parseFloat(c.dataset.importe); return { clave: v, vacio: isNaN(v), dir: -1 }; },
      nombre:  function (c) {
        const t = (c.dataset.titulo || '').trim();
        return { clave: t.toLowerCase(), vacio: !t, dir: 1, texto: true };
      },
    };

    function ordenar(modo) {
      const fn = criterios[modo] || criterios.dias;
      Array.from(grid.querySelectorAll('.card'))
        .map(function (c) { return { c: c, k: fn(c) }; })
        .sort(function (a, b) {
          // Las 'vacías' siempre al final, sin importar la dirección.
          if (a.k.vacio && b.k.vacio) return 0;
          if (a.k.vacio) return 1;
          if (b.k.vacio) return -1;
          const dir = a.k.dir || 1;
          if (a.k.texto) return a.k.clave.localeCompare(b.k.clave, 'es') * dir;
          if (a.k.clave < b.k.clave) return -dir;
          if (a.k.clave > b.k.clave) return dir;
          return 0;
        })
        // appendChild mueve las tarjetas SIN cambiar su visibilidad, así convive con
        // cualquier filtro del menú lateral (solo recoloca, no muestra ni oculta).
        .forEach(function (o) { grid.appendChild(o.c); });
    }

    selectOrden.addEventListener('change', function () { ordenar(selectOrden.value); });
    ordenar(selectOrden.value);   // orden inicial = opción por defecto (días restantes)
  }
"""


def slug(texto):
    """Convierte un texto en algo seguro para usar como clase CSS:
    'a_revisar' -> 'a-revisar'. Lo usamos para dar un color a cada categoría."""
    limpio = "".join(c if c.isalnum() else "-" for c in texto.lower())
    return limpio.strip("-") or "otra"


def formatea_euros(valor):
    """Formatea un importe como euros a la española: 722654.59 -> '722.654,59 €'.
    Devuelve None si no hay valor (para decidir fuera qué mostrar)."""
    if valor is None:
        return None
    # f"{:,.2f}" da el formato anglosajón ("722,654.59"); cambiamos los separadores
    # a la española usando "§" como marca temporal para no pisar un símbolo con otro.
    s = f"{valor:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    return f"{s} €"


def formatea_fecha(iso):
    """Convierte una fecha ISO ('2026-07-23') a DD/MM/YYYY.
    Devuelve None si falta o no se entiende."""
    if not iso:
        return None
    try:
        return date.fromisoformat(iso).strftime("%d/%m/%Y")
    except ValueError:
        return None


def dias_restantes(iso, hoy):
    """Días desde 'hoy' hasta la fecha ISO de fin de plazo ('2026-07-23').
    Positivo si aún falta, 0 si vence hoy, negativo si ya pasó.
    Devuelve None si no hay fecha o no se entiende."""
    if not iso:
        return None
    try:
        return (date.fromisoformat(iso) - hoy).days
    except ValueError:
        return None


def o_guion(texto):
    """Devuelve el texto, o '—' si es None (para celdas sin dato)."""
    return texto if texto is not None else "—"


def construye_tarjeta(lic, es_nueva, hoy):
    """Devuelve el HTML (texto) de UNA tarjeta para una licitación.
    'hoy' es la fecha de hoy en Europe/Madrid, para el contador de días."""
    # Escapamos con html.escape TODO lo que venga del JSON, para no romper el HTML.
    titulo = html.escape(lic.get("titulo", "(sin título)"))
    enlace = html.escape(lic.get("enlace", ""))

    categoria_raw = lic.get("categoria", "")
    categoria_clase = "cat-" + slug(categoria_raw)               # clase para el color
    categoria_texto = html.escape(categoria_raw.replace("_", " "))

    # La lista de CPV: cada código escapado y metido en su propia etiqueta <code>.
    # dict.fromkeys quita CPV repetidos conservando el orden (una licitación puede
    # traer el mismo código dos veces y no queremos pintarlo dos veces).
    cpvs = list(dict.fromkeys(lic.get("cpv", []) or []))
    if cpvs:
        cpv_html = " ".join(f"<code>{html.escape(c)}</code>" for c in cpvs)
    else:
        cpv_html = "—"

    # --- Datos económicos y fechas (ya guardados en el JSON por filtrar.py) ----
    # Los formateamos para mostrarlos. Si un campo es null, mostramos "—" (con
    # o_guion) para que la tarjeta mantenga siempre la misma estructura y quede
    # claro que ese dato no está disponible. Estos textos los generamos nosotros
    # (números y fechas), así que son seguros y no hace falta escaparlos.
    presu_con = o_guion(formatea_euros(lic.get("presupuesto_con_iva")))
    presu_sin = o_guion(formatea_euros(lic.get("presupuesto_sin_iva")))
    valor_est = o_guion(formatea_euros(lic.get("valor_estimado")))
    fecha_pub = o_guion(formatea_fecha(lic.get("fecha_publicacion")))

    # Fin de plazo: a la fecha le añadimos una coletilla según los días que falten.
    #   - aún abierto (>0): "· quedan X días" (verde)
    #   - vence hoy  (=0):  "· vence hoy" (ámbar)
    #   - ya pasó    (<0):  "· cerrado" (rojo)
    #   - sin fecha (None): solo "—", sin coletilla
    fin_plazo_txt = formatea_fecha(lic.get("fecha_fin_plazo"))
    dias = dias_restantes(lic.get("fecha_fin_plazo"), hoy)
    if fin_plazo_txt is None:
        fin_plazo = "—"
    elif dias is None:
        fin_plazo = fin_plazo_txt
    elif dias > 0:
        # "queda 1 día" (singular) / "quedan N días" (plural).
        texto_dias = "queda 1 día" if dias == 1 else f"quedan {dias} días"
        fin_plazo = f'{fin_plazo_txt} · <span class="quedan">{texto_dias}</span>'
    elif dias == 0:
        fin_plazo = f'{fin_plazo_txt} · <span class="vence-hoy">vence hoy</span>'
    else:
        fin_plazo = f'{fin_plazo_txt} <span class="cerrado">· cerrado</span>'

    datos_html = f"""<div class="datos">
        <div class="dato"><span class="et-dato">Presupuesto (con IVA)</span><span class="val-dato">{presu_con}</span></div>
        <div class="dato"><span class="et-dato">Presupuesto (sin IVA)</span><span class="val-dato">{presu_sin}</span></div>
        <div class="dato"><span class="et-dato">Valor estimado</span><span class="val-dato">{valor_est}</span></div>
        <div class="dato"><span class="et-dato">Fin de plazo</span><span class="val-dato">{fin_plazo}</span></div>
        <div class="dato"><span class="et-dato">Publicado</span><span class="val-dato">{fecha_pub}</span></div>
      </div>"""

    # primera_vez la formateamos a DD/MM/YYYY (la generamos nosotros: texto seguro).
    fecha = datetime.fromisoformat(lic["primera_vez"]).strftime("%d/%m/%Y")

    # La etiqueta "NUEVO" solo aparece si la licitación es reciente.
    etiqueta_nuevo = '<span class="tag nuevo">NUEVO</span>' if es_nueva else ""

    # --- Atributos data-* para que el JS pueda ordenar SIN textos formateados ---
    # Valores EN BRUTO; si falta alguno, el atributo queda vacío ("").
    importe_bruto = lic.get("presupuesto_con_iva")
    data_importe = "" if importe_bruto is None else f"{importe_bruto}"
    data_fin_plazo = lic.get("fecha_fin_plazo") or ""        # ISO YYYY-MM-DD
    data_fecha_pub = lic.get("fecha_publicacion") or ""      # ISO YYYY-MM-DD
    # Fecha de subida a la web = primera_vez; nos quedamos con la parte de fecha (ISO).
    try:
        data_fecha_subida = datetime.fromisoformat(lic["primera_vez"]).date().isoformat()
    except (KeyError, ValueError):
        data_fecha_subida = ""
    # data-titulo va dentro de comillas dobles, y 'titulo' ya está escapado: seguro.

    return f"""    <article class="card"
      data-importe="{data_importe}" data-fin-plazo="{data_fin_plazo}"
      data-fecha-pub="{data_fecha_pub}" data-fecha-subida="{data_fecha_subida}"
      data-titulo="{titulo}">
      <h2 class="card-title"><a href="{enlace}" target="_blank" rel="noopener">{titulo}</a></h2>
      <div class="tags"><span class="tag cat {categoria_clase}">{categoria_texto}</span>{etiqueta_nuevo}</div>
      <div class="cpv"><span class="et">CPV</span> {cpv_html}</div>
      {datos_html}
      <div class="fecha">Detectada el <b>{fecha}</b></div>
    </article>"""


# --- 1. Leemos el archivo de datos ------------------------------------------
# Si no existe (o está vacío), seguimos adelante con un diccionario vacío
# para generar igualmente la página con un mensaje de "todavía no hay nada".
ruta_json = Path("data") / "licitaciones.json"
datos = {}
if ruta_json.exists():
    contenido = ruta_json.read_text(encoding="utf-8").strip()
    if contenido:                       # solo si el archivo tiene algo dentro
        datos = json.loads(contenido)

# --- 2. Pasamos los valores a una lista y la ordenamos ----------------------
# De más RECIENTE a más antigua según "primera_vez" (por eso reverse=True).
licitaciones = list(datos.values())
licitaciones.sort(key=lambda lic: datetime.fromisoformat(lic["primera_vez"]), reverse=True)

# --- 3. ¿A partir de qué fecha algo cuenta como "nuevo"? --------------------
# Comparamos SOLO fechas (sin horas). 'hoy' en zona Europe/Madrid (igual que el
# contador de días de las tarjetas), para no usar la hora UTC del runner de Actions.
hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
fecha_limite = hoy - timedelta(days=DIAS_NUEVO)

# Desplegable para ordenar las tarjetas en el navegador (lo rellena el JS).
# El primer <option> es el orden por defecto: días restantes (más urgente primero).
ORDEN_HTML = """    <div class="orden-barra">
      <label for="orden">Ordenar por</label>
      <select id="orden">
        <option value="dias">Días restantes</option>
        <option value="pub">Fecha de publicación</option>
        <option value="nombre">Nombre (A–Z)</option>
        <option value="subida">Fecha de subida a la web</option>
        <option value="importe">Importe</option>
      </select>
    </div>
"""

# --- 4. Construimos el cuerpo: las tarjetas, o un mensaje si no hay nada -----
if licitaciones:
    tarjetas = []
    for lic in licitaciones:
        # Tomamos solo la parte de fecha de primera_vez y la comparamos con el límite.
        fecha_primera = datetime.fromisoformat(lic["primera_vez"]).date()
        es_nueva = fecha_primera >= fecha_limite
        tarjetas.append(construye_tarjeta(lic, es_nueva, hoy))
    cuerpo = "\n".join(tarjetas)
    orden_html = ORDEN_HTML          # solo mostramos el desplegable si hay tarjetas
else:
    cuerpo = ('    <p class="vacio"><span class="emoji">📭</span>'
              'Aún no hay licitaciones guardadas. '
              'Ejecuta <code>filtrar.py</code> para recopilarlas.</p>')
    orden_html = ""

# --- 5. Construimos las opciones del menú lateral ---------------------------
# La primera opción se marca como "activa" (es la que se está viendo).
opciones_html = []
for i, opcion in enumerate(OPCIONES_MENU):
    clase = "nav-item activo" if i == 0 else "nav-item"
    opciones_html.append(
        f'<a class="{clase}" href="{html.escape(opcion["enlace"])}">'
        f'<span class="nav-icono">{opcion["icono"]}</span>'
        f'<span>{html.escape(opcion["nombre"])}</span></a>'
    )
menu_html = "\n        ".join(opciones_html)

# Título de la sección que se está viendo (la primera opción del menú).
titulo_seccion = html.escape(OPCIONES_MENU[0]["nombre"]) if OPCIONES_MENU else "Inicio"

# --- 6. Montamos la página completa -----------------------------------------
# Hora local de España. Usamos ZoneInfo("Europe/Madrid") en vez de la hora del
# sistema (que en GitHub Actions es UTC); así se ajusta solo el horario de
# verano/invierno y la web siempre muestra la hora correcta de aquí.
generado = datetime.now(ZoneInfo("Europe/Madrid")).strftime("%d/%m/%Y %H:%M")
total = len(licitaciones)

# Truco: {CSS}, {JS}, {menu_html}, {cuerpo}... se rellenan con las cadenas de arriba.
# Esta f-string solo ve esos huecos, así que las llaves de dentro de CSS/JS no le molestan.
pagina = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(TITULO_PAGINA)}</title>
<style>{CSS}</style>
</head>
<body>
<aside class="sidebar" id="sidebar">
  <div class="brand"><span class="logo">📡</span><span>{html.escape(TITULO_PAGINA)}</span></div>
  <nav>
    <details open class="nav-group">
      <summary><span>Menú</span><span class="chevron">›</span></summary>
      <div class="nav-lista">
        {menu_html}
      </div>
    </details>
  </nav>
</aside>

<div class="contenido">
  <div class="wrap">
    <div class="barra-superior">
      <button class="menu-toggle" aria-label="Abrir o cerrar el menú">☰</button>
      <div class="titulos">
        <h1>{titulo_seccion}</h1>
        <p class="meta">Generado el {generado} <span class="badge">{total} licitaciones</span></p>
      </div>
    </div>
{orden_html}    <section id="listado" class="grid">
{cuerpo}
    </section>
  </div>
</div>

<script>{JS}</script>
</body>
</html>
"""

# --- 7. Creamos la carpeta docs/ (si no existe) y escribimos el HTML --------
ruta_docs = Path("docs")
ruta_docs.mkdir(parents=True, exist_ok=True)
ruta_salida = ruta_docs / "index.html"
ruta_salida.write_text(pagina, encoding="utf-8")

print(f"OK: pagina generada en {ruta_salida} con {total} licitaciones.")
