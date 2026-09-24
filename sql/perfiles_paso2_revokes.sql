-- ============================================================================
-- F1 · PASO 2 · CERRAR LOS PERMISOS QUE SOBRAN  (antes de crear el perfil avensis)
--
-- QUÉ ES ESTO: hoy el rol "anon" (el de la clave pública que va dentro de la
-- web) tiene permisos COMPLETOS sobre las 6 tablas privadas, y tres funciones
-- que escriben saltándose la seguridad quedaron abiertas a cualquier usuario
-- con sesión. Lo único que frena a "anon" ahora mismo es la política de RLS.
-- Eso es UNA puerta cerrada donde tiene que haber DOS. Esto cierra la segunda.
--
-- ESTO NO ES UNA RESTRICCIÓN NUEVA: es corregir tres "revoke" que se olvidaron
-- del rol authenticated (automarcar_ganadas.sql:136, purga_catalogo.sql:82,
-- competidores_schema.sql:109) y el permiso por defecto que Supabase da a anon.
--
-- VERIFICADO ANTES DE ESCRIBIRLO, contra la base real y contra la API:
--   · Sin sesión, la web hace UNA sola llamada a Supabase: leer radar_config
--     (el bloque de generar_web.py:3602). Nada más.
--   · Las otras 5 tablas privadas ya devuelven CERO filas a un anónimo.
--   · El navegador solo usa dos funciones: buscar_licitaciones y
--     menores_resumen_cif. Ninguna de las dos se toca aquí.
--   · Las tres funciones que se cierran solo las llama backfill_catalogo.py
--     con la credencial de servicio, que conserva el permiso.
--
-- CÓMO SE USA: son DOS bloques. El bloque 1 se puede ejecutar ya. El bloque 2
-- NO: tiene que esperar a que el robot lea la configuración con la credencial
-- de servicio. Está al final, separado y con su propio aviso.
--
-- SE PUEDE REPETIR sin problema: un "revoke" de algo ya revocado no da error.
-- ============================================================================


-- ############################################################################
-- BLOQUE 1 — EJECUTABLE YA. No depende de ningún cambio de código.
-- ############################################################################

-- FOTO DE ANTES, por si hay que volver atrás (permisos actuales de las 6 tablas):
--   postgres=arwdDxtm | anon=arwdDxtm | authenticated=arwdDxtm
--   service_role=arwdDxtm | claude_ro=r
-- Y de las 3 funciones:
--   postgres=X | authenticated=X | service_role=X

-- (1) decisiones · nadie la lee sin sesión: la carga empieza con
--     "if (!session) return" (generar_web.py:1576).
revoke all privileges on table public.decisiones from anon;

-- (2) cartera · solo se pinta tras el login. El script de subida masiva
--     (subir_pdfs_cartera.py) entra con usuario y contraseña de verdad.
revoke all privileges on table public.cartera from anon;

-- (3) cartera_documentos · tabla puente de los PDFs de la cartera; cero
--     accesos anónimos.
revoke all privileges on table public.cartera_documentos from anon;

-- (4) contratos · solo se toca desde el modal Detalles, detrás del login.
revoke all privileges on table public.contratos from anon;

-- (5) documentos · metadatos de los PDFs; el front los pide ya autenticado
--     (las URLs se firman para 60 segundos).
revoke all privileges on table public.documentos from anon;

-- (6) automarcar_ganadas_lodepa · escribe en public.decisiones saltándose la
--     seguridad (SECURITY DEFINER). El front NO la llama nunca; su único
--     llamador es backfill_catalogo.py:655 con la credencial de servicio.
--     Hoy cualquier usuario con sesión podría ejecutarla desde el navegador.
revoke all privileges on function public.automarcar_ganadas_lodepa(text[], boolean) from authenticated;
grant execute on function public.automarcar_ganadas_lodepa(text[], boolean) to service_role;

-- (7) purga_catalogo · BORRA catálogo histórico, también saltándose la
--     seguridad. Único llamador: backfill_catalogo.py:1375 con la credencial
--     de servicio. Es el más peligroso de los tres si se deja abierto.
revoke all privileges on function public.purga_catalogo(integer, boolean, integer) from authenticated;
grant execute on function public.purga_catalogo(integer, boolean, integer) to service_role;

-- (8) refrescar_competidores · reconstruye la tabla de 77.700 competidores.
--     OJO, no confundir con la TABLA public.competidores: la ficha de
--     competidor de la web lee la tabla, no esta función, y sigue igual.
revoke all privileges on function public.refrescar_competidores() from authenticated;
grant execute on function public.refrescar_competidores() to service_role;

-- Nota: no hace falta revocar también "from public" en las tres funciones:
-- sus permisos no incluyen la entrada de PUBLIC, así que ahí no hay nada.


-- ----------------------------------------------------------------------------
-- ROLLBACK DEL BLOQUE 1 · deshace exactamente lo de arriba, nada más.
-- Descomenta solo la línea que necesites.
-- ----------------------------------------------------------------------------
-- grant all privileges on table public.decisiones to anon;
-- grant all privileges on table public.cartera to anon;
-- grant all privileges on table public.cartera_documentos to anon;
-- grant all privileges on table public.contratos to anon;
-- grant all privileges on table public.documentos to anon;
-- grant execute on function public.automarcar_ganadas_lodepa(text[], boolean) to authenticated;
-- grant execute on function public.purga_catalogo(integer, boolean, integer) to authenticated;
-- grant execute on function public.refrescar_competidores() to authenticated;


-- ############################################################################
-- BLOQUE 2 — listo para ejecutar (las condiciones se cumplieron el 24/09/2026).
--
-- Esta única línea era la que podía romper la publicación del Radar, y además
-- lo habría hecho EN SILENCIO (el workflow saldría en verde publicando la web
-- con los criterios equivocados). Por eso fue lo último de este fichero.
--
-- radar_config es la ÚNICA tabla que un anónimo lee de verdad hoy, y tiene
-- TRES lectores anónimos:
--     · el navegador antes del login  (generar_web.py:3602)
--     · filtrar.py:88                 (en GitHub Actions, sin credencial)
--     · generar_web.py:38             (ídem, y su error se traga con
--                                      "except Exception: pass", sin avisar)
--
-- CONDICIONES CUMPLIDAS EL 24/09/2026 (PR #25 mergeado). Se deja escrito lo que
-- se comprobó, porque es lo que hace seguro este revoke:
--   [x] 1. filtrar.py y generar_web.py leen la configuración con
--          SUPABASE_SERVICE_ROLE y abortan con exit 1 en Actions si no pueden.
--   [x] 2. radar.yml pasa ese secreto a los pasos "Filtrar licitaciones" y
--          "Generar la web".
--   [x] 3. El navegador vuelve a leer la configuración DESPUÉS del login, con
--          tres cautelas: gana la lectura más reciente, una lectura fallida no
--          pisa lo aplicado, y los ajustes de vista no se re-imponen.
--   [x] 4. workflow_dispatch desde master en verde, con la línea
--          "Config del radar leída de Supabase (credencial: SUPABASE_SERVICE_ROLE)."
--          en los DOS pasos, y la web publicada con las etiquetas de siempre.
--
-- COMPROBADO ADEMÁS, justo antes de ejecutarlo (24/09/2026):
--   · authenticated conserva select/insert/update sobre radar_config, y la
--     política de lectura es {public} —que lo incluye—, así que la sesión del
--     navegador sigue leyendo y guardando igual. Lo único que se cierra es anon.
--   · Las otras cinco privadas ya tienen a anon cerrado (bloque 1), y las tres
--     funciones SECURITY DEFINER ya no las puede ejecutar authenticated.
-- ############################################################################

revoke all privileges on table public.radar_config from anon;

-- ROLLBACK DEL BLOQUE 2:
-- grant all privileges on table public.radar_config to anon;


-- ############################################################################
-- LO QUE NO SE PUEDE REVOCAR, aunque lo parezca
--
-- · cpv_texto(text[]) y norm_expediente(text): la función del Buscador
--   (buscar_licitaciones) corre con los permisos de QUIEN LLAMA y las invoca
--   montando la consulta sobre la marcha (buscador_rpc.sql:185 y :196).
--   Además son la expresión de dos índices. Si se les quita el permiso, los
--   filtros de CPV por prefijo y de nº de expediente dejan de funcionar — y
--   como son los que menos usas, podrías tardar semanas en notarlo.
--
-- · buscar_licitaciones y menores_resumen_cif: son las dos únicas funciones
--   que usa la web. Conservan su permiso; aquí no se tocan.
-- ############################################################################
