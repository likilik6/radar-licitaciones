# Radar de Licitaciones — contexto para Claude Code

Léelo entero al empezar cada sesión. Si algo de aquí choca con el código o con la base, gana
lo que veas: avísame y corrige este archivo.

> **Este repo es PÚBLICO.** Aquí solo va lo técnico. Nada de CIFs de competidores, importes de
> cartera, clientes ni estrategia: ni en este archivo, ni en commits, ni en PRs. Lo privado vive
> fuera de git (mira `.gitignore` antes de añadir un archivo nuevo).

## Quién soy y cómo trabajamos

- Soy Alejandro: economista y actuario, **no programador de formación**. Aprendo mientras
  construyo: explícame cada pieza y ve paso a paso.
- Respuestas en español, concisas y accionables. Código y SQL listos para copiar y pegar.
- **Diseño antes que código.** Cada encargo empieza por un PASO 0: comprobar el esquema y los
  datos reales (no suponerlos), contarme lo que ves y enseñarme el plan. No implementes hasta
  que te diga que sí.
- **Una cosa de punta a punta**, verificada, antes de pasar a la siguiente.
- **Un cambio = una rama + un PR.** Yo reviso y mergeo; no hagas push a `master` ni merges tú.
  Commits en español con el formato `Área: qué cambia, visto desde el usuario`
  (p. ej. `Menores: la vista usa la RPC única, con la ruta por etapas como red`).
- **Pregúntame antes de:** escribir en la base de producción, cambiar permisos o RLS, tocar los
  workflows o su cron, borrar archivos o ramas, y cualquier carga grande de datos.
- **SQL contra Supabase:** lo preparas como archivo `.sql` idempotente (`if not exists`,
  `create or replace`) y yo lo ejecuto en el SQL Editor. El editor solo enseña el resultado de la
  última consulta: lo que haya que comprobar, en una tabla al final.
- **Claves:** nunca en código, logs, commits ni chat. Viven en `.env` y en los Secrets de GitHub.
- **Al cerrar cada encargo, actualiza la sección «Estado»** de este archivo: es lo único que la
  siguiente sesión sabe de lo que hicimos.

### Reglas de verificación

- Un cero no es un hallazgo hasta verificarlo por otra vía con un caso conocido.
- Toda criba por texto lleva lista de exclusión, y se contrasta con ejemplos reales.
- Si una consulta toca un tope (paginación, límite de filas), dilo; no concluyas sobre datos topados.
- No leas tendencia en series por año: la cobertura del catálogo cambia mucho entre años.
- Un job en verde no significa datos frescos.

## Qué es

Vigila los feeds oficiales de la Plataforma de Contratación del Sector Público y publica una web
estática (GitHub Pages) con las licitaciones que interesan a LODEPA (bioseguridad ambiental,
purificación y monitorización del aire en hospitales). Tras el login, una capa privada en Supabase
añade Buscador, Desiertas, Menores, Competencia, Cartera y Calendario.

- Web: `https://likilik6.github.io/radar-licitaciones/` (repo `likilik6/radar-licitaciones`, rama `master`).
- Feeds ATOM con bloque CODICE: estatal (`sindicacion_643`) y agregadas (`sindicacion_1044`,
  `PlataformasAgregadasSinMenores`). Lista en `feeds.py` → `FEEDS`. Menores estatales: sindicación 1143.
- Sin cabecera `User-Agent` de navegador, el WAF de la Plataforma bloquea la petición.

## Entorno local

- Windows + PowerShell. Entorno: `.\.venv\Scripts\Activate.ps1`; dependencias:
  `pip install -r requirements.txt` (versiones fijadas, las mismas que usa Actions).
- `.env` (no se sube): `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_RO_URL`. Léelo con
  `lee_env` de `utiles.py`, la única copia que aguanta el BOM que mete Windows.
- Tests: `python test_<nombre>.py` (cada uno se ejecuta suelto) y `node test_menores_api.mjs`.

## Mapa del repo

Pipeline del Radar:
- `feeds.py` — lista de feeds, descarga con paginación y extractor CODICE común
  (`extrae_entrada`, `extrae_adjudicaciones`).
- `filtrar.py` — clasifica cada licitación con los criterios de `radar_config` y guarda
  `data/licitaciones.json` (indexado por `entry.id`, con `primera_vez` / `ultima_vez`).
- `generar_web.py` — pinta `docs/index.html`. `docs/` no se versiona: lo publica Actions.
- `utiles.py` — normalización compartida y `lee_env`.
- `nuts.py` — mapeo de códigos NUTS a comunidad autónoma (lo usa `backfill_catalogo.py`).
- `intereses.yaml` — dos usos. (1) Reserva de los criterios del Radar: `filtrar.py` lo usa si no
  puede leer `radar_config` (en Actions aborta) o si la config está vacía; los criterios buenos
  del Radar son los del panel ⚙. (2) Fuente **viva** del nicho de Menores («Solo mi nicho», vía
  `menores_nicho.py`, que llama `generar_web.py` en cada publicación) y semilla del panel.
  Editarlo cambia el nicho de Menores en la siguiente pasada.
- `data/` — además del JSON del Radar: rótulos de CPV y NUTS (`cpv_nombres.json`,
  `nuts_nombres.json`, que regeneran `actualizar_cpv.py` y `actualizar_nuts.py`) y la
  configuración de Menores (`menores_fuentes.json`, `menores_alias_organos.json`,
  `menores_nicho_exclusiones.json`).

Catálogo, cruces e informes:
- `backfill_catalogo.py` — carga diaria del catálogo (`licitaciones`, `adjudicaciones`), backfill
  desde los ZIP de datos abiertos, purga a 3 años, desiertas y marcado automático de ganadas.
- `menores_autonomicos.py`, `menores_nicho.py`, `medir_menores.py` — contratos menores.
- `informe_empresa.py` — informe de una empresa (solo SELECT; salida en `informes/`, fuera de git).
- `backup_perfiles.py` — copia de las tablas privadas antes de una migración.
- `diagnostico_feed.py` — huella del feed (qué recibe GitHub frente a un PC normal).
- Front servido dentro de la web: `buscador_api.js`, `menores_api.js`.
- SQL: un `.sql` por pieza (`*_schema.sql`, `*_rpc.sql`, `*_indices.sql`) y `sql/` para la
  migración a perfiles. Los `cartera_*.sql` llevan datos privados y no se suben. Ojo: no todo lo
  ejecutado en Supabase está en master (mira «Estado»). El DDL de `decisiones`, `contratos` y
  `documentos` solo vive en la base y en el backup; el de `cartera`/`cartera_documentos`, en los
  `cartera_*.sql` del PC; y `radar_config.sql` es el de antes de los perfiles (hoy la tabla ya no
  tiene la regla de fila única y lleva columna `perfil`).

Workflows (`.github/workflows/`):

| Workflow | Cuándo | Qué hace |
|---|---|---|
| `radar.yml` | L-V 05:23 UTC y a mano | filtrar → generar web → commit de `data/licitaciones.json` → Pages; job `catalogo`: `backfill_catalogo.py --diario` y purga a 3 años (a mano, solo si se marca `ejecutar_catalogo`) |
| `mantenimiento.yml` | domingos 06:17 UTC | repaso completo de desiertas; a mano con `comprobar_feed`, solo la huella del feed |
| `menores_autonomicos.yml` | sáb y dom 03:15 UTC (si `MENORES_AUTONOMICOS_ACTIVO`) y a mano (simula por defecto) | carga de menores autonómicos |
| `backfill.yml`, `automarcar.yml` | solo a mano | backfill del catálogo; cruce de ganadas |

GitHub arranca el cron del Radar con 4,5-8 h de retraso (medido en octubre de 2026). Hay alarmas
de desfase y de fuente muda. **No mergees a `master` con una pasada de `radar.yml` en marcha:** su
push de la persistencia se rechaza y ese día no publica (pasó el 16/10). Mira antes
`gh run list --workflow radar.yml --limit 1`. Los PR no tienen CI: los tests se pasan a mano.

## Supabase

- Proyecto `uzktrhpgkyctlnqgdsys` (UE), plan Micro. Disco con tope de 30 GB y los excesos se
  facturan. Al 95 % la base pasa a solo lectura y el disco solo crece una vez cada 4 h: antes de
  una carga grande, estima filas y GB; después, carga por tramos midiendo entre tramo y tramo.
- Las cuatro credenciales:
  1. **publishable / anon** — pública a propósito en el JS; la protege la RLS.
  2. **`SUPABASE_SERVICE_ROLE`** — Secret de Actions; la usa el pipeline. Salta la RLS.
  3. **`SUPABASE_SECRET_KEY`** — `.env` local (`informe_empresa.py`, `backup_perfiles.py`). Salta la RLS.
  4. **`claude_ro`** — rol de solo lectura para que verifiques datos en vivo (psycopg2, conexión en
     `SUPABASE_RO_URL` o `secrets/supabase_ro.url`; el Python del venv es el que tiene psycopg2).
     `SELECT` sobre `public`, también las tablas privadas. No entra en los
     esquemas `storage` ni `auth`, aunque sus políticas se ven en `pg_policies`. No puede ejecutar
     las RPC de la web.
- Los criterios del Radar (CPV, palabras clave, orden de categorías) viven en la tabla
  `radar_config` y se editan en el panel ⚙ de la web, **no** en `intereses.yaml`. No toques
  `radar_config`: los cambios los hago yo en el panel.
- Catálogo compartido: `licitaciones` (Buscador), `adjudicaciones`, `menores`, `competidores`;
  auxiliares: `menores_cobertura` y `refrescos`. Privadas (6, cada fila con su `perfil`):
  `decisiones`, `contratos`, `documentos`, `cartera`, `cartera_documentos` y `radar_config`.
  Perfiles: `perfiles` y `perfiles_usuarios`.
- En la misma base hay un esquema de otro proyecto, con su propio repo (el único esquema propio
  aparte de `public`). Desde aquí no se toca. Ese proyecto lee con SELECT `licitaciones`,
  `adjudicaciones`, `menores` y `competidores`: antes de renombrar o borrar columnas de esas
  tablas, avísame.

## Reglas que no se rompen

1. **Espejo estático:** la web solo refleja datos; las decisiones viven en Supabase tras login.
2. **`estado` se calcula al pintar, nunca se guarda.** `caducada` sale de `fecha_fin_plazo` frente
   a la hora de Europe/Madrid, y los estados manuales mandan sobre ella.
3. **Toda tabla nueva en `public` necesita `GRANT` a `authenticated` y política RLS.** Una sin la
   otra no basta.
4. **`licitacion_id` (= `entry.id` del feed) es la clave en todas las tablas.** Es una URL larga:
   nunca como ruta en Storage. Los archivos se nombran por UUID y se enlazan por columna.
5. La clave de configuración es el valor guardado; las etiquetas bonitas van en la presentación.
6. «Lo nuevo» es un filtro sobre `primera_vez`, no un concepto aparte.
7. En el front, solo `INITIAL_SESSION` y `SIGNED_IN` recargan datos; `TOKEN_REFRESHED` no (parpadeo).
8. **Permisos que no se tocan nunca:** el `EXECUTE` de `authenticated` sobre `cpv_texto`,
   `norm_expediente`, `buscar_licitaciones`, `menores_resumen_cif` y `menores_buscar`.
   Las dos primeras son la expresión de índices trigram y las llama una función SECURITY INVOKER:
   si se revocan, se caen en silencio los filtros de CPV y de expediente. `menores_buscar` sin
   `EXECUTE` no da error: la web cae a la ruta lenta. Antes de tocar permisos de una función, mira
   qué índices y funciones invoker dependen de ella.
9. **Los contratos menores no van al Radar** (se publican ya adjudicados: no son oportunidad).
   Viven solo en su vista.

## Rendimiento

Lo lento no es tener muchos datos, es contar y escanear sin índice.

1. Lo pesado, al build diario de Actions, no al navegador.
2. Cada bloque pesado carga aparte y en paralelo, con su «cargando…».
3. Nunca `count` exacto en tablas grandes: reutiliza el patrón de `menores_api.js` (estimación
   del planner; exacto solo por debajo de 10.000).
4. `EXPLAIN` antes de dar por bueno un filtro nuevo.
5. Paginar siempre. Precalcular solo si se mide lento.

## Deuda técnica conocida

- En `radar.yml` el artefacto de Pages se sube después del commit de persistencia, y el push no
  hace `pull --rebase`: si ese push falla (basta un merge durante la pasada), no se publica una web
  ya generada. Arreglo: subir el artefacto antes del commit.
- Hay tres copias de `lee_env` (`utiles.py`, `backup_perfiles.py`, `informe_empresa.py`) y un
  cuarto lector del `.env` (`_del_env_local` en `menores_autonomicos.py`); solo el de `utiles.py`
  aguanta el BOM. Unificar cuando se toque alguna.
- Pendiente de OK: recortar un `EXECUTE` sobrante en las funciones del Buscador (sin fuga).

## Estado (30/10/2026)

Revisado el 30/10 contra el repo, GitHub y la base (con `claude_ro`).

- **Robot:** funciona. Pasada programada los 21 laborables del 1 al 29/10; 20 en verde. La del
  16/10 falló en el push de la persistencia (se mergeó un PR a mitad de pasada) y no publicó: esa
  web salió de una pasada manual anterior. Datos frescos a 29/10 en el JSON y en el catálogo. Los
  fines de semana (`mantenimiento.yml`, `menores_autonomicos.yml`), en verde todo octubre; cada
  carga de menores avisa «no pude anotar el refresco de cobertura» (sin investigar).
- **Supabase:** unos 5,3 GB de 30 según `pg_size_pretty` (`menores` 2,7, `licitaciones` 2,2,
  `adjudicaciones` 0,3). Regla 8 intacta.
- **PR abiertos** (todos aplican sobre master sin conflictos; revisados el 30/10):
  - #35 — RPC única `menores_buscar` (SQL). Ya está ejecutada en Supabase con el mismo cuerpo:
    se mergea tal cual y **no se vuelve a ejecutar**.
  - #36 — la vista Menores usa esa RPC, con la ruta por etapas como red. Rompe
    `test_menores_api.mjs` (su cliente falso no tiene `.rpc`): antes de mergear necesita un
    `try/catch` de una línea alrededor de `porRpc()`. Después, `radar.yml` a mano y medir con sesión.
  - #33 — la poda refresca también el motivo, no solo la categoría. Choca en dos sitios triviales
    con `versiones-guarda` (el import de `filtrar.py` y un bloque de `utiles.py`): quedarse con los
    dos lados.
  - #34 — el aviso de desfase pasa a las 16:30.
  - #39 — `informe_mercado.py`. Antes de mergear, un commit que sustituya por inventados los
    datos reales que aún quedan en `test_informe_mercado.py`.
  - #26 — deja `sql/perfiles_paso2_revokes.sql` como registro de lo ejecutado el 23-24/09
    (comprobado en vigor).
  - #40 — `estado_expediente` en `extrae_entrada`: completo y probado, pero sin consumidor. Es
    el mismo código que trae `versiones-guarda`: se cierra si esa rama entra antes.
  - #41 — `buscador_schema.sql`, el esquema de BG-1 de `licitaciones` (17 de las 21 columnas de
    hoy; lo demás está en otros `.sql`).
- **Perfiles:** migración a dos perfiles en curso; qué está ejecutado y qué falta se lleva fuera
  del repo. `sql/perfiles_migracion.sql` y `verificar_perfiles.py` (escribe en producción) están
  en la rama local `perfiles-migracion`, sin subir. No crees usuarios en Auth sin mi OK.
- **En curso, sin commit:** el worktree `versiones` (rama `versiones-guarda`, sin remoto): guarda
  de versiones y bajas, reparación y un workflow nuevo, `reparacion.yml`. Su `versiones_schema.sql`
  no está ejecutado: primero el SQL y después el merge. Cuando entre, el commit automático añadirá
  2 claves a todas las fichas de `data/licitaciones.json`.
- **En el PC (checkout principal, rama `rpc-menores-front`):** `feeds.py` modificado sin commit
  (la versión vieja de `estado_expediente`, ya guardada en #40: se descarta) y, sin versionar,
  `buscador_schema.sql` (#41), `sql/perfiles_migracion.sql`, `verificar_perfiles.py` y este
  `CLAUDE.md`. Al mergear cada PR hay que descartar o borrar su copia local, o `git pull`
  fallará. `medir_small.py` y `medidas_micro.json` (prueba Micro contra Small, descartada) se
  movieron fuera del repo.
- **Worktrees:** `fin-plazo` e `informe-fin-plazo` retirados el 30/10 (sus ramas siguen). Quedan
  `informe-mercado` (espera al #39), `versiones` (trabajo vivo) y `radar-estado-organizacion-985e83`
  (el de este PR; se retira al mergearlo).
- **Ramas locales:** 39 (35 más las 4 del 30/10). Falta decidir cuáles se borran. Ojo:
  `git branch --merged` incluye `versiones-guarda` (no tiene commits, pero su worktree tiene
  trabajo vivo) y no incluye `fin-plazo-madrid` ni `informe-fin-plazo-madrid` (entraron por squash
  en #37 y #38).
