# Genera una página web (HTML) con las licitaciones que el filtro ya ha guardado.
# Este script NO descarga ni filtra nada: solo "pinta" lo que hay en
# data/licitaciones.json. Usa solo la librería estándar (json, html, pathlib, datetime).
#
# Reparto de responsabilidades sobre el "estado" de cada licitación:
#   - PÚBLICO  (activa/caducada por fecha): lo calcula ESTE script, como siempre.
#   - PRIVADO  (ganada/perdida/presentada/descartada + estrella favorita): lo aporta
#     el NAVEGADOR leyendo la tabla 'decisiones' de Supabase, y solo tras iniciar
#     sesión (ver el <script type="module">).
import sys
import json
import html
import yaml          # para leer intereses.yaml (semilla del panel de ajustes)
import requests      # para leer la config del radar (dias_nuevo) desde Supabase
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo   # para mostrar la hora en la zona horaria de España

# Hacemos que la consola muestre acentos y "ñ" correctamente en Windows.
sys.stdout.reconfigure(encoding="utf-8")

# --- Constantes que puedes cambiar a tu gusto -------------------------------
TITULO_PAGINA = "Radar de licitaciones — LODEPA"
DIAS_NUEVO = 7   # por defecto: "NUEVO" = detectado en los últimos 7 días (el panel
                 # de ajustes puede cambiar este número; ver lee_dias_nuevo()).

# Conexión a Supabase (la MISMA clave publishable pública que usa el JS del panel).
# Solo la usamos para LEER la config del radar (dias_nuevo); todo lo demás es local.
SUPABASE_URL = "https://uzktrhpgkyctlnqgdsys.supabase.co"
SUPABASE_KEY = "sb_publishable_3J3pFbMlNzu-NUDs1-740g_lu8YsRv_"


def lee_config_radar():
    """Lee la config del radar (Supabase, tabla radar_config). Devuelve el dict de
    config, o {} si no se puede (Supabase caído, tabla vacía, sin red)."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/radar_config",
            params={"id": "eq.1", "select": "config"},
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=20,
        )
        r.raise_for_status()
        filas = r.json()
        if filas and isinstance(filas[0].get("config"), dict):
            return filas[0]["config"]
    except Exception:
        pass
    return {}


def _cpv_activos_de_config(config, defaults):
    """Conjunto de PREFIJOS CPV ACTIVOS según la config del radar: los CPV activos
    de config['categorias'] (términos como texto o {"v","on"}; on=false se ignora);
    o, si no hay categorías en la config, los CPV de intereses.yaml (defaults).
    Devuelve set() (vacío) si no hay ninguno => en ese caso NO se filtra el desplegable."""
    def activos(lista):
        salida = []
        for it in lista or []:
            if isinstance(it, dict):
                if it.get("on", True) and it.get("v"):
                    salida.append(str(it["v"]))
            elif isinstance(it, str) and it.strip():
                salida.append(it.strip())
        return salida
    prefijos = set()
    cats = config.get("categorias") if isinstance(config, dict) else None
    if isinstance(cats, dict) and cats:
        for crit in cats.values():
            if isinstance(crit, dict):
                prefijos.update(activos(crit.get("cpv")))
    else:
        for crit in (defaults or {}).values():
            if isinstance(crit, dict):
                for c in (crit.get("cpv") or []):
                    prefijos.add(str(c))
    return prefijos

# Opciones del menú lateral (el "desplegable" de la izquierda). Iremos añadiendo
# más opciones en el futuro; basta con añadir más diccionarios a esta lista.
OPCIONES_MENU = [
    {"nombre": "Radar", "vista": "radar", "enlace": "#vista-radar", "icono": "📡"},
    # Subapartado de Radar (BG-5): reutiliza el bloque #vista-radar mostrando la vista
    # híbrida de marcadas (JSON + catálogo). 'sub' = sub-item indentado; 'badge' = nº marcadas.
    {"nombre": "En observación", "vista": "observacion", "enlace": "#vista-radar", "icono": "★", "sub": True, "badge": True},
    {"nombre": "Buscador", "vista": "buscador", "enlace": "#vista-buscador", "icono": "🔍"},
    {"nombre": "Cartera", "vista": "cartera", "enlace": "#vista-cartera", "icono": "💼"},
    {"nombre": "Calendario", "vista": "calendario", "enlace": "#vista-calendario", "icono": "📅"},
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
  /* BG-5: sub-item 'En observación' bajo Radar (indentado, estrella ámbar) + badge contador. */
  .nav-item.nav-sub { margin-left:18px; padding-top:8px; padding-bottom:8px; font-size:.88rem; }
  .nav-item.nav-sub .nav-icono { color:#f59e0b; }
  .nav-count {
    margin-left:auto; background:rgba(255,255,255,.16); color:#fff; font-weight:700;
    font-size:.72rem; min-width:20px; text-align:center; padding:1px 7px; border-radius:999px;
  }
  .nav-item.activo .nav-count { background:rgba(255,255,255,.28); }

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
    position:relative; overflow:hidden;   /* para la franja de estado (::after) a la derecha */
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
  /* "y N más": botón que despliega/colapsa los CPV extra de la tarjeta. */
  .cpv-mas {
    font:inherit; font-size:.74rem; font-style:italic; color:var(--acento-2);
    background:none; border:none; padding:0 2px; cursor:pointer; text-decoration:underline;
  }
  .cpv-mas:hover { color:var(--acento); }
  .cpv-mas:focus-visible { outline:2px solid var(--acento); outline-offset:1px; border-radius:4px; }
  /* CPV colapsados: ocultos por [hidden]; al mostrarse fluyen como un <code> más. */
  .cpv-extra:not([hidden]) { display:contents; }

  /* ---- Datos económicos y fechas ---- */
  .datos { display:flex; flex-direction:column; gap:5px; font-size:.84rem; margin-top:2px;
           padding-top:10px; border-top:1px solid var(--borde); }
  .dato { display:flex; justify-content:space-between; gap:12px; align-items:baseline; }
  .dato .et-dato { color:var(--suave); }
  .dato .val-dato { color:var(--texto); font-weight:600; text-align:right; }
  .dato .quedan { color:#15803d; font-weight:600; }     /* plazo abierto: verde */
  .dato .vence-hoy { color:#b45309; font-weight:600; }  /* vence hoy: ámbar (urgente) */
  .dato .cerrado { color:#b91c1c; font-weight:600; }    /* plazo cerrado: rojo */
  /* Coletilla "quedan X días" en el tono fuerte del semáforo (rojo <3, ámbar <7,
     verde >=7). Van DESPUÉS de .quedan/.vence-hoy para ganar por orden de fuente. */
  .dato .urg-tx-roja  { color:#b91c1c; }
  .dato .urg-tx-ambar { color:#b45309; }
  .dato .urg-tx-verde { color:#15803d; }

  /* ---- Pestañas por estado (filtran la vista; privadas, dentro del radar) ---- */
  /* Responsivo: en pantalla estrecha hacen scroll horizontal sin romper el layout. */
  .tabs {
    display:flex; gap:6px; margin-bottom:16px; padding-bottom:4px;
    overflow-x:auto; flex-wrap:nowrap; -webkit-overflow-scrolling:touch;
    scrollbar-width:thin;
  }
  .tab {
    flex:0 0 auto; white-space:nowrap; cursor:pointer;
    font:inherit; font-size:.85rem; font-weight:600;
    padding:8px 14px; border-radius:999px;
    border:1px solid var(--borde); background:var(--panel); color:var(--suave);
    transition:background .15s, color .15s, border-color .15s;
  }
  .tab:hover { color:var(--texto); border-color:#c7d2fe; }
  .tab.activa { background:var(--acento); border-color:var(--acento-2); color:#fff; }
  .tab-count { font-weight:700; opacity:.85; }

  /* Tarjeta oculta por el filtro de pestaña/categoría (más específico que .card). */
  .card.oculta-filtro { display:none; }

  /* ---- Barra para ordenar las tarjetas ---- */
  .orden-barra { display:flex; align-items:center; gap:10px; margin-bottom:16px; flex-wrap:wrap; }
  .orden-barra label { font-size:.85rem; color:var(--suave); font-weight:600; }
  /* BG-5: en el subapartado 'En observación' (#vista-radar.modo-observacion) ocultamos
     los controles PROPIOS del Radar: tablist de estados, filtro CPV y 'Ordenar por'.
     Con clase (no atributo [hidden], que .tabs/.orden-barra {display:flex} pisaban). */
  #vista-radar.modo-observacion #tabs,
  #vista-radar.modo-observacion .orden-barra { display:none !important; }
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
  /* Mensaje discreto cuando la pestaña activa no tiene tarjetas (lo togglea el JS). */
  .vacio-pestana {
    grid-column:1/-1; text-align:center; color:var(--suave);
    font-size:.95rem; padding:40px 20px;
  }

  /* ---- Cortina de login: pantalla centrada cuando NO hay sesión ---- */
  .login-pantalla { min-height:100vh; display:flex; align-items:center; justify-content:center; padding:24px; }
  .login-pantalla[hidden] { display:none; }      /* gana al display:flex de arriba */
  .login-caja {
    width:100%; max-width:380px; display:flex; flex-direction:column; gap:18px;
    background:var(--panel); border:1px solid var(--borde); border-radius:16px;
    padding:28px 24px; box-shadow:0 10px 30px rgba(15,23,42,.08);
  }
  .login-brand { display:flex; align-items:center; gap:10px; font-weight:700; font-size:1.02rem; }
  .login-brand .logo {
    width:40px; height:40px; flex:0 0 auto; border-radius:11px; color:#fff; font-size:1.25rem;
    display:flex; align-items:center; justify-content:center;
    background:linear-gradient(135deg,var(--acento),#22d3ee);
  }
  .login-caja form { display:flex; flex-direction:column; gap:12px; margin:0; }
  .login-caja input {
    font:inherit; font-size:.95rem; padding:11px 13px; border-radius:10px; width:100%;
    border:1px solid var(--borde); background:#fff; color:var(--texto);
  }
  .login-caja input:focus { outline:2px solid var(--acento); outline-offset:1px; }
  .login-caja button {
    font:inherit; font-size:.95rem; font-weight:600; padding:11px 14px; border-radius:10px; cursor:pointer;
    border:1px solid var(--acento-2); background:var(--acento); color:#fff;
  }
  .login-caja button:hover { background:var(--acento-2); }
  .login-caja .auth-error { font-size:.85rem; color:#b91c1c; font-weight:600; }

  /* ---- Estado de sesión (en la barra superior, ya logueado) ---- */
  .conectado { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-left:auto; }
  .conectado-tx { font-size:.9rem; color:var(--texto); }
  .conectado-tx b { color:var(--acento-2); font-weight:700; }
  .conectado .auth-sec {
    font:inherit; font-size:.88rem; font-weight:600; padding:8px 14px; border-radius:9px; cursor:pointer;
    border:1px solid var(--borde); background:var(--panel); color:var(--texto);
  }
  .conectado .auth-sec:hover { background:#e2e8f0; }

  /* ---- Estado manual (badge): PRIVADO, lo pinta el navegador tras login ---- */
  .tag.estado { color:#fff; background:#334155; }   /* fondo por defecto (estado inesperado) */
  .tag.estado-ganada     { background:#15803d; }   /* verde */
  .tag.estado-perdida    { background:#b91c1c; }   /* rojo */
  .tag.estado-presentada { background:#4f46e5; }   /* índigo */
  .tag.estado-descartada { background:#64748b; }   /* gris */

  /* ---- Fondo de estado en TODA la tarjeta + franja mate a la derecha ----------
     (PRIVADO: solo con sesión). Fondo = lavado del color del badge (texto legible);
     franja derecha (::after) = el MISMO color en mate. No tocamos la categoría.
     Se rige por data-estado, que ya mantiene pintaEstado (carga, guardado, limpieza),
     y solo con body.sesion: entra y sale con la sesión SIN lógica nueva. 'activa' sin
     marca no lleva ni fondo ni franja (neutro), para que resalte solo lo marcado.
     'caducada' (estado por fecha, sin marca) usa gris porque data-estado vale
     "caducada" (el estadoBase por fecha que ya conoce cada tarjeta). */
  body.sesion .card[data-estado="ganada"]     { background:#d8f0e1; }   /* verde */
  body.sesion .card[data-estado="perdida"]    { background:#fbdada; }   /* rojo */
  body.sesion .card[data-estado="presentada"] { background:#e3e0fb; }   /* índigo */
  body.sesion .card[data-estado="descartada"] { background:#dfe5ec; }   /* slate */
  body.sesion .card[data-estado="caducada"]   { background:#e9ecf0; }   /* gris muy tenue */

  /* Franja mate a la derecha (overflow:hidden de .card la recorta al redondeo). */
  body.sesion .card[data-estado="ganada"]::after,
  body.sesion .card[data-estado="perdida"]::after,
  body.sesion .card[data-estado="presentada"]::after,
  body.sesion .card[data-estado="descartada"]::after,
  body.sesion .card[data-estado="caducada"]::after {
    content:""; position:absolute; top:0; right:0; bottom:0; width:6px; pointer-events:none;
  }
  body.sesion .card[data-estado="ganada"]::after     { background:#15803d; }
  body.sesion .card[data-estado="perdida"]::after    { background:#b91c1c; }
  body.sesion .card[data-estado="presentada"]::after { background:#4f46e5; }
  body.sesion .card[data-estado="descartada"]::after { background:#64748b; }
  body.sesion .card[data-estado="caducada"]::after   { background:#94a3b8; }

  /* ---- Semáforo de urgencia por días a FIN DE PLAZO (público, por fecha) ------
     rojo <3, ámbar <7, verde >=7 días (lo calcula Python en render: clasifica_urgencia).
     Tiñe el BORDE y un fondo MUY sutil (~7%) de TODA la tarjeta, sin tapar título ni
     importes. Solo en tarjetas 'activas' (sin decisión manual ni caducadas): si hay
     estado privado, su data-estado es otro y manda su color (no lo pisamos). El tono
     fuerte del texto lo pone .urg-tx-* en la coletilla "quedan X días". */
  body.sesion .card[data-estado="activa"].urg-roja  { border-color:#fca5a5; background:rgba(185,28,28,.07); }
  body.sesion .card[data-estado="activa"].urg-ambar { border-color:#fcd34d; background:rgba(180,83,9,.07); }
  body.sesion .card[data-estado="activa"].urg-verde { border-color:#86efac; background:rgba(21,128,61,.07); }

  /* ---- Controles por tarjeta (estado + estrella): SOLO con sesión ---- */
  /* Se hornean ocultos; body.sesion los muestra al iniciar sesión. */
  .card-ctrl { display:none; align-items:center; gap:8px; flex-wrap:wrap; }
  body.sesion .card-ctrl { display:flex; }
  .card-ctrl .ctrl-cap {
    font-size:.72rem; color:var(--suave); text-transform:uppercase; letter-spacing:.04em;
  }
  .card-ctrl .ctrl-estado {
    font:inherit; font-size:.82rem; padding:5px 8px; border-radius:8px; cursor:pointer;
    border:1px solid var(--borde); background:#fff; color:var(--texto);
  }
  .card-ctrl .ctrl-estado:focus { outline:2px solid var(--acento); outline-offset:1px; }
  .ctrl-estrella {
    font-size:1.25rem; line-height:1; padding:2px 4px; border:none; background:none;
    cursor:pointer; color:#cbd5e1; border-radius:6px;   /* vacía (gris) por defecto */
  }
  .ctrl-estrella:hover { color:#f59e0b; }
  .ctrl-estrella.marcada { color:#f59e0b; }              /* llena (ámbar) = favorita */
  .ctrl-estrella:focus-visible { outline:2px solid var(--acento); outline-offset:1px; }
  .ctrl-estrella:disabled, .card-ctrl .ctrl-estado:disabled { opacity:.55; cursor:progress; }
  .card-aviso { width:100%; font-size:.78rem; color:#b91c1c; font-weight:600; }
  .ctrl-detalles {
    flex:0 0 auto; font:inherit; font-size:.78rem; font-weight:600; cursor:pointer;
    padding:5px 10px; border-radius:8px;
    border:1px solid var(--borde); background:var(--panel); color:var(--texto);
  }
  .ctrl-detalles:hover { border-color:#c7d2fe; color:var(--acento-2); }
  .ctrl-detalles:focus-visible { outline:2px solid var(--acento); outline-offset:1px; }

  /* ---- Modal de detalles del contrato (privado, tras login) ---- */
  .modal-fondo {
    position:fixed; inset:0; z-index:100; display:flex; align-items:flex-start; justify-content:center;
    padding:24px; overflow-y:auto; background:rgba(15,23,42,.45);
  }
  .modal-fondo[hidden] { display:none; }
  .modal-caja {
    width:100%; max-width:560px; margin:auto; background:var(--panel);
    border:1px solid var(--borde); border-radius:16px; padding:22px 22px 18px;
    box-shadow:0 20px 50px rgba(15,23,42,.25);
  }
  .modal-cab { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
  .modal-cab h2 { margin:0; font-size:1.15rem; }
  .modal-cerrar {
    flex:0 0 auto; border:none; background:none; cursor:pointer; font-size:1.1rem;
    color:var(--suave); border-radius:8px; padding:4px 8px;
  }
  .modal-cerrar:hover { color:var(--texto); background:#e2e8f0; }
  .modal-sub { margin:2px 0 14px; color:var(--suave); font-size:.85rem; }
  .contrato-form { display:grid; grid-template-columns:1fr 1fr; gap:12px 14px; }
  .contrato-form label {
    display:flex; flex-direction:column; gap:4px;
    font-size:.74rem; color:var(--suave); font-weight:600; text-transform:uppercase; letter-spacing:.03em;
  }
  .contrato-form label.ancho { grid-column:1/-1; }
  .contrato-form input, .contrato-form textarea {
    font:inherit; font-size:.9rem; padding:8px 10px; border-radius:9px; width:100%;
    border:1px solid var(--borde); background:#fff; color:var(--texto); text-transform:none; letter-spacing:normal;
  }
  .contrato-form textarea { resize:vertical; }
  .contrato-form input:focus, .contrato-form textarea:focus { outline:2px solid var(--acento); outline-offset:1px; }
  .modal-pie { grid-column:1/-1; display:flex; align-items:center; gap:10px; margin-top:6px; flex-wrap:wrap; }
  .modal-pie .btn-pri, .modal-pie .btn-sec {
    font:inherit; font-size:.9rem; font-weight:600; padding:9px 16px; border-radius:9px; cursor:pointer;
  }
  .modal-pie .btn-pri { border:1px solid var(--acento-2); background:var(--acento); color:#fff; }
  .modal-pie .btn-pri:hover { background:var(--acento-2); }
  .modal-pie .btn-pri:disabled { opacity:.6; cursor:progress; }
  .modal-pie .btn-sec { border:1px solid var(--borde); background:var(--panel); color:var(--texto); }
  .modal-pie .btn-sec:hover { background:#e2e8f0; }
  /* Botón "Añadir/Actualizar a cartera" (acción positiva; verde). */
  .modal-pie .btn-cartera { border-color:#86efac; color:#15803d; }
  .modal-pie .btn-cartera:hover:not(:disabled) { background:#dcfce7; border-color:#4ade80; }
  .modal-pie .btn-cartera:disabled { opacity:.5; cursor:not-allowed; }

  /* ---- Panel de ajustes del radar (⚙️) ---- */
  .ajustes-btn { font-size:1rem; line-height:1; padding:6px 9px; }
  .aj-caja { max-width:720px; }
  .aj-sec { border-top:1px solid var(--borde); margin-top:14px; padding-top:12px; }
  .aj-sec:first-of-type { border-top:none; margin-top:6px; }
  .aj-h { margin:0 0 4px; font-size:1rem; }
  .aj-ayuda { margin:0 0 10px; color:var(--suave); font-size:.78rem; }
  .aj-grupo { margin-bottom:14px; }
  .aj-grupo-tit { font-weight:700; font-size:.82rem; text-transform:uppercase; letter-spacing:.03em; margin-bottom:6px; }
  .aj-sub2 { color:var(--suave); font-size:.72rem; font-weight:600; text-transform:uppercase; letter-spacing:.03em; margin:8px 0 4px; }
  .aj-chips { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
  .aj-chips:empty::after { content:'(ninguno)'; color:var(--suave); font-size:.8rem; }
  .aj-chip {
    display:inline-flex; align-items:center; gap:6px; font-size:.82rem; padding:4px 9px;
    border-radius:999px; border:1px solid var(--borde); background:#fff; cursor:pointer; user-select:none;
  }
  .aj-chip.off { opacity:.45; text-decoration:line-through; }
  .aj-chip .x { color:var(--suave); font-weight:700; padding:0 1px; }
  .aj-chip .x:hover { color:#b91c1c; }
  .aj-add { display:flex; gap:6px; margin-top:6px; flex-wrap:wrap; }
  .aj-add input { font:inherit; font-size:.82rem; padding:6px 9px; border-radius:8px; border:1px solid var(--borde); background:#fff; }
  .aj-add input.cpv { min-width:min(320px, 100%); }
  .aj-add button { font:inherit; font-size:.82rem; padding:6px 11px; border-radius:8px; border:1px solid var(--borde); background:var(--panel); cursor:pointer; }
  .aj-add button:hover { background:#e2e8f0; }
  .aj-bloque { margin-bottom:10px; }
  .aj-et { display:block; font-size:.72rem; color:var(--suave); font-weight:600; text-transform:uppercase; letter-spacing:.03em; margin-bottom:4px; }
  .aj-checks { display:flex; flex-wrap:wrap; gap:6px 14px; }
  .aj-checks label { display:inline-flex; align-items:center; gap:5px; font-size:.85rem; }
  .aj-linea { display:flex; align-items:center; gap:10px; font-size:.88rem; margin:8px 0; flex-wrap:wrap; }
  .aj-linea select, .aj-linea input[type=number] { font:inherit; font-size:.85rem; padding:6px 9px; border-radius:8px; border:1px solid var(--borde); background:#fff; }
  .aj-msg { font-size:.82rem; font-weight:600; margin-right:auto; }
  .aj-msg.ok { color:#15803d; } .aj-msg.err { color:#b91c1c; }
  /* Hueco de feedback SIEMPRE presente (aunque los mensajes estén ocultos): fija los
     botones a la derecha para que no salten al aparecer "Guardado ✓"/"Añadido ✓". */
  .pie-feedback { margin-right:auto; display:inline-flex; align-items:center; gap:10px; min-height:1.1em; }
  .contrato-aviso { font-size:.82rem; color:#b91c1c; font-weight:600; }
  .contrato-ok { font-size:.82rem; color:#15803d; font-weight:700; }
  @media (max-width:560px) { .contrato-form { grid-template-columns:1fr; } }

  /* ---- Documentos (PDF) dentro del modal: privados, vía Storage tras login ---- */
  .docs-sec { margin-top:18px; padding-top:16px; border-top:1px solid var(--borde); }
  .docs-titulo { margin:0 0 10px; font-size:.95rem; }
  .docs-lista { list-style:none; margin:0 0 10px; padding:0; display:flex; flex-direction:column; gap:6px; }
  .doc-item {
    display:flex; align-items:center; justify-content:space-between; gap:10px;
    padding:7px 10px; border:1px solid var(--borde); border-radius:9px; background:#fff; font-size:.85rem;
  }
  .doc-meta { color:var(--texto); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .doc-acciones { display:flex; gap:6px; flex:0 0 auto; }
  .doc-btn {
    font:inherit; font-size:.78rem; font-weight:600; cursor:pointer; padding:4px 9px; border-radius:7px;
    border:1px solid var(--borde); background:var(--panel); color:var(--texto);
  }
  .doc-btn:hover { border-color:#c7d2fe; color:var(--acento-2); }
  .doc-btn.doc-borrar:hover { border-color:#fca5a5; color:#b91c1c; }
  .docs-vacio { margin:0 0 10px; color:var(--suave); font-size:.85rem; }
  .docs-form { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .docs-form input[type=file] { font:inherit; font-size:.82rem; flex:1 1 180px; min-width:0; }
  .docs-form select {
    font:inherit; font-size:.85rem; padding:7px 9px; border-radius:8px; cursor:pointer;
    border:1px solid var(--borde); background:#fff; color:var(--texto);
  }
  .docs-form .btn-pri {
    font:inherit; font-size:.85rem; font-weight:600; cursor:pointer; padding:7px 14px; border-radius:8px;
    border:1px solid var(--acento-2); background:var(--acento); color:#fff;
  }
  .docs-form .btn-pri:hover { background:var(--acento-2); }
  .docs-form .btn-pri:disabled { opacity:.6; cursor:progress; }

  /* ---- Vistas Radar / Cartera (se alternan desde el lateral) ---- */
  #vista-radar[hidden], #vista-cartera[hidden], #vista-buscador[hidden] { display:none; }
  /* ---- Buscador general (BG-4) ---- */
  .bg-gate { background:var(--panel); border:1px solid var(--borde); border-radius:12px; padding:22px; color:var(--suave); text-align:center; }
  .bg-barra { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:14px; }
  .bg-input { flex:1 1 320px; min-width:240px; padding:11px 13px; border:1px solid var(--borde); border-radius:9px; font:inherit; background:var(--panel); color:var(--texto); }
  .bg-input:focus { outline:none; border-color:var(--acento); box-shadow:0 0 0 3px rgba(99,102,241,.15); }
  .bg-sel { padding:10px 12px; border:1px solid var(--borde); border-radius:9px; font:inherit; background:var(--panel); color:var(--texto); cursor:pointer; }
  .bg-mas-btn { padding:10px 14px; border:1px solid var(--borde); border-radius:9px; font:inherit; font-weight:600; background:var(--panel); color:var(--acento-2); cursor:pointer; white-space:nowrap; }
  .bg-mas-btn:hover { border-color:var(--acento); }
  .bg-mas-btn[aria-expanded="true"] { background:var(--acento); color:#fff; border-color:transparent; }
  /* Filtros rápidos (pills) */
  .bg-pills { display:flex; flex-wrap:wrap; align-items:center; gap:10px 18px; margin-bottom:12px; }
  .bg-pill-grupo { display:inline-flex; align-items:center; gap:6px; flex-wrap:wrap; }
  .bg-pill-et { font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.04em; color:var(--suave); margin-right:2px; }
  .bg-pill { padding:6px 13px; border:1px solid var(--borde); border-radius:999px; font:inherit; font-size:.84rem; background:var(--panel); color:var(--texto); cursor:pointer; transition:background .12s,color .12s,border-color .12s; }
  .bg-pill:hover { border-color:var(--acento); color:var(--acento-2); }
  .bg-pill.activo { background:var(--acento); border-color:transparent; color:#fff; font-weight:600; }
  .bg-orden-lbl { display:inline-flex; align-items:center; gap:6px; font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.04em; color:var(--suave); }
  .bg-orden-lbl .bg-sel { font-size:.84rem; text-transform:none; font-weight:400; color:var(--texto); }
  /* Panel avanzado (colapsable): CPV / importe / fechas */
  .bg-avanzado { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:14px 20px; padding:16px; margin-bottom:14px; border:1px solid var(--borde); border-radius:11px; background:var(--bg); }
  .bg-campo { display:flex; flex-direction:column; gap:6px; }
  .bg-campo > label { font-size:.78rem; font-weight:700; color:var(--suave); }
  .bg-input-sm { padding:8px 10px; border:1px solid var(--borde); border-radius:8px; font:inherit; font-size:.88rem; background:var(--panel); color:var(--texto); min-width:0; }
  .bg-input-sm:focus { outline:none; border-color:var(--acento); box-shadow:0 0 0 3px rgba(99,102,241,.15); }
  .bg-cpv-fila { display:flex; gap:8px; }
  .bg-cpv-fila .bg-input-sm { flex:1 1 auto; }
  .bg-mini-btn { padding:8px 12px; border:1px solid var(--borde); border-radius:8px; font:inherit; font-size:.84rem; font-weight:600; background:var(--panel); color:var(--acento-2); cursor:pointer; white-space:nowrap; }
  .bg-mini-btn:hover { border-color:var(--acento); }
  .bg-hint { font-size:.76rem; color:var(--suave); }
  .bg-rango { display:flex; align-items:center; gap:8px; }
  .bg-rango .bg-input-sm { flex:1 1 0; }
  .bg-rango-sep { color:var(--suave); }
  /* Chips de filtros activos (removibles) */
  .bg-chips { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
  .bg-chip { display:inline-flex; align-items:center; gap:7px; padding:5px 6px 5px 12px; border-radius:999px; font-size:.82rem; background:rgba(99,102,241,.10); border:1px solid rgba(99,102,241,.28); color:var(--acento-2); }
  .bg-chip-x { border:0; background:rgba(99,102,241,.16); color:var(--acento-2); width:18px; height:18px; border-radius:999px; cursor:pointer; font-size:.9rem; line-height:1; display:inline-flex; align-items:center; justify-content:center; padding:0; }
  .bg-chip-x:hover { background:var(--acento); color:#fff; }
  .bg-cabecera { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; }
  .bg-contador { font-size:.9rem; color:var(--suave); }
  .bg-contador b { color:var(--acento); }
  .bg-limpiar { border:0; background:none; color:var(--acento-2); font:inherit; font-size:.84rem; font-weight:600; cursor:pointer; text-decoration:underline; white-space:nowrap; }
  .bg-limpiar:hover { color:var(--acento); }
  .bg-msg { color:var(--suave); font-size:.92rem; padding:8px 0 14px; }
  .bg-card .bg-org { font-size:.82rem; color:var(--suave); margin:2px 0 8px; }
  .bg-card .bg-meta { font-size:.82rem; color:var(--texto); margin-bottom:8px; }
  .bg-card .bg-fuente { text-transform:capitalize; font-weight:600; }
  .bg-card .bg-plazo { font-size:.82rem; color:var(--suave); border-top:1px solid var(--borde); padding-top:9px; margin-top:4px; }
  .bg-card .bg-plazo .et { color:var(--suave); }
  .bg-card .bg-exp { font-size:.82rem; color:var(--suave); margin-bottom:8px; }
  .bg-card .bg-exp .et { color:var(--suave); }
  .bg-exp-input { min-width:190px; }
  #bg-resultados .card.urg-roja  { border-color:#fca5a5; background:rgba(185,28,28,.06); }
  #bg-resultados .card.urg-ambar { border-color:#fcd34d; background:rgba(180,83,9,.06); }
  #bg-resultados .card.urg-verde { border-color:#86efac; background:rgba(21,128,61,.05); }
  /* BG-5: estrella «En observación» en los resultados del buscador (arriba a la derecha). */
  .bg-card .bg-card-ctrl { justify-content:flex-end; margin-bottom:2px; }
  /* BG-5: tarjeta de CATÁLOGO hidratada en 'En observación' (origen buscador). */
  .tag.cat-catalogo { background:#e0f2fe; color:#075985; }
  .cat-org { font-size:.82rem; color:var(--suave); margin:2px 0 8px; word-break:break-word; }
  .cat-hueco { font-size:.85rem; color:var(--suave); margin:6px 0; }
  .bg-pag { display:flex; align-items:center; justify-content:center; gap:14px; margin:20px 0 8px; }
  /* El atributo [hidden] no basta contra `.bg-pag{display:flex}` (author gana al UA);
     con esta regla (especificidad 0,2,0 > 0,1,0) bgPag.hidden=true oculta de verdad la
     paginación y no queda un "Página 1 de N" heredado en la vista vacía / de error. */
  .bg-pag[hidden] { display:none; }
  .bg-pag-btn { font:inherit; font-size:.86rem; font-weight:600; cursor:pointer; padding:8px 14px; border-radius:8px; border:1px solid var(--borde); background:var(--panel); color:var(--texto); }
  .bg-pag-btn:hover:not(:disabled) { border-color:var(--acento); color:var(--acento-2); }
  .bg-pag-btn:disabled { opacity:.45; cursor:default; }
  .bg-pag-info { font-size:.86rem; color:var(--suave); }
  #cartera-contenido { overflow-x:auto; }            /* tabla ancha: scroll horizontal */
  #cartera-contenido > p { margin:0 0 14px; color:var(--suave); font-size:.92rem; }
  #cartera-contenido > p strong { color:var(--texto); }
  .cartera-tabla { width:100%; border-collapse:collapse; font-size:.85rem; }
  .cartera-tabla th, .cartera-tabla td {
    padding:8px 10px; text-align:left; border-bottom:1px solid var(--borde); vertical-align:top;
  }
  .cartera-tabla th {
    color:var(--suave); font-weight:700; font-size:.72rem; text-transform:uppercase;
    letter-spacing:.03em; white-space:nowrap;
  }
  .cartera-tabla tbody tr:hover { background:#f8fafc; }
  .cartera-tabla .fila-resuelto { opacity:.5; }      /* estado "Resuelto": atenuada */
  /* Badge 📡 en filas de cartera enlazadas a un contrato (abre el modal Detalles). */
  .cart-radar-badge {
    font:inherit; font-size:.82rem; line-height:1; cursor:pointer; vertical-align:middle;
    padding:1px 6px; margin-left:6px; border-radius:6px;
    border:1px solid #bfdbfe; background:#eff6ff;
  }
  .cart-radar-badge:hover { background:#dbeafe; border-color:#93c5fd; }
  /* Botón "Docs" de cada fila de la cartera (abre el modal de documentos). */
  .cart-docs-btn {
    font:inherit; font-size:.78rem; font-weight:600; cursor:pointer; padding:4px 10px; border-radius:7px;
    border:1px solid var(--borde); background:var(--panel); color:var(--texto); white-space:nowrap;
  }
  .cart-docs-btn:hover { border-color:#c7d2fe; color:var(--acento-2); }
  /* Sección de documentos cuando va suelta (modal de cartera, sin contrato encima). */
  .docs-sec-suelta { margin-top:4px; padding-top:0; border-top:0; }

  /* Tabla de cartera compacta + chips + detalle en el modal. */
  .cartera-tabla td, .cartera-tabla th{ padding:.5rem .6rem; }
  .cartera-tabla td.num, .cartera-tabla th.num{ text-align:right; white-space:nowrap; }
  .cartera-tabla tbody tr{ cursor:pointer; }
  .chip{ display:inline-block; padding:.1rem .5rem; border-radius:999px; font-size:.75rem; background:#eef2ff; color:#3730a3; }
  #cdoc-datos{ margin:4px 0 6px; }
  .det-fila{ display:flex; gap:.5rem; padding:.25rem 0; border-bottom:1px solid #f1f5f9; }
  .det-et{ flex:0 0 42%; color:var(--suave); font-size:.85rem; } .det-val{ flex:1; word-break:break-word; }
  .cdoc-anot{ margin-top:14px; }
  #cdoc-anotaciones{ width:100%; min-height:70px; margin:.4rem 0; box-sizing:border-box; font:inherit; font-size:.9rem;
    padding:8px 10px; border-radius:9px; border:1px solid var(--borde); color:var(--texto); resize:vertical; }
  .cdoc-anot .btn-pri{ font:inherit; font-size:.85rem; font-weight:600; cursor:pointer; padding:7px 14px; border-radius:8px;
    border:1px solid var(--acento-2); background:var(--acento); color:#fff; }
  .cdoc-anot .btn-pri:hover{ background:var(--acento-2); }
  .cdoc-anot .btn-pri:disabled{ opacity:.6; cursor:progress; }
  /* Columna destacada "Fin de vigencia": el dato que más resalta. */
  .cartera-tabla .col-venc { white-space:nowrap; }
  .venc { display:inline-block; padding:4px 9px; border-radius:8px; line-height:1.2; }
  .venc strong { font-size:.88rem; }
  .venc small { font-size:.68rem; opacity:.9; }
  .venc-rojo  { background:#fee2e2; color:#b91c1c; }   /* vencido / rojo */
  .venc-ambar { background:#fef3c7; color:#b45309; }   /* < 180 días / ámbar */
  .venc-verde { background:#dcfce7; color:#15803d; }   /* holgado / verde */
  .venc-na    { color:var(--suave); }                  /* sin fecha: texto en gris */

  /* ---- Calendario: lista de eventos por mes (cartera + radar) ---- */
  .cal-leyenda { margin:0 0 14px; color:var(--suave); font-size:.82rem;
    display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
  .cal-mes-tit { margin:18px 0 8px; font-size:.95rem; color:var(--texto); text-transform:capitalize; }
  .cal-lista { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:6px; }
  .cal-evento {
    display:flex; align-items:center; gap:10px; font-size:.86rem;
    padding:9px 12px; border:1px solid var(--borde); border-radius:10px; background:#fff;
  }
  .cal-evento.cal-urgente { border-color:#fca5a5; background:#fff7f7; }   /* < 30 días: resaltado */
  .cal-punto { display:inline-block; width:10px; height:10px; border-radius:50%; flex:0 0 auto; }
  .cal-punto-cierre-presentacion { background:#6366f1; }   /* cierres de presentación: índigo */
  .cal-punto-fin-contrato        { background:#f59e0b; }   /* fin de contrato/prórroga: ámbar */
  .cal-fecha { flex:0 0 auto; min-width:56px; font-weight:700; color:var(--texto); }
  .cal-cuerpo { flex:1 1 auto; min-width:0; display:flex; flex-direction:column; }
  .cal-titulo { color:var(--texto); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .cal-detalle { color:var(--suave); font-size:.78rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .cal-plazo { flex:0 0 auto; color:var(--suave); font-size:.78rem; white-space:nowrap; }
  .cal-urgente .cal-plazo { color:#b91c1c; font-weight:700; }
  .cal-vacio { color:var(--suave); font-size:.9rem; }
  /* Calendario en dos mitades: rejilla (izq) + lista de próximos (der). */
  .cal-split{ display:flex; gap:1rem; align-items:flex-start; }
  .cal-mes-panel{ flex:1 1 50%; } .cal-lista-panel{ flex:1 1 50%; max-height:72vh; overflow:auto; }
  @media(max-width:800px){ .cal-split{ flex-direction:column; } }
  .cal-nav{ display:flex; justify-content:space-between; align-items:center; margin-bottom:.4rem; font-weight:600; text-transform:capitalize; }
  .cal-nav button{ cursor:pointer; border:1px solid #ddd; border-radius:6px; background:#fff; padding:0 .5rem; }
  .cal-grid{ display:grid; grid-template-columns:repeat(7,1fr); gap:2px; }
  .cal-dow{ text-align:center; font-size:.7rem; color:var(--suave); font-weight:600; }
  .cal-dia{ min-height:42px; border:1px solid #eee; border-radius:6px; padding:2px 4px; font-size:.78rem; position:relative; }
  .cal-dia-evt{ cursor:pointer; background:#f8fafc; } .cal-vacia{ border:none; }
  .cal-dia-puntos{ position:absolute; bottom:3px; left:4px; display:flex; gap:2px; }
  .resaltado{ outline:2px solid #f59e0b; outline-offset:2px; }

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
  // El ORDEN y las PESTAÑAS viven en el script de módulo (zona privada): solo
  // importan con sesión (el radar está oculto por la cortina sin ella), y el orden
  // necesita saber la pestaña activa (en "Activas" las favoritas van primero).
"""


# JavaScript de MÓDULO (ESM) para el login con Supabase y el pintado de lo PRIVADO
# (estados manuales + estrella). Va en un <script type="module"> aparte porque usa
# import. Es una cadena NORMAL: al insertarla con {JS_SUPABASE} en la f-string de la
# página, sus llaves { } y sus plantillas ${...} no se reinterpretan.
JS_SUPABASE = """
  // ====== Login + leer/guardar estados y estrellas PRIVADOS (Supabase) ======
  // El radar (datos del feed + activa/caducada por fecha) es PÚBLICO y lo hornea
  // Python. Los estados manuales (ganada/perdida/presentada/descartada) y la
  // estrella (favorita) son PRIVADOS: se leen Y se guardan en Supabase SOLO tras
  // iniciar sesión (los controles de cada tarjeta solo aparecen con sesión).
  import { createClient } from 'https://esm.sh/@supabase/supabase-js@2';

  // --- Constantes de conexión (al principio del JS, como pediste) ----------
  // La clave 'publishable' es PÚBLICA y segura de exponer en el navegador con RLS
  // activado. Aquí va SOLO la publishable; jamás la clave secreta (la de servidor).
  const SUPABASE_URL = "https://uzktrhpgkyctlnqgdsys.supabase.co";
  const SUPABASE_KEY = "sb_publishable_3J3pFbMlNzu-NUDs1-740g_lu8YsRv_";
  const supabase = createClient(SUPABASE_URL, SUPABASE_KEY);

  // Etiqueta legible por estado manual (el color lo pone la clase CSS estado-*).
  const ESTADOS = { ganada: 'GANADA', perdida: 'PERDIDA', presentada: 'PRESENTADA', descartada: 'DESCARTADA' };

  // Estado en memoria de las decisiones reales: licitacion_id -> {estado, favorita}.
  // Es el espejo de la tabla 'decisiones'; se rellena al cargar y se mantiene al
  // guardar, para no tener que releer Supabase en cada cambio.
  const decisionesPorId = new Map();
  let sesionActiva = false;   // ¿hay sesión? Gobierna mostrar controles y permitir escribir.

  // --- Referencias al login y a la cortina de presentación -----------------
  const radar         = document.getElementById('radar');           // todo el radar (oculto sin sesión)
  const loginPantalla = document.getElementById('login-pantalla');  // formulario de login (siempre en el DOM)
  const formLogin = document.getElementById('login-form');
  const inEmail   = document.getElementById('login-email');
  const inPass    = document.getElementById('login-pass');
  const errLogin  = document.getElementById('login-error');
  const lblEmail  = document.getElementById('conectado-email');
  const btnLogout = document.getElementById('logout-btn');

  // --- Índice tarjeta por id de licitación (data-licitacion-id == entry.id) -
  // Usamos un Map (no querySelector) porque los id son URIs con ':' y '/' que
  // romperían un selector de atributo; con el Map casamos por string exacto.
  const tarjetasPorId = new Map();
  document.querySelectorAll('.card[data-licitacion-id]').forEach(function (card) {
    tarjetasPorId.set(card.getAttribute('data-licitacion-id'), card);
    // Guardamos el estado PÚBLICO base, para restaurarlo al cerrar sesión.
    card.dataset.estadoBase = card.dataset.estado;
  });
  // IDs de las tarjetas NATIVAS del Radar (las horneadas desde data/licitaciones.json).
  // Se captura ANTES de cualquier hidratación (BG-5): sirve para saber qué favoritas
  // del buscador NO están en el JSON y hay que traerlas del catálogo.
  const idsRadarJSON = new Set(tarjetasPorId.keys());
  // Tarjetas HIDRATADAS del catálogo (BG-5): licitacion_id -> card. Son las favoritas
  // que vienen del buscador (no están en el JSON) y solo se muestran en 'En observación'.
  const catalogoInyectado = new Map();

  // --- Pintado de lo PRIVADO sobre una tarjeta (idempotente) ---------------
  // Estas dos funciones son la ÚNICA lógica de pintado: las usan tanto la carga
  // inicial como el guardado y la limpieza al cerrar sesión (no se duplica nada).
  function pintaEstado(card, estado) {
    // 'estado' = clave manual (ganada/perdida/presentada/descartada) o null/'' (Activa).
    const previo = card.querySelector('.tags .tag.estado');
    if (previo) previo.remove();                       // siempre partimos de cero
    const clave = estado ? String(estado).toLowerCase() : '';
    const sel = card.querySelector('.ctrl-estado');
    if (sel) sel.value = clave || 'activa';            // el menú refleja el estado
    if (clave) {
      card.dataset.estado = clave;                     // el manual GANA al público por fecha
      const badge = document.createElement('span');
      badge.className = 'tag estado estado-' + clave + ' js-privado';
      badge.textContent = ESTADOS[clave] || clave.toUpperCase();
      (card.querySelector('.tags') || card).appendChild(badge);
    } else {
      card.dataset.estado = card.dataset.estadoBase || 'activa';  // vuelve al público
    }
  }

  function pintaFavorita(card, fav) {
    const esFav = fav === true;
    card.dataset.favorita = esFav ? 'true' : 'false';
    const btn = card.querySelector('.ctrl-estrella');
    if (btn) {
      btn.textContent = esFav ? '★' : '☆';
      btn.classList.toggle('marcada', esFav);
      btn.setAttribute('aria-pressed', esFav ? 'true' : 'false');
    }
  }

  // Aviso de error por tarjeta (para no mentir sobre lo que se guardó).
  function mostrarAviso(card, msg) {
    const av = card.querySelector('.card-aviso');
    if (av) { av.textContent = msg; av.hidden = false; }
  }
  function ocultarAviso(card) {
    const av = card.querySelector('.card-aviso');
    if (av) { av.hidden = true; av.textContent = ''; }
  }

  // Devuelve cada tarjeta a su estado público base y vacía el Map (al cerrar sesión).
  function limpiarPrivado() {
    limpiarCatalogoInyectado();   // BG-5: quita del DOM las tarjetas hidratadas del catálogo
    tarjetasPorId.forEach(function (card) {
      pintaEstado(card, null);
      pintaFavorita(card, false);
      ocultarAviso(card);
    });
    decisionesPorId.clear();
  }

  // ====== Pestañas por estado: filtran/ordenan la VISTA (no llaman a Supabase) ===
  // Reutilizan data-estado (el estado EFECTIVO que ya mantiene pintaEstado) y
  // data-favorita, más la misma lógica de orden de antes. Parte de la zona privada:
  // todo esto vive dentro de #radar, que solo se ve con sesión.
  const grid = document.getElementById('listado');
  const selectOrden = document.getElementById('orden');
  const selectCpv = document.getElementById('filtro-cpv');   // desplegable de filtro por CPV
  const barraTabs = document.getElementById('tabs');
  const vacioPestana = document.getElementById('vacio-pestana');   // mensaje "pestaña vacía"
  const navObsCount = document.querySelector('.nav-item[data-vista="observacion"] .nav-count');  // badge del menú
  let pestanaActiva = 'activas';   // pestaña activa por defecto

  // 'Hoy' a medianoche para ordenar por días restantes (igual que antes).
  const hoy = new Date(); hoy.setHours(0, 0, 0, 0);
  function diasHasta(iso) {
    if (!iso) return NaN;
    const f = new Date(iso + 'T00:00:00');
    return isNaN(f) ? NaN : Math.round((f - hoy) / 86400000);
  }
  function tiempo(iso) {
    if (!iso) return NaN;
    const f = new Date(iso + 'T00:00:00');
    return isNaN(f) ? NaN : f.getTime();
  }
  // Criterios de orden: clave + si va 'vacio' al final + dirección (mismos de antes).
  const criterios = {
    dias: function (c) {
      const d = diasHasta(c.dataset.finPlazo);
      if (isNaN(d) || d < 0) return { vacio: true };   // sin fecha o cerrada: al final
      return { clave: d, dir: 1 };
    },
    pub:     function (c) { const t = tiempo(c.dataset.fechaPub);    return { clave: t, vacio: isNaN(t), dir: -1 }; },
    subida:  function (c) { const t = tiempo(c.dataset.fechaSubida); return { clave: t, vacio: isNaN(t), dir: -1 }; },
    importe: function (c) { const v = parseFloat(c.dataset.importe); return { clave: v, vacio: isNaN(v), dir: -1 }; },
    nombre:  function (c) {
      const t = (c.dataset.titulo || '').trim();
      return { clave: t.toLowerCase(), vacio: !t, dir: 1, texto: true };
    },
  };

  function cards() { return grid ? Array.from(grid.querySelectorAll('.card')) : []; }

  // Mapa pestaña -> valor de data-estado que le corresponde (las de estado).
  const ESTADO_DE_PESTANA = {
    activas: 'activa', presentadas: 'presentada', ganadas: 'ganada',
    perdidas: 'perdida', descartadas: 'descartada', caducadas: 'caducada',
  };
  // ¿La tarjeta pertenece a una pestaña? data-estado es el estado EFECTIVO.
  function perteneceAPestana(card, pestana) {
    // Tarjetas HIDRATADAS del catálogo (buscador): aparecen SOLO en 'En observación'
    // y solo mientras sigan marcadas. NO se propagan a las pestañas por estado ni a
    // 'Todas' (esas siguen leyendo únicamente el JSON del Radar). BG-5, límite de alcance.
    if (card.dataset.origen === 'catalogo') {
      return pestana === 'favoritas' && card.dataset.favorita === 'true';
    }
    if (pestana === 'todas') return true;
    if (pestana === 'favoritas') return card.dataset.favorita === 'true';  // sin importar estado
    return card.dataset.estado === ESTADO_DE_PESTANA[pestana];
  }
  // Hueco para componer con el menú lateral de categorías: HOY no hay filtro de
  // categoría activo, así que siempre pasa. Si se añade, aquí se comprobará y la
  // visibilidad será (pertenece a la pestaña) Y (pasa el filtro de categoría).
  function categoriaVisible(card) { return true; }

  // Filtro por CPV (desplegable). cpvActivo = "" significa "Todos los CPV" (no filtra).
  // Cada tarjeta lleva sus códigos en data-cpv separados por espacio; pasa el filtro
  // si entre ellos está el CPV elegido.
  let cpvActivo = '';
  function cpvVisible(card) {
    if (!cpvActivo) return true;
    return (card.dataset.cpv || '').split(' ').indexOf(cpvActivo) !== -1;
  }

  // Ajuste "ocultar caducadas por defecto" (panel ⚙️). Cuando está activo, las
  // tarjetas caducadas no se muestran, salvo si estás en la pestaña "Caducadas".
  let ocultarCaducadas = false;
  function caducadasVisible(card) {
    if (!ocultarCaducadas || pestanaActiva === 'caducadas') return true;
    return card.dataset.estado !== 'caducada';
  }

  // Filtro de vista por la CONFIG del radar (⚙️): oculta AL INSTANTE las tarjetas que
  // ya no encajan con lo ACTIVO en la config (al quitar un CPV/palabra/grupo/territorio),
  // sin esperar a la próxima recogida del robot. Las ocultas siguen en el DOM y
  // reaparecen si reactivas. Si no hay config (cfgFiltro=null), no oculta nada.
  let cfgFiltro = null;
  function normalizaJS(s) {
    return String(s || '').toLowerCase().normalize('NFD').replace(/[\\u0300-\\u036f]/g, '');
  }
  function _activosDe(lista) {
    const r = [];
    (lista || []).forEach(function (it) {
      if (it && typeof it === 'object') { if (it.on !== false && it.v) r.push(String(it.v)); }
      else if (typeof it === 'string' && it.trim()) r.push(it.trim());
    });
    return r;
  }
  function construyeFiltroConfig(cfg) {
    const cats = (cfg && cfg.categorias) || {};
    if (!Object.keys(cats).length) return null;   // sin config -> no filtramos la vista
    const prefijos = [], palabras = [];
    for (const g in cats) {
      const c = cats[g] || {};
      _activosDe(c.cpv).forEach(function (x) { prefijos.push(x); });
      _activosDe(c.palabras_clave).forEach(function (x) { palabras.push(normalizaJS(x)); });
    }
    return {
      prefijos: prefijos, palabras: palabras,
      fuentes: Array.isArray(cfg.fuentes) ? cfg.fuentes : [],
      plataformas: Array.isArray(cfg.plataformas) ? cfg.plataformas : [],
      regiones: Array.isArray(cfg.regiones) ? cfg.regiones : []
    };
  }
  function configVisible(card) {
    if (!cfgFiltro) return true;
    const cpvs = (card.dataset.cpv || '').split(' ').filter(Boolean);
    const cpvMatch = cfgFiltro.prefijos.length > 0 && cpvs.some(function (c) {
      return cfgFiltro.prefijos.some(function (p) { return c.indexOf(p) === 0; });
    });
    const titulo = normalizaJS(card.dataset.titulo || '');
    const kwMatch = cfgFiltro.palabras.some(function (w) { return titulo.indexOf(w) !== -1; });
    if (!cpvMatch && !kwMatch) return false;   // ni por CPV ni por palabra
    const plat = card.dataset.plataforma || 'Estado';
    if (cfgFiltro.plataformas.length && cfgFiltro.plataformas.indexOf(plat) === -1) return false;
    const reg = card.dataset.region || '';
    if (cfgFiltro.regiones.length && cfgFiltro.regiones.indexOf(reg) === -1) return false;
    const fuente = card.dataset.fuente || 'estatal';
    if (cfgFiltro.fuentes.length && cfgFiltro.fuentes.indexOf(fuente) === -1) return false;
    return true;
  }

  // Muestra/oculta cada tarjeta según pestaña Y categoría Y CPV Y caducadas Y config.
  function aplicarFiltro() {
    cards().forEach(function (c) {
      // Las hidratadas del catálogo (BG-5) solo obedecen a la pestaña ('En observación'):
      // NO se les aplican los filtros de vista del Radar (categoría/CPV/caducadas/config),
      // para que 'En observación' muestre SIEMPRE todas las marcadas.
      const visible = (c.dataset.origen === 'catalogo')
        ? perteneceAPestana(c, pestanaActiva)
        : perteneceAPestana(c, pestanaActiva) && categoriaVisible(c)
          && cpvVisible(c) && caducadasVisible(c) && configVisible(c);
      c.classList.toggle('oculta-filtro', !visible);
    });
  }

  // Ordena reutilizando 'criterios'. En la pestaña Activas, las FAVORITAS van primero
  // (antes que el resto) y dentro de cada grupo se respeta el orden elegido. Ordena
  // todas las tarjetas; como las ocultas no se ven, en la práctica ordena las visibles.
  function ordenar(modo) {
    const fn = criterios[modo] || criterios.dias;
    const favPrimero = (pestanaActiva === 'activas');
    cards()
      .map(function (c) { return { c: c, k: fn(c), fav: c.dataset.favorita === 'true' }; })
      .sort(function (a, b) {
        if (favPrimero && a.fav !== b.fav) return a.fav ? -1 : 1;   // favoritas primero (Activas)
        if (a.k.vacio && b.k.vacio) return 0;
        if (a.k.vacio) return 1;
        if (b.k.vacio) return -1;
        const dir = a.k.dir || 1;
        if (a.k.texto) return a.k.clave.localeCompare(b.k.clave, 'es') * dir;
        if (a.k.clave < b.k.clave) return -dir;
        if (a.k.clave > b.k.clave) return dir;
        return 0;
      })
      .forEach(function (o) { grid.appendChild(o.c); });
  }

  function ordenActual() { return selectOrden ? selectOrden.value : 'dias'; }
  function actualizarVista() {
    aplicarFiltro();
    ordenar(ordenActual());
    // Mensaje de pestaña vacía: visible solo si NINGUNA tarjeta pasó el filtro. Se
    // reevalúa aquí, que es el único embudo del click de pestaña y de refrescarTodo().
    if (vacioPestana) {
      const hayVisibles = cards().some(function (c) { return !c.classList.contains('oculta-filtro'); });
      vacioPestana.hidden = hayVisibles;
    }
  }

  // Contador GLOBAL por pestaña: nº de tarjetas que le pertenecen (NO depende del
  // filtro de categoría ni de la pestaña activa; solo de data-estado/favorita).
  function actualizarContadores() {
    const todas = cards();
    if (barraTabs) barraTabs.querySelectorAll('.tab').forEach(function (tab) {
      const p = tab.dataset.pestana;
      const n = todas.filter(function (c) { return perteneceAPestana(c, p); }).length;
      const span = tab.querySelector('.tab-count');
      if (span) span.textContent = '(' + n + ')';
    });
    // BG-5: badge del menú 'En observación' = nº de marcadas (mismo criterio que la
    // antigua pestaña 'favoritas': nativas marcadas + hidratadas del catálogo).
    if (navObsCount) {
      const n = todas.filter(function (c) { return perteneceAPestana(c, 'favoritas'); }).length;
      navObsCount.textContent = n;
      navObsCount.hidden = (n === 0);
    }
  }

  // Tras un cambio de datos (carga/guardado) o al revelar el radar: recalcula
  // contadores y re-aplica filtro + orden, para que vista y números no se desfasen.
  function refrescarTodo() { actualizarContadores(); actualizarVista(); }

  // Activa una pestaña por nombre (marca el botón y re-filtra). La usan el click
  // del usuario y el salto desde el Calendario (para que la tarjeta destino se vea).
  function seleccionarPestana(nombre) {
    pestanaActiva = nombre;
    if (barraTabs) barraTabs.querySelectorAll('.tab').forEach(function (t) {
      const act = (t.dataset.pestana === nombre);
      t.classList.toggle('activa', act);
      t.setAttribute('aria-selected', act ? 'true' : 'false');
    });
    actualizarVista();
  }
  // Click en una pestaña: cambia la activa y re-filtra (los contadores no cambian).
  if (barraTabs) {
    barraTabs.addEventListener('click', function (e) {
      const btn = e.target.closest('.tab');
      if (!btn) return;
      if (vistaActiva === 'observacion') return;   // el tablist no gobierna 'En observación'
      seleccionarPestana(btn.dataset.pestana);
    });
  }
  // Cambiar el desplegable de orden: solo re-ordena (filtro y contadores no cambian).
  if (selectOrden) {
    selectOrden.addEventListener('change', function () { ordenar(ordenActual()); });
  }
  // Cambiar el desplegable de CPV: re-aplica el filtro (la pestaña activa y los
  // contadores globales no cambian; solo qué tarjetas se ven dentro de la pestaña).
  if (selectCpv) {
    selectCpv.addEventListener('change', function () {
      cpvActivo = selectCpv.value;
      actualizarVista();
    });
  }
  // Desplegar/colapsar los CPV extra de una tarjeta ("y N más" <-> "ocultar").
  // Delegado en el grid: las tarjetas son estáticas (horneadas), basta un listener.
  if (grid) {
    grid.addEventListener('click', function (e) {
      const btn = e.target.closest('.cpv-mas');
      if (!btn) return;
      const extra = btn.parentElement.querySelector('.cpv-extra');
      const abierto = btn.getAttribute('aria-expanded') === 'true';
      if (extra) extra.hidden = abierto;                 // abierto -> ocultar; cerrado -> mostrar
      btn.setAttribute('aria-expanded', abierto ? 'false' : 'true');
      btn.textContent = abierto ? btn.dataset.abrir : btn.dataset.cerrar;
    });
  }

  // ====== BG-5 · 'En observación': núcleo de escritura + hidratación catálogo ===
  // Núcleo ÚNICO de escritura de una decisión (estado + estrella) en 'decisiones'
  // por licitacion_id. Lo usan IGUAL el Radar y el buscador (mismo mecanismo, no
  // uno nuevo): sin marca (ni estado ni favorita) -> borra la fila; si no -> upsert
  // del objeto COMPLETO con onConflict. Mantiene el espejo decisionesPorId. NO pinta
  // (eso lo hace quien llama, sobre su propio DOM). Lanza si Supabase da error.
  async function persistirDecision(id, estado, favorita) {
    estado = estado || null;
    favorita = favorita === true;
    if (estado === null && favorita === false) {
      const { error } = await supabase.from('decisiones').delete().eq('licitacion_id', id);
      if (error) throw error;
      decisionesPorId.delete(id);
    } else {
      const fila = { licitacion_id: id, estado: estado, favorita: favorita, updated_at: new Date().toISOString() };
      const { error } = await supabase.from('decisiones').upsert(fila, { onConflict: 'licitacion_id' });
      if (error) throw error;
      decisionesPorId.set(id, { estado: estado, favorita: favorita });
    }
  }

  // Columnas del catálogo (public.licitaciones) para hidratar una tarjeta. Sin tsv.
  const COLS_CATALOGO = 'licitacion_id,titulo,objeto,num_expediente,organo_contratacion,cpv,fuente,'
    + 'presupuesto_con_iva,presupuesto_sin_iva,valor_estimado,fecha_publicacion,fecha_fin_plazo,enlace';

  // Controles (estrella + estado + Detalles) IGUALES que los del Radar (CONTROLES_CARD
  // de Python), para el PUENTE COMPLETO: los listeners delegados del grid ya los atienden.
  const CONTROLES_CARD_JS =
      '<div class="card-ctrl">'
    + '<button type="button" class="ctrl-estrella" aria-pressed="false" aria-label="En observación" title="Poner o quitar de «En observación»">☆</button>'
    + '<span class="ctrl-cap">Estado</span>'
    + '<select class="ctrl-estado" aria-label="Estado manual">'
    + '<option value="activa">Activa</option><option value="presentada">Presentada</option>'
    + '<option value="ganada">Ganada</option><option value="perdida">Perdida</option>'
    + '<option value="descartada">Descartada</option></select>'
    + '<button type="button" class="ctrl-detalles" aria-label="Detalles del contrato" title="Detalles del contrato">Detalles</button>'
    + '<span class="card-aviso" hidden></span></div>';

  // Escapa texto para HTML/atributos (incluye comillas dobles).
  function catEsc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  // El catálogo guarda las fechas como timestamptz (con hora/zona); nos quedamos con
  // la parte de fecha (YYYY-MM-DD) para que la lógica de días/orden del Radar (que
  // hace new Date(iso + 'T00:00:00')) funcione igual que con las del JSON.
  function catFechaSolo(v) {
    const s = String(v == null ? '' : v);
    return (s.length >= 10 && s.charAt(4) === '-' && s.charAt(7) === '-') ? s.slice(0, 10) : '';
  }
  function catFecha(iso) {
    const s = catFechaSolo(iso);   // 'YYYY-MM-DD'
    if (!s) return '—';
    // dd/mm/aaaa CON ceros (igual que formatea_fecha del Radar); sin depender del locale
    // (toLocaleDateString daba '12/5/2026' sin cero en algunos entornos).
    return s.slice(8, 10) + '/' + s.slice(5, 7) + '/' + s.slice(0, 4);
  }
  // Semáforo por días restantes, igual que clasifica_urgencia() de Python.
  function catUrg(dias) {
    if (dias === null || isNaN(dias) || dias < 0) return '';
    if (dias < 3) return 'roja';
    if (dias < 7) return 'ambar';
    return 'verde';
  }
  function catChipsCpv(cpv) {
    const lista = Array.isArray(cpv) ? cpv.filter(Boolean) : [];
    if (!lista.length) return '—';
    const TOPE = 6;
    const vis = lista.slice(0, TOPE), extra = lista.slice(TOPE);
    let h = vis.map(function (c) { return '<code>' + catEsc(c) + '</code>'; }).join(' ');
    if (extra.length) {
      const mas = 'y ' + extra.length + ' más';
      h += ' <span class="cpv-extra" hidden>'
        + extra.map(function (c) { return '<code>' + catEsc(c) + '</code>'; }).join(' ')
        + '</span><button type="button" class="cpv-mas" aria-expanded="false" data-abrir="'
        + mas + '" data-cerrar="ocultar">' + mas + '</button>';
    }
    return h;
  }

  // HTML de una tarjeta de CATÁLOGO (origen buscador). Tarjeta NEUTRA (el catálogo no
  // tiene 'categoria' del Radar) + badge de 'fuente'. Mismos data-* y controles que
  // una tarjeta del Radar para que el semáforo/orden/estado/Detalles funcionen igual.
  function construyeTarjetaCatalogo(fila) {
    const id = fila.licitacion_id;
    const finSolo = catFechaSolo(fila.fecha_fin_plazo);
    const pubSolo = catFechaSolo(fila.fecha_publicacion);
    const dias = finSolo ? diasHasta(finSolo) : NaN;
    const urg = catUrg(dias);
    // Estado PÚBLICO por fecha, calculado en render (nunca estático), como el Radar.
    const estado = (finSolo && !isNaN(dias) && dias < 0) ? 'caducada' : 'activa';
    let coletilla = '';
    if (finSolo && !isNaN(dias)) {
      if (dias > 0) coletilla = ' · <span class="quedan urg-tx-' + urg + '">' + (dias === 1 ? 'queda 1 día' : 'quedan ' + dias + ' días') + '</span>';
      else if (dias === 0) coletilla = ' · <span class="vence-hoy urg-tx-roja">vence hoy</span>';
      else coletilla = ' <span class="cerrado">· cerrado</span>';
    }
    const titulo = catEsc(fila.titulo || '(sin título)');
    const enlace = fila.enlace ? catEsc(fila.enlace) : '';
    const tituloHtml = enlace ? '<a href="' + enlace + '" target="_blank" rel="noopener">' + titulo + '</a>' : titulo;
    const fuente = fila.fuente ? catEsc(fila.fuente) : '';
    const org = fila.organo_contratacion ? '<div class="cat-org">' + catEsc(fila.organo_contratacion) + '</div>' : '';
    const cpvArr = Array.isArray(fila.cpv) ? fila.cpv : [];
    const importe = (fila.presupuesto_con_iva != null) ? String(fila.presupuesto_con_iva) : '';
    // Datos de la licitación para el snapshot a cartera (mismo patrón que las del Radar).
    const dataObjeto = catEsc(fila.objeto || fila.titulo || '');
    const dataOrgano = catEsc(fila.organo_contratacion || '');
    const dataPresuSin = (fila.presupuesto_sin_iva != null) ? String(fila.presupuesto_sin_iva) : '';
    const numExpRow = fila.num_expediente
      ? '<div class="dato"><span class="et-dato">Nº de expediente</span><span class="val-dato">' + catEsc(fila.num_expediente) + '</span></div>'
      : '';
    const datos = '<div class="datos">'
      + numExpRow
      + '<div class="dato"><span class="et-dato">Presupuesto (con IVA)</span><span class="val-dato">' + fmtEur(fila.presupuesto_con_iva) + '</span></div>'
      + '<div class="dato"><span class="et-dato">Presupuesto (sin IVA)</span><span class="val-dato">' + fmtEur(fila.presupuesto_sin_iva) + '</span></div>'
      + '<div class="dato"><span class="et-dato">Valor estimado</span><span class="val-dato">' + fmtEur(fila.valor_estimado) + '</span></div>'
      + '<div class="dato"><span class="et-dato">Fin de plazo</span><span class="val-dato">' + catFecha(finSolo) + coletilla + '</span></div>'
      + '<div class="dato"><span class="et-dato">Publicado</span><span class="val-dato">' + catFecha(pubSolo) + '</span></div>'
      + '</div>';
    return '<article class="card' + (urg ? ' urg-' + urg : '') + '"'
      + ' data-importe="' + importe + '" data-fin-plazo="' + finSolo + '"'
      + ' data-fecha-pub="' + pubSolo + '" data-fecha-subida=""'
      + ' data-cpv="' + catEsc(cpvArr.join(' ')) + '"'
      + ' data-plataforma="" data-region="" data-fuente="' + (fuente || 'estatal') + '"'
      + ' data-titulo="' + titulo + '" data-licitacion-id="' + catEsc(id) + '"'
      + ' data-objeto="' + dataObjeto + '" data-organo="' + dataOrgano + '" data-presu-sin="' + dataPresuSin + '"'
      + ' data-estado="' + estado + '" data-favorita="false" data-origen="catalogo">'
      + CONTROLES_CARD_JS
      + '<h2 class="card-title">' + tituloHtml + '</h2>'
      + org
      + '<div class="tags"><span class="tag cat cat-catalogo">catálogo' + (fuente ? ' · ' + fuente : '') + '</span></div>'
      + '<div class="cpv"><span class="et">CPV</span> ' + catChipsCpv(cpvArr) + '</div>'
      + datos + '</article>';
  }

  // Tarjeta MÍNIMA para una marcada que el catálogo no devuelve (purgada/ausente): no
  // rompemos la pestaña; mantenemos la estrella para poder quitarla de 'En observación'.
  function construyeTarjetaCatalogoMinima(id) {
    const idEsc = catEsc(id);
    return '<article class="card" data-importe="" data-fin-plazo="" data-fecha-pub=""'
      + ' data-fecha-subida="" data-cpv="" data-plataforma="" data-region="" data-fuente=""'
      + ' data-titulo="' + idEsc + '" data-licitacion-id="' + idEsc + '"'
      + ' data-estado="activa" data-favorita="false" data-origen="catalogo">'
      + CONTROLES_CARD_JS
      + '<h2 class="card-title">Licitación fuera del catálogo</h2>'
      + '<div class="tags"><span class="tag cat cat-catalogo">catálogo</span></div>'
      + '<p class="cat-hueco">No se encontró en el catálogo (pudo purgarse). Puedes quitarla de «En observación» con la estrella.</p>'
      + '<div class="cat-org">' + idEsc + '</div></article>';
  }

  // Inserta una tarjeta de catálogo en el grid del Radar (si no existe ya y no es una
  // nativa del JSON: en ese caso GANA el JSON). La registra en tarjetasPorId para que
  // los listeners delegados (estado/estrella/Detalles) del grid la atiendan.
  function _insertarCatalogo(id, htmlStr) {
    if (idsRadarJSON.has(id) || !grid) return null;   // el JSON gana: no duplicar
    let card = catalogoInyectado.get(id);
    if (!card) {
      const tmp = document.createElement('div');
      tmp.innerHTML = htmlStr;
      card = tmp.firstElementChild;
      if (!card) return null;
      // Estado PÚBLICO base (por fecha), para que pintaEstado() lo restaure al quitar
      // el estado manual (igual que las nativas del Radar).
      card.dataset.estadoBase = card.dataset.estado;
      grid.appendChild(card);
      catalogoInyectado.set(id, card);
      tarjetasPorId.set(id, card);
    }
    return card;
  }
  function _pintarCatalogo(card, id) {
    if (!card) return;
    const dec = decisionesPorId.get(id) || { estado: null, favorita: true };
    pintaEstado(card, dec.estado);
    pintaFavorita(card, dec.favorita === true);
  }
  function inyectarCatalogo(fila) {
    const id = fila && fila.licitacion_id;
    if (!id) return null;
    const card = _insertarCatalogo(id, construyeTarjetaCatalogo(fila));
    _pintarCatalogo(card, id);
    return card;
  }
  function inyectarCatalogoMinimo(id) {
    const card = _insertarCatalogo(id, construyeTarjetaCatalogoMinima(id));
    _pintarCatalogo(card, id);
    return card;
  }
  function quitarCatalogo(id) {
    const card = catalogoInyectado.get(id);
    if (card) { card.remove(); catalogoInyectado.delete(id); tarjetasPorId.delete(id); }
  }
  function limpiarCatalogoInyectado() {
    catalogoInyectado.forEach(function (card, id) { card.remove(); tarjetasPorId.delete(id); });
    catalogoInyectado.clear();
  }

  // Trae del catálogo las decisiones PRIVADAS que NO están en el JSON del Radar y las
  // inyecta en el grid. Un solo select(...).in(...) por PK (sin RPC). Las que el catálogo
  // no devuelva (purgadas) salen como tarjeta mínima. Termina con refrescarTodo.
  // Se hidrata tanto lo favorito (estrella, «En observación») COMO lo que tenga estado
  // manual (ganada/perdida/presentada/descartada) — en particular las GANADAS
  // auto-marcadas por CIF (Fase D2), que van con favorita=false y, sin esto, no
  // aparecerían en su pestaña porque su expediente no está en el destilado del Radar.
  async function hidratarObservacion() {
    const faltantes = [];
    decisionesPorId.forEach(function (d, id) {
      const tieneEstadoManual = !!(d && d.estado && d.estado !== 'activa');
      if (d && (d.favorita === true || tieneEstadoManual) && !idsRadarJSON.has(id)) faltantes.push(id);
    });
    // Quita hidratadas obsoletas (ya no marcadas).
    const objetivo = new Set(faltantes);
    Array.from(catalogoInyectado.keys()).forEach(function (id) { if (!objetivo.has(id)) quitarCatalogo(id); });
    if (!faltantes.length) { refrescarTodo(); return; }
    let filas = [];
    try {
      const { data, error } = await supabase.from('licitaciones').select(COLS_CATALOGO).in('licitacion_id', faltantes);
      if (error) throw error;
      filas = data || [];
    } catch (err) {
      console.error('Error hidratando «En observación» desde el catálogo:', err.message || err);
      faltantes.forEach(function (id) { inyectarCatalogoMinimo(id); });   // no rompemos la pestaña
      refrescarTodo();
      return;
    }
    const encontrados = new Set();
    filas.forEach(function (fila) { encontrados.add(fila.licitacion_id); inyectarCatalogo(fila); });
    faltantes.forEach(function (id) { if (!encontrados.has(id)) inyectarCatalogoMinimo(id); });
    refrescarTodo();
  }

  // Tras marcar/quitar la estrella desde el BUSCADOR: refleja el cambio en 'En
  // observación' al instante. Si es nativa del Radar, repinta su gemela; si viene del
  // catálogo, la inyecta (al marcar) o la quita (al desmarcar). 'fila' = fila que el
  // buscador ya tiene a mano (evita una consulta extra).
  function sincronizarObservacionTrasFav(id, fila, favorita) {
    if (idsRadarJSON.has(id)) {
      const c = tarjetasPorId.get(id);
      if (c) pintaFavorita(c, favorita === true);
    } else if (favorita === true) {
      if (fila) inyectarCatalogo(fila); else inyectarCatalogoMinimo(id);
    } else {
      quitarCatalogo(id);
    }
    refrescarTodo();
  }

  // --- Leer la tabla 'decisiones', rellenar el Map y pintar (SOLO con sesión) -
  async function cargarDecisiones(session) {
    if (!session) return;                  // el rol anónimo no debe consultar
    limpiarPrivado();                      // idempotente: parte de cero y repinta
    const { data, error } = await supabase.from('decisiones').select('*');
    if (error) { console.error('Error leyendo decisiones:', error.message); return; }
    (data || []).forEach(function (fila) {
      const dec = { estado: fila.estado || null, favorita: fila.favorita === true };
      decisionesPorId.set(fila.licitacion_id, dec);   // espejo en memoria
      const card = tarjetasPorId.get(fila.licitacion_id);
      if (!card) return;                   // la decisión no casa con ninguna tarjeta visible
      pintaEstado(card, dec.estado);
      pintaFavorita(card, dec.favorita);
    });
    refrescarTodo();   // ya con data-estado/favorita reales: filtra, ordena y cuenta
    // BG-5 + D2: hidrata desde el catálogo las decisiones (favoritas y/o con estado
    // manual, p.ej. GANADAS auto-marcadas por CIF) que NO están en el JSON del Radar,
    // por PK. Async, sin bloquear: las nativas ya se ven.
    hidratarObservacion();
  }

  // --- Guardar un cambio en Supabase y, si confirma, reflejarlo ------------
  // 'cambios' es {estado: <clave|null>} o {favorita: <bool>}. Se fusiona con lo
  // que ya hay en el Map para mandar el objeto COMPLETO y no perder el otro campo.
  async function guardarDecision(card, cambios) {
    if (!sesionActiva) return;             // sin sesión no se escribe (RLS lo bloquearía)
    const id = card.getAttribute('data-licitacion-id');
    const actual = decisionesPorId.get(id) || { estado: null, favorita: false };
    const estado = ('estado' in cambios) ? (cambios.estado || null) : (actual.estado || null);
    const favorita = ('favorita' in cambios) ? (cambios.favorita === true) : (actual.favorita === true);
    const sel = card.querySelector('.ctrl-estado');
    const btn = card.querySelector('.ctrl-estrella');
    if (sel) sel.disabled = true;          // bloqueamos mientras escribimos
    if (btn) btn.disabled = true;
    ocultarAviso(card);
    try {
      // Escritura por el núcleo compartido (mismo mecanismo que usa el buscador).
      await persistirDecision(id, estado, favorita);
      // Confirmado por Supabase: AHORA sí reflejamos en la tarjeta (Fase 3 pintado).
      pintaEstado(card, estado);
      pintaFavorita(card, favorita);
    } catch (err) {
      console.error('Error guardando decisión:', err);
      mostrarAviso(card, 'No se pudo guardar: ' + (err.message || err));
      // NO mentimos: revertimos los controles y el badge al último estado guardado.
      pintaEstado(card, actual.estado || null);
      pintaFavorita(card, actual.favorita === true);
    } finally {
      if (sel) sel.disabled = false;
      if (btn) btn.disabled = false;
      // Reactividad: data-estado/favorita ya reflejan el resultado (guardado o
      // revertido), así que recalculamos contadores y re-aplicamos filtro + orden.
      // Si la tarjeta ya no pertenece a la pestaña activa, desaparece de la vista.
      refrescarTodo();
    }
  }

  // --- Cortina de presentación: radar vs login según haya sesión -----------
  // OJO: esto es solo una CORTINA visual, NO seguridad. El radar (datos del feed
  // y estado por fecha) es PÚBLICO: está en el HTML y cualquiera puede inspeccionarlo
  // aunque aquí lo ocultemos. Lo que de verdad protege los datos PRIVADOS (estados
  // manuales y favoritas) es la RLS de Supabase, no este mostrar/ocultar.
  function pintarSesion(session) {
    const hay = !!session;
    radar.hidden = !hay;             // con sesión: se revela el radar completo
    loginPantalla.hidden = hay;      // con sesión: se oculta el login (y al revés)
    if (hay) lblEmail.textContent = session.user.email || '';
    errLogin.hidden = true;
    errLogin.textContent = '';
  }

  // --- Iniciar sesión ------------------------------------------------------
  formLogin.addEventListener('submit', async function (e) {
    e.preventDefault();
    errLogin.hidden = true;
    const { error } = await supabase.auth.signInWithPassword({
      email: inEmail.value.trim(),
      password: inPass.value,
    });
    if (error) {
      errLogin.textContent = 'No se pudo iniciar sesión: ' + error.message;
      errLogin.hidden = false;
      return;
    }
    inPass.value = '';
    // El pintado lo dispara onAuthStateChange (SIGNED_IN); no hace falta aquí.
  });

  // --- Cerrar sesión -------------------------------------------------------
  btnLogout.addEventListener('click', async function () {
    await supabase.auth.signOut();
    // El borrado de lo privado lo dispara onAuthStateChange (SIGNED_OUT).
  });

  // --- Reaccionar a los cambios de sesión ----------------------------------
  // onAuthStateChange emite INITIAL_SESSION al cargar, así que cubre recargar la
  // página estando ya logueado (supabase-js persiste la sesión en el navegador).
  // Diferimos el trabajo async con setTimeout(0): es el patrón recomendado para
  // no bloquear el lock interno de auth desde dentro del callback.
  supabase.auth.onAuthStateChange(function (event, session) {
    sesionActiva = !!session;
    // body.sesion muestra/oculta los controles de todas las tarjetas a la vez.
    document.body.classList.toggle('sesion', sesionActiva);
    pintarSesion(session);
    // Al revelar el radar, filtra/cuenta YA con el estado base por fecha (Activas por
    // defecto), para no enseñar todas las tarjetas mientras llegan las decisiones.
    if (session) refrescarTodo();
    setTimeout(function () {
      if (!session) {
        limpiarPrivado();
        if (carteraCont) carteraCont.innerHTML = '';      // no dejar datos privados en el DOM
        if (calendarioCont) calendarioCont.innerHTML = '';
        mostrarVista('radar');                            // volver a la vista por defecto
        return;
      }
      // Solo (re)leemos los datos cuando cambia la sesión de verdad. Un refresco
      // de token (TOKEN_REFRESHED, ~cada hora) o USER_UPDATED traen 'session' pero NO
      // cambian los datos: recargar repetiría el limpiarPrivado()+await de
      // cargarDecisiones y haría PARPADEAR badges/estrellas/colores. Lo evitamos.
      if (event === 'INITIAL_SESSION' || event === 'SIGNED_IN') {
        cargarDecisiones(session);
        if (vistaActiva === 'cartera') cargarCartera();        // si ya estábamos en la Cartera
        if (vistaActiva === 'calendario') cargarCalendario();  // o en el Calendario
      }
    }, 0);
  });

  // --- Controles de cada tarjeta: guardar al cambiar (solo con sesión) ------
  // Delegamos en el contenedor del listado (grid, ya definido arriba): un solo par
  // de listeners sirve para todas las tarjetas (y para las que se reordenen). Los
  // controles están ocultos sin sesión, y guardarDecision revalida sesionActiva.
  if (grid) {
    grid.addEventListener('change', function (e) {
      const sel = e.target.closest('.ctrl-estado');
      if (!sel) return;
      const card = sel.closest('.card');
      // "Activa" -> sin decisión manual (estado null).
      const estado = (sel.value === 'activa') ? null : sel.value;
      guardarDecision(card, { estado: estado });
    });
    grid.addEventListener('click', function (e) {
      const btn = e.target.closest('.ctrl-estrella');
      if (!btn) return;
      const card = btn.closest('.card');
      const id = card.getAttribute('data-licitacion-id');
      const actual = decisionesPorId.get(id) || {};
      guardarDecision(card, { favorita: !(actual.favorita === true) });
    });
  }

  // ====== Detalles del CONTRATO por licitación (tabla 'contratos', privado) ======
  // MISMO patrón que 'decisiones': mismo cliente 'supabase', misma sesión, lectura
  // por licitacion_id y upsert con updated_at + onConflict. El botón "Detalles" se
  // hornea oculto en cada tarjeta (lo muestra body.sesion) y abre este panel único.
  // Diferencia: la fila se LEE al abrir (no en bloque) y el contrato no se pinta en
  // la tarjeta, así que el panel solo refleja sus propios datos.
  const COLS_CONTRATO = ['num_expediente', 'adjudicatario', 'cif_adjudicatario',
    'importe_sin_iva', 'importe_con_iva', 'fecha_adjudicacion', 'fecha_inicio',
    'fecha_fin', 'prorroga_hasta', 'notas'];
  const COLS_NUM_CONTRATO = ['importe_sin_iva', 'importe_con_iva'];

  const modal       = document.getElementById('contrato-modal');
  const modalForm   = document.getElementById('contrato-form');
  const modalSub    = document.getElementById('contrato-licitacion');
  const modalAviso  = document.getElementById('contrato-aviso');
  const modalOk     = document.getElementById('contrato-ok');
  const btnGuardarC = document.getElementById('contrato-guardar');
  const docsLista   = document.getElementById('docs-lista');
  const docsVacio   = document.getElementById('docs-vacio');
  const docsForm    = document.getElementById('docs-form');
  const docFile     = document.getElementById('doc-file');
  const docTipo     = document.getElementById('doc-tipo');
  const docSubir    = document.getElementById('doc-subir');
  let contratoId = null;   // licitacion_id del contrato/documentos abierto en el panel
  // --- Enlace contrato -> cartera (botón "Añadir/Actualizar a cartera" del pie) ---
  const btnCartera = document.getElementById('contrato-cartera');
  const carteraOk  = document.getElementById('cartera-ok');
  let contratoExiste = false;    // ¿hay fila en 'contratos' para esta licitación?
  let carteraEnlazada = null;    // id (uuid) de la fila de 'cartera' enlazada, o null
  let contratoLicMeta = null;    // {objeto, cliente, presupuestoSin, titulo} de la licitación (solo INSERT)

  function campoC(col) { return document.getElementById('c-' + col); }
  function limpiarFormC() {
    COLS_CONTRATO.forEach(function (c) { const el = campoC(c); if (el) el.value = ''; });
  }
  function rellenarFormC(fila) {
    COLS_CONTRATO.forEach(function (c) {
      const el = campoC(c); if (!el) return;
      const v = fila ? fila[c] : null;
      el.value = (v === null || v === undefined) ? '' : v;
    });
  }
  // Texto vacío -> null (para no guardar cadenas vacías); número con coma o punto.
  function txtONull(v) { v = (v == null ? '' : String(v)).trim(); return v === '' ? null : v; }
  function numONull(v) {
    const s = (v == null ? '' : String(v)).trim();
    if (s === '') return null;
    const n = Number(s.replace(',', '.'));
    return isNaN(n) ? null : n;
  }
  function avisoC(msg) { if (modalAviso) { modalAviso.textContent = msg || ''; modalAviso.hidden = !msg; } }
  function okC(mostrar) { if (modalOk) modalOk.hidden = !mostrar; }
  function okCartera(msg) { if (carteraOk) { if (msg) { carteraOk.textContent = msg; carteraOk.hidden = false; } else { carteraOk.hidden = true; } } }
  // 'YYYY-MM-DD' (input date) -> 'dd/mm/aaaa' con ceros; null si no es fecha válida.
  function fmtDDMMYYYY(v) {
    const s = (v == null ? '' : String(v)).slice(0, 10);
    if (!(s.length === 10 && s.charAt(4) === '-' && s.charAt(7) === '-')) return null;
    return s.slice(8, 10) + '/' + s.slice(5, 7) + '/' + s.slice(0, 4);
  }
  // Estado del botón cartera: sin contrato -> deshabilitado; enlazada -> "Actualizar"; si no -> "Añadir".
  function actualizarBotonCartera() {
    if (!btnCartera) return;
    const enlazada = !!carteraEnlazada;   // ya hay fila en cartera para esta licitación
    btnCartera.textContent = enlazada ? 'Actualizar en cartera' : 'Añadir a cartera';
    if (!contratoExiste) {
      // Sin contrato guardado no se puede volcar; el TEXTO sí refleja si ya está enlazada
      // (p. ej. fila legacy enlazada por SQL y aún sin contrato: dice "Actualizar", deshabilitado).
      btnCartera.disabled = true;
      btnCartera.title = enlazada ? 'Guarda primero el contrato para actualizar' : 'Guarda primero el contrato';
    } else {
      btnCartera.disabled = false;
      btnCartera.title = enlazada ? 'Re-sincroniza en la cartera los campos que vienen del contrato' : 'Copia este contrato a la cartera';
    }
  }

  function cerrarContrato() {
    if (modal) modal.hidden = true;
    contratoId = null;
    contratoExiste = false; carteraEnlazada = null; contratoLicMeta = null;
    okCartera('');
  }

  // Núcleo: abre el panel para una licitación (por id). Lee su fila de 'contratos' y si
  // está enlazada a 'cartera' (en paralelo). 'licMeta' = datos de la licitación para el
  // snapshot a cartera (solo se usan al INSERTAR); al abrir desde la cartera va null.
  async function abrirContratoCore(id, subtitulo, licMeta) {
    if (!sesionActiva || !modal || !id) return;
    contratoId = id;
    contratoLicMeta = licMeta || null;
    contratoExiste = false; carteraEnlazada = null;
    if (modalSub) modalSub.textContent = subtitulo || '';
    limpiarFormC(); avisoC(''); okC(false); okCartera('');
    actualizarBotonCartera();              // arranca deshabilitado hasta saber si hay contrato
    modal.hidden = false;                  // mostramos ya (vacío) y rellenamos al llegar
    pintarDocumentos(id);                  // lista de documentos de esta licitación
    try {
      // Contrato + estado de enlace a cartera, en paralelo (mismo licitacion_id).
      const [rC, rK] = await Promise.all([
        supabase.from('contratos').select('*').eq('licitacion_id', id).maybeSingle(),
        supabase.from('cartera').select('id').eq('licitacion_id', id).maybeSingle(),
      ]);
      if (contratoId !== id || modal.hidden) return;   // se cambió/cerró entre tanto
      if (rC.error) throw rC.error;
      rellenarFormC(rC.data);              // data === null -> formulario vacío
      contratoExiste = !!rC.data;
      carteraEnlazada = (rK && !rK.error && rK.data) ? rK.data.id : null;   // si falla el enlace, tratamos como no enlazada
      actualizarBotonCartera();
    } catch (err) {
      console.error('Error leyendo contrato:', err);
      avisoC('No se pudieron cargar los datos: ' + (err.message || err));
    }
  }
  // Abrir desde una TARJETA (Radar o catálogo): id + subtítulo + datos de la licitación.
  function abrirContrato(card) {
    if (!card) return;
    abrirContratoCore(card.getAttribute('data-licitacion-id'), card.getAttribute('data-titulo') || '', {
      objeto: card.getAttribute('data-objeto') || '',
      cliente: card.getAttribute('data-organo') || '',
      presupuestoSin: card.getAttribute('data-presu-sin') || '',
      titulo: card.getAttribute('data-titulo') || '',
    });
  }
  // Abrir por licitacion_id SIN tarjeta (badge de la cartera): la fila ya existe en
  // cartera, así que el botón será "Actualizar" (solo campos del contrato). Sin licMeta.
  function abrirContratoPorId(id, subtitulo) {
    abrirContratoCore(id, subtitulo || '', null);
  }

  // Guardar: upsert con licitacion_id + updated_at (igual que en decisiones).
  async function guardarContrato() {
    if (!sesionActiva || !contratoId) return;
    const fila = { licitacion_id: contratoId, updated_at: new Date().toISOString() };
    COLS_CONTRATO.forEach(function (c) {
      const el = campoC(c);
      const v = el ? el.value : '';
      fila[c] = (COLS_NUM_CONTRATO.indexOf(c) >= 0) ? numONull(v) : txtONull(v);
    });
    if (btnGuardarC) btnGuardarC.disabled = true;
    avisoC(''); okC(false); okCartera('');
    try {
      const { error } = await supabase.from('contratos').upsert(fila, { onConflict: 'licitacion_id' });
      if (error) throw error;
      okC(true);                           // feedback; NO cerramos: ya se puede "Añadir a cartera"
      contratoExiste = true;               // hay contrato guardado -> habilita el botón cartera
      actualizarBotonCartera();
    } catch (err) {
      console.error('Error guardando contrato:', err);
      avisoC('No se pudo guardar: ' + (err.message || err));   // no mentimos: no cerramos
    } finally {
      if (btnGuardarC) btnGuardarC.disabled = false;
    }
  }

  // --- Añadir/Actualizar en cartera (snapshot MANUAL del contrato) -------------
  // INSERT (aún no enlazada): todos los campos mapeados. UPDATE (ya enlazada): SOLO los
  // que vienen del contrato (expediente/importes/fechas/prórroga); los curados a mano
  // (cliente/objeto/presupuesto/estado/notas) NO se pisan. Enlace por onConflict:
  // 'licitacion_id' (requiere el índice único de cartera_enlace.sql).
  async function guardarEnCartera() {
    if (!sesionActiva || !contratoId || !contratoExiste) return;
    if (btnCartera) btnCartera.disabled = true;
    avisoC(''); okC(false); okCartera('');
    try {
      // (1) RELEER el enlace JUSTO ahora: nunca decidimos INSERT/UPDATE con un flag
      //     obsoleto o errado de la apertura. Si no podemos saberlo con certeza (error),
      //     abortamos: preferimos no arriesgar pisar campos curados a mano.
      const { data: kRow, error: kErr } = await supabase.from('cartera')
        .select('id').eq('licitacion_id', contratoId).maybeSingle();
      if (kErr) throw kErr;
      carteraEnlazada = kRow ? kRow.id : null;

      // Campos que VIENEN del contrato (leídos del form = la verdad tras guardar).
      const vFin = campoC('fecha_fin') ? campoC('fecha_fin').value : '';   // YYYY-MM-DD (input date)
      const sync = {
        licitacion_id: contratoId,
        expediente: txtONull(campoC('num_expediente') ? campoC('num_expediente').value : ''),
        importe_adjudicacion: numONull(campoC('importe_sin_iva') ? campoC('importe_sin_iva').value : ''),
        importe_total_civa: numONull(campoC('importe_con_iva') ? campoC('importe_con_iva').value : ''),
        fecha_adjudicacion: fmtDDMMYYYY(campoC('fecha_adjudicacion') ? campoC('fecha_adjudicacion').value : ''),
        fecha_inicio: fmtDDMMYYYY(campoC('fecha_inicio') ? campoC('fecha_inicio').value : ''),
        prorrogas: fmtDDMMYYYY(campoC('prorroga_hasta') ? campoC('prorroga_hasta').value : ''),
        fin_vigencia: fmtDDMMYYYY(vFin),
        fin_vigencia_fecha: (vFin && vFin.length >= 10) ? vFin.slice(0, 10) : null,
      };

      let filaId = carteraEnlazada;        // id (uuid) de la fila destino, si ya la conocemos
      let esActualizar = !!carteraEnlazada;

      // (2) Si NO está enlazada por licitacion_id, ¿hay una fila LEGACY (licitacion_id
      //     NULL) con el MISMO expediente? Ofrecemos ENLAZARLA en vez de duplicar (evita
      //     el doble conteo del KPI). El casamiento manual por SQL sigue disponible.
      if (!esActualizar && sync.expediente) {
        const { data: legacy, error: lErr } = await supabase.from('cartera')
          .select('id,cliente').is('licitacion_id', null).eq('expediente', sync.expediente);
        if (lErr) throw lErr;
        if (legacy && legacy.length === 1) {
          const quien = legacy[0].cliente ? ' (' + legacy[0].cliente + ')' : '';
          if (window.confirm('Ya hay una fila en la cartera con el expediente «' + sync.expediente +
              '» sin enlazar' + quien + '.\\n¿Enlazarla a este contrato en vez de crear una fila nueva? (Recomendado)')) {
            esActualizar = true; filaId = legacy[0].id;
          }
        }
      }

      if (esActualizar) {
        // UPDATE por id: SOLO los campos del contrato (fija licitacion_id si era legacy).
        // NUNCA pisa los curados a mano (cliente/objeto/presupuesto/estado/notas).
        const { error } = await supabase.from('cartera').update(sync).eq('id', filaId);
        if (error) throw error;
        carteraEnlazada = filaId;
        okCartera('Actualizado en cartera ✓');
      } else {
        // INSERT de fila NUEVA (con los campos de la licitación, que no se re-sincronizan).
        // Si por una carrera ya existiera esa licitacion_id, el índice único hace FALLAR el
        // INSERT (se avisa), en vez de pisar en silencio los campos curados.
        const m = contratoLicMeta || {};
        const fila = Object.assign({}, sync, {
          objeto: m.objeto || m.titulo || null,
          cliente: (m.cliente && String(m.cliente).trim()) ? m.cliente : '(completar por SQL)',
          presupuesto_licitacion: numONull(m.presupuestoSin),
          estado: 'Vigente',
          notas: txtONull(campoC('notas') ? campoC('notas').value : ''),
        });
        const { data, error } = await supabase.from('cartera').insert(fila).select('id').maybeSingle();
        if (error) throw error;
        if (data && data.id) carteraEnlazada = data.id;
        okCartera('Añadido a cartera ✓');
      }
      if (vistaActiva === 'cartera') cargarCartera();   // si estamos en la Cartera, refresca ya
    } catch (err) {
      console.error('Error guardando en cartera:', err);
      avisoC('No se pudo guardar en cartera: ' + (err.message || err));
    } finally {
      actualizarBotonCartera();   // reevalúa disabled/texto (ahora enlazada -> "Actualizar")
    }
  }

  if (modalForm) modalForm.addEventListener('submit', function (e) { e.preventDefault(); guardarContrato(); });
  if (btnCartera) btnCartera.addEventListener('click', guardarEnCartera);
  const btnCerrarC = document.getElementById('contrato-cerrar');
  const btnCancelarC = document.getElementById('contrato-cancelar');
  if (btnCerrarC) btnCerrarC.addEventListener('click', cerrarContrato);
  if (btnCancelarC) btnCancelarC.addEventListener('click', cerrarContrato);
  if (modal) modal.addEventListener('click', function (e) { if (e.target === modal) cerrarContrato(); }); // clic en el fondo
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && modal && !modal.hidden) cerrarContrato();
  });

  // --- Documentos PDF por licitación (tabla 'documentos' + Storage privado) ----
  // Mismo cliente 'supabase'; bucket privado. Subir-antes-de-insertar (y limpiar el
  // huérfano si falla el insert); abrir con URL firmada de 60 s; solo PDF, máx 25 MB.
  const BUCKET = 'documentos';

  async function listarDocumentos(licitacionId) {
    const { data, error } = await supabase
      .from('documentos').select('*')
      .eq('licitacion_id', licitacionId)
      .order('subido_en', { ascending: false });
    if (error) { console.error(error); return []; }
    return data;
  }

  async function subirDocumento(licitacionId, file, tipo) {
    if (file.type !== 'application/pdf') { alert('Solo PDF.'); return false; }
    if (file.size > 25 * 1024 * 1024) { alert('Máx. 25 MB.'); return false; }
    const id = crypto.randomUUID();
    const ruta = `${id}.pdf`;
    const { error: upErr } = await supabase.storage
      .from(BUCKET).upload(ruta, file, { contentType: 'application/pdf', upsert: false });
    if (upErr) { console.error(upErr); alert('Error al subir.'); return false; }
    const { error: insErr } = await supabase
      .from('documentos')
      .insert({ id, licitacion_id: licitacionId, tipo, nombre: file.name, ruta });
    if (insErr) {
      await supabase.storage.from(BUCKET).remove([ruta]); // limpiar huérfano
      console.error(insErr); alert('Error al guardar.'); return false;
    }
    return true;
  }

  async function abrirDocumento(ruta) {
    const { data, error } = await supabase.storage
      .from(BUCKET).createSignedUrl(ruta, 60);
    if (error) { console.error(error); alert('No se pudo abrir.'); return; }
    window.open(data.signedUrl, '_blank');
  }

  async function borrarDocumento(id, ruta) {
    if (!confirm('¿Borrar este documento?')) return false;
    await supabase.storage.from(BUCKET).remove([ruta]);
    await supabase.from('documentos').delete().eq('id', id);
    return true;
  }

  // Pinta la lista de documentos de 'licitacionId' y cablea sus botones Abrir/Borrar.
  async function pintarDocumentos(licitacionId) {
    if (!docsLista) return;
    docsLista.innerHTML = '';                          // limpiamos antes de recargar
    const docs = await listarDocumentos(licitacionId);
    if (contratoId !== licitacionId) return;           // se cambió de licitación entre tanto
    if (docsVacio) docsVacio.hidden = docs.length > 0;
    docs.forEach(function (d) {
      const li = document.createElement('li');
      li.className = 'doc-item';
      const meta = document.createElement('span');
      meta.className = 'doc-meta';
      const fecha = d.subido_en ? String(d.subido_en).slice(0, 10) : '';
      meta.textContent = (d.tipo || 'otro') + ' · ' + (d.nombre || '') + (fecha ? ' · ' + fecha : '');
      const acciones = document.createElement('span');
      acciones.className = 'doc-acciones';
      const btnAbrir = document.createElement('button');
      btnAbrir.type = 'button'; btnAbrir.className = 'doc-btn'; btnAbrir.textContent = 'Abrir';
      btnAbrir.addEventListener('click', function () { abrirDocumento(d.ruta); });
      const btnBorrar = document.createElement('button');
      btnBorrar.type = 'button'; btnBorrar.className = 'doc-btn doc-borrar'; btnBorrar.textContent = 'Borrar';
      btnBorrar.addEventListener('click', async function () {
        const ok = await borrarDocumento(d.id, d.ruta);
        if (ok) pintarDocumentos(licitacionId);        // recargamos tras borrar
      });
      acciones.appendChild(btnAbrir); acciones.appendChild(btnBorrar);
      li.appendChild(meta); li.appendChild(acciones);
      docsLista.appendChild(li);
    });
  }

  // Mini-formulario de subida: PDF + tipo -> subir y recargar la lista.
  if (docsForm) {
    docsForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      if (!sesionActiva || !contratoId) return;
      const file = (docFile && docFile.files) ? docFile.files[0] : null;
      if (!file) { alert('Elige un archivo PDF.'); return; }
      const tipo = docTipo ? docTipo.value : 'otro';
      if (docSubir) docSubir.disabled = true;
      try {
        const ok = await subirDocumento(contratoId, file, tipo);
        if (ok) { docsForm.reset(); pintarDocumentos(contratoId); }   // recargamos tras subir
      } finally {
        if (docSubir) docSubir.disabled = false;
      }
    });
  }

  // Botón "Detalles" de cada tarjeta (delegado en el grid, como los demás controles).
  if (grid) {
    grid.addEventListener('click', function (e) {
      const btn = e.target.closest('.ctrl-detalles');
      if (!btn) return;
      abrirContrato(btn.closest('.card'));
    });
  }

  // ====== Vistas: Radar / Cartera / Calendario (entradas del lateral) =========
  // Solo alternan en el navegador qué bloque se ve; cartera y calendario se leen de
  // Supabase al entrar (SOLO con sesión). Radar es la vista por defecto.
  const vistaRadar      = document.getElementById('vista-radar');
  const vistaCartera    = document.getElementById('vista-cartera');
  const vistaCalendario = document.getElementById('vista-calendario');
  const vistaBuscador   = document.getElementById('vista-buscador');   // BG-4
  const carteraCont     = document.getElementById('cartera-contenido');
  const calendarioCont  = document.getElementById('calendario-contenido');
  const tituloSeccion   = document.getElementById('titulo-seccion');
  const metaRadar       = document.getElementById('meta-radar');
  // BG-5: 'observacion' es un subapartado que REUTILIZA el bloque del Radar
  // (#vista-radar): mismas tarjetas, controles y modales; solo fuerza la vista de
  // marcadas ('favoritas') y oculta el tablist + el filtro de CPV del Radar.
  const VISTAS  = ['radar', 'observacion', 'cartera', 'calendario', 'buscador'];
  const TITULOS = { radar: 'Radar', observacion: 'En observación', cartera: 'Cartera', calendario: 'Calendario', buscador: 'Buscador' };
  let vistaActiva = 'radar';   // vista por defecto

  // Ajusta el modo 'En observación' vs Radar sobre el MISMO grid: fuerza la pestaña
  // 'favoritas' al entrar y la deshace al volver al Radar (donde 'favoritas' ya no es tab).
  function aplicarModoObservacion() {
    if (vistaActiva === 'observacion') seleccionarPestana('favoritas');
    else if (vistaActiva === 'radar' && pestanaActiva === 'favoritas') seleccionarPestana('activas');
  }

  function mostrarVista(nombre) {
    vistaActiva = (VISTAS.indexOf(nombre) >= 0) ? nombre : 'radar';
    const enRadar = (vistaActiva === 'radar' || vistaActiva === 'observacion');
    if (vistaRadar)      vistaRadar.hidden      = !enRadar;                        // Radar y 'En observación' comparten bloque
    if (vistaCartera)    vistaCartera.hidden    = (vistaActiva !== 'cartera');
    if (vistaCalendario) vistaCalendario.hidden = (vistaActiva !== 'calendario');
    if (vistaBuscador)   vistaBuscador.hidden   = (vistaActiva !== 'buscador');   // BG-4
    // Marca activa la entrada del lateral y ajusta la cabecera.
    document.querySelectorAll('.sidebar .nav-item[data-vista]').forEach(function (a) {
      a.classList.toggle('activo', a.getAttribute('data-vista') === vistaActiva);
    });
    if (tituloSeccion) tituloSeccion.textContent = TITULOS[vistaActiva] || 'Radar';
    if (metaRadar) metaRadar.hidden = (vistaActiva !== 'radar');   // la meta es solo del radar
    // Tablist + filtro CPV + 'Ordenar por' son PROPIOS del Radar: en 'En observación'
    // los ocultamos con una clase en #vista-radar (su CSS los pone display:none). El
    // atributo [hidden] NO valía: .tabs/.orden-barra {display:flex} lo pisaban.
    const enObs = (vistaActiva === 'observacion');
    if (vistaRadar) vistaRadar.classList.toggle('modo-observacion', enObs);
    aplicarModoObservacion();
    // Al entrar con sesión, (re)cargamos la fuente correspondiente.
    if (vistaActiva === 'cartera' && sesionActiva) cargarCartera();
    if (vistaActiva === 'calendario' && sesionActiva) cargarCalendario();
    if (vistaActiva === 'buscador' && window.__bgEntrar) window.__bgEntrar();   // BG-4
  }

  // Clic en las entradas del lateral -> alternar vista (sin saltar por el ancla).
  document.querySelectorAll('.sidebar .nav-item[data-vista]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      mostrarVista(a.getAttribute('data-vista'));
    });
  });

  // --- Cartera: lógica de lectura y pintado (tabla de solo lectura) ---------
  const fmtEur = n => (n == null ? '—' :
    new Intl.NumberFormat('es-ES', { style:'currency', currency:'EUR' }).format(n));
  const carteraPorId = new Map();   // id de cartera -> fila (para el subtítulo del modal de docs)
  let resaltarPendiente = null;     // selector a resaltar tras pintar la cartera (salto del Calendario)

  // OJO: el radar ya tiene su propia diasHasta() (ordena por días de plazo). La de la
  // cartera la llamamos diasHastaVig() para NO pisarla (misma lógica que me diste).
  function diasHastaVig(iso){ if(!iso) return null; const h=new Date(); h.setHours(0,0,0,0); return Math.round((new Date(iso+'T00:00:00')-h)/86400000); }
  function badgeVigencia(iso, texto){
    const d = diasHastaVig(iso);
    if(d===null) return `<span class="venc-na">${texto||'—'}</span>`;
    const cls = d<0 ? 'venc-rojo' : (d<180 ? 'venc-ambar' : 'venc-verde');
    const et  = d<0 ? `vencido hace ${-d} días` : `vence en ${d} días`;
    const fecha = new Date(iso+'T00:00:00').toLocaleDateString('es-ES');
    return `<span class="venc ${cls}"><strong>${fecha}</strong><br><small>${et}</small></span>`;
  }

  async function cargarCartera() {
    // Ordenamos por fin de vigencia ASC (lo más urgente primero); los vacíos al final.
    const { data, error } = await supabase.from('cartera').select('*')
      .order('fin_vigencia_fecha', { ascending: true, nullsFirst: false })
      .order('cliente');
    if (error) { console.error(error); return; }
    renderCartera(data || []);
  }

  function renderCartera(filas) {
    const cont = document.getElementById('cartera-contenido');
    carteraPorId.clear();
    filas.forEach(function (f) { carteraPorId.set(String(f.id), f); });   // para el modal de docs
    const total = filas.reduce((s, f) => s + (Number(f.importe_adjudicacion) || 0), 0);
    let html = `<p>${filas.length} adjudicaciones · Total adjudicado (s/IVA): <strong>${fmtEur(total)}</strong></p>`;
    html += `<table class="cartera-tabla"><thead><tr>
      <th class="col-venc">Fin de vigencia</th><th>Cliente</th><th>CCAA</th>
      <th class="num">Adjudicación (s/IVA)</th><th>Estado</th><th>Ver</th></tr></thead><tbody>`;
    for (const f of filas) {
      const resuelto = (f.estado || '').toLowerCase().includes('resuelto');
      // Badge en filas ENLAZADAS a un contrato (por licitacion_id); clic -> modal Detalles.
      const licId = f.licitacion_id || '';
      // Escapamos con catEsc (& < > ") todo texto que venga de datos (algunos ya llegan
      // del feed externo vía la nueva pieza cartera: cliente<-organismo, objeto).
      const radarBadge = licId
        ? ' <button type="button" class="cart-radar-badge" data-licid="' + catEsc(licId)
          + '" data-lictit="' + catEsc(f.objeto || f.cliente || '')
          + '" title="Ver ficha del contrato (Detalles)">📡</button>'
        : '';
      html += `<tr class="${resuelto ? 'fila-resuelto' : ''}" data-cartera-id="${f.id}" title="${catEsc(f.notas || '')}">
        <td class="col-venc">${badgeVigencia(f.fin_vigencia_fecha, f.fin_vigencia)}</td>
        <td>${catEsc(f.cliente || '')}${radarBadge}</td>
        <td>${f.ccaa ? '<span class="chip">'+catEsc(f.ccaa)+'</span>' : ''}</td>
        <td class="num">${fmtEur(f.importe_adjudicacion)}</td>
        <td>${f.estado ? '<span class="chip">'+catEsc(f.estado)+'</span>' : ''}</td>
        <td><button type="button" class="cart-docs-btn">Ver</button></td></tr>`;
    }
    html += `</tbody></table>`;
    cont.innerHTML = html;
    // Si venimos de pinchar un evento del Calendario, resaltamos su fila AHORA que
    // ya está pintada (sin carrera contra la latencia de la consulta a Supabase).
    if (resaltarPendiente) { const s = resaltarPendiente; resaltarPendiente = null; resaltarEn(s); }
  }

  // --- Calendario: combina vencimientos de cartera (privado) y cierres de -----
  // presentación del radar (público), ordenados por fecha. Reutiliza diasHastaVig.
  // Las licitaciones del radar las leemos de las TARJETAS ya horneadas (datos
  // públicos): título = data-titulo; fin de presentación = data-fin-plazo
  // (fecha_fin_plazo, el EndDate del CODICE). No hay campo "órgano" en los datos.
  function licitacionesRadar() {
    // Excluimos las hidratadas del catálogo (BG-5): el Calendario del Radar sigue
    // siendo solo del JSON; las de origen buscador viven únicamente en 'En observación'.
    return Array.from(document.querySelectorAll('.card[data-licitacion-id]:not([data-origen="catalogo"])')).map(function (card) {
      return { titulo: card.dataset.titulo || '', finPresentacion: card.dataset.finPlazo || '', id: card.dataset.licitacionId };
    });
  }

  function textoPlazo(d) {
    if (d === null || isNaN(d)) return '';
    if (d === 0) return 'hoy';
    if (d === 1) return 'mañana';
    if (d > 0) return 'en ' + d + ' días';
    return 'hace ' + (-d) + ' días';
  }

  async function cargarCalendario() {
    const eventos = [];
    // Fuente 1: cartera (privada) -> fin de contrato/prórroga (filas con fecha).
    // Llevamos el id de cartera para poder navegar al evento (carteraId).
    const { data, error } = await supabase.from('cartera')
      .select('id,cliente,objeto,fin_vigencia_fecha').not('fin_vigencia_fecha', 'is', null);
    if (error) { console.error(error); }
    (data || []).forEach(function (f) {
      eventos.push({ tipo: 'fin-contrato', fecha: f.fin_vigencia_fecha, titulo: f.cliente || '', detalle: f.objeto || '', carteraId: String(f.id) });
    });
    // Fuente 2: radar (público) -> cierre de presentación, TODAS con fin >= hoy.
    // Llevamos el id de licitación (entry.id) para navegar a su tarjeta.
    licitacionesRadar().forEach(function (l) {
      const f = l.finPresentacion;
      if (f && diasHastaVig(f) >= 0) {
        eventos.push({ tipo: 'cierre-presentacion', fecha: f, titulo: l.titulo, detalle: '', licitacionId: l.id });
      }
    });
    eventos.sort(function (a, b) { return a.fecha.localeCompare(b.fecha); });   // ISO -> orden cronológico
    calendarioEventos = eventos;
    renderRejilla(calendarioEventos);     // mitad izquierda: rejilla mensual
    renderCalendario(calendarioEventos);  // mitad derecha: lista de próximos (igual que antes)
  }

  function renderCalendario(eventos) {
    const cont = document.getElementById('calendario-contenido');
    if (!cont) return;
    cont.innerHTML = '';
    if (!eventos.length) {
      const vacio = document.createElement('p');
      vacio.className = 'cal-vacio';
      vacio.textContent = 'No hay eventos en el calendario.';
      cont.appendChild(vacio);
      return;
    }
    // Leyenda (texto estático, sin datos de usuario).
    const ley = document.createElement('p');
    ley.className = 'cal-leyenda';
    ley.innerHTML = '<span class="cal-punto cal-punto-cierre-presentacion"></span> Cierre de presentación'
      + ' &nbsp; <span class="cal-punto cal-punto-fin-contrato"></span> Fin de contrato/prórroga';
    cont.appendChild(ley);

    let mesActual = '';
    let ul = null;
    eventos.forEach(function (ev) {
      const fechaObj = new Date(ev.fecha + 'T00:00:00');
      const claveMes = String(ev.fecha).slice(0, 7);   // YYYY-MM
      if (claveMes !== mesActual) {                     // nuevo grupo de mes
        mesActual = claveMes;
        const h = document.createElement('h3');
        h.className = 'cal-mes-tit';
        h.textContent = fechaObj.toLocaleDateString('es-ES', { month: 'long', year: 'numeric' });
        cont.appendChild(h);
        ul = document.createElement('ul');
        ul.className = 'cal-lista';
        cont.appendChild(ul);
      }
      const d = diasHastaVig(ev.fecha);
      const li = document.createElement('li');
      li.className = 'cal-evento';
      if (d !== null && d >= 0 && d < 30) li.classList.add('cal-urgente');   // < 30 días: resaltado

      const punto = document.createElement('span');
      punto.className = 'cal-punto cal-punto-' + ev.tipo;   // color por tipo
      li.appendChild(punto);

      const fecha = document.createElement('span');
      fecha.className = 'cal-fecha';
      fecha.textContent = fechaObj.toLocaleDateString('es-ES', { day: 'numeric', month: 'short' });
      li.appendChild(fecha);

      const cuerpo = document.createElement('span');
      cuerpo.className = 'cal-cuerpo';
      const t = document.createElement('span');
      t.className = 'cal-titulo';
      t.textContent = ev.titulo || '';                  // textContent: seguro con datos privados
      cuerpo.appendChild(t);
      if (ev.detalle) {
        const dt = document.createElement('span');
        dt.className = 'cal-detalle';
        dt.textContent = ev.detalle;
        cuerpo.appendChild(dt);
      }
      li.appendChild(cuerpo);

      const plazo = document.createElement('span');
      plazo.className = 'cal-plazo';
      plazo.textContent = textoPlazo(d);
      li.appendChild(plazo);

      li.style.cursor = 'pointer';                          // clicable: salta a su info
      li.addEventListener('click', function () { irAEvento(ev); });
      ul.appendChild(li);
    });
  }

  // --- Rejilla mensual (mitad izquierda) -------------------------------------
  let calendarioEventos = [];   // eventos del calendario (lista + rejilla comparten)
  let calMes = null;
  // Mes actual en Europe/Madrid (no en la zona del navegador): para que el calendario
  // se abra por defecto en el mes en que estamos hoy. Si el entorno no soporta
  // timeZone, cae a la hora local del navegador.
  function mesActualMadrid(){
    try {
      const p = new Intl.DateTimeFormat('en-CA', { timeZone:'Europe/Madrid', year:'numeric', month:'2-digit' }).formatToParts(new Date());
      return { y:+p.find(x=>x.type==='year').value, m:+p.find(x=>x.type==='month').value - 1 };
    } catch(e) {
      const f = new Date(); return { y:f.getFullYear(), m:f.getMonth() };
    }
  }
  function renderRejilla(eventos){
    const cont = document.getElementById('cal-mes'); if(!cont) return;
    if(!calMes){ calMes = mesActualMadrid(); }   // por defecto: mes actual (Europe/Madrid)
    const {y,m} = calMes, porDia = {};
    eventos.forEach(e => (porDia[e.fecha] = porDia[e.fecha]||[]).push(e));
    const primero = new Date(y,m,1), inicio = (primero.getDay()+6)%7, diasMes = new Date(y,m+1,0).getDate();
    let html = `<div class="cal-nav"><button type="button" id="cal-prev">‹</button><span>${primero.toLocaleDateString('es-ES',{month:'long',year:'numeric'})}</span><button type="button" id="cal-next">›</button></div><div class="cal-grid">`;
    ['L','M','X','J','V','S','D'].forEach(d => html += `<div class="cal-dow">${d}</div>`);
    for(let i=0;i<inicio;i++) html += `<div class="cal-dia cal-vacia"></div>`;
    for(let dia=1; dia<=diasMes; dia++){
      const iso = `${y}-${String(m+1).padStart(2,'0')}-${String(dia).padStart(2,'0')}`, evs = porDia[iso]||[];
      const puntos = [...new Set(evs.map(e=>e.tipo))].map(t=>`<span class="cal-punto cal-punto-${t}"></span>`).join('');
      html += `<div class="cal-dia${evs.length?' cal-dia-evt':''}" data-fecha="${iso}">${dia}<div class="cal-dia-puntos">${puntos}</div></div>`;
    }
    cont.innerHTML = html + `</div>`;
    document.getElementById('cal-prev').onclick = () => { if(--calMes.m<0){calMes.m=11;calMes.y--;} renderRejilla(eventos); };
    document.getElementById('cal-next').onclick = () => { if(++calMes.m>11){calMes.m=0;calMes.y++;} renderRejilla(eventos); };
    cont.querySelectorAll('.cal-dia-evt').forEach(el => el.onclick = () => { const e=(porDia[el.dataset.fecha]||[])[0]; if(e) irAEvento(e); });
  }

  // --- Click en un evento -> ir a su información (Cartera o Radar) ------------
  function irAEvento(ev){
    if(ev.tipo==='fin-contrato' && ev.carteraId){
      // La tabla de cartera se re-fetchea (async) al entrar; en vez de competir con
      // un setTimeout fijo, dejamos marcado el destino y lo resalta renderCartera al
      // terminar de pintar (ver resaltarPendiente).
      resaltarPendiente = '#cartera-contenido tr[data-cartera-id="'+ev.carteraId+'"]';
      mostrarVista('cartera');
    }
    else if(ev.tipo==='cierre-presentacion' && ev.licitacionId){
      // La tarjeta puede estar oculta por el filtro de pestaña (display:none); pasamos
      // a "Todas" para que se vea, y la resaltamos cuando el radar ya está visible.
      mostrarVista('radar');
      seleccionarPestana('todas');
      setTimeout(()=>resaltarEn('.card[data-licitacion-id="'+ev.licitacionId+'"]'),80);
    }
  }
  function resaltarEn(sel){ const el=document.querySelector(sel); if(!el) return; el.scrollIntoView({behavior:'smooth',block:'center'}); el.classList.add('resaltado'); setTimeout(()=>el.classList.remove('resaltado'),2500); }

  // ====== Documentos de una adjudicación de la cartera (cartera_documentos) =====
  // Mismo patrón que los documentos de las licitaciones (mismo cliente, bucket
  // 'documentos'), keyed por cartera_id y con ruta cartera/{uuid}.pdf. Subir-antes-
  // de-insertar (limpia el huérfano si falla), URL firmada 60 s, solo PDF, máx 25 MB.
  const cdocModal = document.getElementById('cartera-docs-modal');
  const cdocForm  = document.getElementById('cdoc-form');
  const cdocSub   = document.getElementById('cdoc-sub');
  const cdocLista = document.getElementById('cdoc-lista');
  const cdocVacio = document.getElementById('cdoc-vacio');
  const cdocFile  = document.getElementById('cdoc-file');
  const cdocTipo  = document.getElementById('cdoc-tipo');
  const cdocSubir = document.getElementById('cdoc-subir');
  let carteraDocsId = null;   // cartera_id del modal de documentos abierto

  async function listarDocsCartera(id){ const {data}=await supabase.from('cartera_documentos').select('*').eq('cartera_id',id).order('subido_en',{ascending:false}); return data||[]; }
  async function subirDocCartera(carteraId,file,tipo){
    if(file.type!=='application/pdf'){alert('Solo PDF.');return false;}
    if(file.size>25*1024*1024){alert('Máx. 25 MB.');return false;}
    const id=crypto.randomUUID(), ruta=`cartera/${id}.pdf`;
    const {error:up}=await supabase.storage.from(BUCKET).upload(ruta,file,{contentType:'application/pdf',upsert:false});
    if(up){console.error(up);alert('Error al subir.');return false;}
    const {error:ins}=await supabase.from('cartera_documentos').insert({id,cartera_id:carteraId,tipo,nombre:file.name,ruta});
    if(ins){await supabase.storage.from(BUCKET).remove([ruta]);console.error(ins);alert('Error al guardar.');return false;}
    return true;
  }
  async function abrirDocCartera(ruta){ const {data,error}=await supabase.storage.from(BUCKET).createSignedUrl(ruta,60); if(error){console.error(error);alert('No se pudo abrir.');return;} window.open(data.signedUrl,'_blank'); }
  async function borrarDocCartera(id,ruta){ if(!confirm('¿Borrar este documento?'))return false; await supabase.storage.from(BUCKET).remove([ruta]); await supabase.from('cartera_documentos').delete().eq('id',id); return true; }

  // Pinta la lista de documentos de esa adjudicación y cablea Abrir/Borrar.
  async function pintarDocsCartera(carteraId) {
    if (!cdocLista) return;
    cdocLista.innerHTML = '';
    const docs = await listarDocsCartera(carteraId);
    if (carteraDocsId !== carteraId) return;          // se cambió/cerró entre tanto
    if (cdocVacio) cdocVacio.hidden = docs.length > 0;
    docs.forEach(function (d) {
      const li = document.createElement('li');
      li.className = 'doc-item';
      const meta = document.createElement('span');
      meta.className = 'doc-meta';
      const fecha = d.subido_en ? String(d.subido_en).slice(0, 10) : '';
      meta.textContent = (d.tipo || 'otro') + ' · ' + (d.nombre || '') + (fecha ? ' · ' + fecha : '');
      const acc = document.createElement('span');
      acc.className = 'doc-acciones';
      const bAbrir = document.createElement('button');
      bAbrir.type = 'button'; bAbrir.className = 'doc-btn'; bAbrir.textContent = 'Abrir';
      bAbrir.addEventListener('click', function () { abrirDocCartera(d.ruta); });
      const bBorrar = document.createElement('button');
      bBorrar.type = 'button'; bBorrar.className = 'doc-btn doc-borrar'; bBorrar.textContent = 'Borrar';
      bBorrar.addEventListener('click', async function () {
        const ok = await borrarDocCartera(d.id, d.ruta);
        if (ok) pintarDocsCartera(carteraId);          // recargamos tras borrar
      });
      acc.appendChild(bAbrir); acc.appendChild(bBorrar);
      li.appendChild(meta); li.appendChild(acc);
      cdocLista.appendChild(li);
    });
  }

  // Detalle (campos completos) + anotación editable del registro de cartera.
  function filaDato(et, val){ return val ? `<div class="det-fila"><span class="det-et">${et}</span><span class="det-val">${val}</span></div>` : ''; }
  function pintarDetalle(f){
    document.getElementById('cdoc-datos').innerHTML =
      filaDato('Objeto', f.objeto) + filaDato('Expediente', f.expediente) +
      filaDato('Procedimiento', f.procedimiento) +
      filaDato('Presupuesto licitación', fmtEur(f.presupuesto_licitacion)) +
      filaDato('Adjudicación (s/IVA)', fmtEur(f.importe_adjudicacion)) +
      filaDato('Total (c/IVA)', fmtEur(f.importe_total_civa)) +
      filaDato('Inicio', f.fecha_inicio) + filaDato('Duración', f.duracion) +
      filaDato('Prórrogas', f.prorrogas) + filaDato('Fin de vigencia', f.fin_vigencia) +
      filaDato('Notas', f.notas);
    document.getElementById('cdoc-anotaciones').value = f.anotaciones || '';
  }
  async function guardarAnotacion(carteraId, texto){
    const { error } = await supabase.from('cartera').update({ anotaciones: texto }).eq('id', carteraId);
    if(error){ console.error(error); alert('No se pudo guardar.'); return; }
    const f = carteraPorId.get(String(carteraId)); if(f) f.anotaciones = texto;
  }

  function abrirDocsCartera(carteraId) {
    if (!sesionActiva || !cdocModal || !carteraId) return;
    carteraDocsId = carteraId;
    const f = carteraPorId.get(String(carteraId)) || {};
    if (cdocSub) cdocSub.textContent = (f.cliente || '') + (f.objeto ? ' · ' + f.objeto : '');
    pintarDetalle(f);                    // rellena #cdoc-datos y la anotación
    if (cdocLista) cdocLista.innerHTML = '';
    cdocModal.hidden = false;
    pintarDocsCartera(carteraId);
  }
  function cerrarDocsCartera() { if (cdocModal) cdocModal.hidden = true; carteraDocsId = null; }

  // Subida: PDF + tipo -> subir y recargar la lista.
  if (cdocForm) {
    cdocForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      if (!sesionActiva || !carteraDocsId) return;
      const file = (cdocFile && cdocFile.files) ? cdocFile.files[0] : null;
      if (!file) { alert('Elige un archivo PDF.'); return; }
      const tipo = cdocTipo ? cdocTipo.value : 'otro';
      if (cdocSubir) cdocSubir.disabled = true;
      try {
        const ok = await subirDocCartera(carteraDocsId, file, tipo);
        if (ok) { cdocForm.reset(); pintarDocsCartera(carteraDocsId); }
      } finally {
        if (cdocSubir) cdocSubir.disabled = false;
      }
    });
  }
  // Guardar la anotación del registro abierto.
  const cdocGuardarAnot = document.getElementById('cdoc-guardar-anot');
  if (cdocGuardarAnot) {
    cdocGuardarAnot.addEventListener('click', async function () {
      if (!sesionActiva || !carteraDocsId) return;
      const ta = document.getElementById('cdoc-anotaciones');
      cdocGuardarAnot.disabled = true;
      try { await guardarAnotacion(carteraDocsId, ta ? ta.value : ''); }
      finally { cdocGuardarAnot.disabled = false; }
    });
  }
  const cdocCerrar = document.getElementById('cdoc-cerrar');
  if (cdocCerrar) cdocCerrar.addEventListener('click', cerrarDocsCartera);
  if (cdocModal) cdocModal.addEventListener('click', function (e) { if (e.target === cdocModal) cerrarDocsCartera(); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && cdocModal && !cdocModal.hidden) cerrarDocsCartera();
  });

  // Clic en una fila de la cartera (o en su botón "Ver") -> abre el detalle.
  if (carteraCont) {
    // En un clic REAL de ratón, el evento 'click' puede retargetearse al <td> (mousedown y
    // mouseup caen en subelementos distintos), y entonces e.target.closest('.cart-radar-badge')
    // fallaba y se abría el modal de la FILA (documentos) en vez del de Detalles. Recordamos
    // el badge del 'pointerdown' (que sí apunta al elemento pulsado) y lo usamos de respaldo;
    // además cortamos la propagación en la rama del badge.
    let cartBadgeDown = null;
    carteraCont.addEventListener('pointerdown', function (e) {
      cartBadgeDown = e.target.closest('.cart-radar-badge');
    });
    carteraCont.addEventListener('click', function (e) {
      const badge = e.target.closest('.cart-radar-badge') || cartBadgeDown;
      cartBadgeDown = null;
      if (badge) {
        e.stopPropagation();
        abrirContratoPorId(badge.getAttribute('data-licid'), badge.getAttribute('data-lictit') || '');
        return;
      }
      const tr = e.target.closest('tr[data-cartera-id]');
      if (!tr) return;
      abrirDocsCartera(tr.getAttribute('data-cartera-id'));
    });
  }

  // ====== Panel de Ajustes del radar (⚙️) =================================
  // Lee/escribe la tabla radar_config (misma 'supabase' y sesión). Los grupos
  // (criticas/a_revisar/pruebas) con sus CPV y palabras se editan aquí y, al
  // guardar, SUSTITUYEN a intereses.yaml en la próxima recogida del robot.
  const ajModal = document.getElementById('ajustes-modal');
  const ajBtn = document.getElementById('btn-ajustes');
  const ajGrupos = document.getElementById('aj-grupos');
  const ajFuentes = document.getElementById('aj-fuentes');
  const ajPlataformas = document.getElementById('aj-plataformas');
  const ajRegiones = document.getElementById('aj-regiones');
  const ajMsg = document.getElementById('aj-msg');
  const ajCatalogo = document.getElementById('aj-cpv-catalogo');
  const ajGuardar = document.getElementById('aj-guardar');
  const FUENTES = ['estatal', 'agregadas'];
  let ajCfg = null;          // estado de edición en memoria
  let ajCatCargado = false;  // catálogo CPV ya volcado en el datalist
  let ajCpvNombres = {};     // codigo -> nombre

  function ajEsc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function ajTerm(x) {
    if (x && typeof x === 'object') return { v: String(x.v), on: x.on !== false };
    return { v: String(x), on: true };
  }
  function ajSoloDigitos(s) {
    let d = '';
    for (let i = 0; i < s.length; i++) { const c = s[i]; if (c >= '0' && c <= '9') d += c; }
    return d;
  }

  // Carga el catálogo de CPV (data->docs) para buscar por nombre en el datalist.
  async function ajCargarCatalogo() {
    if (ajCatCargado) return;
    ajCatCargado = true;
    try {
      const r = await fetch('cpv_nombres.json');
      ajCpvNombres = await r.json();
      const frag = document.createDocumentFragment();
      for (const c in ajCpvNombres) {
        const o = document.createElement('option');
        o.value = c + ' — ' + ajCpvNombres[c];
        frag.appendChild(o);
      }
      ajCatalogo.appendChild(frag);
    } catch (e) { ajCpvNombres = {}; }
  }

  function ajChip(grupo, tipo, idx, t) {
    const nombre = (tipo === 'cpv') ? (ajCpvNombres[t.v] || '') : '';
    const etq = nombre ? (t.v + ' · ' + nombre) : t.v;
    const tit = nombre ? ' title="' + ajEsc(nombre) + '"' : '';
    return '<span class="aj-chip ' + (t.on ? '' : 'off') + '" data-g="' + ajEsc(grupo) +
      '" data-t="' + tipo + '" data-i="' + idx + '"' + tit + '>' +
      '<span class="lbl">' + ajEsc(etq) + '</span><span class="x" data-x="1">✕</span></span>';
  }
  function ajCajaAdd(grupo, tipo, placeholder, esCpv) {
    return '<div class="aj-add"><input ' + (esCpv ? 'class="cpv" list="aj-cpv-catalogo" ' : '') +
      'placeholder="' + placeholder + '" data-add="' + tipo + '" data-g="' + ajEsc(grupo) + '">' +
      '<button data-addbtn="' + tipo + '" data-g="' + ajEsc(grupo) + '">Añadir</button></div>';
  }
  function ajRenderGrupos() {
    let h = '';
    for (const g in ajCfg.categorias) {
      const cat = ajCfg.categorias[g];
      h += '<div class="aj-grupo"><div class="aj-grupo-tit">' + ajEsc(g.replace(/_/g, ' ')) + '</div>';
      h += '<div class="aj-sub2">CPV</div><div class="aj-chips">';
      cat.cpv.forEach(function (t, i) { h += ajChip(g, 'cpv', i, t); });
      h += '</div>' + ajCajaAdd(g, 'cpv', 'Añadir CPV (código o nombre)', true);
      h += '<div class="aj-sub2">Palabras clave</div><div class="aj-chips">';
      cat.palabras_clave.forEach(function (t, i) { h += ajChip(g, 'palabras_clave', i, t); });
      h += '</div>' + ajCajaAdd(g, 'palabras_clave', 'Añadir palabra', false);
      h += '</div>';
    }
    ajGrupos.innerHTML = h;
  }
  function ajRenderChecks() {
    ajFuentes.innerHTML = FUENTES.map(function (f) {
      return '<label><input type="checkbox" value="' + f + '"' + (ajCfg.fuentes.indexOf(f) >= 0 ? ' checked' : '') + '> ' + f + '</label>';
    }).join('');
    const plats = window.RADAR_PLATAFORMAS || [];
    ajPlataformas.innerHTML = plats.map(function (p) {
      return '<label><input type="checkbox" value="' + ajEsc(p) + '"' + (ajCfg.plataformas.indexOf(p) >= 0 ? ' checked' : '') + '> ' + ajEsc(p) + '</label>';
    }).join('') || '<span class="aj-ayuda">— (aún sin datos)</span>';
    const regs = window.RADAR_REGIONES || {};
    ajRegiones.innerHTML = Object.keys(regs).map(function (code) {
      return '<label><input type="checkbox" value="' + ajEsc(code) + '"' + (ajCfg.regiones.indexOf(code) >= 0 ? ' checked' : '') + '> ' + ajEsc(regs[code]) + '</label>';
    }).join('') || '<span class="aj-ayuda">— (aún sin datos)</span>';
  }
  function ajRenderVista() {
    document.getElementById('aj-ocultar-caducadas').checked = !!ajCfg.vista.ocultar_caducadas;
    document.getElementById('aj-pestana').value = ajCfg.vista.pestana_inicial || 'activas';
    document.getElementById('aj-orden').value = ajCfg.vista.orden_inicial || 'dias';
    document.getElementById('aj-dias-nuevo').value = ajCfg.vista.dias_nuevo || 7;
  }
  function ajRender() { ajRenderGrupos(); ajRenderChecks(); ajRenderVista(); }

  function ajNormalizaCats(cats) {
    const out = {};
    for (const g in cats) {
      const c = cats[g] || {};
      out[g] = { cpv: (c.cpv || []).map(ajTerm), palabras_clave: (c.palabras_clave || []).map(ajTerm) };
    }
    return out;
  }
  async function ajLeerConfig() {
    try {
      const { data } = await supabase.from('radar_config').select('config').eq('id', 1).maybeSingle();
      return (data && data.config) || {};
    } catch (e) { return {}; }
  }
  async function ajAbrir() {
    ajMsg.textContent = ''; ajMsg.className = 'aj-msg';
    await ajCargarCatalogo();
    const g = await ajLeerConfig();
    const cats = (g.categorias && Object.keys(g.categorias).length) ? g.categorias : (window.RADAR_DEFAULTS || {});
    ajCfg = {
      categorias: ajNormalizaCats(cats),
      fuentes: Array.isArray(g.fuentes) ? g.fuentes.slice() : [],
      plataformas: Array.isArray(g.plataformas) ? g.plataformas.slice() : [],
      regiones: Array.isArray(g.regiones) ? g.regiones.slice() : [],
      vista: Object.assign({ ocultar_caducadas: false, pestana_inicial: 'activas', orden_inicial: 'dias', dias_nuevo: 7 }, g.vista || {})
    };
    ajRender();
    ajModal.hidden = false;
  }
  function ajCerrar() { ajModal.hidden = true; }

  // Clics dentro de los grupos: activar/desactivar, borrar, añadir.
  ajGrupos.addEventListener('click', function (e) {
    const chip = e.target.closest('.aj-chip');
    if (chip) {
      const g = chip.dataset.g, tipo = chip.dataset.t, i = +chip.dataset.i;
      if (e.target.dataset.x) { ajCfg.categorias[g][tipo].splice(i, 1); }
      else { ajCfg.categorias[g][tipo][i].on = !ajCfg.categorias[g][tipo][i].on; }
      ajRenderGrupos();
      return;
    }
    const btn = e.target.closest('[data-addbtn]');
    if (btn) {
      const g = btn.dataset.g, tipo = btn.dataset.addbtn;
      const input = ajGrupos.querySelector('input[data-add="' + tipo + '"][data-g="' + g + '"]');
      let val = (input.value || '').trim();
      if (tipo === 'cpv') { val = ajSoloDigitos(val.split(' ')[0]); }
      if (val) { ajCfg.categorias[g][tipo].push({ v: val, on: true }); input.value = ''; ajRenderGrupos(); }
    }
  });
  ajGrupos.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.target.dataset && e.target.dataset.add) {
      e.preventDefault();
      const sel = '[data-addbtn="' + e.target.dataset.add + '"][data-g="' + e.target.dataset.g + '"]';
      const b = ajGrupos.querySelector(sel); if (b) b.click();
    }
  });

  function ajMarcados(cont) {
    return Array.from(cont.querySelectorAll('input:checked')).map(function (i) { return i.value; });
  }
  function ajRecoge() {
    return {
      categorias: ajCfg.categorias,
      fuentes: ajMarcados(ajFuentes),
      plataformas: ajMarcados(ajPlataformas),
      regiones: ajMarcados(ajRegiones),
      vista: {
        ocultar_caducadas: document.getElementById('aj-ocultar-caducadas').checked,
        pestana_inicial: document.getElementById('aj-pestana').value,
        orden_inicial: document.getElementById('aj-orden').value,
        dias_nuevo: Math.max(1, parseInt(document.getElementById('aj-dias-nuevo').value, 10) || 7)
      }
    };
  }
  async function ajGuardarCfg() {
    if (!sesionActiva) { ajMsg.textContent = 'Inicia sesión para guardar.'; ajMsg.className = 'aj-msg err'; return; }
    const cfg = ajRecoge();
    ajGuardar.disabled = true; ajMsg.textContent = 'Guardando…'; ajMsg.className = 'aj-msg';
    try {
      const { error } = await supabase.from('radar_config')
        .upsert({ id: 1, config: cfg, updated_at: new Date().toISOString() }, { onConflict: 'id' });
      if (error) throw error;
      cfgFiltro = construyeFiltroConfig(cfg);   // filtro de vista al instante
      ajMsg.textContent = 'Guardado ✓ — lo que quitaste se oculta ya; lo nuevo entra en la próxima recogida.';
      ajMsg.className = 'aj-msg ok';
      aplicarVistaInicial(cfg.vista);   // aplica ya los ajustes de vista
      actualizarVista();                // re-aplica el filtro (oculta lo que ya no encaja)
    } catch (e) {
      ajMsg.textContent = 'Error al guardar: ' + (e.message || e);
      ajMsg.className = 'aj-msg err';
    } finally { ajGuardar.disabled = false; }
  }

  if (ajBtn) ajBtn.addEventListener('click', ajAbrir);
  document.getElementById('aj-cerrar').addEventListener('click', ajCerrar);
  document.getElementById('aj-cancelar').addEventListener('click', ajCerrar);
  ajGuardar.addEventListener('click', ajGuardarCfg);
  ajModal.addEventListener('click', function (e) { if (e.target === ajModal) ajCerrar(); });

  // Aplica los ajustes de VISTA (pestaña/orden/ocultar caducadas) a la vista actual.
  function aplicarVistaInicial(vista) {
    if (!vista) return;
    ocultarCaducadas = !!vista.ocultar_caducadas;
    if (selectOrden && vista.orden_inicial) selectOrden.value = vista.orden_inicial;
    // BG-5: si la pestaña inicial guardada es 'favoritas', abrimos el subapartado
    // 'En observación' (ya no es una pestaña del Radar); si no, pestaña normal.
    if (vista.pestana_inicial === 'favoritas') mostrarVista('observacion');
    else if (vista.pestana_inicial) seleccionarPestana(vista.pestana_inicial);
    else actualizarVista();
  }
  // Al cargar la página: lee la config (lectura pública), monta el filtro de vista
  // por config (oculta lo que ya no encaja) y aplica los ajustes de vista guardados.
  (async function () {
    const g = await ajLeerConfig();
    cfgFiltro = construyeFiltroConfig(g);
    if (g && g.vista) aplicarVistaInicial(g.vista);
    else actualizarVista();
  })();
"""


# ============================================================================
# BG-4 · BUSCADOR GENERAL (UI sobre buscador_api.js)
# ----------------------------------------------------------------------------
# Decisión: "inline en build". Leemos buscador_api.js TAL CUAL (una sola fuente
# de verdad: el .js sigue siendo el módulo real y testeable) y le quitamos el
# 'export' para poder incrustarlo dentro del <script type=module>. Va envuelto
# en un IIFE para aislar sus helpers (COLUMNAS, aNumero, ...) del resto del
# módulo. Dentro del IIFE tiene en scope el cliente `supabase`, `fmtEur`,
# `sesionActiva` y `vistaActiva` que define JS_SUPABASE. Si el fichero no está,
# el buscador queda inerte pero el Radar se genera igual (degradación segura).
# ============================================================================
_ruta_api_buscador = Path("buscador_api.js")
_BUSCADOR_API_SRC = (
    _ruta_api_buscador.read_text(encoding="utf-8").replace(
        "export function crearBuscador", "function crearBuscador"
    )
    if _ruta_api_buscador.exists()
    else ""
)

# UI del buscador (cadena NORMAL: lleva llaves { } de JS, no es f-string).
JS_BUSCADOR_UI = """
  // === BG-4 · UI del Buscador general (vista #vista-buscador) ================
  // Privado: solo con sesión (el catálogo de ~588k es authenticated). Render de
  // tarjetas DINÁMICO en JS desde buscar() (los resultados no están horneados).
  const _bg = crearBuscador(supabase);
  const bgBuscar = _bg.buscar;

  const bgGate    = document.getElementById('bg-gate');
  const bgPanel   = document.getElementById('bg-panel');
  const bgTexto   = document.getElementById('bg-texto');
  const bgOrden   = document.getElementById('bg-orden');
  const bgPills   = document.getElementById('bg-pills');
  const bgMas     = document.getElementById('bg-mas');
  const bgAvanz   = document.getElementById('bg-avanzado');
  const bgCpv     = document.getElementById('bg-cpv');
  const bgCpvAdd  = document.getElementById('bg-cpv-add');
  const bgExp     = document.getElementById('bg-exp');
  const bgImpMin  = document.getElementById('bg-imp-min');
  const bgImpMax  = document.getElementById('bg-imp-max');
  const bgFinDesde= document.getElementById('bg-fin-desde');
  const bgFinHasta= document.getElementById('bg-fin-hasta');
  const bgPubDesde= document.getElementById('bg-pub-desde');
  const bgPubHasta= document.getElementById('bg-pub-hasta');
  const bgChips   = document.getElementById('bg-chips');
  const bgLimpiar = document.getElementById('bg-limpiar');
  const bgCont    = document.getElementById('bg-contador');
  const bgMsg     = document.getElementById('bg-estado-msg');
  const bgRes     = document.getElementById('bg-resultados');
  const bgPag     = document.getElementById('bg-paginacion');
  const bgPrev    = document.getElementById('bg-prev');
  const bgNext    = document.getElementById('bg-next');
  const bgPagInfo = document.getElementById('bg-pag-info');

  const BG_POR_PAGINA = 25;
  let bgPagina = 1;
  let bgYaBuscado = false;
  let bgCargando = false;
  // BG-5: filas de la página actual por licitacion_id, para que el toggle de la
  // estrella tenga a mano la fila del catálogo (evita re-consultar al hidratar).
  const bgFilasPorId = new Map();

  // Estado de filtros: FUENTE DE VERDAD. Los pills / inputs / chips lo reflejan;
  // bgParams() lo traduce a los params de buscar(). '' = sin filtro (Auto/Todas).
  const bgFiltros = {
    fuente: '', estado: '', orden: 'fecha_fin_plazo:asc',
    cpvPrefijo: [],                       // prefijos CPV (OR) -> RPC
    expediente: '',                       // Nº de expediente (contiene, normalizado) -> RPC
    impMin: '', impMax: '',               // valor_estimado (rango)
    finDesde: '', finHasta: '',           // fecha_fin_plazo (rango, yyyy-mm-dd)
    pubDesde: '', pubHasta: '',           // fecha_publicacion (rango, yyyy-mm-dd)
  };

  function bgEscape(s){ const d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML; }
  function bgFecha(iso){ if(!iso) return '—'; const d = new Date(iso); return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString('es-ES'); }
  function bgDias(iso){ if(!iso) return null; const h = new Date(); h.setHours(0,0,0,0); const d = new Date(iso); if(Number.isNaN(d.getTime())) return null; d.setHours(0,0,0,0); return Math.round((d - h) / 86400000); }
  function bgUrg(dias){ if(dias == null || dias < 0) return ''; if(dias < 3) return 'roja'; if(dias < 7) return 'ambar'; return 'verde'; }
  // Normaliza un Nº de expediente IGUAL que la BD (MAYÚSCULAS, sin espacios / . -),
  // para decidir en cliente si tiene el mínimo de 3 chars que dispara el filtro.
  function bgNormExp(s){ return String(s == null ? '' : s).toUpperCase().replace(/[\\s./-]/g, ''); }

  function bgChipsCpv(cpv){
    const lista = Array.isArray(cpv) ? cpv.filter(Boolean) : [];
    if(!lista.length) return '—';
    const TOPE = 6;
    const vis = lista.slice(0, TOPE), extra = lista.slice(TOPE);
    let html = vis.map(function(c){ return '<code>' + bgEscape(c) + '</code>'; }).join(' ');
    if(extra.length){
      const mas = 'y ' + extra.length + ' más';
      html += ' <span class="cpv-extra" hidden>'
            + extra.map(function(c){ return '<code>' + bgEscape(c) + '</code>'; }).join(' ')
            + '</span><button type="button" class="cpv-mas" aria-expanded="false" data-abrir="'
            + mas + '" data-cerrar="ocultar">' + mas + '</button>';
    }
    return html;
  }

  function bgTarjeta(f){
    const dias = bgDias(f.fecha_fin_plazo);
    const urg = bgUrg(dias);
    let coletilla = '';
    if(f.fecha_fin_plazo && dias != null){
      if(dias > 0) coletilla = ' · <span class="quedan urg-tx-' + urg + '">' + (dias === 1 ? 'queda 1 día' : 'quedan ' + dias + ' días') + '</span>';
      else if(dias === 0) coletilla = ' · <span class="vence-hoy urg-tx-roja">vence hoy</span>';
      else coletilla = ' <span class="cerrado">· cerrado</span>';
    }
    const enlace = f.enlace ? bgEscape(f.enlace) : '';
    const titulo = bgEscape(f.titulo || '(sin título)');
    const tituloHtml = enlace ? '<a href="' + enlace + '" target="_blank" rel="noopener">' + titulo + '</a>' : titulo;
    const org = f.organo_contratacion ? '<div class="bg-org">' + bgEscape(f.organo_contratacion) + '</div>' : '';
    const fuente = f.fuente ? '<span class="bg-fuente">' + bgEscape(f.fuente) + '</span>' : '';
    const valor = (f.valor_estimado != null) ? '<span class="bg-valor">' + fmtEur(f.valor_estimado) + '</span>' : '';
    const sep = (fuente && valor) ? ' · ' : '';
    // BG-5: estrella «En observación» en cada resultado. Escribe en 'decisiones' por
    // licitacion_id con el MISMO mecanismo del Radar (persistirDecision, módulo). El
    // estado inicial de la estrella se lee del espejo decisionesPorId ya cargado.
    const id = f.licitacion_id || '';
    const marcada = !!((decisionesPorId.get(id) || {}).favorita);
    const estrella = '<div class="card-ctrl bg-card-ctrl">'
      + '<button type="button" class="ctrl-estrella' + (marcada ? ' marcada' : '') + '"'
      + ' aria-pressed="' + (marcada ? 'true' : 'false') + '" aria-label="En observación"'
      + ' title="Poner o quitar de «En observación»">' + (marcada ? '★' : '☆') + '</button></div>';
    const exp = f.num_expediente ? '<div class="bg-exp"><span class="et">Nº exp.</span> ' + bgEscape(f.num_expediente) + '</div>' : '';
    return '<article class="card bg-card' + (urg ? ' urg-' + urg : '') + '" data-licitacion-id="' + bgEscape(id) + '">'
      + estrella
      + '<h2 class="card-title">' + tituloHtml + '</h2>'
      + org
      + '<div class="bg-meta">' + fuente + sep + valor + '</div>'
      + exp
      + '<div class="cpv"><span class="et">CPV</span> ' + bgChipsCpv(f.cpv) + '</div>'
      + '<div class="bg-plazo"><span class="et">Fin de plazo</span> ' + bgFecha(f.fecha_fin_plazo) + coletilla + '</div>'
      + '</article>';
  }

  function bgParams(){
    const f = bgFiltros;
    const partes = (f.orden || 'fecha_fin_plazo:asc').split(':');
    return {
      texto: bgTexto ? bgTexto.value : '',
      fuente: f.fuente || undefined,
      estado: f.estado || undefined,
      cpvPrefijo: f.cpvPrefijo.length ? f.cpvPrefijo.slice() : undefined,
      expediente: f.expediente || undefined,
      importeMin: f.impMin !== '' ? f.impMin : undefined,
      importeMax: f.impMax !== '' ? f.impMax : undefined,
      // Rango por día: 'desde' = inicio del día; 'hasta' = fin del día (inclusivo).
      fechaFinDesde: f.finDesde ? f.finDesde + 'T00:00:00' : undefined,
      fechaFinHasta: f.finHasta ? f.finHasta + 'T23:59:59' : undefined,
      fechaPubDesde: f.pubDesde ? f.pubDesde + 'T00:00:00' : undefined,
      fechaPubHasta: f.pubHasta ? f.pubHasta + 'T23:59:59' : undefined,
      ordenCampo: partes[0],
      ordenAsc: partes[1] !== 'desc',
      pagina: bgPagina,
      porPagina: BG_POR_PAGINA,
    };
  }

  // --- Pills: en cada grupo (fuente/estado) hay UN valor activo (radio).
  function bgSelPill(grupo, val){
    const cont = bgPills && bgPills.querySelector('.bg-pill-grupo[data-filtro="' + grupo + '"]');
    if(!cont) return;
    cont.querySelectorAll('.bg-pill').forEach(function(b){ b.classList.toggle('activo', (b.dataset.val || '') === val); });
  }

  // --- CPV: normaliza un token a prefijo (recorta y quita el dígito de control
  //     "-N" si el usuario pega el código oficial). Sin regex (evita escapes).
  function bgNormPrefijo(s){ return String(s == null ? '' : s).trim().split('-')[0].trim(); }
  function bgAddCpv(){
    if(!bgCpv) return;
    const tokens = bgCpv.value.split(',').join(' ').split(';').join(' ').split(' ');
    let cambiado = false;
    tokens.forEach(function(t){
      const p = bgNormPrefijo(t);
      if(p && bgFiltros.cpvPrefijo.indexOf(p) < 0){ bgFiltros.cpvPrefijo.push(p); cambiado = true; }
    });
    bgCpv.value = '';
    if(cambiado) bgSincroniza();
  }

  // Etiqueta "desde/hasta" para un rango (fmt formatea cada extremo).
  function bgRangoTxt(desde, hasta, fmt){
    if(desde && hasta) return fmt(desde) + ' – ' + fmt(hasta);
    if(desde) return 'desde ' + fmt(desde);
    return 'hasta ' + fmt(hasta);
  }

  // Pinta los chips de filtros activos y decide si se ve "Limpiar filtros".
  function bgRenderChips(){
    const f = bgFiltros;
    const chips = [];
    const FUENTE = { estatal:'Estatal', agregadas:'Agregadas' };
    const ESTADO = { abierta:'Abiertas', cerrada:'Cerradas', todas:'Todas' };
    if(f.fuente) chips.push({ tipo:'fuente', txt:'Fuente: ' + (FUENTE[f.fuente] || f.fuente) });
    if(f.estado) chips.push({ tipo:'estado', txt:'Estado: ' + (ESTADO[f.estado] || f.estado) });
    f.cpvPrefijo.forEach(function(p){ chips.push({ tipo:'cpv', val:p, txt:'CPV ' + p + '…' }); });
    // Solo mostramos el chip de expediente si de verdad dispara (>=3 chars normalizados),
    // para no engañar: con menos, el filtro no se aplica (búsqueda trivial).
    if(bgNormExp(f.expediente).length >= 3) chips.push({ tipo:'exp', txt:'Expediente: ' + f.expediente });
    if(f.impMin !== '' || f.impMax !== ''){
      let t;
      if(f.impMin !== '' && f.impMax !== '') t = fmtEur(Number(f.impMin)) + ' – ' + fmtEur(Number(f.impMax));
      else if(f.impMin !== '') t = '≥ ' + fmtEur(Number(f.impMin));
      else t = '≤ ' + fmtEur(Number(f.impMax));
      chips.push({ tipo:'importe', txt:'Importe: ' + t });
    }
    if(f.finDesde || f.finHasta) chips.push({ tipo:'fin', txt:'Fin: ' + bgRangoTxt(f.finDesde, f.finHasta, bgFecha) });
    if(f.pubDesde || f.pubHasta) chips.push({ tipo:'pub', txt:'Publicación: ' + bgRangoTxt(f.pubDesde, f.pubHasta, bgFecha) });

    if(bgChips){
      bgChips.innerHTML = chips.map(function(c){
        return '<span class="bg-chip" data-tipo="' + c.tipo + '"' + (c.val != null ? ' data-val="' + bgEscape(c.val) + '"' : '') + '>'
             + bgEscape(c.txt)
             + ' <button type="button" class="bg-chip-x" aria-label="Quitar filtro">×</button></span>';
      }).join('');
      bgChips.hidden = chips.length === 0;
    }
    const hayTexto = !!(bgTexto && bgTexto.value.trim());
    if(bgLimpiar) bgLimpiar.hidden = !(chips.length || hayTexto);
  }

  // Cambió un filtro -> refresca chips, vuelve a la página 1 y relanza.
  function bgSincroniza(){ bgRenderChips(); bgReinicia(); }

  // "Limpiar filtros": resetea TODO (incluido el texto) y relanza.
  function bgLimpiarTodo(){
    if(bgTexto) bgTexto.value = '';
    bgFiltros.fuente = ''; bgFiltros.estado = ''; bgFiltros.orden = 'fecha_fin_plazo:asc';
    bgFiltros.cpvPrefijo = [];
    bgFiltros.expediente = '';
    bgFiltros.impMin = ''; bgFiltros.impMax = '';
    bgFiltros.finDesde = ''; bgFiltros.finHasta = ''; bgFiltros.pubDesde = ''; bgFiltros.pubHasta = '';
    bgSelPill('fuente', ''); bgSelPill('estado', '');
    if(bgOrden) bgOrden.value = 'fecha_fin_plazo:asc';
    [bgImpMin, bgImpMax, bgFinDesde, bgFinHasta, bgPubDesde, bgPubHasta, bgCpv, bgExp].forEach(function(el){ if(el) el.value = ''; });
    bgSincroniza();
  }

  // ¿El error es un timeout de sentencia (57014)? La 1ª consulta tras inactividad puede
  // expirar y la 2ª ir instantánea (arranque en frío conocido de Postgres).
  function bgEs57014(err){
    if(!err) return false;
    const code = (err.code || '') + '';
    const msg = (err.message || '') + '';
    return code === '57014' || /statement timeout/i.test(msg);
  }
  // Búsqueda con UN auto-reintento si la 1ª da 57014, para que el primer acceso en frío
  // no enseñe el error. Reintenta la MISMA consulta tras una breve espera (una sola vez).
  async function bgBuscarR(params){
    let r = await bgBuscar(params);
    if(r && r.error && bgEs57014(r.error)){
      if(bgMsg){ bgMsg.hidden = false; bgMsg.textContent = 'Reintentando…'; }
      await new Promise(function(res){ setTimeout(res, 700); });
      r = await bgBuscar(params);   // único reintento; su resultado se devuelve tal cual
    }
    return r;
  }

  async function bgRun(){
    if(!sesionActiva){ bgActualizarGate(); return; }
    if(bgCargando) return;
    bgCargando = true;
    bgYaBuscado = true;
    if(bgMsg){ bgMsg.hidden = false; bgMsg.textContent = 'Buscando…'; }
    if(bgPag){ bgPag.hidden = true; if(bgPagInfo) bgPagInfo.textContent = ''; }
    const r = await bgBuscarR(bgParams());
    bgCargando = false;
    if(r.error){
      if(bgRes) bgRes.innerHTML = '';
      if(bgMsg){ bgMsg.hidden = false; bgMsg.textContent = 'Error en la búsqueda: ' + (r.error.message || r.error); }
      if(bgCont) bgCont.textContent = '';
      if(bgPag){ bgPag.hidden = true; if(bgPagInfo) bgPagInfo.textContent = ''; }   // sin "Página N" heredado
      return;
    }
    const filas = r.filas || [];
    bgFilasPorId.clear();
    if(!filas.length){
      if(bgRes) bgRes.innerHTML = '';
      if(bgMsg){ bgMsg.hidden = false; bgMsg.textContent = 'Sin resultados. Prueba con otros términos o filtros.'; }
      if(bgCont) bgCont.textContent = '';
      if(bgPag) bgPag.hidden = true;
      return;
    }
    filas.forEach(function(f){ if(f && f.licitacion_id) bgFilasPorId.set(f.licitacion_id, f); });
    if(bgMsg) bgMsg.hidden = true;
    if(bgRes) bgRes.innerHTML = filas.map(bgTarjeta).join('');
    const ini = (r.pagina - 1) * r.porPagina + 1;
    const fin = ini + filas.length - 1;
    // Contador: 'topado' = el total es un TOPE ("más de N"); 'aproximado' = "≈ N".
    const nTot = (r.total || 0).toLocaleString('es-ES');
    const tot = r.topado ? ('más de ' + nTot) : ((r.aproximado ? '≈ ' : '') + nTot);
    if(bgCont) bgCont.innerHTML = 'Mostrando <b>' + ini + '–' + fin + '</b> de <b>' + tot + '</b>';
    const totalPag = Math.max(1, Math.ceil((r.total || 0) / r.porPagina));
    if(bgPag) bgPag.hidden = false;
    if(bgPrev) bgPrev.disabled = r.pagina <= 1;
    if(bgNext) bgNext.disabled = r.pagina >= totalPag;
    const pagPref = r.topado ? '' : (r.aproximado ? '≈ ' : '');
    const pagSuf = r.topado ? '+' : '';
    if(bgPagInfo) bgPagInfo.textContent = 'Página ' + r.pagina + ' de ' + pagPref + totalPag + pagSuf;
  }

  let bgTimer = null;
  function bgReinicia(){ bgPagina = 1; bgRun(); }

  // Texto: debounce. Re-render de chips también (el botón "Limpiar" aparece con texto).
  if(bgTexto) bgTexto.addEventListener('input', function(){ clearTimeout(bgTimer); bgTimer = setTimeout(function(){ bgRenderChips(); bgReinicia(); }, 300); });

  // Orden (select).
  if(bgOrden) bgOrden.addEventListener('change', function(){ bgFiltros.orden = bgOrden.value; bgReinicia(); });

  // Pills fuente/estado (delegación: un valor activo por grupo).
  if(bgPills) bgPills.addEventListener('click', function(e){
    const btn = e.target.closest('.bg-pill'); if(!btn) return;
    const grupo = btn.closest('.bg-pill-grupo'); if(!grupo) return;
    const campo = grupo.dataset.filtro;
    if(campo !== 'fuente' && campo !== 'estado') return;
    bgFiltros[campo] = btn.dataset.val || '';
    grupo.querySelectorAll('.bg-pill').forEach(function(b){ b.classList.toggle('activo', b === btn); });
    bgSincroniza();
  });

  // Panel "Más filtros": mostrar/ocultar.
  if(bgMas) bgMas.addEventListener('click', function(){
    const abierto = bgMas.getAttribute('aria-expanded') === 'true';
    bgMas.setAttribute('aria-expanded', abierto ? 'false' : 'true');
    if(bgAvanz) bgAvanz.hidden = abierto;
  });

  // CPV por prefijo: botón "Añadir" o Enter.
  if(bgCpvAdd) bgCpvAdd.addEventListener('click', bgAddCpv);
  if(bgCpv) bgCpv.addEventListener('keydown', function(e){ if(e.key === 'Enter'){ e.preventDefault(); bgAddCpv(); } });

  // Nº de expediente: al confirmar (blur/Enter) actualiza estado y relanza.
  if(bgExp){
    bgExp.addEventListener('change', function(){ bgFiltros.expediente = bgExp.value; bgSincroniza(); });
    bgExp.addEventListener('keydown', function(e){ if(e.key === 'Enter'){ e.preventDefault(); bgFiltros.expediente = bgExp.value; bgSincroniza(); } });
  }

  // Importe y fechas: al confirmar (change = blur/Enter) actualizan estado y relanzan.
  if(bgImpMin)   bgImpMin.addEventListener('change', function(){ bgFiltros.impMin = bgImpMin.value; bgSincroniza(); });
  if(bgImpMax)   bgImpMax.addEventListener('change', function(){ bgFiltros.impMax = bgImpMax.value; bgSincroniza(); });
  if(bgFinDesde) bgFinDesde.addEventListener('change', function(){ bgFiltros.finDesde = bgFinDesde.value; bgSincroniza(); });
  if(bgFinHasta) bgFinHasta.addEventListener('change', function(){ bgFiltros.finHasta = bgFinHasta.value; bgSincroniza(); });
  if(bgPubDesde) bgPubDesde.addEventListener('change', function(){ bgFiltros.pubDesde = bgPubDesde.value; bgSincroniza(); });
  if(bgPubHasta) bgPubHasta.addEventListener('change', function(){ bgFiltros.pubHasta = bgPubHasta.value; bgSincroniza(); });

  // "Limpiar filtros".
  if(bgLimpiar) bgLimpiar.addEventListener('click', bgLimpiarTodo);

  // Chips activos: quitar uno (delegación).
  if(bgChips) bgChips.addEventListener('click', function(e){
    const x = e.target.closest('.bg-chip-x'); if(!x) return;
    const chip = x.closest('.bg-chip'); if(!chip) return;
    const tipo = chip.dataset.tipo;
    if(tipo === 'fuente'){ bgFiltros.fuente = ''; bgSelPill('fuente', ''); }
    else if(tipo === 'estado'){ bgFiltros.estado = ''; bgSelPill('estado', ''); }
    else if(tipo === 'cpv'){ const v = chip.dataset.val; bgFiltros.cpvPrefijo = bgFiltros.cpvPrefijo.filter(function(p){ return p !== v; }); }
    else if(tipo === 'exp'){ bgFiltros.expediente = ''; if(bgExp) bgExp.value = ''; }
    else if(tipo === 'importe'){ bgFiltros.impMin = ''; bgFiltros.impMax = ''; if(bgImpMin) bgImpMin.value = ''; if(bgImpMax) bgImpMax.value = ''; }
    else if(tipo === 'fin'){ bgFiltros.finDesde = ''; bgFiltros.finHasta = ''; if(bgFinDesde) bgFinDesde.value = ''; if(bgFinHasta) bgFinHasta.value = ''; }
    else if(tipo === 'pub'){ bgFiltros.pubDesde = ''; bgFiltros.pubHasta = ''; if(bgPubDesde) bgPubDesde.value = ''; if(bgPubHasta) bgPubHasta.value = ''; }
    bgSincroniza();
  });

  if(bgPrev) bgPrev.addEventListener('click', function(){ if(bgPagina > 1){ bgPagina--; bgRun(); } });
  if(bgNext) bgNext.addEventListener('click', function(){ bgPagina++; bgRun(); });
  // "y N más" de CPV (delegado en el contenedor de resultados del buscador).
  if(bgRes) bgRes.addEventListener('click', function(e){
    const btn = e.target.closest('.cpv-mas'); if(!btn) return;
    const extra = btn.parentElement.querySelector('.cpv-extra');
    const abierto = btn.getAttribute('aria-expanded') === 'true';
    if(extra) extra.hidden = abierto;
    btn.setAttribute('aria-expanded', abierto ? 'false' : 'true');
    btn.textContent = abierto ? btn.dataset.abrir : btn.dataset.cerrar;
  });

  // BG-5: estrella «En observación» de cada resultado (delegado). Escribe en
  // 'decisiones' con el MISMO núcleo del Radar (persistirDecision) y refleja el
  // cambio en la pestaña 'En observación' al instante (sincronizarObservacionTrasFav).
  async function bgToggleObservacion(card){
    if(!sesionActiva || !card) return;
    const id = card.getAttribute('data-licitacion-id'); if(!id) return;
    const actual = decisionesPorId.get(id) || { estado:null, favorita:false };
    const nuevaFav = !(actual.favorita === true);
    const btn = card.querySelector('.ctrl-estrella');
    if(btn) btn.disabled = true;
    try{
      await persistirDecision(id, actual.estado || null, nuevaFav);
      pintaFavorita(card, nuevaFav);   // pinta la estrella del propio resultado
      sincronizarObservacionTrasFav(id, bgFilasPorId.get(id) || null, nuevaFav);
    }catch(err){
      console.error('Error guardando «En observación» (buscador):', err.message || err);
      pintaFavorita(card, actual.favorita === true);   // no mentir: revertir visual
    }finally{
      if(btn) btn.disabled = false;
    }
  }
  if(bgRes) bgRes.addEventListener('click', function(e){
    const btn = e.target.closest('.ctrl-estrella'); if(!btn) return;
    const card = btn.closest('.card'); if(card) bgToggleObservacion(card);
  });

  function bgActualizarGate(){
    const dentro = !!sesionActiva;
    if(bgGate)  bgGate.hidden  = dentro;
    if(bgPanel) bgPanel.hidden = !dentro;
    if(dentro && !bgYaBuscado) bgRun();   // primera búsqueda al entrar logueado
  }

  // Reaccionar a login/logout (listener propio, independiente del módulo).
  supabase.auth.onAuthStateChange(function(){
    setTimeout(function(){ if(vistaActiva === 'buscador') bgActualizarGate(); }, 0);
  });

  // Hook que llama mostrarVista al entrar en la vista.
  window.__bgEntrar = function(){ bgActualizarGate(); if(sesionActiva && bgTexto) bgTexto.focus(); };

  // Si la página cargó directamente en el buscador, refresca el gate ya.
  if(typeof vistaActiva !== 'undefined' && vistaActiva === 'buscador') window.__bgEntrar();
"""

# Envolvemos api + UI en un IIFE (aísla helpers; ve `supabase` por closure).
JS_BUSCADOR = (
    ("\n  // ===================== BG-4: BUSCADOR GENERAL =====================\n"
     "  (function () {\n" + _BUSCADOR_API_SRC + "\n" + JS_BUSCADOR_UI + "\n  })();\n")
    if _BUSCADOR_API_SRC else ""
)


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


def clasifica_urgencia(dias):
    """Semáforo por días que faltan para el fin de plazo (solo plazos ABIERTOS,
    dias >= 0): 'roja' si <3, 'ambar' si <7, 'verde' si >=7. Devuelve None cuando
    no aplica (sin fecha, o plazo ya cerrado): en ese caso la tarjeta no se tiñe.
    'dias' viene de dias_restantes(), que ya cuenta en Europe/Madrid."""
    if dias is None or dias < 0:
        return None
    if dias < 3:
        return "roja"
    if dias < 7:
        return "ambar"
    return "verde"


def o_guion(texto):
    """Devuelve el texto, o '—' si es None (para celdas sin dato)."""
    return texto if texto is not None else "—"


# --- Estado PÚBLICO por fecha (activa/caducada) -----------------------------
# Este es el ÚNICO estado que calcula Python: el público, derivado de la fecha de
# fin de plazo. Los estados manuales (ganada/perdida/presentada/descartada) y la
# estrella (favorita) son PRIVADOS y los pinta el navegador desde Supabase tras
# iniciar sesión; aquí no se tocan.

def _fecha_a_date(valor):
    """Convierte la fecha de fin de plazo del JSON a un objeto date.
    El feed la guarda como 'YYYY-MM-DD' (cbc:EndDate del CODICE); por si algún
    día viniera con hora ('YYYY-MM-DDThh:mm...'), nos quedamos con los 10 primeros
    caracteres. Devuelve None si falta o no se entiende."""
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return None


def calcular_estado(fecha_fin, ahora):
    """Calcula el estado PÚBLICO de una licitación por su fecha de fin de plazo:
      - tiene fecha y ya pasó (< ahora) -> 'caducada'.
      - en cualquier otro caso (incluida sin fecha) -> 'activa'.
    'ahora' es la fecha de hoy en Europe/Madrid (misma zona que las fechas del
    JSON). Los estados manuales y la favorita NO se calculan aquí: son privados y
    los aporta el navegador desde Supabase tras login."""
    fin = _fecha_a_date(fecha_fin)
    if fin is not None and fin < ahora:
        return "caducada"
    return "activa"


# Controles por tarjeta (estrella favorita + menú de estado manual + botón Detalles).
# Se HORNEAN en cada tarjeta pero arrancan OCULTOS por CSS: solo se ven con sesión
# iniciada (body.sesion los muestra). El navegador rellena su valor desde Supabase y
# guarda los cambios (ver JS_SUPABASE). "Activa" = sin decisión manual (la licitación
# sigue su estado público por fecha, que puede ser caducada). "Detalles" abre el panel
# del contrato (tabla 'contratos'), privado igual que las decisiones.
CONTROLES_CARD = """<div class="card-ctrl">
        <button type="button" class="ctrl-estrella" aria-pressed="false" aria-label="En observación" title="Poner o quitar de «En observación»">☆</button>
        <span class="ctrl-cap">Estado</span>
        <select class="ctrl-estado" aria-label="Estado manual">
          <option value="activa">Activa</option>
          <option value="presentada">Presentada</option>
          <option value="ganada">Ganada</option>
          <option value="perdida">Perdida</option>
          <option value="descartada">Descartada</option>
        </select>
        <button type="button" class="ctrl-detalles" aria-label="Detalles del contrato" title="Detalles del contrato">Detalles</button>
        <span class="card-aviso" hidden></span>
      </div>"""


# Modal ÚNICO de detalles del contrato (tabla 'contratos'). Se hornea una sola vez
# (dentro de #radar, zona privada) y arranca oculto; el JS lo abre para una licitación
# concreta, lee su fila por licitacion_id y guarda con upsert. NADA de esto se rellena
# en Python: los importes/datos son PRIVADOS y solo viajan en el navegador tras login.
# Los <input> llevan id "c-<columna>" para mapearlos con la tabla desde el JS.
CONTRATO_MODAL = """  <div class="modal-fondo" id="contrato-modal" hidden>
    <div class="modal-caja" role="dialog" aria-modal="true" aria-labelledby="contrato-titulo">
      <div class="modal-cab">
        <h2 id="contrato-titulo">Detalles del contrato</h2>
        <button type="button" class="modal-cerrar" id="contrato-cerrar" aria-label="Cerrar panel">✕</button>
      </div>
      <p class="modal-sub" id="contrato-licitacion"></p>
      <form id="contrato-form" class="contrato-form">
        <label>Nº de expediente<input type="text" id="c-num_expediente" autocomplete="off"></label>
        <label>Adjudicatario<input type="text" id="c-adjudicatario" autocomplete="off"></label>
        <label>CIF del adjudicatario<input type="text" id="c-cif_adjudicatario" autocomplete="off"></label>
        <label>Importe sin IVA (€)<input type="number" step="0.01" inputmode="decimal" id="c-importe_sin_iva"></label>
        <label>Importe con IVA (€)<input type="number" step="0.01" inputmode="decimal" id="c-importe_con_iva"></label>
        <label>Fecha de adjudicación<input type="date" id="c-fecha_adjudicacion"></label>
        <label>Fecha de inicio<input type="date" id="c-fecha_inicio"></label>
        <label>Fecha de fin<input type="date" id="c-fecha_fin"></label>
        <label>Prórroga hasta<input type="date" id="c-prorroga_hasta"></label>
        <label class="ancho">Notas<textarea id="c-notas" rows="3"></textarea></label>
        <div class="modal-pie">
          <span class="pie-feedback">
            <span class="contrato-aviso" id="contrato-aviso" hidden></span>
            <span class="contrato-ok" id="contrato-ok" hidden>Guardado ✓</span>
            <span class="contrato-ok" id="cartera-ok" hidden>Hecho ✓</span>
          </span>
          <button type="button" class="btn-sec btn-cartera" id="contrato-cartera" disabled title="Guarda primero el contrato">Añadir a cartera</button>
          <button type="button" class="btn-sec" id="contrato-cancelar">Cerrar</button>
          <button type="submit" class="btn-pri" id="contrato-guardar">Guardar</button>
        </div>
      </form>
      <section class="docs-sec">
        <h3 class="docs-titulo">Documentos</h3>
        <ul class="docs-lista" id="docs-lista"></ul>
        <p class="docs-vacio" id="docs-vacio" hidden>Aún no hay documentos.</p>
        <form class="docs-form" id="docs-form">
          <input type="file" accept="application/pdf" id="doc-file">
          <select id="doc-tipo" aria-label="Tipo de documento">
            <option value="pliego">Pliego</option>
            <option value="contrato">Contrato</option>
            <option value="oferta">Oferta</option>
            <option value="otro">Otro</option>
          </select>
          <button type="submit" class="btn-pri" id="doc-subir">Subir</button>
        </form>
      </section>
    </div>
  </div>
"""


# Modal de DOCUMENTOS de una adjudicación de la cartera. Mismo patrón/clases que el
# modal de las licitaciones (.modal-fondo/.modal-caja/.docs-sec...), pero sin formulario
# de contrato: solo subir/listar/abrir/borrar PDFs (tabla cartera_documentos, bucket
# 'documentos'). Se hornea vacío; lo rellena el JS tras login (privado).
CARTERA_DOCS_MODAL = """  <div class="modal-fondo" id="cartera-docs-modal" hidden>
    <div class="modal-caja" role="dialog" aria-modal="true" aria-labelledby="cdoc-titulo">
      <div class="modal-cab">
        <h2 id="cdoc-titulo">Detalle de la adjudicación</h2>
        <button type="button" class="modal-cerrar" id="cdoc-cerrar" aria-label="Cerrar panel">✕</button>
      </div>
      <p class="modal-sub" id="cdoc-sub"></p>
      <div id="cdoc-datos"></div>
      <div class="cdoc-anot">
        <h3 class="docs-titulo">Anotaciones</h3>
        <textarea id="cdoc-anotaciones" placeholder="Notas privadas sobre esta adjudicación…"></textarea>
        <button type="button" class="btn-pri" id="cdoc-guardar-anot">Guardar anotación</button>
      </div>
      <section class="docs-sec docs-sec-suelta">
        <h3 class="docs-titulo">Documentos</h3>
        <ul class="docs-lista" id="cdoc-lista"></ul>
        <p class="docs-vacio" id="cdoc-vacio" hidden>Aún no hay documentos.</p>
        <form class="docs-form" id="cdoc-form">
          <input type="file" accept="application/pdf" id="cdoc-file">
          <select id="cdoc-tipo" aria-label="Tipo de documento">
            <option value="contrato">Contrato</option>
            <option value="pliego">Pliego</option>
            <option value="oferta">Oferta</option>
            <option value="otro">Otro</option>
          </select>
          <button type="submit" class="btn-pri" id="cdoc-subir">Subir</button>
        </form>
      </section>
    </div>
  </div>
"""


# Panel de AJUSTES del radar (⚙️, privado tras login). El JS (JS_SUPABASE) lo rellena
# (grupos/CPV/palabras, plataformas, regiones) al abrirlo y guarda en radar_config.
AJUSTES_MODAL = """  <div class="modal-fondo" id="ajustes-modal" hidden>
    <div class="modal-caja aj-caja" role="dialog" aria-modal="true" aria-labelledby="aj-titulo">
      <div class="modal-cab">
        <h2 id="aj-titulo">⚙️ Ajustes del radar</h2>
        <button type="button" class="modal-cerrar" id="aj-cerrar" aria-label="Cerrar ajustes">✕</button>
      </div>
      <p class="modal-sub">Configura qué busca el radar y cómo se ve. Lo de "qué caza" afecta a la próxima recogida del robot.</p>

      <section class="aj-sec">
        <h3 class="aj-h">Qué caza el radar</h3>
        <p class="aj-ayuda">Por grupo: sus CPV y palabras clave. Clic en un término para activarlo/desactivarlo; ✕ para borrarlo.</p>
        <div id="aj-grupos"></div>
      </section>

      <section class="aj-sec">
        <h3 class="aj-h">Dónde busca</h3>
        <div class="aj-bloque"><span class="aj-et">Fuentes</span><div class="aj-checks" id="aj-fuentes"></div></div>
        <div class="aj-bloque"><span class="aj-et">Plataformas</span><div class="aj-checks" id="aj-plataformas"></div></div>
        <div class="aj-bloque"><span class="aj-et">Regiones</span><div class="aj-checks" id="aj-regiones"></div></div>
        <p class="aj-ayuda">Sin marcar nada en un bloque = no filtra por eso (entra todo).</p>
      </section>

      <section class="aj-sec">
        <h3 class="aj-h">Vista</h3>
        <label class="aj-linea"><input type="checkbox" id="aj-ocultar-caducadas"> Ocultar caducadas por defecto</label>
        <label class="aj-linea">Pestaña inicial
          <select id="aj-pestana">
            <option value="favoritas">En observación</option><option value="activas">Activas</option>
            <option value="presentadas">Presentadas</option><option value="ganadas">Ganadas</option>
            <option value="perdidas">Perdidas</option><option value="descartadas">Descartadas</option>
            <option value="caducadas">Caducadas</option><option value="todas">Todas</option>
          </select>
        </label>
        <label class="aj-linea">Orden inicial
          <select id="aj-orden">
            <option value="dias">Días restantes</option><option value="pub">Fecha de publicación</option>
            <option value="nombre">Nombre (A–Z)</option><option value="subida">Fecha de subida</option>
            <option value="importe">Importe</option>
          </select>
        </label>
        <label class="aj-linea">Días para marcar «NUEVO»
          <input type="number" id="aj-dias-nuevo" min="1" max="60" step="1" style="width:70px">
        </label>
      </section>

      <div class="modal-pie">
        <span class="aj-msg" id="aj-msg"></span>
        <button type="button" class="btn-sec" id="aj-cancelar">Cancelar</button>
        <button type="button" class="btn-pri" id="aj-guardar">Guardar</button>
      </div>
      <datalist id="aj-cpv-catalogo"></datalist>
    </div>
  </div>
"""


def construye_tarjeta(lic, es_nueva, hoy, estado):
    """Devuelve el HTML (texto) de UNA tarjeta para una licitación.
    'hoy' es la fecha de hoy en Europe/Madrid, para el contador de días.
    'estado' es el estado PÚBLICO por fecha (activa/caducada). Lo privado (estado
    manual y estrella) lo añade el navegador desde Supabase tras login; aquí solo
    se hornean los controles (ocultos) y data-favorita="false" como base."""
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
    # Mostramos PRIMERO los CPV que COINCIDEN con un prefijo activo de la config
    # (mismo criterio "empieza por" de filtrar.py: c.startswith(p)) y colapsamos el
    # resto: un acuerdo marco puede traer cientos de CPV (uno por lote) y volcarlos
    # todos hace la tarjeta ilegible. La coincidencia se calcula aquí, en render
    # (estado público), igual que el resto del espejo estático.
    if cpv_prefijos_activos:
        coincidentes = [c for c in cpvs if any(c.startswith(p) for p in cpv_prefijos_activos)]
    else:
        coincidentes = []   # sin prefijos activos: no sabemos cuáles coinciden
    _TOPE_CPV = 6
    if coincidentes:
        _set_coin = set(coincidentes)
        no_coincidentes = [c for c in cpvs if c not in _set_coin]
        cpvs_visibles = coincidentes[:_TOPE_CPV]
        # El resto (coincidentes que no caben + los no coincidentes) va colapsado.
        cpvs_extra = coincidentes[_TOPE_CPV:] + no_coincidentes
    else:
        # Ninguno coincide por CPV (cazada por palabra clave, o sin prefijos activos):
        # enseñamos los primeros como contexto y colapsamos el resto.
        cpvs_visibles = cpvs[:_TOPE_CPV]
        cpvs_extra = cpvs[_TOPE_CPV:]

    def _chip(c):   # un <code> con el NOMBRE del CPV como title (tooltip al pasar el ratón)
        return f'<code title="{html.escape(cpv_nombres.get(c, ""))}">{html.escape(c)}</code>'

    if cpvs_visibles:
        cpv_html = " ".join(_chip(c) for c in cpvs_visibles)
        if cpvs_extra:
            # Los extra van ocultos en un <span hidden>; el botón "y N más" los
            # despliega/colapsa en el navegador (delegado en el grid). data-abrir/
            # data-cerrar guardan las dos etiquetas para no recalcular el conteo.
            extra_html = " ".join(_chip(c) for c in cpvs_extra)
            mas = f"y {len(cpvs_extra)} más"
            cpv_html += (
                f' <span class="cpv-extra" hidden>{extra_html}</span>'
                f'<button type="button" class="cpv-mas" aria-expanded="false"'
                f' data-abrir="{mas}" data-cerrar="ocultar">{mas}</button>'
            )
    else:
        cpv_html = "—"
    # data-cpv lleva TODOS los códigos (sin recortar): no se ven, pero el filtro por
    # CPV y el filtro de vista por config los necesitan completos para casar bien.
    data_cpv = html.escape(" ".join(cpvs))
    # Territorio/fuente como data-* para el filtro de vista por config (en el navegador).
    data_plataforma = html.escape(lic.get("plataforma") or "Estado")
    data_region = html.escape(lic.get("region_codigo") or "")
    data_fuente = html.escape(lic.get("fuente") or "estatal")

    # --- Datos económicos y fechas (ya guardados en el JSON por filtrar.py) ----
    # Los formateamos para mostrarlos. Si un campo es null, mostramos "—" (con
    # o_guion) para que la tarjeta mantenga siempre la misma estructura y quede
    # claro que ese dato no está disponible. Estos textos los generamos nosotros
    # (números y fechas), así que son seguros y no hace falta escaparlos.
    presu_con = o_guion(formatea_euros(lic.get("presupuesto_con_iva")))
    presu_sin = o_guion(formatea_euros(lic.get("presupuesto_sin_iva")))
    valor_est = o_guion(formatea_euros(lic.get("valor_estimado")))
    fecha_pub = o_guion(formatea_fecha(lic.get("fecha_publicacion")))

    # Fin de plazo: a la fecha le añadimos una coletilla según los días que falten,
    # en el TONO FUERTE del semáforo de urgencia (rojo <3, ámbar <7, verde >=7).
    #   - aún abierto (>0): "· quedan X días" (tono según urgencia)
    #   - vence hoy  (=0):  "· vence hoy" (rojo: es <3 días)
    #   - ya pasó    (<0):  "· cerrado" (rojo)
    #   - sin fecha (None): solo "—", sin coletilla
    fin_plazo_txt = formatea_fecha(lic.get("fecha_fin_plazo"))
    dias = dias_restantes(lic.get("fecha_fin_plazo"), hoy)
    urgencia = clasifica_urgencia(dias)   # 'roja'|'ambar'|'verde'|None (semáforo público)
    if fin_plazo_txt is None:
        fin_plazo = "—"
    elif dias is None:
        fin_plazo = fin_plazo_txt
    elif dias > 0:
        # "queda 1 día" (singular) / "quedan N días" (plural).
        texto_dias = "queda 1 día" if dias == 1 else f"quedan {dias} días"
        fin_plazo = f'{fin_plazo_txt} · <span class="quedan urg-tx-{urgencia}">{texto_dias}</span>'
    elif dias == 0:
        fin_plazo = f'{fin_plazo_txt} · <span class="vence-hoy urg-tx-roja">vence hoy</span>'
    else:
        fin_plazo = f'{fin_plazo_txt} <span class="cerrado">· cerrado</span>'

    # Nº de expediente del órgano (si el feed lo trae). Va como fila del bloque de datos.
    _num_exp = lic.get("num_expediente")
    num_exp_row = (f'\n        <div class="dato"><span class="et-dato">Nº de expediente</span>'
                   f'<span class="val-dato">{html.escape(_num_exp)}</span></div>') if _num_exp else ""

    datos_html = f"""<div class="datos">{num_exp_row}
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

    # Datos de la LICITACIÓN para el snapshot a cartera (botón del modal Detalles).
    # El Radar guarda 'organismo' (órgano de contratación = cliente) y NO trae 'objeto'
    # (usamos el título como respaldo). Se escapan porque van en atributos.
    data_objeto = html.escape(lic.get("objeto") or lic.get("titulo", ""))
    data_organo = html.escape(lic.get("organismo") or "")
    _presu_sin = lic.get("presupuesto_sin_iva")
    data_presu_sin = "" if _presu_sin is None else f"{_presu_sin}"

    # data-licitacion-id: el MISMO id del JSON (entry.id). Es la clave con la que el
    # navegador casa cada fila de la tabla 'decisiones' de Supabase con su tarjeta.
    lic_id = html.escape(lic.get("id", ""))

    # data-estado es el estado PÚBLICO por fecha (activa/caducada); 'estado' siempre
    # es uno de esos dos: seguro. data-favorita arranca en "false": lo privado (estado
    # manual y estrella) lo sobrescribe el navegador tras login. La clase urg-* (si la
    # hay) tiñe la tarjeta por urgencia; el CSS solo la aplica si data-estado="activa".
    clase_urgencia = f" urg-{urgencia}" if urgencia else ""
    return f"""    <article class="card{clase_urgencia}"
      data-importe="{data_importe}" data-fin-plazo="{data_fin_plazo}"
      data-fecha-pub="{data_fecha_pub}" data-fecha-subida="{data_fecha_subida}"
      data-cpv="{data_cpv}"
      data-plataforma="{data_plataforma}" data-region="{data_region}" data-fuente="{data_fuente}"
      data-titulo="{titulo}" data-licitacion-id="{lic_id}"
      data-objeto="{data_objeto}" data-organo="{data_organo}" data-presu-sin="{data_presu_sin}"
      data-estado="{estado}" data-favorita="false">
      {CONTROLES_CARD}
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

# Nombres de los CPV (código -> nombre en español), para el desplegable de filtro
# y el tooltip de cada CPV en las tarjetas. Lo genera actualizar_cpv.py desde la
# lista oficial. Si el archivo no está, seguimos sin nombres (mostramos solo el
# código): la web se genera igual.
ruta_cpv = Path("data") / "cpv_nombres.json"
cpv_nombres = {}
if ruta_cpv.exists():
    try:
        cpv_nombres = json.loads(ruta_cpv.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        cpv_nombres = {}

# --- 2. Pasamos los valores a una lista y la ordenamos ----------------------
# De más RECIENTE a más antigua según "primera_vez" (por eso reverse=True).
licitaciones = list(datos.values())
licitaciones.sort(key=lambda lic: datetime.fromisoformat(lic["primera_vez"]), reverse=True)

# --- 3. ¿A partir de qué fecha algo cuenta como "nuevo"? --------------------
# Comparamos SOLO fechas (sin horas). 'hoy' en zona Europe/Madrid (igual que el
# contador de días de las tarjetas), para no usar la hora UTC del runner de Actions.
# El umbral (días) sale de la config del radar (panel de ajustes); por defecto 7.
hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
config_radar = lee_config_radar()
_dn = (config_radar.get("vista") or {}).get("dias_nuevo")
dias_nuevo = _dn if isinstance(_dn, int) and _dn > 0 else DIAS_NUEVO
fecha_limite = hoy - timedelta(days=dias_nuevo)

# --- 3.bis Datos que necesita el panel de ajustes (⚙️) ----------------------
# Semilla de criterios (la primera vez que abras el panel, antes de guardar nada):
# los grupos/cpv/palabras de intereses.yaml, para no arrancar en blanco.
try:
    with open("intereses.yaml", encoding="utf-8") as f:
        criterios_defecto = yaml.safe_load(f) or {}
except (OSError, yaml.YAMLError):
    criterios_defecto = {}

# CPV ACTIVOS (prefijos) para limitar el desplegable "Filtrar por CPV" a solo los
# CPV que el radar tiene activos ahora mismo (los de la config; o los del YAML si
# aún no hay config). Vacío = no filtrar (mostrar todos los presentes).
cpv_prefijos_activos = _cpv_activos_de_config(config_radar, criterios_defecto)

# Plataformas y regiones PRESENTES en los datos, para los selectores de territorio.
# "Estado" representa el feed estatal (sus licitaciones no traen plataforma agregadora).
_plataformas = set()
_regiones = {}     # código NUTS -> nombre legible (texto si lo hay; si no, el código)
for _lic in licitaciones:
    _plat = _lic.get("plataforma") or ("Estado" if _lic.get("fuente") == "estatal" else None)
    if _plat:
        _plataformas.add(_plat)
    _cod = _lic.get("region_codigo")
    if _cod:
        _regiones.setdefault(_cod, _cod)
        if _lic.get("region"):                 # si viene el nombre en texto, lo preferimos
            _regiones[_cod] = _lic["region"]
plataformas_panel = sorted(_plataformas)
regiones_panel = dict(sorted(_regiones.items(), key=lambda kv: kv[1].lower()))

# Lo dejamos disponible al JS como variables globales window.RADAR_* (un <script>
# aparte, antes del JS principal). json.dumps escapa solo lo necesario.
DATOS_CONFIG_JS = (
    "window.RADAR_DEFAULTS = " + json.dumps(criterios_defecto, ensure_ascii=False) + ";\n"
    "window.RADAR_PLATAFORMAS = " + json.dumps(plataformas_panel, ensure_ascii=False) + ";\n"
    "window.RADAR_REGIONES = " + json.dumps(regiones_panel, ensure_ascii=False) + ";\n"
)

# Barra de PESTAÑAS por estado (filtra la vista en el navegador; la rellena/activa
# el JS). Orden fijo y etiqueta; la pestaña activa por defecto es "Activas". El
# contador (N) de cada una lo pone el JS tras cargar las decisiones (zona privada).
PESTANAS = [
    # BG-5: 'En observación' YA NO es una pestaña del Radar; es un subapartado propio
    # del menú lateral (vista 'observacion', reutiliza este mismo grid). La lógica de
    # pestaña 'favoritas' se conserva (perteneceAPestana/badge), pero sin botón en el
    # tablist. El campo guardado en 'decisiones' sigue siendo 'favorita' (la estrella).
    ("activas", "Activas"),
    ("presentadas", "Presentadas"),
    ("ganadas", "Ganadas"),
    ("perdidas", "Perdidas"),
    ("descartadas", "Descartadas"),
    ("caducadas", "Caducadas"),
    ("todas", "Todas"),
]
_botones_tab = []
for _clave, _etiqueta in PESTANAS:
    _es_activa = (_clave == "activas")     # pestaña activa por defecto
    _botones_tab.append(
        f'<button class="tab{" activa" if _es_activa else ""}" data-pestana="{_clave}"'
        f' role="tab" aria-selected="{"true" if _es_activa else "false"}">'
        f'{_etiqueta} <span class="tab-count"></span></button>'
    )
TABS_HTML = ('    <nav class="tabs" id="tabs" role="tablist" aria-label="Filtrar por estado">\n      '
             + "\n      ".join(_botones_tab) + "\n    </nav>\n")

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

# Desplegable para FILTRAR por CPV. Recogemos los CPV que aparecen en las
# licitaciones (sin repetir dentro de cada una) y cuántas los tienen, y los
# listamos como "código — nombre (N)", ordenados por código (las familias quedan
# juntas). El JS usa el atributo data-cpv de cada tarjeta para ocultar/mostrar.
# Solo se muestra si hay CPV que filtrar (si no hay datos, queda como "").
def _cpv_esta_activo(codigo):
    """¿Este código CPV casa con algún prefijo activo de la config? Si no hay
    prefijos activos (sin config ni YAML), no filtramos (devolvemos True para todos)."""
    if not cpv_prefijos_activos:
        return True
    return any(codigo.startswith(p) for p in cpv_prefijos_activos)

conteo_cpv = {}
for _lic in licitaciones:
    for _codigo in dict.fromkeys(_lic.get("cpv", []) or []):
        if _cpv_esta_activo(_codigo):      # solo CPV activos en la config
            conteo_cpv[_codigo] = conteo_cpv.get(_codigo, 0) + 1

if conteo_cpv:
    _opciones_cpv = []
    for _codigo in sorted(conteo_cpv):
        _nombre = cpv_nombres.get(_codigo, "")
        _texto = f"{_codigo} — {_nombre}" if _nombre else _codigo
        _texto += f" ({conteo_cpv[_codigo]})"
        _opciones_cpv.append(
            f'<option value="{html.escape(_codigo)}">{html.escape(_texto)}</option>'
        )
    FILTRO_CPV_HTML = ('    <div class="orden-barra">\n'
                       '      <label for="filtro-cpv">Filtrar por CPV</label>\n'
                       '      <select id="filtro-cpv">\n'
                       '        <option value="">Todos los CPV</option>\n'
                       '        ' + "\n        ".join(_opciones_cpv) + "\n"
                       '      </select>\n'
                       '    </div>\n')
else:
    FILTRO_CPV_HTML = ""

# --- 4. Construimos el cuerpo: las tarjetas, o un mensaje si no hay nada -----
if licitaciones:
    tarjetas = []
    for lic in licitaciones:
        # Tomamos solo la parte de fecha de primera_vez y la comparamos con el límite.
        fecha_primera = datetime.fromisoformat(lic["primera_vez"]).date()
        es_nueva = fecha_primera >= fecha_limite
        # Estado PÚBLICO por fecha (activa/caducada). Lo manual y la estrella son
        # privados y los pinta el navegador desde Supabase tras login.
        estado = calcular_estado(lic.get("fecha_fin_plazo"), hoy)
        tarjetas.append(construye_tarjeta(lic, es_nueva, hoy, estado))
    cuerpo = "\n".join(tarjetas)
    orden_html = ORDEN_HTML          # solo mostramos el desplegable si hay tarjetas
    # Mensaje discreto para cuando la pestaña activa no tenga ninguna tarjeta visible.
    # Arranca oculto; el JS lo muestra/oculta de forma reactiva (ver actualizarVista).
    vacio_pestana_html = ('\n    <p class="vacio-pestana" id="vacio-pestana" hidden>'
                          'No hay licitaciones en esta pestaña.</p>')
else:
    cuerpo = ('    <p class="vacio"><span class="emoji">📭</span>'
              'Aún no hay licitaciones guardadas. '
              'Ejecuta <code>filtrar.py</code> para recopilarlas.</p>')
    orden_html = ""
    vacio_pestana_html = ""           # sin datos ya hay un .vacio; no hace falta otro

# --- 5. Construimos las opciones del menú lateral ---------------------------
# La primera opción se marca como "activa" (es la que se está viendo).
opciones_html = []
for i, opcion in enumerate(OPCIONES_MENU):
    clases = ["nav-item"]
    if opcion.get("sub"):
        clases.append("nav-sub")          # sub-item indentado (BG-5: 'En observación')
    if i == 0:
        clases.append("activo")           # Radar = vista por defecto
    # Badge contador (nº de marcadas); arranca oculto, lo rellena el JS tras login.
    badge = '<span class="nav-count" hidden></span>' if opcion.get("badge") else ""
    opciones_html.append(
        f'<a class="{" ".join(clases)}" data-vista="{opcion["vista"]}" href="{html.escape(opcion["enlace"])}">'
        f'<span class="nav-icono">{opcion["icono"]}</span>'
        f'<span>{html.escape(opcion["nombre"])}</span>{badge}</a>'
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
<!-- Cortina de login: SIEMPRE en el DOM. Visible sin sesión; el JS la oculta al
     entrar y muestra el radar. (No es seguridad: ver el comentario en JS_SUPABASE.) -->
<div class="login-pantalla" id="login-pantalla">
  <div class="login-caja">
    <div class="login-brand"><span class="logo">📡</span><span>{html.escape(TITULO_PAGINA)}</span></div>
    <form id="login-form">
      <input type="email" id="login-email" placeholder="Email" autocomplete="username" required>
      <input type="password" id="login-pass" placeholder="Contraseña" autocomplete="current-password" required>
      <button type="submit">Iniciar sesión</button>
      <span class="auth-error" id="login-error" hidden></span>
    </form>
  </div>
</div>

<!-- Radar (público): arranca OCULTO; el JS lo revela cuando hay sesión. -->
<div id="radar" hidden>
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
        <h1 id="titulo-seccion">{titulo_seccion}</h1>
        <p class="meta" id="meta-radar">Generado el {generado} <span class="badge">{total} licitaciones</span></p>
      </div>
      <div class="conectado" id="conectado">
        <span class="conectado-tx">Conectado como <b id="conectado-email"></b></span>
        <button type="button" class="auth-sec ajustes-btn" id="btn-ajustes" title="Ajustes del radar" aria-label="Ajustes del radar">⚙️</button>
        <button type="button" class="auth-sec" id="logout-btn">Cerrar sesión</button>
      </div>
    </div>
    <div id="vista-radar">
{TABS_HTML}{FILTRO_CPV_HTML}{orden_html}    <section id="listado" class="grid">
{cuerpo}{vacio_pestana_html}
    </section>
    </div>
    <div id="vista-cartera" hidden>
      <div id="cartera-contenido"></div>
    </div>
    <div id="vista-calendario" hidden>
      <div class="cal-split">
        <div id="cal-mes" class="cal-mes-panel"></div>
        <div id="calendario-contenido" class="cal-lista-panel"></div>
      </div>
    </div>
    <div id="vista-buscador" hidden>
      <div id="bg-gate" class="bg-gate" hidden>🔒 Inicia sesión para usar el buscador general (catálogo completo de licitaciones).</div>
      <div id="bg-panel" hidden>
        <div class="bg-barra">
          <input id="bg-texto" class="bg-input" type="search" autocomplete="off"
                 placeholder="Buscar por título, objeto u órgano…  (p. ej. calidad del aire)">
          <button id="bg-mas" class="bg-mas-btn" type="button" aria-expanded="false"
                  aria-controls="bg-avanzado">Más filtros</button>
        </div>
        <div id="bg-pills" class="bg-pills">
          <div class="bg-pill-grupo" data-filtro="fuente" role="group" aria-label="Fuente">
            <span class="bg-pill-et">Fuente</span>
            <button type="button" class="bg-pill activo" data-val="">Todas</button>
            <button type="button" class="bg-pill" data-val="estatal">Estatal</button>
            <button type="button" class="bg-pill" data-val="agregadas">Agregadas</button>
          </div>
          <div class="bg-pill-grupo" data-filtro="estado" role="group" aria-label="Estado del plazo">
            <span class="bg-pill-et">Estado</span>
            <button type="button" class="bg-pill activo" data-val="">Auto</button>
            <button type="button" class="bg-pill" data-val="abierta">Abiertas</button>
            <button type="button" class="bg-pill" data-val="cerrada">Cerradas</button>
            <button type="button" class="bg-pill" data-val="todas">Todas</button>
          </div>
          <label class="bg-orden-lbl">Orden
            <select id="bg-orden" class="bg-sel" title="Ordenar por">
              <option value="fecha_fin_plazo:asc">Fin de plazo ↑</option>
              <option value="fecha_fin_plazo:desc">Fin de plazo ↓</option>
              <option value="valor_estimado:desc">Importe ↓</option>
              <option value="valor_estimado:asc">Importe ↑</option>
              <option value="fecha_publicacion:desc">Publicación ↓</option>
              <option value="fecha_publicacion:asc">Publicación ↑</option>
            </select>
          </label>
        </div>
        <div id="bg-avanzado" class="bg-avanzado" hidden>
          <div class="bg-campo">
            <label for="bg-cpv">CPV (empieza por)</label>
            <div class="bg-cpv-fila">
              <input id="bg-cpv" class="bg-input-sm" type="text" inputmode="numeric" autocomplete="off"
                     placeholder="p. ej. 9073">
              <button id="bg-cpv-add" class="bg-mini-btn" type="button">Añadir</button>
            </div>
            <small class="bg-hint">Enter o «Añadir». Varios códigos = casa cualquiera. «9073» → familia 9073xxxx.</small>
          </div>
          <div class="bg-campo">
            <label for="bg-exp">Nº de expediente</label>
            <input id="bg-exp" class="bg-input-sm bg-exp-input" type="text" autocomplete="off" placeholder="p. ej. V/0013/A/26/2">
            <small class="bg-hint">Busca por contenido; ignora «/ . -» y mayúsculas. Mínimo 3 caracteres.</small>
          </div>
          <div class="bg-campo">
            <label for="bg-imp-min">Importe estimado (€)</label>
            <div class="bg-rango">
              <input id="bg-imp-min" class="bg-input-sm" type="number" min="0" step="1000" placeholder="mín." aria-label="Importe mínimo">
              <span class="bg-rango-sep">–</span>
              <input id="bg-imp-max" class="bg-input-sm" type="number" min="0" step="1000" placeholder="máx." aria-label="Importe máximo">
            </div>
          </div>
          <div class="bg-campo">
            <label for="bg-fin-desde">Fin de plazo</label>
            <div class="bg-rango">
              <input id="bg-fin-desde" class="bg-input-sm" type="date" aria-label="Fin de plazo desde">
              <span class="bg-rango-sep">–</span>
              <input id="bg-fin-hasta" class="bg-input-sm" type="date" aria-label="Fin de plazo hasta">
            </div>
          </div>
          <div class="bg-campo">
            <label for="bg-pub-desde">Fecha de publicación</label>
            <div class="bg-rango">
              <input id="bg-pub-desde" class="bg-input-sm" type="date" aria-label="Publicación desde">
              <span class="bg-rango-sep">–</span>
              <input id="bg-pub-hasta" class="bg-input-sm" type="date" aria-label="Publicación hasta">
            </div>
          </div>
        </div>
        <div id="bg-chips" class="bg-chips" hidden></div>
        <div class="bg-cabecera">
          <span id="bg-contador" class="bg-contador"></span>
          <button id="bg-limpiar" class="bg-limpiar" type="button" hidden>Limpiar filtros</button>
        </div>
        <div id="bg-estado-msg" class="bg-msg" hidden></div>
        <div id="bg-resultados" class="grid"></div>
        <div id="bg-paginacion" class="bg-pag" hidden>
          <button id="bg-prev" class="bg-pag-btn" type="button">‹ Anterior</button>
          <span id="bg-pag-info" class="bg-pag-info"></span>
          <button id="bg-next" class="bg-pag-btn" type="button">Siguiente ›</button>
        </div>
      </div>
    </div>
  </div>
</div>
{CONTRATO_MODAL}{CARTERA_DOCS_MODAL}{AJUSTES_MODAL}</div>

<script>{DATOS_CONFIG_JS}</script>
<script>{JS}</script>
<script type="module">{JS_SUPABASE}{JS_BUSCADOR}</script>
</body>
</html>
"""

# --- 7. Creamos la carpeta docs/ (si no existe) y escribimos el HTML --------
ruta_docs = Path("docs")
ruta_docs.mkdir(parents=True, exist_ok=True)
ruta_salida = ruta_docs / "index.html"
ruta_salida.write_text(pagina, encoding="utf-8")

# Copiamos el catálogo de nombres de CPV a docs/ para que el panel de ajustes
# pueda buscar CPV por nombre (el navegador lo descarga con fetch('cpv_nombres.json')).
_cat_cpv = Path("data") / "cpv_nombres.json"
if _cat_cpv.exists():
    (ruta_docs / "cpv_nombres.json").write_text(_cat_cpv.read_text(encoding="utf-8"), encoding="utf-8")

print(f"OK: pagina generada en {ruta_salida} con {total} licitaciones.")
