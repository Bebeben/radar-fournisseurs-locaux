# pricing-carburants-les-pieux

Système automatisé de pilotage des prix carburants pour **Super U Les Pieux** (Cotentin, 50340).

> **Le système PROPOSE, il n'APPLIQUE JAMAIS.** Aucun changement de prix automatique. Aucune connexion automatique à prix-carburants.gouv.fr. Déclaration manuelle obligatoire.

## Vue d'ensemble

- **Cadence** : 3 runs/jour (7h, 10h, 13h heure Paris) du lundi au samedi. Dimanche exclu.
- **Sources** : API publique `data.economie.gouv.fr`, Gmail (libellé `Factures/Carburant`), Google Sheet (Commande et prix station v2).
- **Sortie** : un mail à Benjamin — `ACTION` (proposition de repricing), `INFO` (signal pour brief lendemain), ou silence.
- **Stratégie** : cible 2 cts/L de marge brute, plancher 1 ct/L. Voir `../docs/strategie_tarifaire.md`.

## Structure

```
pricing-carburants-les-pieux/
├── README.md                   # ce fichier
├── CHECKLIST_DEPLOIEMENT.md    # étapes ordonnées pour la mise en prod
├── requirements.txt            # dépendances Python
├── .env.example                # template variables d'env
├── .gitignore
├── src/
│   ├── config_loader.py        # charge YAML + détecte A_COMPLETER
│   ├── moteur_decision.py      # cœur métier : arbre de décision, fonctions pures
│   ├── mail_builder.py         # composition des mails ACTION / INFO
│   ├── api_carburants.py       # client data.economie.gouv.fr
│   ├── gmail_factures.py       # détection factures Gmail
│   ├── sheet_io.py             # lecture/écriture Google Sheet
│   └── main.py                 # orchestrateur
└── tests/
    ├── test_config_loader.py
    ├── test_moteur_decision.py
    ├── test_mail_builder.py
    └── fixtures/
        ├── reponse_api_exemple.json
        └── sheet_state_exemple.json
```

Les configs YAML (stations, runs, gmail, magasin) sont à la **racine du pack de specs**, pas ici — dans `../config/`.

## Prérequis

- Python **3.12+** (la syntaxe `type X = ...` PEP 695 est utilisée)
- Accès internet pour l'API prix-carburants (en mode live)
- Plan Claude Code Pro + connecteur Gmail activé (pour les routines cloud, non bloquant pour tester en local)
- Google Cloud credentials (service account pour Sheets, OAuth pour Gmail) — uniquement en production

## Installation

```bash
cd pricing-carburants-les-pieux
pip install -r requirements.txt
cp .env.example .env    # puis éditer .env
```

## Usage

### Avant prise de fonction (sans email, sans Sheet réel)

```bash
DRY_RUN=true MOCK_API=true MAIL_ACTIF=false python -m src.main
```

- Charge les configs depuis `../config/`
- Lit l'état Sheet depuis `tests/fixtures/sheet_state_exemple.json`
- Lit les prix concurrents depuis `tests/fixtures/reponse_api_exemple.json`
- **Affiche la décision du moteur** (ACTION/INFO/SILENCE + justification)
- **Saute l'étape mail** (pas d'email pro encore créé)

### Dry-run avec mail simulé (après création email pro)

```bash
DRY_RUN=true MOCK_API=true MAIL_ACTIF=true python -m src.main
```

Ajoute la composition + l'affichage du mail qui aurait été envoyé.

### Live (production, après prise de fonction)

```bash
DRY_RUN=false MOCK_API=false python -m src.main
```

Prérequis :
- Tous les `A_COMPLETER` / `A_COMPLETER_PRISE_FONCTION` remplis dans les YAML
- Credentials Google (Sheets + Gmail) en place dans `./credentials/`
- Voir `CHECKLIST_DEPLOIEMENT.md`

### Tests

```bash
python -m pytest tests/ -v
```

36 tests couvrent : calcul marge, plancher, verrou intra-journée, arbre de décision (6 scénarios), exceptions verrou, format mail (virgule, bloc À déclarer).

## Garde-fous absolus

Ces règles sont codées explicitement et ne doivent pas être contournées :

| Règle | Code |
|---|---|
| Le système propose, n'applique jamais | `main.py` ne fait ni POST prix-carburants.gouv.fr, ni écriture POS |
| Pas plus d'un mail par run | `construire_mail()` renvoie un seul `Mail` ou `None` |
| Aucun run le dimanche | `est_jour_actif()` dans `main.py` (sauf `weekend_actif=true` explicite) |
| Plancher de marge respecté | `verifier_plancher()` — passer sous le plancher déclenche INFO manuel |
| Pas de production avec placeholders | `valider_pour_production()` lève `RuntimeError` si un `A_COMPLETER*` subsiste |

## Stratégie en résumé

1. **Verrou actif** (repricing déjà fait aujourd'hui) → veille silencieuse sauf exceptions (concurrent sous plancher, 3+ baisses, autre U bouge significativement)
2. **Nouvelle facture** + écart marge/cible > 0.3 cts/L → `ACTION` repricing vers cible
3. **Concurrent principal** (Intermarché Les Pieux) passe sous nos prix :
   - Alignement possible (reste ≥ plancher) → `ACTION`
   - Alignement sous plancher → `INFO` décision manuelle
4. **Rien ne bouge** → `SILENCE`

## Références

- `../docs/strategie_tarifaire.md` — stratégie v1.1 (règles métier)
- `../docs/proposition_automation.md` — architecture + flux mail + schéma Sheet
- `../prompts/routine_principale.md` — prompt opérationnel de la routine
- `../PROMPT_CLAUDE_CODE.md` — prompt de construction initial
- `../tasks/lessons.md` — journal des corrections apprises (SELF-LEARNING)
