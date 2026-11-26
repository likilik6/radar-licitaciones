# -*- coding: utf-8 -*-
"""ya_lanzado_hoy.py — ¿la tarea de las 5:00 del PC de Radar Farma ya lanzó hoy el Radar? (26/11/2026)

Lo usa el job «comprueba» de radar.yml, solo en las ejecuciones PROGRAMADAS. GitHub arranca los programados de este
repo horas tarde (ver el cron), así que el PC de Radar Farma lanza el Radar a las 5:00 a mano (workflow_dispatch, con
la casilla «diaria», que le pone el título TITULO) y el cron queda de respaldo:
  - si hoy (hora de España) hay una ejecución lanzada por el PC que acabó bien o sigue en marcha, la programada no hace
    nada: repetirla serían ~60 peticiones más a la Plataforma y una segunda ingesta del catálogo;
  - si el PC no la lanzó (apagado, sin red) o su ejecución falló o se canceló, la programada corre entera;
  - si no se puede consultar la API de GitHub, también corre: mejor dos veces que ninguna.

Escribe «saltar=true» o «saltar=false» en $GITHUB_OUTPUT (y lo explica en el log). Solo biblioteca estándar.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

# Tiene que ser EXACTAMENTE el run-name que pone radar.yml cuando «diaria» está marcada.
TITULO = "Radar de licitaciones (5:00, desde el PC)"
MADRID = ZoneInfo("Europe/Madrid")


def dia_madrid(iso):
    """La fecha en España de un instante ISO de la API ('2026-11-27T04:00:05Z')."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MADRID).date()


def decide(runs, hoy):
    """(saltar, motivo) a partir de las ejecuciones de radar.yml lanzadas a mano (las de la API, más nuevas primero)."""
    for r in runs:
        if r.get("display_title") != TITULO or not r.get("created_at") or dia_madrid(r["created_at"]) != hoy:
            continue
        if r.get("status") != "completed":
            return True, f"la del PC de hoy sigue en marcha ({r.get('status')}): {r.get('html_url')}"
        if r.get("conclusion") == "success":
            return True, f"la del PC de hoy acabó bien: {r.get('html_url')}"
    return False, "hoy no hay una ejecución del PC que acabara bien: corre la programada"


def main():
    saltar, motivo = False, "no es una ejecución programada"
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        url = (f"https://api.github.com/repos/{os.environ.get('GITHUB_REPOSITORY')}/actions/workflows/radar.yml/runs"
               f"?event=workflow_dispatch&per_page=30")
        peticion = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {os.environ.get('GH_TOKEN', '')}",
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(peticion, timeout=30) as r:
                runs = json.load(r).get("workflow_runs") or []
            saltar, motivo = decide(runs, datetime.now(MADRID).date())
        except Exception as e:  # noqa: BLE001 — sin API se corre: mejor dos veces que ninguna
            saltar, motivo = False, f"no pude consultar la API de GitHub ({type(e).__name__}: {e}); corre la programada"
    print(f"saltar={'true' if saltar else 'false'}: {motivo}")
    salida = os.environ.get("GITHUB_OUTPUT")
    if salida:
        with open(salida, "a", encoding="utf-8") as f:
            f.write(f"saltar={'true' if saltar else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
