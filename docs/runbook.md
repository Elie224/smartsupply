# Runbook — exploiter SmartSupply au quotidien

Commandes PowerShell, depuis le dossier du projet. `$py` = le Python de l'environnement du projet :

```powershell
$py = "C:\Users\KOURO\anaconda3\envs\smartsupply\python.exe"   # à mettre dans $PROFILE
[Console]::OutputEncoding = [Text.Encoding]::UTF8               # accents corrects dans la console
```

## 1. Gestes courants

| Je veux… | Commande |
|---|---|
| Tout vérifier | `& $py -m smartsupply.verify` |
| Savoir si le cycle a tourné | `& $py -m smartsupply.health` |
| Démarrer / arrêter tout | `docker compose up -d` / `docker compose stop` |
| Voir l'état des conteneurs | `docker compose ps` |
| Lancer un passage du cycle tout de suite | `docker compose restart pipeline` (rattrapage au démarrage) |
| Voir les derniers passages | `docker compose logs --tail 50 pipeline` ou Grafana « Cycle automatique » |
| Recopier dans MLflow à la main | `& $py -m smartsupply.models.tracking` |
| Recalculer la surveillance | `& $py -m smartsupply.models.monitoring` |
| Réentraîner + prévoir à la main | `& $py -m smartsupply.models.nightly` |
| Tester l'envoi d'e-mail | `docker compose exec alertmanager amtool alert add TestSmartSupply severity=info --annotation=summary=TestEnvoi --alertmanager.url=http://localhost:9093` |
| Régénérer les tableaux Grafana | `& $py scripts/build_grafana_dashboards.py` (Grafana les relit en 1 min) |
| Lancer les tests | `& $py -m pytest -q` |
| Voir les modèles retenus (lequel est en service) | `& $py -m smartsupply.models.rollback` |
| Contrôler le modèle en service (garde-fou, comme la nuit) | `& $py -m smartsupply.models.guard` |
| **Démontrer** le retour arrière automatique (3 pannes simulées, base remise en l'état ; pas pendant le cycle de 2h30) | `& $py -m smartsupply.models.demo_rollback` → rapport `docs/demo_retour_arriere.md` |
| **Retour arrière** : remettre le modèle précédent | `& $py -m smartsupply.models.rollback --precedent --raison "pourquoi"` (ou `--vers RUN_ID`) ; annuler : `--annuler RUN_ID` |
| Publier le site public tout de suite | `& $py -m smartsupply.publish` (`--force` : même sans changement) |

Après une modification du code : `docker compose build pipeline` puis `docker compose up -d` (le pipeline et l'API partagent l'image). Après une modification de `pyproject.toml`, la reconstruction retélécharge les dépendances (plusieurs minutes).

Après une modification de `database/*.sql` : relancer le fichier concerné, par exemple
`docker compose exec postgres psql -U smartsupply -d smartsupply -f /database/analytics_views.sql`.

## 2. Le matin

1. Si une fenêtre d'alerte Windows ou un e-mail est arrivé : lire la consigne qu'il contient.
2. Power BI : **Actualiser** (la carte « État du cycle » montre l'état à la dernière actualisation).
3. Facultatif : `verify`, Grafana « Modèle », MLflow (nouvel entraînement de la nuit, retenu ou refusé).

## 3. Incidents connus et corrections

| Symptôme | Cause | Correction |
|---|---|---|
| `connection refused` / `connexion impossible` sur le port 15432 après une **mise en veille** | Le relais réseau de Docker Desktop se bloque après la veille | `docker compose up -d --force-recreate postgres` |
| `InvalidOperation : L'expression après « & »…` | `$py` n'existe pas dans ce nouveau terminal | Redéfinir `$py` (en haut de cette page) ou l'ajouter au profil PowerShell |
| `OpenBLAS error: Memory allocation still failed` ; Grafana « cannot allocate memory » | Mémoire saturée (Docker limité à ~8 Go, d'autres projets lancés) | Arrêter les projets inutilisés (`docker stop …`) ; `$env:OPENBLAS_NUM_THREADS="1"` ; ou donner plus de mémoire à Docker (`%USERPROFILE%\.wslconfig` : `[wsl2]` `memory=12GB`, puis `wsl --shutdown`) |
| `Bind for 0.0.0.0:XXXX failed: port is already allocated` | Un autre projet utilise déjà ce port (ex. 13000 par un autre Grafana, 4200 par un autre Prefect) | Changer le port dans `.env` (`GRAFANA_PORT`, `PREFECT_PORT`, `API_PORT`…) puis `docker compose up -d` |
| Un fichier modifié revient à son ancienne version | Il était ouvert dans VS Code, qui l'a réenregistré | Fermer le fichier dans VS Code **sans enregistrer** avant de le remplacer |
| `verify` : « Prefect … aucun run planifié » juste après un redémarrage | Le pipeline fait d'abord son passage de rattrapage, puis remet la planification | Attendre 1 à 2 minutes et relancer `verify` |
| `verify` : « Prometheus … cibles injoignables » juste après un redémarrage | Prometheus n'a pas encore relevé ses cibles | Attendre 1 minute et relancer |
| Étape MLflow très lente (minutes) au redémarrage | MLflow démarre en même temps que le pipeline | Passager ; vérifier `smartsupply_pipeline_step_duration_seconds{step="mlflow"}` au passage suivant |
| `verify` : MLflow « x/y entraînements recopiés » | MLflow était arrêté pendant un passage | `& $py -m smartsupply.models.tracking` (ou attendre le passage suivant) |
| API : « sert le modèle depuis la base » | MLflow injoignable | `docker compose up -d mlflow` ; l'API repasse sur MLflow en 5 minutes |
| Alertmanager : « la messagerie refuse le compte » | Mot de passe d'application Gmail faux ou révoqué | Recréer un mot de passe d'application, mettre à jour `SMTP_PASSWORD` dans `.env`, `docker compose up -d alertmanager` |
| E-mail d'alerte non reçu | Dossier spam, ou conteneur sans accès Internet | Vérifier les spams ; `docker compose logs --tail 20 alertmanager` |
| Grafana : carte « No data » | Champ texte (ou requête vide) | Vérifier dans Grafana (Éditer > Requête) ; pour un texte, la carte doit afficher « tous les champs » |
| Contrôles qualité en échec (le cycle s'arrête) | Incohérence dans les données simulées | `docker compose logs pipeline` ; le message nomme le contrôle ; corriger avant de relancer |

## 4. Repartir de zéro (développement uniquement)

Efface toute l'activité simulée, les features, les modèles, les prévisions et le journal (le référentiel est conservé) :

```powershell
docker compose stop pipeline api
docker compose exec postgres psql -U smartsupply -d smartsupply -f /database/reset_simulation.sql
& $py -m smartsupply.generator.bootstrap        # ≈ 1 h
& $py -m smartsupply.features.build
& $py -m smartsupply.models.nightly
docker compose up -d
```

Les runs déjà présents dans MLflow ne sont pas effacés : supprimer l'expérience `smartsupply-demande-7j` et le modèle enregistré dans l'interface MLflow pour repartir propre.

## 4 bis. Site public

| Message de l'étape « publication » | Cause et correction |
|---|---|
| `non configurée` | `GITHUB_REPO` / `GITHUB_TOKEN` absents de `.env` : normal si on ne veut pas de site. Après les avoir ajoutés : `docker compose up -d pipeline` |
| `échec GitHub 401` | Jeton expiré ou faux : en créer un nouveau, le remplacer dans `.env`, `docker compose up -d pipeline` |
| `échec GitHub 403 / 404` | Le jeton n'a pas accès à ce dépôt, ou pas le droit *Contents : Read and write* |
| `échec GitHub 409` | Dépôt vide : pousser d'abord la branche `main` |
| Le site montre des données d'hier | Normal avant 2 h 30. Sinon : le cycle a-t-il tourné (`smartsupply.health`) ? Les fichiers sont mis en cache 30 min par le site |
| « Simulation indisponible » sur le site | Versions de LightGBM / scikit-learn différentes entre l'image et le site : comparer `etat.json → versions` à `app/requirements.txt` |

## 4 ter. Grafana Cloud (tableau public)

- Vérifier l'envoi : `docker compose logs prometheus | Select-String "Grafana Cloud"` (doit dire « envoi activé »), puis dans Grafana Cloud > Explore : `smartsupply_model_wape`.
- `401` / `403` dans `docker compose logs prometheus` : jeton faux ou sans `metrics:write` → nouveau jeton dans `.env`, puis `docker compose up -d --force-recreate prometheus`.
- Après une modification du tableau public : `& $py scripts/build_grafana_dashboards.py`, puis réimporter `docker/grafana/cloud/smartsupply-public.json` dans Grafana Cloud (même uid : il remplace l'ancien, le lien public reste le même).

## 4 quater. MLflow public (DagsHub)

- Synchroniser à la main : `& $py -m smartsupply.models.tracking` (affiche le local puis DagsHub).
- `DagsHub : échec (... 401 ...)` : jeton faux ou expiré → nouveau jeton (DagsHub > Your Settings > Tokens) dans `.env`, puis `docker compose up -d --force-recreate pipeline`.
- Une panne de DagsHub n'arrête ni le cycle ni le MLflow local : le retard est rattrapé au passage suivant.

## 4 sexies. Retour arrière automatique (garde-fou)

| Alerte / message | Ce qui s'est passé | Que faire |
|---|---|---|
| `RetourArriereAutomatique` | Le modèle en service avait un problème : il a été retiré, le précédent a repris sa place, la prévision du jour a été refaite | Rien d'urgent. Voir la raison : `& $py -m smartsupply.models.rollback`. Le modèle retiré ne revient jamais tout seul |
| `CycleEnEchec` avec « aucun modèle de secours valable » | Le modèle en service ET les modèles précédents ont échoué aux contrôles : pas de prévision aujourd'hui | `& $py -m smartsupply.models.guard` pour le détail ; regarder les données du jour (`smartsupply.verify`) ; un nouvel entraînement la nuit suivante peut régler le problème |

## 4 quinquies. Sauvegardes de la base

Le service `backup` sauvegarde les bases `smartsupply` et `mlflow` **chaque semaine** dans le dossier `backups` du projet (8 gardées par base, soit 2 mois). Chaque fichier est relu avant d'être gardé. Alertes : `SauvegardeAncienne` (plus de 8 jours), `SauvegardeAbsente` ; contrôle : `smartsupply.verify`.

| Je veux… | Commande |
|---|---|
| Voir les sauvegardes | `Get-ChildItem backups` |
| Sauvegarder tout de suite | `docker compose exec backup sh /scripts/backup.sh --maintenant` |
| Voir ce que fait le service | `docker compose logs --tail 20 backup` |
| **Tester** une sauvegarde (sans rien toucher) | `docker compose exec backup createdb smartsupply_test` puis `docker compose exec backup pg_restore --no-owner -d smartsupply_test /backups/<fichier>.dump` ; comparer, puis `docker compose exec backup dropdb smartsupply_test` |
| **Restaurer** (remplace les données actuelles) | `docker compose stop pipeline api grafana` → `docker compose exec backup pg_restore --clean --if-exists -d smartsupply /backups/<fichier>.dump` → `docker compose up -d` |

Les sauvegardes sont sur le même disque que la base : pour une vraie protection, copier de temps en temps le dossier `backups` ailleurs (clé USB, OneDrive...). Les rôles en lecture seule (Power BI, Grafana) ne sont pas dans les sauvegardes : après une réinstallation complète, relancer `database/roles.sql` et `database/roles_grafana.sql`.

## 5. Non encore outillé

- **Retour arrière du champion** : le modèle en service est le dernier retenu dans `ml.model_runs` ; il n'existe pas encore de commande pour revenir à un modèle précédent.
- **Sauvegarde de la base** : pas de sauvegarde automatique. Ponctuellement :
  `docker compose exec postgres pg_dump -U smartsupply -d smartsupply -Fc -f /tmp/smartsupply.dump` puis `docker compose cp postgres:/tmp/smartsupply.dump .`
