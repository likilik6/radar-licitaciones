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

  /* ---- Datos económicos y fechas ---- */
  .datos { display:flex; flex-direction:column; gap:5px; font-size:.84rem; margin-top:2px;
           padding-top:10px; border-top:1px solid var(--borde); }
  .dato { display:flex; justify-content:space-between; gap:12px; align-items:baseline; }
  .dato .et-dato { color:var(--suave); }
  .dato .val-dato { color:var(--texto); font-weight:600; text-align:right; }
  .dato .quedan { color:#15803d; font-weight:600; }     /* plazo abierto: verde */
  .dato .vence-hoy { color:#b45309; font-weight:600; }  /* vence hoy: ámbar (urgente) */
  .dato .cerrado { color:#b91c1c; font-weight:600; }    /* plazo cerrado: rojo */

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
  .contrato-aviso { font-size:.82rem; color:#b91c1c; font-weight:600; margin-right:auto; }
  .contrato-ok { font-size:.82rem; color:#15803d; font-weight:700; margin-right:auto; }
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
  const barraTabs = document.getElementById('tabs');
  const vacioPestana = document.getElementById('vacio-pestana');   // mensaje "pestaña vacía"
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
    if (pestana === 'todas') return true;
    if (pestana === 'favoritas') return card.dataset.favorita === 'true';  // sin importar estado
    return card.dataset.estado === ESTADO_DE_PESTANA[pestana];
  }
  // Hueco para componer con el menú lateral de categorías: HOY no hay filtro de
  // categoría activo, así que siempre pasa. Si se añade, aquí se comprobará y la
  // visibilidad será (pertenece a la pestaña) Y (pasa el filtro de categoría).
  function categoriaVisible(card) { return true; }

  // Muestra/oculta cada tarjeta según pestaña activa Y categoría. Solo en el navegador.
  function aplicarFiltro() {
    cards().forEach(function (c) {
      const visible = perteneceAPestana(c, pestanaActiva) && categoriaVisible(c);
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
    if (!barraTabs) return;
    const todas = cards();
    barraTabs.querySelectorAll('.tab').forEach(function (tab) {
      const p = tab.dataset.pestana;
      const n = todas.filter(function (c) { return perteneceAPestana(c, p); }).length;
      const span = tab.querySelector('.tab-count');
      if (span) span.textContent = '(' + n + ')';
    });
  }

  // Tras un cambio de datos (carga/guardado) o al revelar el radar: recalcula
  // contadores y re-aplica filtro + orden, para que vista y números no se desfasen.
  function refrescarTodo() { actualizarContadores(); actualizarVista(); }

  // Click en una pestaña: cambia la activa y re-filtra (los contadores no cambian).
  if (barraTabs) {
    barraTabs.addEventListener('click', function (e) {
      const btn = e.target.closest('.tab');
      if (!btn) return;
      pestanaActiva = btn.dataset.pestana;
      barraTabs.querySelectorAll('.tab').forEach(function (t) {
        const act = (t === btn);
        t.classList.toggle('activa', act);
        t.setAttribute('aria-selected', act ? 'true' : 'false');
      });
      actualizarVista();
    });
  }
  // Cambiar el desplegable de orden: solo re-ordena (filtro y contadores no cambian).
  if (selectOrden) {
    selectOrden.addEventListener('change', function () { ordenar(ordenActual()); });
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
    const sinMarca = (estado === null && favorita === false);

    const sel = card.querySelector('.ctrl-estado');
    const btn = card.querySelector('.ctrl-estrella');
    if (sel) sel.disabled = true;          // bloqueamos mientras escribimos
    if (btn) btn.disabled = true;
    ocultarAviso(card);
    try {
      if (sinMarca) {
        // Sin estado ni favorita: la fila no debe existir -> delete + fuera del Map.
        const { error } = await supabase.from('decisiones').delete().eq('licitacion_id', id);
        if (error) throw error;
        decisionesPorId.delete(id);
      } else {
        // Objeto COMPLETO; upsert con conflicto en licitacion_id (no pierde campos).
        const fila = { licitacion_id: id, estado: estado, favorita: favorita, updated_at: new Date().toISOString() };
        const { error } = await supabase.from('decisiones').upsert(fila, { onConflict: 'licitacion_id' });
        if (error) throw error;
        decisionesPorId.set(id, { estado: estado, favorita: favorita });
      }
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
      if (!session) { limpiarPrivado(); return; }
      // Solo (re)leemos las decisiones cuando cambia la sesión de verdad. Un refresco
      // de token (TOKEN_REFRESHED, ~cada hora) o USER_UPDATED traen 'session' pero NO
      // cambian los datos: recargar repetiría el limpiarPrivado()+await de
      // cargarDecisiones y haría PARPADEAR badges/estrellas/colores. Lo evitamos.
      if (event === 'INITIAL_SESSION' || event === 'SIGNED_IN') cargarDecisiones(session);
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

  function cerrarContrato() {
    if (modal) modal.hidden = true;
    contratoId = null;
  }

  // Abrir el panel para UNA tarjeta: leer su fila de 'contratos' y rellenar el form.
  async function abrirContrato(card) {
    if (!sesionActiva || !modal || !card) return;
    const id = card.getAttribute('data-licitacion-id');
    contratoId = id;
    if (modalSub) modalSub.textContent = card.getAttribute('data-titulo') || '';
    limpiarFormC(); avisoC(''); okC(false);
    modal.hidden = false;                  // mostramos ya (vacío) y rellenamos al llegar
    pintarDocumentos(id);                  // carga y pinta la lista de documentos de esta licitación
    try {
      const { data, error } = await supabase.from('contratos').select('*')
        .eq('licitacion_id', id).maybeSingle();   // 0 o 1 fila (licitacion_id es PK)
      if (error) throw error;
      if (contratoId !== id || modal.hidden) return;   // se cambió/cerró entre tanto
      rellenarFormC(data);                 // data === null -> formulario vacío
    } catch (err) {
      console.error('Error leyendo contrato:', err);
      avisoC('No se pudieron cargar los datos: ' + (err.message || err));
    }
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
    avisoC(''); okC(false);
    try {
      const { error } = await supabase.from('contratos').upsert(fila, { onConflict: 'licitacion_id' });
      if (error) throw error;
      okC(true);                           // feedback breve y cerramos
      setTimeout(cerrarContrato, 800);
    } catch (err) {
      console.error('Error guardando contrato:', err);
      avisoC('No se pudo guardar: ' + (err.message || err));   // no mentimos: no cerramos
    } finally {
      if (btnGuardarC) btnGuardarC.disabled = false;
    }
  }

  if (modalForm) modalForm.addEventListener('submit', function (e) { e.preventDefault(); guardarContrato(); });
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
        <button type="button" class="ctrl-estrella" aria-pressed="false" aria-label="Favorita" title="Marcar como favorita">☆</button>
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
          <span class="contrato-aviso" id="contrato-aviso" hidden></span>
          <span class="contrato-ok" id="contrato-ok" hidden>Guardado ✓</span>
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

    # data-licitacion-id: el MISMO id del JSON (entry.id). Es la clave con la que el
    # navegador casa cada fila de la tabla 'decisiones' de Supabase con su tarjeta.
    lic_id = html.escape(lic.get("id", ""))

    # data-estado es el estado PÚBLICO por fecha (activa/caducada); 'estado' siempre
    # es uno de esos dos: seguro. data-favorita arranca en "false": lo privado (estado
    # manual y estrella) lo sobrescribe el navegador tras login.
    return f"""    <article class="card"
      data-importe="{data_importe}" data-fin-plazo="{data_fin_plazo}"
      data-fecha-pub="{data_fecha_pub}" data-fecha-subida="{data_fecha_subida}"
      data-titulo="{titulo}" data-licitacion-id="{lic_id}"
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

# --- 2. Pasamos los valores a una lista y la ordenamos ----------------------
# De más RECIENTE a más antigua según "primera_vez" (por eso reverse=True).
licitaciones = list(datos.values())
licitaciones.sort(key=lambda lic: datetime.fromisoformat(lic["primera_vez"]), reverse=True)

# --- 3. ¿A partir de qué fecha algo cuenta como "nuevo"? --------------------
# Comparamos SOLO fechas (sin horas). 'hoy' en zona Europe/Madrid (igual que el
# contador de días de las tarjetas), para no usar la hora UTC del runner de Actions.
hoy = datetime.now(ZoneInfo("Europe/Madrid")).date()
fecha_limite = hoy - timedelta(days=DIAS_NUEVO)

# Barra de PESTAÑAS por estado (filtra la vista en el navegador; la rellena/activa
# el JS). Orden fijo y etiqueta; la pestaña activa por defecto es "Activas". El
# contador (N) de cada una lo pone el JS tras cargar las decisiones (zona privada).
PESTANAS = [
    ("favoritas", "Favoritas"),
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
        <h1>{titulo_seccion}</h1>
        <p class="meta">Generado el {generado} <span class="badge">{total} licitaciones</span></p>
      </div>
      <div class="conectado" id="conectado">
        <span class="conectado-tx">Conectado como <b id="conectado-email"></b></span>
        <button type="button" class="auth-sec" id="logout-btn">Cerrar sesión</button>
      </div>
    </div>
{TABS_HTML}{orden_html}    <section id="listado" class="grid">
{cuerpo}{vacio_pestana_html}
    </section>
  </div>
</div>
{CONTRATO_MODAL}</div>

<script>{JS}</script>
<script type="module">{JS_SUPABASE}</script>
</body>
</html>
"""

# --- 7. Creamos la carpeta docs/ (si no existe) y escribimos el HTML --------
ruta_docs = Path("docs")
ruta_docs.mkdir(parents=True, exist_ok=True)
ruta_salida = ruta_docs / "index.html"
ruta_salida.write_text(pagina, encoding="utf-8")

print(f"OK: pagina generada en {ruta_salida} con {total} licitaciones.")
