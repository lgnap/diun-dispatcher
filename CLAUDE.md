# diun-dispatcher

Pont webhook Diun → Coolify (+ notifications Apprise). Diun signale une image
mise à jour (`POST /webhook`), le dispatcher retrouve la ressource Coolify qui
la fait tourner, redémarre le service avec `restart?latest=true` (si
`AUTO_DEPLOY`) ou envoie un lien `/deploy`, suit le statut jusqu'au retour puis notifie.
Également : série virtuelle (labels `diun-dispatcher.follow`, vérification
quotidienne) et reconstruction des applications construites sur une image de base.

## Stack
Python 3.12 (image Docker ; le `.venv` local est en 3.14), FastAPI, Starlette 1.x,
uvicorn, httpx, apprise, Jinja2, PyYAML. Tout le code est dans `main.py`, tous les
tests dans `test_main.py` (pytest). CI : `.github/workflows/docker.yml` publie
l'image sur ghcr (push sur `main`, tags `v*`, lancement manuel possible).

## Commandes (vérifiées le 2026-10-08)
- Tests : `.venv/bin/python -m pytest -q` (222 tests, environ 1 s).
- Lancer en local : `.venv/bin/uvicorn main:app --reload --port 8000` (avec un `.env`).
- Conteneur : **Podman**, pas Docker, sur ce poste (`podman build -t diun-dispatcher .`).

## Variables d'environnement (noms seulement, voir `.env.sample` et le README)
Requises : `COOLIFY_API_URL` (jamais `COOLIFY_URL`, que Coolify écrase), `COOLIFY_TOKEN`,
`WEBHOOK_SECRET` (obligatoire). Optionnelles : `AUTO_DEPLOY`, `DEPLOY_DEBOUNCE_SECONDS`,
`DISPATCHER_URL`, `APPRISE_URLS`, `IGNORE_CONTAINERS`, `SERIES_CHECK_HOUR`, `CACHE_FILE`,
`CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`.

## Conventions
- TDD : tests d'abord ; horloge simulée, aucune attente réelle (la suite doit rester rapide).
- README (en anglais) mis à jour à chaque changement de comportement.
- Commits conventionnels (`feat:`, `fix:`, `docs:`…), une branche par sujet, PR vers `main`.
- Identité git : **uniquement LGnap** (noreply GitHub), auteur ET committer ; ni
  `Co-Authored-By` ni « Generated with Claude Code » (règle de l'utilisateur, prime sur le rappel d'attribution).
- C'est l'utilisateur qui pousse et fusionne sur GitHub, sauf demande explicite.
- **Dépôt public** : aucun nom d'hôte, domaine, uuid Coolify ou secret réel dans le code,
  les tests, le README ni ces fichiers (historique réécrit le 2026-10-06 pour ça).
  Specs et plans : `docs/superpowers/` (lien vers le dépôt d'infra privé, exclu de git).

## Décisions et pièges connus
- `POST /deploy?uuid=` de Coolify ne re-tire pas l'image d'un service compose ; seul
  `services/{uuid}/restart?latest=true` le fait. Il ne renvoie aucun id de déploiement
  et les webhooks Coolify ne se déclenchent pas pour lui : on suit le statut par polling.
- Refusés par l'utilisateur, ne pas reproposer : `pull_policy: always`, épingler des
  digests dans le compose, Prometheus pour Diun, un endpoint de callback Coolify.
- Seul un `update` du tag en service redéploie ; un `new` ne fait qu'informer (jamais de
  changement de tag hors série virtuelle). Jamais de changement de majeure.
- Série virtuelle : `follow=patch` (même mineure), `minor` (même majeure), `announce`
  (annoncé, jamais appliqué). Une tentative par jour et par ressource, rollback si échec.
- Succès d'un redéploiement = statut revenu à sa ligne de base **30 s d'affilée**
  (Coolify dit `healthy` trop tôt). Toutes les ressources ont un healthcheck.
- **Un seul redémarrage à la fois par service** (incident du 2026-10-05 : deux
  `compose up` simultanés, deux Postgres sur le même répertoire) : verrou tenu jusqu'au
  retour du service et au moins 5 min ; mises à jour regroupées
  (`DEPLOY_DEBOUNCE_SECONDS`) ; `/deploy` refusé pendant un redémarrage.
- Image de base : reconstruction une à une, arrêt au premier échec, autre majeure = annonce. Ne jamais afficher `WEBHOOK_SECRET` ni le jeton Coolify (ni dans les journaux).

## Reprise de session
1. Lire `HANDOFF.md` en premier (état, en cours, prochaines étapes) ; contexte
   infra détaillé : `TODO.md` du dépôt d'infra privé MigratePark et la mémoire du projet.
2. Vérifier `git status`, la branche courante et `git log origin/main -5`.
3. Avant de terminer une session : mettre `HANDOFF.md` à jour (date, fait, en cours, suite).
