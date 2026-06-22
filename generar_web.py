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
"""


def slug(texto):
    """Convierte un texto en algo seguro para usar como clase CSS:
    'a_revisar' -> 'a-revisar'. Lo usamos para dar un color a cada categoría."""
    limpio = "".join(c if c.isalnum() else "-" for c in texto.lower())
    return limpio.strip("-") or "otra"


def construye_tarjeta(lic, es_nueva):
    """Devuelve el HTML (texto) de UNA tarjeta para una licitación."""
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

    # primera_vez la formateamos a DD/MM/YYYY (la generamos nosotros: texto seguro).
    fecha = datetime.fromisoformat(lic["primera_vez"]).strftime("%d/%m/%Y")

    # La etiqueta "NUEVO" solo aparece si la licitación es reciente.
    etiqueta_nuevo = '<span class="tag nuevo">NUEVO</span>' if es_nueva else ""

    return f"""    <article class="card">
      <h2 class="card-title"><a href="{enlace}" target="_blank" rel="noopener">{titulo}</a></h2>
      <div class="tags"><span class="tag cat {categoria_clase}">{categoria_texto}</span>{etiqueta_nuevo}</div>
      <div class="cpv"><span class="et">CPV</span> {cpv_html}</div>
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
# Comparamos SOLO fechas (sin horas): hoy menos DIAS_NUEVO días.
hoy = date.today()
fecha_limite = hoy - timedelta(days=DIAS_NUEVO)

# --- 4. Construimos el cuerpo: las tarjetas, o un mensaje si no hay nada -----
if licitaciones:
    tarjetas = []
    for lic in licitaciones:
        # Tomamos solo la parte de fecha de primera_vez y la comparamos con el límite.
        fecha_primera = datetime.fromisoformat(lic["primera_vez"]).date()
        es_nueva = fecha_primera >= fecha_limite
        tarjetas.append(construye_tarjeta(lic, es_nueva))
    cuerpo = "\n".join(tarjetas)
else:
    cuerpo = ('    <p class="vacio"><span class="emoji">📭</span>'
              'Aún no hay licitaciones guardadas. '
              'Ejecuta <code>filtrar.py</code> para recopilarlas.</p>')

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
    <section id="listado" class="grid">
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
