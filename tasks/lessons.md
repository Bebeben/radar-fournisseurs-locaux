# Leçons — Produits locaux

Fichier mis à jour automatiquement après chaque correction de Benjamin.
Format : [YYYY-MM-DD] | ce qui s'est mal passé | règle à suivre la prochaine fois

---

<!-- Entrées ajoutées ici, les plus récentes en bas -->
[2026-05-27] | oublié le label "© du Centre" (marque régionale Centre-Val de Loire) dans la liste des labels à scraper, alors qu'il était apparu deux fois dans mes propres résultats de recherche (Croquets de Charost, huile L'originale via cducentre.com) | quand un domaine ou une marque apparaît plusieurs fois dans les résultats de recherche pendant une session, le repérer comme signal et le considérer systématiquement pour les listes de labels/sources, surtout pour les marques régionales (© du Centre, Berry Province, Saveurs du Limousin, Marque Parc, etc.)
[2026-05-27] | laissé un import `from streamlit_folium import st_folium` dans app.py marqué "noqa F401" alors que la lib n'était pas dans requirements.txt — crash ModuleNotFoundError au lancement | ne JAMAIS laisser d'import non utilisé "au cas où" ; si une lib n'est pas dans requirements.txt ET pas utilisée dans le code, virer l'import sec. Vérifier mentalement chaque ligne `import` avant Write d'un fichier Python : (1) la lib est-elle dans requirements ? (2) est-elle réellement utilisée plus bas ?
[2026-05-27] | construit une clé de cache concaténant TOUS les codes NAF + départements → nom de fichier de 300+ caractères, dépasse MAX_PATH Windows (260) → FileNotFoundError au write | Benjamin est sur Windows. Toujours hasher/tronquer les noms de fichiers générés dynamiquement quand ils dépendent d'une liste : préfixe lisible (≤40 char) + hash court (md5[:12]). Plafond NAME_MAX = 80 caractères par sécurité.
