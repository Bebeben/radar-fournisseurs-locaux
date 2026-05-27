"""Tests achat_telegram : parser prix, balise J/J+1, variation anormale."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.achat_telegram import (
    determiner_pour_jour,
    detecter_variation_anormale,
    est_commande_accept,
    est_commande_aide,
    lien_sheet,
    parser_message_prix,
    parser_message_prix_vente,
    parser_reponse_achat,
)


# ------------------------------------------------------------
# parser_message_prix
# ------------------------------------------------------------


def test_parser_message_prix_ok():
    texte = "prix E85=1,650 E10=1,590 SP98=1,650 GAZ=1,720"
    p = parser_message_prix(texte)
    assert p == {"E85": 1.650, "SP95-E10": 1.590, "SP98": 1.650, "Gazole": 1.720}


def test_parser_message_prix_avec_point():
    texte = "prix E85=1.650 E10=1.590 SP98=1.650 GAZ=1.720"
    p = parser_message_prix(texte)
    assert p["E85"] == 1.650


def test_parser_message_prix_partiel():
    """Si seuls SP95 et Gazole sont fournis, on les garde."""
    texte = "prix E85=1,650 GAZ=1,720"
    p = parser_message_prix(texte)
    assert p == {"E85": 1.650, "Gazole": 1.720}


def test_parser_message_prix_alias_gazole():
    """Le Gazole accepte plusieurs alias : GAZ, GAZOLE, GASOIL, DIESEL (insensible casse).

    Bug Benjamin du 12/05/2026 : il avait tape 'prix gazole=2,06' au lieu de
    'prix gaz=2,06', le regex etait strict 'GAZ=' donc rien capte.
    """
    for txt in ("prix gazole=2,06", "prix GAZOLE=2,06", "prix Gazole=2,06",
                "prix gasoil=2,06", "prix diesel=2,06", "prix GAZ=2,06"):
        p = parser_message_prix(txt)
        assert p == {"Gazole": 2.06}, f"'{txt}' devrait matcher Gazole=2.06, got {p}"


def test_parser_message_prix_alias_e10():
    """E10 accepte aussi 'SP95-E10=' explicite."""
    assert parser_message_prix("prix SP95-E10=1,58") == {"SP95-E10": 1.58}
    assert parser_message_prix("prix E10=1,58") == {"SP95-E10": 1.58}


def test_parser_message_prix_sans_prefixe_ok():
    """Préfixe 'prix' désormais OPTIONNEL : 'gaz=2,1' sans 'prix' est accepté.

    Bug Benjamin 12/05/2026 : il avait tapé 'gaz=2,1' sans 'prix', le parser
    ignorait silencieusement. Maintenant tout message contenant carburant=X,XX
    est interprété comme prix d'achat (sauf 'oui ...' / 'prix vente ...').
    """
    assert parser_message_prix("gaz=2,1") == {"Gazole": 2.1}
    assert parser_message_prix("E10=1,580 SP98=1,720") == {"SP95-E10": 1.58, "SP98": 1.72}


def test_parser_message_prix_phrase_sans_carburant():
    """Phrase libre sans carburant = None (pas d'interférence avec messages classiques)."""
    assert parser_message_prix("pause bot") is None
    assert parser_message_prix("/help") is None
    assert parser_message_prix("reprise") is None
    assert parser_message_prix("salut comment ça va") is None


def test_parser_message_prix_oui_non_concerne():
    """'oui SP95=...' n'est PAS un message prix (différencier de la réponse achat)."""
    texte = "oui E85=1,650 E10=1,590 SP98=1,650 GAZ=1,720"
    assert parser_message_prix(texte) is None


def test_parser_message_prix_exclut_prix_vente():
    """'prix vente SP95=...' est une commande différente, ne match pas parser_message_prix."""
    texte = "prix vente E85=1,720 E10=1,705"
    assert parser_message_prix(texte) is None


# ------------------------------------------------------------
# parser_message_prix_vente
# ------------------------------------------------------------


def test_parser_message_prix_vente_complet():
    texte = "prix vente E85=1,720 E10=1,705 SP98=1,820 GAZ=1,950"
    p = parser_message_prix_vente(texte)
    assert p == {"E85": 1.720, "SP95-E10": 1.705, "SP98": 1.820, "Gazole": 1.950}


def test_parser_message_prix_vente_partiel():
    """Override d'un seul carburant : ok."""
    texte = "prix vente E85=1,649"
    p = parser_message_prix_vente(texte)
    assert p == {"E85": 1.649}


def test_parser_message_prix_vente_alias_vente():
    """'vente SP95=...' (sans prix) doit aussi marcher."""
    texte = "vente E85=1,649"
    p = parser_message_prix_vente(texte)
    assert p == {"E85": 1.649}


def test_parser_message_prix_vente_exclut_prix_achat():
    """'prix SP95=...' est une commande achat, ne match pas parser_message_prix_vente."""
    texte = "prix E85=1,650"
    assert parser_message_prix_vente(texte) is None


# ------------------------------------------------------------
# est_commande_accept
# ------------------------------------------------------------


def test_est_commande_accept_variantes():
    assert est_commande_accept("accepter")
    assert est_commande_accept("accept")
    assert est_commande_accept("OK")
    assert est_commande_accept("  appliquer  ")
    assert est_commande_accept("valider")
    assert est_commande_accept("J'ACCEPTE")
    # Variantes ajoutees apres bug Benjamin 13/05 ("Ok aligne")
    assert est_commande_accept("ok aligne")
    assert est_commande_accept("Ok aligne")
    assert est_commande_accept("OK ALIGNE")
    assert est_commande_accept("aligne")
    assert est_commande_accept("aligner")
    assert est_commande_accept("alignement")
    assert est_commande_accept("ok alignement")
    assert est_commande_accept("appliquer recos")
    assert est_commande_accept("valider recos")
    assert est_commande_accept("go")


def test_est_commande_accept_faux_positifs():
    assert not est_commande_accept("oui")
    assert not est_commande_accept("non")
    assert not est_commande_accept("prix vente E85=1,649")
    assert not est_commande_accept("ok prix vente plus tard")


# ------------------------------------------------------------
# est_commande_aide
# ------------------------------------------------------------


def test_est_commande_aide_variantes():
    for cmd in ("aide", "AIDE", "Aide", "?", "??", "help", "/help", "/aide",
                "commande", "commandes", "menu", "liste", "aide ?"):
        assert est_commande_aide(cmd), f"'{cmd}' devrait être reconnu comme aide"


def test_est_commande_aide_faux_positifs():
    assert not est_commande_aide("oui")
    assert not est_commande_aide("prix E85=1,650")
    assert not est_commande_aide("accepter")
    assert not est_commande_aide("aidez moi")  # phrase libre, pas une commande


# ------------------------------------------------------------
# lien_sheet
# ------------------------------------------------------------


def test_lien_sheet_avec_id():
    url = lien_sheet("ABC123")
    assert url == "https://docs.google.com/spreadsheets/d/ABC123/edit"


def test_lien_sheet_vide():
    assert lien_sheet("") == ""


# ------------------------------------------------------------
# determiner_pour_jour
# ------------------------------------------------------------


def test_pour_jour_avant_bascule():
    paris = ZoneInfo("Europe/Paris")
    msg = datetime(2026, 5, 1, 9, 30, tzinfo=paris)
    assert determiner_pour_jour(msg, "11:00") == "J"


def test_pour_jour_apres_bascule():
    paris = ZoneInfo("Europe/Paris")
    msg = datetime(2026, 5, 1, 14, 0, tzinfo=paris)
    assert determiner_pour_jour(msg, "11:00") == "J+1"


def test_pour_jour_pile_bascule():
    """Bascule pile à 11h00 → considéré J+1 (>=)."""
    paris = ZoneInfo("Europe/Paris")
    msg = datetime(2026, 5, 1, 11, 0, tzinfo=paris)
    assert determiner_pour_jour(msg, "11:00") == "J+1"


def test_pour_jour_seuil_personnalise():
    paris = ZoneInfo("Europe/Paris")
    msg = datetime(2026, 5, 1, 10, 30, tzinfo=paris)
    # Avec seuil 10h30 : 10h30 = J+1
    assert determiner_pour_jour(msg, "10:30") == "J+1"
    msg2 = datetime(2026, 5, 1, 10, 29, tzinfo=paris)
    assert determiner_pour_jour(msg2, "10:30") == "J"


# ------------------------------------------------------------
# detecter_variation_anormale
# ------------------------------------------------------------


def test_variation_pas_anormale_si_petit_ecart():
    nouveau = {"Gazole": 1.700}
    ancien = {"Gazole": 1.720}
    alertes = detecter_variation_anormale(nouveau, ancien, seuil_eur=0.10)
    assert alertes == []


def test_variation_anormale_si_grand_ecart():
    nouveau = {"Gazole": 1.500}
    ancien = {"Gazole": 1.720}
    alertes = detecter_variation_anormale(nouveau, ancien, seuil_eur=0.10)
    assert len(alertes) == 1
    assert "Gazole" in alertes[0]


def test_variation_aucun_precedent():
    """Si pas de prix précédent, pas d'alerte."""
    nouveau = {"Gazole": 1.500}
    alertes = detecter_variation_anormale(nouveau, None, seuil_eur=0.10)
    assert alertes == []


# ------------------------------------------------------------
# parser_reponse_achat (avec inline boutons OUI/NON)
# ------------------------------------------------------------


def test_reponse_oui_simple():
    """Réponse 'oui' tout court (depuis bouton OUI) doit être interprétée comme commande."""
    p = parser_reponse_achat("oui")
    assert p is not None
    assert p["achat"] is True
    assert p["prix"] == {}  # pas de prix dans le message


def test_reponse_non_simple():
    p = parser_reponse_achat("non")
    assert p == {"achat": False}


def test_reponse_oui_avec_prix_explicit():
    """Réponse 'oui SP95=... GAZ=...' doit aussi marcher (ancienne syntaxe)."""
    p = parser_reponse_achat("oui E85=1,650 GAZ=1,720")
    assert p["achat"] is True
    assert p["prix"]["E85"] == 1.650
