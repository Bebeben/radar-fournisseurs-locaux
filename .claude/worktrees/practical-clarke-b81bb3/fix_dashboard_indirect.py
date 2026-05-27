"""
fix_dashboard_indirect
======================

Script one-shot idempotent qui scanne l'onglet Dashboard et wrap toutes les
references directes vers `Reco prix`, `Concurrents`, `Prix d'achat` en
`INDIRECT(...)` -- sinon Sheets decale auto les ref a chaque insertion
newest-on-top et les formules cassent silencieusement.

Usage :
    python fix_dashboard_indirect.py             # dry-run (lecture seule, log les changements)
    python fix_dashboard_indirect.py --apply     # ecrit les corrections dans le Sheet

Idempotent : re-lancer le script ne casse rien (les cellules deja en
INDIRECT sont reconnues et sautees).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("fix_dashboard")

# Onglets en insertion newest-on-top : leurs ref directes A2/C2/etc se font
# decaler par Sheets a chaque insertion -> doivent etre wrapped INDIRECT.
ONGLETS_NEWEST_TOP = ["Reco prix", "Concurrents", "Prix d'achat"]

# Pattern qui match une reference SHEET avec NUMERO DE LIGNE explicite.
# - Reco prix       -> toujours quotes (espace dans le nom)         : 'Reco prix'!A2 ou 'Reco prix'!$L$2:$L$500
# - Concurrents     -> avec ou sans quotes                          : Concurrents!A2 ou 'Concurrents'!A2
# - Prix d'achat    -> toujours quotes, apostrophe doublee a l'API  : 'Prix d''achat'!A2
# On NE match PAS les colonnes entieres type 'Reco prix'!C:C qui ne sont pas affectees
# par les insertions newest-on-top.
PATTERN_REF = re.compile(
    r"""
    (?<!INDIRECT\(\")          # exclure ce qui est deja dans INDIRECT("
    (?<!INDIRECT\(')           # idem si quote simple (rare)
    (
        (?:'Reco\ prix'|'Concurrents'|Concurrents|'Prix\ d''achat')   # nom d'onglet
        !
        \$?[A-Z]+\$?\d+                                                # cellule (ligne explicite obligatoire)
        (?::\$?[A-Z]+\$?\d*)?                                          # range optionnel (ligne fin facultative ex O15:O)
    )
    """,
    re.VERBOSE,
)


def wrap_si_ref_directe(formule: str) -> tuple[str, list[str]]:
    """Detecte les refs directes vers onglets newest-on-top et les wrap en INDIRECT.

    Retourne (nouvelle_formule, [refs modifiees]). Si rien a faire, retourne
    (formule, []).
    """
    if not formule or not isinstance(formule, str) or not formule.startswith("="):
        return formule, []

    refs = []

    def repl(m: re.Match) -> str:
        ref = m.group(1)
        refs.append(ref)
        # INDIRECT prend une string. La ref peut contenir des apostrophes simples
        # (issues du nom 'Prix d''achat'), c'est OK dans une string entouree de "
        return f'INDIRECT("{ref}")'

    nouvelle = PATTERN_REF.sub(repl, formule)
    return nouvelle, refs


def main() -> int:
    racine = Path(__file__).resolve().parent
    env_file = racine / ".env"
    if env_file.is_file():
        load_dotenv(env_file)

    apply_mode = "--apply" in sys.argv

    sheet_id = os.getenv("GSHEET_ID", "")
    creds_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials/google_sheets_sa.json")
    if not Path(creds_path).is_absolute():
        creds_path = str(racine / creds_path)

    if not sheet_id:
        log.error("GSHEET_ID manquant (verifier .env ou variable d'env)")
        return 1
    if not Path(creds_path).is_file():
        log.error("credentials introuvables : %s", creds_path)
        return 1

    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # 1. Trouver l'onglet Dashboard
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    dashboard_id = None
    for s in meta["sheets"]:
        if s["properties"]["title"] == "Dashboard":
            dashboard_id = s["properties"]["sheetId"]
            break
    if dashboard_id is None:
        log.error("Onglet 'Dashboard' introuvable dans le Sheet")
        return 1
    log.info("Onglet Dashboard trouve (sheetId=%s)", dashboard_id)

    # 2. Lire toutes les formules du Dashboard (jusqu'a Z200, large)
    res = (
        svc.spreadsheets()
        .values()
        .get(
            spreadsheetId=sheet_id,
            range="Dashboard!A1:Z200",
            valueRenderOption="FORMULA",
        )
        .execute()
    )
    rows = res.get("values", [])

    # 3. Scanner et detecter les cellules a corriger
    corrections: list[dict] = []
    for r_idx, row in enumerate(rows):
        for c_idx, val in enumerate(row):
            nouvelle, refs = wrap_si_ref_directe(val)
            if refs:
                a1 = f"{_col_letter(c_idx + 1)}{r_idx + 1}"
                corrections.append({
                    "cellule": a1,
                    "avant": val,
                    "apres": nouvelle,
                    "refs": refs,
                })

    if not corrections:
        log.info("Aucune cellule a corriger -- le Dashboard est deja propre.")
        return 0

    log.info("=" * 70)
    log.info("%d cellule(s) du Dashboard a corriger :", len(corrections))
    log.info("=" * 70)
    for c in corrections:
        log.info("[%s]  %s", c["cellule"], ", ".join(c["refs"]))
        log.info("    AVANT : %s", c["avant"])
        log.info("    APRES : %s", c["apres"])
        log.info("")

    if not apply_mode:
        log.info("=" * 70)
        log.info("DRY-RUN : aucune modification ecrite. Relancer avec --apply pour appliquer.")
        return 0

    # 4. Apply -- batchUpdate avec les nouvelles formules
    data = [
        {"range": f"Dashboard!{c['cellule']}", "values": [[c["apres"]]]}
        for c in corrections
    ]
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()
    log.info("=" * 70)
    log.info("OK : %d cellule(s) mise(s) a jour dans le Dashboard.", len(corrections))
    return 0


def _col_letter(n: int) -> str:
    """1 -> A, 2 -> B, 27 -> AA, ..."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


if __name__ == "__main__":
    sys.exit(main())
