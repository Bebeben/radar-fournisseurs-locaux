# Checklist de déploiement — Pricing carburants Super U Les Pieux

État au 26 avril 2026, à reprendre le jour de la prise de fonction effective.

---

## 🟢 État actuel — ce qui fonctionne déjà

### Infrastructure
- [x] **Repo GitHub privé** : https://github.com/Bebeben/pricing-carburants-les-pieux
- [x] **Google Sheet** : `Pricing carburants Les Pieux` (ID `1OBfPaZowMi2eq1VnswmxjSjLkBtutHWFjdvTLomndAw`)
- [x] **Service account Google Cloud** : `pricing-bot@pricing-les-pieux.iam.gserviceaccount.com` avec accès Éditeur au Sheet
- [x] **GitHub Actions** : workflow `pricing.yml` actif, planifié 6h+7h UTC lun-sam (= 8h Paris)
- [x] **Secrets GitHub** : `GSHEET_ID`, `GOOGLE_SHEETS_SA_JSON` configurés

### Code
- [x] **Backend dual** : xlsx local OU gsheet cloud (variable `BACKEND`)
- [x] **Moteur de décision** complet (cible, plancher, alignement, exceptions)
- [x] **Tests** : 38 verts (config_loader, mail_builder, moteur_decision)
- [x] **Validation placeholders** différenciée hard/soft

### Cadence actuelle
- [x] **1 run/jour à 8h Paris** (le code filtre les fausses heures)
- [x] **MAIL_ACTIF=false** (étape mail désactivée tant que pas d'email pro)

---

## 🟡 À FAIRE le jour de la prise de fonction Les Pieux

Ordre conseillé. Compter ~1h en tout.

### 1. Compléter les YAML (15 min)

Remplacer tous les `A_COMPLETER_PRISE_FONCTION` dans :

#### `config/parametres_magasin.yaml`
- [ ] `identite.siret` — 14 chiffres (depuis Kbis post-cession)
- [ ] `contact.telephone_station` — numéro standard de la station
- [ ] `contact.email_direction` — email pro Les Pieux
- [ ] `prix_carburants_gouv.login_espace_gerant` — login (pas le mot de passe !)

#### `config/parametres_gmail.yaml`
- [ ] `destinataire.email_principal` — email où tu reçois les alertes
- [ ] `expediteur.email` — email d'envoi (peut être identique)
- [ ] `filtrage_factures_gmail.expediteurs_attendus[0]` — adresse SIPLEC depuis 1ère facture
- [ ] `filtrage_factures_gmail.expediteurs_attendus[1]` — autre fournisseur si applicable

#### Onglet Stations du Google Sheet (édition directe, pas de YAML)
- [ ] Vérifier l'ID Super U Bricquebec : trancher entre `50260003` (Rue Bitouzé) et `50260004` (Rue Frémine) avec Google Maps
- [ ] Ajouter / retirer des concurrents si besoin selon ce que tu observes terrain

### 2. Pousser les modifs (2 min)

```cmd
cd "C:\Users\bgela\OneDrive\Documents\Claude_ IA\Station service\pricing-carburants-les-pieux"
git add config/
git commit -m "Prise de fonction: remplit placeholders Les Pieux"
git push
```

### 3. Activer Gmail (15 min)

- [ ] Activer le **connecteur Gmail dans Claude.ai** : Settings → Connectors → Gmail
- [ ] Dans Gmail : créer le **libellé `Factures/Carburant`**
- [ ] Configurer un **filtre auto Gmail** : `De: factures@siplec.fr` → Appliquer le libellé `Factures/Carburant`
- [ ] Récupérer les **OAuth credentials Gmail** (à finaliser ensemble — il y a un setup Google Cloud à faire pour Gmail comme on l'a fait pour Sheets)
- [ ] Stocker `gmail_oauth.json` + `gmail_token.json` dans `credentials/` localement, et en GitHub Secret pour le cloud

### 4. Activer le mail (1 min)

Dans `.github/workflows/pricing.yml`, changer :
```yaml
MAIL_ACTIF: 'true'   # était 'false'
```
Push, et les prochains runs enverront effectivement les mails.

### 5. Passer à 3 runs/jour (1 min)

Dans `.github/workflows/pricing.yml`, modifier :
```yaml
on:
  schedule:
    - cron: '0 5,6,8,9,11,12 * * 1-6'  # au lieu de '0 6,7 * * 1-6'
```
Et :
```yaml
RUN_HEURES_PARIS: '7,10,13'  # au lieu de '8'
```

Le code filtrera côté Python pour exécuter une fois à 7h, 10h et 13h Paris (peu importe été/hiver).

### 6. Premier run en présence (10 min)

- [ ] Trigger manuel du workflow → vérifier que tout est vert
- [ ] Vérifier que le mail arrive bien dans `email_principal`
- [ ] Vérifier la cohérence : le mail doit afficher tes vrais prix d'achat HT (depuis le Sheet `Pricing live`) et la décision doit te paraître logique
- [ ] Si un mouvement détecté : lire la proposition, valider mentalement, appliquer en caisse + déclarer sur prix-carburants.gouv.fr

### 7. Surveiller la 1ère semaine

- [ ] Lire les 3 mails / jour la 1ère semaine pour vérifier la cohérence
- [ ] Ajuster les seuils dans `parametres_runs.yaml` si trop / trop peu de propositions :
  - `seuil_ecart_significatif_cts: 0.3` — augmenter pour moins de propositions
  - Marge cible et plancher dans `Paramètres` du Sheet

---

## 📚 Documents à consulter à la reprise

| Fichier | Quand le lire |
|---|---|
| `tasks/lessons.md` (à la racine du pack) | **Au début** — Claude le charge auto via SessionStart hook, te donne le contexte des 8 leçons cumulées |
| `README.md` | Vue d'ensemble du projet et commandes principales |
| `GUIDE_MIGRATION_CLOUD.md` | Si tu dois recréer un compte service Google (rare) |
| `GUIDE_GITHUB_ACTIONS.md` | Si tu veux comprendre comment marche l'auto-exécution |
| `docs/strategie_tarifaire.md` | Doctrine métier, à relire avant de modifier les seuils |
| `docs/proposition_automation.md` | Architecture technique |

---

## 🔧 Si tu changes de magasin un jour (autre que Les Pieux)

Voir le récap dans la session du 26/04 — résumé :
1. Nouveau Google Sheet (copier la structure)
2. Mettre à jour Secret `GSHEET_ID`
3. Mettre à jour les YAML (SIRET, emails, etc.)
4. Partager le nouveau Sheet avec le service account
5. Push et trigger un run de test

---

## 🆘 En cas de problème

| Symptôme | Cause probable | Fix |
|---|---|---|
| Run rouge sur GitHub Actions | Code error | Vérifier les logs de l'étape rouge |
| Pas de ligne dans Reco prix le matin | Cron skip (heure ≠ 8h) ou Sheet plein | Vérifier l'onglet Concurrents + log GitHub |
| Mail non reçu | `MAIL_ACTIF=false` ou connecteur Gmail KO | Vérifier l'env var workflow + connecteur Claude |
| Décision incohérente | Prix d'achat dans Sheet pas à jour | Mettre à jour `Pricing live` après chaque livraison (manuel ou via Gmail post-livraison) |
| Marge sous plancher reçue en INFO | Concurrent agressif | Décision manuelle : accepter la baisse temporaire ou passer à plancher 0.5 (opération nationale) |

---

## 📝 Évolutions V2 (post-stabilisation)

- Run dimanche 18h en mode info pure (préparation lundi)
- Lecture facture SIPLEC automatique → màj `Pricing live` du Sheet
- Tableau de bord marge pondérée hebdomadaire
- Si tu pilotes 2 magasins en parallèle (Vaucresson + Les Pieux) : matrix GitHub Actions

---

**Bonne reprise Benjamin. Le système est prêt à tourner pour toi.**
