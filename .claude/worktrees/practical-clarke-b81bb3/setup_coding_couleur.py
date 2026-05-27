"""Color coding marge pondérée — vert/orange/rouge avec tolérance configurable."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET_ID = "1OBfPaZowMi2eq1VnswmxjSjLkBtutHWFjdvTLomndAw"
creds = Credentials.from_service_account_file("credentials/pricing-les-pieux-89e0a724edd4.json", scopes=["https://www.googleapis.com/auth/spreadsheets"])
svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

meta = svc.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

# === A) Paramètres : ajouter ligne 14 "Tolérance marge pondérée (points)" ===
param_id = sheet_ids["Paramètres"]
svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [{
    "insertDimension": {
        "range": {"sheetId": param_id, "dimension": "ROWS", "startIndex": 13, "endIndex": 14},
        "inheritFromBefore": True,
    }
}]}).execute()
svc.spreadsheets().values().update(
    spreadsheetId=SHEET_ID, range="Paramètres!B14:D14",
    valueInputOption="USER_ENTERED",
    body={"values": [["Tolérance marge pondérée (points)", 0.002, "Plage orange autour de la cible. Exemple : cible 3,5% + tolérance 0,2 pts → orange entre 3,3% et 3,5%, rouge < 3,3%, vert >= 3,5%"]]}
).execute()
svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [{
    "repeatCell": {
        "range": {"sheetId": param_id, "startRowIndex": 13, "endRowIndex": 14, "startColumnIndex": 2, "endColumnIndex": 3},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "PERCENT", "pattern": "0.00%"}}},
        "fields": "userEnteredFormat.numberFormat"
    }
}]}).execute()
print("A) Parametres L14 : tolerance 0,2 pts ajoutee")

# === B) Color coding sur Pricing live!C24 (marge %) et H24 (marge €) ===
pl_id = sheet_ids["Pricing live"]

# Helper pour construire un rule conditional
def cond_rule(sheet_id, row_start, row_end, col_start, col_end, formula, bg_color, text_bold=True):
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": row_start, "endRowIndex": row_end, "startColumnIndex": col_start, "endColumnIndex": col_end}],
                "booleanRule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "format": {"backgroundColor": bg_color, "textFormat": {"bold": text_bold}}
                }
            },
            "index": 0
        }
    }

GREEN = {"red": 0.72, "green": 0.88, "blue": 0.72}
ORANGE = {"red": 1.0, "green": 0.85, "blue": 0.55}
RED = {"red": 0.96, "green": 0.78, "blue": 0.78}

# Pour Pricing live C24 (marge %) ET H24 (marge €) — on color les 2 selon C24 (%)
# Les formules CUSTOM_FORMULA prennent la cellule top-left du range comme reference
# Donc pour C24 directement et H24 indirectement (lien vers C24)
reqs = [
    # C24 GREEN si >= cible
    cond_rule(pl_id, 23, 24, 2, 3, '=$C$24>=Paramètres!$C$12', GREEN),
    # C24 ORANGE si dans tolerance
    cond_rule(pl_id, 23, 24, 2, 3, '=AND($C$24<Paramètres!$C$12;$C$24>=Paramètres!$C$12-Paramètres!$C$14)', ORANGE),
    # C24 RED si sous tolerance
    cond_rule(pl_id, 23, 24, 2, 3, '=$C$24<Paramètres!$C$12-Paramètres!$C$14', RED),
    # H24 idem (référence à C24 pour la couleur)
    cond_rule(pl_id, 23, 24, 7, 8, '=$C$24>=Paramètres!$C$12', GREEN),
    cond_rule(pl_id, 23, 24, 7, 8, '=AND($C$24<Paramètres!$C$12;$C$24>=Paramètres!$C$12-Paramètres!$C$14)', ORANGE),
    cond_rule(pl_id, 23, 24, 7, 8, '=$C$24<Paramètres!$C$12-Paramètres!$C$14', RED),
]
svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": reqs}).execute()
print("B) Pricing live C24 + H24 : color coding applique")

# === C) Reco prix col L (Marge pondérée %) toutes lignes ===
rp_id = sheet_ids["Reco prix"]
# Range : ligne 2 jusqu a 1000, col L = index 11
reqs_rp = [
    cond_rule(rp_id, 1, 1000, 11, 12, '=$L2>=Paramètres!$C$12', GREEN, False),
    cond_rule(rp_id, 1, 1000, 11, 12, '=AND($L2<Paramètres!$C$12;$L2>=Paramètres!$C$12-Paramètres!$C$14)', ORANGE, False),
    cond_rule(rp_id, 1, 1000, 11, 12, '=$L2<Paramètres!$C$12-Paramètres!$C$14', RED, False),
]
svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": reqs_rp}).execute()
print("C) Reco prix col L : color coding applique")

# === D) Dashboard B6 (KPI marge pondérée dernier run) ===
dash_id = sheet_ids["Dashboard"]
reqs_d = [
    cond_rule(dash_id, 5, 6, 1, 2, '=$B$6>=$B$7', GREEN),
    cond_rule(dash_id, 5, 6, 1, 2, '=AND($B$6<$B$7;$B$6>=$B$7-Paramètres!$C$14)', ORANGE),
    cond_rule(dash_id, 5, 6, 1, 2, '=$B$6<$B$7-Paramètres!$C$14', RED),
]
svc.spreadsheets().batchUpdate(spreadsheetId=SHEET_ID, body={"requests": reqs_d}).execute()
print("D) Dashboard B6 : color coding applique")

print()
print("Tout en place. Va voir : marge ponderee 3,5% cible avec orange entre 3,3-3,5%, rouge < 3,3%, vert >= 3,5%.")
