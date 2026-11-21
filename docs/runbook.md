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

## 5. Non encore outillé

- **Retour arrière du champion** : le modèle en service est le dernier retenu dans `ml.model_runs` ; il n'existe pas encore de commande pour revenir à un modèle précédent.
- **Sauvegarde de la base** : pas de sauvegarde automatique. Ponctuellement :
  `docker compose exec postgres pg_dump -U smartsupply -d smartsupply -Fc -f /tmp/smartsupply.dump` puis `docker compose cp postgres:/tmp/smartsupply.dump .`
