# État actuel du système pricing carburants Les Pieux

**Dernière mise à jour : 2026-05-07**

Document mis à jour à chaque session pour que toute reprise (future Claude session, retour après vacances, transmission) ait le contexte exact.

---

## 🟢 Système opérationnel

### Architecture
- **Repo GitHub privé** : https://github.com/Bebeben/pricing-carburants-les-pieux
- **Google Sheet** : `Pricing carburants Les Pieux` (ID `1OBfPaZowMi2eq1VnswmxjSjLkBtutHWFjdvTLomndAw`)
- **Service account** : `pricing-bot@pricing-les-pieux.iam.gserviceaccount.com` (Éditeur du Sheet + dossier Backups)
- **Telegram bot** : @pricing_lespieux_bot (créé via @BotFather)
- **Cron-job.org** : 8 cronjobs actifs (cf. tableau ci-dessous)

### Workflows GitHub Actions actifs

| Workflow | Trigger | Cron-job.org | Heure Paris | Action |
|---|---|---|---|---|
| Pricing carburants Les Pieux | workflow_dispatch | ✅ oui | Lun-Sam 8h | Run pricing complet + notif texte + visuel PNG si ACTION |
| **Pricing check (changement concurrent)** | workflow_dispatch | ✅ oui | Lun-Sam 10h/12h/14h/16h | Run léger : append Concurrents/Reco prix toujours, notif Telegram + visuel uniquement si ACTION ou changement détecté |
| Rappel ACTION pending | workflow_dispatch | ✅ oui | Lun-Sam 11h25 | Si reco du matin = ACTION non traitée |
| Question achat carburants | workflow_dispatch | ✅ oui | Lun-Sam 9h30 | "T'as commandé ?" boutons OUI/NON/PAUSE BOT |
| Lecture reponse achat | workflow_dispatch | ✅ oui | Lun-Sam 12h30 | Batch lecture messages 7h-12h30, maj Pricing live si OUI (fenêtre réponse depuis 9h) |
| Recap hebdo carburants | workflow_dispatch | ✅ oui | Lundi 7h | Synthèse semaine N-1 |
| Polling Telegram | workflow_dispatch | ✅ oui | Lun-Sam 8h-13h toutes les 15 min | Lit toutes commandes (pause/reprise/prix achat/accept/aide) avec offset last_update_id |
| Toggle bot | workflow_dispatch (manuel) | ❌ non | À la demande | Pause/Reprise via app GitHub mobile |

### Workflows désactivés
- `backup.yml` : abandonné (quota service account = 0). Backup natif Google Sheet utilisé à la place (Fichier > Historique des versions).

---

## 📊 Onglets Google Sheet

| Onglet | Rôle | Modifié par |
|---|---|---|
| **Dashboard** | KPIs auto + 2 graphiques (marge pondérée + prix Intermarché 30j) | Auto (formules) |
| **Stations** | Liste des stations à surveiller | Benjamin (manuel) |
| **Concurrents** | Prix relevés à chaque run pricing (newest-on-top) | Auto |
| **Reco prix** | Recommandations + col Suivi (newest-on-top) | Auto |
| **Prix d'achat** | Saisie libre prix fournisseur via Telegram (newest-on-top) | Auto via Telegram |
| **Pricing live** | État courant prix achat HT + vente TTC | Auto à OUI commande, manuel sinon |
| **Commande** | Historique commandes effectives | Auto |
| **Transport** | Coûts transport (template original) | Manuel |
| **Parametres** | Cibles, planchers, mix, seuils, master flag | Benjamin (manuel) |
| **Recap hebdo** | Archives récaps lundi | Auto |
| **_DataIM** (caché) | Vue filtrée Intermarché pour graphiques | Auto (formule QUERY) |

### Stations actuelles
| Type | Nom | ID API | Alignement actif | Notes |
|---|---|---|---|---|
| Reference | Super U Les Pieux | 50340002 | NON | Notre station |
| Concurrent | Intermarché Les Pieux | 50340003 | OUI (tous carburants) | Concurrent direct, alignement complet |
| Concurrent | Super U Bricquebec | 50260001 | Gazole,E10 | Garde-fou image prix sur ces 2 carburants seulement |

### Paramètres clés (Parametres tab)
- L7 : TVA = 20%
- L10 : Marge cible = 2,0 cts/L (par carburant)
- L11 : Marge plancher = 1,0 cts/L (par carburant)
- L12 : Marge pondérée cible = 3,5%
- L13 : Seuil tolérance écart prix = 0,001 €
- L16 : Délai livraison = 1 jour
- L17 : Livraison samedi possible = NON
- L18 : Heure question achat = 11:30
- L19 : Heure bascule prix J/J+1 = 11:00
- **L19 cols C-F : Mix de vente = E85 5% / E10 60% / SP98 10% / Gazole 25%**
- L20 : **Bot actif** = OUI ⬅ master flag
- L25 : **Telegram last update ID** ⬅ curseur polling, ne pas toucher

### Carburants trackés (mai 2026)
**Ordre d'importance volume** : Gazole > SP95-E10 > SP98 > E85
- ⚠️ SP95 pur n'est PLUS tracké (volume négligeable). Remplacé par E85.
- L'historique Concurrents/Reco prix avant 2026-05-07 contenait du SP95 réel
  dans la colonne renommée "E85" → ancien historique étiqueté E85 mais c'était
  du vrai SP95. À garder en tête pour les analyses long terme.

---

## 🤖 Comportement runtime

### Run pricing 8h (workflow `pricing.yml`)
1. Lit Stations, appelle API gouv pour les concurrents
2. Écrit 1 ligne par concurrent dans Concurrents
3. Calcule décision (cascade pondérée 4 niveaux + tolérance + multi-carb + filtre par carburant)
4. Écrit 1 ligne dans Reco prix (col Statut = ACTION/INFO/STATU QUO, col Niveau cascade)
5. **Notif Telegram texte envoyée à chaque run** (avec adaptation selon statut)
6. **Visuel PNG envoyé via Telegram sendPhoto SI ACTION**

### Pricing check 10h/12h/14h/16h (workflow `pricing_check.yml`)
- Identique au run 8h sauf que la **notif Telegram (texte + visuel) n'est envoyée
  QUE si statut ACTION OU si un changement chez un concurrent est détecté** vs la
  ligne précédente dans Concurrents.
- Append Concurrents/Reco prix toujours fait (traçabilité de tous les relevés).
- Permet de capter les mouvements Inter dans la journée sans spammer Benjamin.

### Polling Telegram 15 min (workflow `polling_telegram.yml`)
- Lit `Parametres!C25` = last_update_id stocké
- getUpdates avec offset = last_update_id + 1 → ne traite que les nouveaux messages
- Aucun message raté, peu importe la fréquence
- Commandes reconnues :
  - `aide`/`?` → liste commandes (BYPASS master flag)
  - `pause`/`reprise` → toggle Bot actif (BYPASS)
  - `prix GAZ=... E10=... SP98=... E85=...` → append Prix d'achat HT + ack
  - `accepter`/`ok` → applique prix proposés du dernier ACTION → maj Pricing live
  - `prix vente E85=...` → override partiel prix de vente

### Logique alignement
- Intermarché Les Pieux = alignement complet sur tous carburants (concurrent principal)
- Super U Bricquebec = alignement partiel sur Gazole + E10 (garde-fou image)
- Si écart `nous − concurrent ≤ 0,001 €` → INFO (tolérance)
- Si écart > 0,001 € → ACTION alignement (avec cascade pondérée)
- 4 niveaux cascade : 1 (cible OK) / 2 (pondérée décroche) / 3 (sous cible cts/L) / 4 (sous plancher = INFO URGENT)

### Flow achat quotidien
1. **À tout moment** : Benjamin envoie `prix GAZ=... E10=... SP98=... E85=...` → ligne dans Prix d'achat (balise J ou J+1 selon heure bascule). Ack Telegram envoyé dans les 15 min par polling.
2. **11h25** : Si reco du matin = ACTION non traitée, rappel Telegram
3. **11h30** : Question Telegram avec 3 boutons (OUI / NON / PAUSE BOT)
4. **12h00** : Lecture batch, si OUI prend les derniers prix "J" pour maj Pricing live + Commande

### Pause/Reprise
- Master flag `Bot actif` en Parametres!C20 (OUI/NON dropdown)
- 3 façons de basculer :
  1. **Sheet direct** : ouvrir Parametres → C20 → choisir OUI/NON (effet immédiat)
  2. **Telegram** : taper `pause` ou `reprise` (lu dans les 5 min entre 8h-13h)
  3. **Workflow GitHub** : Actions → Toggle bot → Pause/Reprise (depuis app GitHub mobile)

---

## 🔧 À faire post-cession Les Pieux

Cf. CHECKLIST_DEPLOIEMENT.md sections "Phase 2-4" :
- Remplir les `A_COMPLETER_PRISE_FONCTION` (SIRET, login gérant, email pro, etc.)
- Activer Gmail connector
- Passer `MAIL_ACTIF=true` dans pricing.yml
- Optionnel : passer à 3 runs/jour (7h/10h/13h)

---

## 📚 Leçons accumulées

Voir `tasks/lessons.md` (à la racine du pack `Station service/`). 19 leçons cumulées. Le hook SessionStart les recharge à chaque nouvelle session Claude Code.

---

## 🆘 Contacts API en cas de problème

- **API prix-carburants gouv** : `data.economie.gouv.fr` (gratuit, sans auth)
- **Google Sheets API** : auth via service account JSON (dans `credentials/`, gitignored)
- **Telegram bot** : auth via `TELEGRAM_BOT_TOKEN` (GitHub Secret)
- **GitHub API** (workflow_dispatch) : auth via PAT classic scope `repo` (utilisé par cron-job.org)

Tous les secrets sont stockés dans GitHub Secrets, jamais dans le code.
