# -*- coding: utf-8 -*-
"""Pruebas de la extracción de ADJUDICACIONES (Fase D1).

- Test UNITARIO con fixtures XML basadas en ejemplos REALES del feed (multi-lote,
  contrato sin lotes, lote desierto, acuerdo marco multi-adjudicatario, scheme UTE).
- Test de mapeo a la tabla (fila_adjudicacion: convenio None -> '').
- Test de NO regresión: extrae_entrada sigue devolviendo las claves del Radar/catálogo
  y fila_para_tabla NO incluye 'adjudicaciones'.
- SMOKE opcional en vivo (--vivo): baja el feed real y comprueba invariantes.

Uso:
  python test_adjudicaciones.py            # tests deterministas (sin red)
  python test_adjudicaciones.py --vivo     # + smoke contra el feed en vivo
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from lxml import etree

import feeds
from feeds import extrae_adjudicaciones, extrae_entrada, normaliza_cif, a_fecha
from backfill_catalogo import fila_adjudicacion, fila_para_tabla, _dedup_adj

NSDECL = (
    'xmlns="http://www.w3.org/2005/Atom" '
    'xmlns:cbc="urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2" '
    'xmlns:cac="urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2" '
    'xmlns:place="urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2" '
    'xmlns:pce="urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2"'
)


def _entry(cfs_inner, idu="https://contrataciondelestado.es/sindicacion/licitacionesPerfilContratante/1"):
    xml = (
        f'<entry {NSDECL}>'
        f'<id>{idu}</id><title>Contrato de prueba</title>'
        f'<link href="{idu}"/><updated>2026-07-01T10:00:00</updated>'
        f'<place:ContractFolderStatus>{cfs_inner}</place:ContractFolderStatus>'
        f'</entry>'
    )
    return etree.fromstring(xml.encode("utf-8"))


# --- Fixtures (basadas en expedientes reales del feed) ----------------------
MULTILOTE = _entry(
    '<cbc:ContractFolderID>20856/2022</cbc:ContractFolderID>'
    '<pce:ContractFolderStatusCode>RES</pce:ContractFolderStatusCode>'
    '<cac:TenderResult>'
    '  <cbc:ResultCode>9</cbc:ResultCode><cbc:AwardDate>2023-06-14</cbc:AwardDate>'
    '  <cbc:ReceivedTenderQuantity>1</cbc:ReceivedTenderQuantity>'
    '  <cbc:SMEAwardedIndicator>false</cbc:SMEAwardedIndicator>'
    '  <cac:WinningParty>'
    '    <cac:PartyIdentification><cbc:ID schemeName="NIF">A28541639</cbc:ID></cac:PartyIdentification>'
    '    <cac:PartyName><cbc:Name>FCC MEDIO AMBIENTE SA</cbc:Name></cac:PartyName>'
    '  </cac:WinningParty>'
    '  <cac:AwardedTenderedProject><cbc:ProcurementProjectLotID>1</cbc:ProcurementProjectLotID>'
    '    <cac:LegalMonetaryTotal>'
    '      <cbc:TaxExclusiveAmount currencyID="EUR">101075394.64</cbc:TaxExclusiveAmount>'
    '      <cbc:PayableAmount currencyID="EUR">111182934.11</cbc:PayableAmount>'
    '    </cac:LegalMonetaryTotal></cac:AwardedTenderedProject>'
    '</cac:TenderResult>'
    '<cac:TenderResult>'
    '  <cbc:ResultCode>9</cbc:ResultCode><cbc:AwardDate>2023-09-25</cbc:AwardDate>'
    '  <cbc:ReceivedTenderQuantity>2</cbc:ReceivedTenderQuantity>'
    '  <cbc:SMEAwardedIndicator>true</cbc:SMEAwardedIndicator>'
    '  <cac:WinningParty>'
    '    <cac:PartyIdentification><cbc:ID schemeName="NIF">B90054065</cbc:ID></cac:PartyIdentification>'
    '    <cac:PartyName><cbc:Name>Grupo CONSIDERA SL</cbc:Name></cac:PartyName>'
    '  </cac:WinningParty>'
    '  <cac:AwardedTenderedProject><cbc:ProcurementProjectLotID>2</cbc:ProcurementProjectLotID>'
    '    <cac:LegalMonetaryTotal>'
    '      <cbc:TaxExclusiveAmount currencyID="EUR">176000</cbc:TaxExclusiveAmount>'
    '      <cbc:PayableAmount currencyID="EUR">212960</cbc:PayableAmount>'
    '    </cac:LegalMonetaryTotal></cac:AwardedTenderedProject>'
    '</cac:TenderResult>'
)

SIN_LOTES = _entry(
    '<cbc:ContractFolderID>2026_0025</cbc:ContractFolderID>'
    '<pce:ContractFolderStatusCode>ADJ</pce:ContractFolderStatusCode>'
    '<cac:TenderResult>'
    '  <cbc:ResultCode>9</cbc:ResultCode><cbc:AwardDate>2026-06-03</cbc:AwardDate>'
    '  <cbc:ReceivedTenderQuantity>1</cbc:ReceivedTenderQuantity>'
    '  <cbc:SMEAwardedIndicator>false</cbc:SMEAwardedIndicator>'
    '  <cac:WinningParty>'
    '    <cac:PartyIdentification><cbc:ID schemeName="NIF"> a-28.141-935 </cbc:ID></cac:PartyIdentification>'
    '    <cac:PartyName><cbc:Name>MAPFRE ESPAÑA SA</cbc:Name></cac:PartyName>'
    '  </cac:WinningParty>'
    '  <cac:AwardedTenderedProject>'  # sin ProcurementProjectLotID -> lote None
    '    <cac:LegalMonetaryTotal>'
    '      <cbc:TaxExclusiveAmount currencyID="EUR">979000</cbc:TaxExclusiveAmount>'
    '      <cbc:PayableAmount currencyID="EUR">979000</cbc:PayableAmount>'
    '    </cac:LegalMonetaryTotal></cac:AwardedTenderedProject>'
    '</cac:TenderResult>'
)

DESIERTO = _entry(
    '<cbc:ContractFolderID>M/0001/A/26/0</cbc:ContractFolderID>'
    '<pce:ContractFolderStatusCode>EV</pce:ContractFolderStatusCode>'
    '<cac:TenderResult>'
    '  <cbc:ResultCode>3</cbc:ResultCode><cbc:AwardDate>2026-07-01</cbc:AwardDate>'
    '  <cbc:ReceivedTenderQuantity>0</cbc:ReceivedTenderQuantity>'
    '</cac:TenderResult>'
)

ACUERDO_MARCO = _entry(  # un TenderResult, un lote, VARIOS adjudicatarios (uno UTE)
    '<pce:ContractFolderStatusCode>ADJ</pce:ContractFolderStatusCode>'
    '<cac:TenderResult>'
    '  <cbc:ResultCode>8</cbc:ResultCode><cbc:ReceivedTenderQuantity>4</cbc:ReceivedTenderQuantity>'
    '  <cac:WinningParty>'
    '    <cac:PartyIdentification><cbc:ID schemeName="NIF">A11111111</cbc:ID></cac:PartyIdentification>'
    '    <cac:PartyName><cbc:Name>Empresa Uno SA</cbc:Name></cac:PartyName>'
    '  </cac:WinningParty>'
    '  <cac:WinningParty>'
    '    <cac:PartyIdentification><cbc:ID schemeName="UTE">U22222222</cbc:ID></cac:PartyIdentification>'
    '    <cac:PartyName><cbc:Name>UTE Dos</cbc:Name></cac:PartyName>'
    '  </cac:WinningParty>'
    '  <cac:AwardedTenderedProject><cbc:ProcurementProjectLotID>1</cbc:ProcurementProjectLotID>'
    '    <cac:LegalMonetaryTotal>'
    '      <cbc:TaxExclusiveAmount currencyID="EUR">50000</cbc:TaxExclusiveAmount>'
    '    </cac:LegalMonetaryTotal></cac:AwardedTenderedProject>'
    '</cac:TenderResult>'
)

SIN_ADJUDICAR = _entry(  # expediente todavía sin TenderResult
    '<cbc:ContractFolderID>NUEVO/1</cbc:ContractFolderID>'
    '<pce:ContractFolderStatusCode>PUB</pce:ContractFolderStatusCode>'
)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_multilote():
    filas = extrae_adjudicaciones(MULTILOTE)
    check(len(filas) == 2, f"multilote: esperaba 2 filas, hay {len(filas)}")
    a, b = filas
    check(a["lote"] == "1" and b["lote"] == "2", "multilote: lotes 1/2")
    check(a["cif_adjudicatario"] == "A28541639", f"multilote: cif A {a['cif_adjudicatario']}")
    check(b["cif_adjudicatario"] == "B90054065", "multilote: cif B")
    check(a["id_scheme"] == "NIF" and b["id_scheme"] == "NIF", "multilote: scheme NIF")
    check(a["adjudicatario"] == "FCC MEDIO AMBIENTE SA", "multilote: nombre A")
    check(a["es_pyme"] is False and b["es_pyme"] is True, "multilote: es_pyme")
    check(abs(a["importe_sin_iva"] - 101075394.64) < 1e-6, "multilote: importe sin IVA A")
    check(abs(a["importe_con_iva"] - 111182934.11) < 1e-6, "multilote: importe con IVA A")
    check(a["n_ofertas"] == 1 and b["n_ofertas"] == 2, "multilote: n_ofertas")
    check(a["fecha_adjudicacion"] == "2023-06-14", "multilote: fecha A")
    check(a["resultado_code"] == "9" and a["resultado"] == "Formalizado", "multilote: resultado")
    check(a["estado_expediente"] == "RES", "multilote: estado RES")
    print("  OK test_multilote")


def test_sin_lotes_y_normaliza_cif():
    filas = extrae_adjudicaciones(SIN_LOTES)
    check(len(filas) == 1, "sin_lotes: 1 fila")
    f = filas[0]
    check(f["lote"] is None, f"sin_lotes: lote None en extractor, era {f['lote']!r}")
    # CIF sucio ' a-28.141-935 ' se normaliza a 'A28141935'
    check(f["cif_adjudicatario"] == "A28141935", f"sin_lotes: cif normalizado, era {f['cif_adjudicatario']!r}")
    check(f["importe_sin_iva"] == 979000.0 and f["importe_con_iva"] == 979000.0, "sin_lotes: importes")
    # convenio de la tabla: lote None -> '' al mapear
    row = fila_adjudicacion("id-x", f)
    check(row["lote"] == "" and row["cif_adjudicatario"] == "A28141935", "sin_lotes: fila_adjudicacion lote ''")
    print("  OK test_sin_lotes_y_normaliza_cif")


def test_desierto():
    filas = extrae_adjudicaciones(DESIERTO)
    check(len(filas) == 1, "desierto: 1 fila")
    f = filas[0]
    check(f["cif_adjudicatario"] is None, "desierto: cif None")
    check(f["adjudicatario"] is None and f["id_scheme"] is None, "desierto: nombre/scheme None")
    check(f["resultado_code"] == "3" and f["resultado"] == "Desierto", "desierto: resultado")
    check(f["importe_sin_iva"] is None and f["lote"] is None, "desierto: sin importe/lote")
    check(f["n_ofertas"] == 0, "desierto: 0 ofertas")
    row = fila_adjudicacion("id-y", f)
    check(row["cif_adjudicatario"] == "" and row["lote"] == "", "desierto: fila_adjudicacion cif/lote ''")
    print("  OK test_desierto")


def test_acuerdo_marco():
    filas = extrae_adjudicaciones(ACUERDO_MARCO)
    check(len(filas) == 2, f"acuerdo_marco: esperaba 2 filas, hay {len(filas)}")
    check({f["lote"] for f in filas} == {"1"}, "acuerdo_marco: mismo lote 1")
    check({f["cif_adjudicatario"] for f in filas} == {"A11111111", "U22222222"}, "acuerdo_marco: dos CIF")
    check({f["id_scheme"] for f in filas} == {"NIF", "UTE"}, "acuerdo_marco: schemes NIF/UTE")
    check(all(f["importe_sin_iva"] == 50000.0 for f in filas), "acuerdo_marco: importe repetido por adjudicatario")
    check(all(f["resultado"] == "Adjudicado" for f in filas), "acuerdo_marco: code 8 -> Adjudicado")
    print("  OK test_acuerdo_marco")


def test_sin_adjudicar():
    check(extrae_adjudicaciones(SIN_ADJUDICAR) == [], "sin_adjudicar: lista vacía")
    print("  OK test_sin_adjudicar")


def test_no_regresion_extrae_entrada():
    reg = extrae_entrada(MULTILOTE, "estatal")
    # clave nueva presente y coherente con extrae_adjudicaciones
    check("adjudicaciones" in reg, "extrae_entrada: falta clave 'adjudicaciones'")
    check(len(reg["adjudicaciones"]) == 2, "extrae_entrada: 2 adjudicaciones")
    # las claves que consume el Radar/catálogo siguen ahí
    for k in ("id", "titulo", "objeto", "enlace", "cpv", "fuente", "num_expediente",
              "presupuesto_con_iva", "fecha_publicacion"):
        check(k in reg, f"extrae_entrada: falta clave '{k}'")
    # el mapeo a public.licitaciones NO arrastra adjudicaciones (aditivo, no rompe catálogo)
    row = fila_para_tabla(reg)
    check("adjudicaciones" not in row, "fila_para_tabla: no debe incluir 'adjudicaciones'")
    print("  OK test_no_regresion_extrae_entrada")


def test_a_fecha():
    from datetime import date, timedelta
    # válidas: se recorta a AAAA-MM-DD
    check(a_fecha("2026-06-03") == "2026-06-03", "a_fecha: fecha simple")
    check(a_fecha("2026-06-03T10:00:00") == "2026-06-03", "a_fecha: con hora")
    # malformadas de CALENDARIO -> None (no revientan el lote al insertar en columna date)
    check(a_fecha("2026-13-40") is None, "a_fecha: mes/día fuera de rango -> None")
    check(a_fecha("2026-02-30") is None, "a_fecha: 30 de febrero -> None")
    check(a_fecha("2026-00-00") is None, "a_fecha: ceros -> None")
    check(a_fecha("no-es-fecha") is None, "a_fecha: basura -> None")
    check(a_fecha(None) is None, "a_fecha: None -> None")
    # IMPLAUSIBLES (basura de origen del feed) -> None
    check(a_fecha("0001-01-03") is None, "a_fecha: año 0001 -> None")
    check(a_fecha("1999-12-31") is None, "a_fecha: antes de 2000 -> None")
    lejos = (date.today() + timedelta(days=3 * 365)).isoformat()   # +3 años: fuera de la ventana
    check(a_fecha(lejos) is None, "a_fecha: muy futura (>hoy+2años) -> None")
    dentro = (date.today() + timedelta(days=30)).isoformat()       # +1 mes: dentro
    check(a_fecha(dentro) == dentro, "a_fecha: futuro cercano -> se conserva")
    print("  OK test_a_fecha")


def test_dedup_adj():
    f = extrae_adjudicaciones(SIN_LOTES)[0]
    r1 = fila_adjudicacion("mismo-id", f)
    r2 = fila_adjudicacion("mismo-id", f)
    check(len(_dedup_adj([r1, r2])) == 1, "dedup: misma clave colapsa a 1")
    r3 = dict(r2); r3["lote"] = "9"
    check(len(_dedup_adj([r1, r3])) == 2, "dedup: distinto lote no colapsa")
    print("  OK test_dedup_adj")


def smoke_vivo():
    import requests, time
    from feeds import CABECERAS, ATOM_NS, FEEDS
    print("\nSMOKE en vivo (invariantes sobre el feed real):")
    total_adj = 0
    desiertos = 0
    schemes = set()
    for feed in FEEDS:
        r = requests.get(feed["url"], headers=CABECERAS, timeout=60)
        r.raise_for_status()
        raiz = etree.fromstring(r.content)
        n_entradas = 0
        for e in raiz.findall("atom:entry", ATOM_NS):
            n_entradas += 1
            reg = extrae_entrada(e, feed["fuente"])
            for a in reg["adjudicaciones"]:
                total_adj += 1
                # invariante: si hay cif, está normalizado (mayúsculas, sin separadores)
                if a["cif_adjudicatario"] is not None:
                    c = a["cif_adjudicatario"]
                    check(c == normaliza_cif(c), f"smoke: cif no normalizado {c!r}")
                    schemes.add(a["id_scheme"])
                else:
                    desiertos += 1
                # invariante: la fila mapea sin excepción y respeta convenios
                row = fila_adjudicacion(reg["id"], a)
                check(row["lote"] is not None and row["cif_adjudicatario"] is not None,
                      "smoke: fila_adjudicacion no debe dejar None en lote/cif")
        print(f"  feed «{feed['fuente']}»: {n_entradas} entradas leídas")
        time.sleep(0.3)
    print(f"  adjudicaciones extraídas: {total_adj}  (desiertas: {desiertos})  schemes: {schemes}")
    check(total_adj > 0, "smoke: esperaba alguna adjudicación en el feed en vivo")
    print("  OK smoke_vivo")


def main():
    print("Tests deterministas (sin red):")
    test_multilote()
    test_sin_lotes_y_normaliza_cif()
    test_desierto()
    test_acuerdo_marco()
    test_sin_adjudicar()
    test_no_regresion_extrae_entrada()
    test_a_fecha()
    test_dedup_adj()
    if "--vivo" in sys.argv:
        smoke_vivo()
    print("\nTODO OK ✔")


if __name__ == "__main__":
    main()
