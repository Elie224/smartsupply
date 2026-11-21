# Model card — Prévision de la demande à 7 jours (SmartSupply AI)

> ⚠️ **Données 100 % synthétiques.** Les performances décrites ci-dessous sont mesurées
> dans le monde simulé (Faker + moteur métier). Elles ne valent pas validation sur une entreprise réelle.

## 1. Usage prévu

| | |
|---|---|
| **Tâche** | Régression : unités **demandées** par produit pendant les 7 jours suivant une date D |
| **Grain** | 1 prévision = 1 produit × 1 date d'observation (tous entrepôts) |
| **Utilisation** | Anticiper la demande pour calculer un risque de rupture et une suggestion de réapprovisionnement |
| **Hors périmètre** | Prévision par entrepôt (version avancée), horizons ≠ 7 jours, produits sans 28 jours d'historique |

## 2. Données

- Historique simulé du **01/01/2024 à hier**, sur le **calendrier réel** (~35 mois en novembre 2026) :
  aucune donnée n'est jamais datée dans le futur. Chaque saison est vue au moins deux fois à l'entraînement.
- Source : `ml.feature_snapshots` (PostgreSQL), exporté en Parquet versionné (`ml.training_datasets`).
- Cible : `target_demand_next_7d` = somme de `requested_qty` sur D+1..D+7 (la **demande**, pas les ventes servies).
- Variables : historique de demande (lags 1/7/14/28 j, moyennes et écart-type mobiles), ruptures récentes,
  prix, promotions **annoncées** à D, calendrier, stock et réceptions **connues** à D, délai fournisseur.
- **Anti-fuite** : prouvé par test (`tests/unit/test_features.py`, `tests/integration/test_features_db.py`) :
  les variables à D sont identiques avec ou sans les données postérieures à D, y compris après un an d'historique.

### Découpage chronologique

| Ensemble | Dates d'observation | Rôle |
|---|---|---|
| Train | 28/01/2024 → 31/03/2026 | Apprentissage + validation glissante |
| *(séparation)* | avril 2026 | Évite que la cible J+1..J+7 du train déborde |
| Validation | 01/05 → 31/07/2026 | **Choix du modèle** |
| *(séparation)* | août 2026 | |
| Test | 01/09 → 13/11/2026 | **Évaluation finale, utilisée une seule fois** (dernière cible connue) |

## 3. Modèle retenu : LightGBM sur cible relative

- `LightGBMRatioModel` (`src/smartsupply/models/estimators.py`).
- Le modèle prédit le **ratio** `demande 7 j / (7 × moyenne 91 j)`, puis le remultiplie.
  - Ratio : les volumes varient d'un facteur ~50 entre produits, alors que saison et promotions ont un effet **relatif**.
  - Base 91 jours : estime le niveau du produit avec beaucoup moins de bruit que 28 jours.
  - Chaque ligne est pondérée par son volume de référence, pour rester aligné sur l'erreur en unités (WAPE).
- Variables dérivées : accélération (moy. 7 j / moy. 28 j), irrégularité, jours de stock, jours de réassort attendus.
- Objectif L1, learning rate 0.03, 1 000 arbres, 31 feuilles, graine 42.
- Le modèle final est réentraîné sur train + validation avant l'évaluation sur le test.

## 4. Performances

**WAPE** = Σ|erreur| / Σ réel ; **biais** = Σ(prévu − réel) / Σ réel (négatif = sous-estimation).

### Validation (mai–juillet 2026 — choix du modèle)

| Modèle | MAE | WAPE | Biais |
|---|---|---|---|
| **LightGBM (ratio, base 91 j)** | **12.50** | **12.6 %** | −1.4 % |
| Ridge (linéaire) | 14.20 | 14.3 % | −2.2 % |
| Naïve 28 j (7 × moy. 28 j) | 14.86 | 15.0 % | −1.6 % |
| Pondérée 7 j / 28 j | 15.07 | 15.2 % | −1.1 % |
| Naïve 7 j | 17.22 | 17.3 % | −0.6 % |
| *Oracle (plancher théorique)* | *10.83* | *10.9 %* | |

### Test final (sept.–nov. 2026, jamais utilisé avant)

| Modèle | MAE | WAPE | Biais |
|---|---|---|---|
| **LightGBM (ratio, base 91 j)** | **12.73** | **13.5 %** | **+0.7 %** |
| Pondérée 7 j / 28 j | 16.35 | 17.4 % | +2.7 % |
| Naïve 28 j | 17.03 | 18.1 % | +3.8 % |
| Naïve 7 j | 18.04 | 19.2 % | +1.5 % |
| *Oracle (plancher théorique)* | *10.99* | *11.7 %* | |

➡️ **−25 % d'erreur** par rapport à la meilleure méthode naïve sur le test, biais quasi nul.

### Le plancher : ce qu'aucun modèle ne peut faire mieux

La demande simulée contient du **hasard** (comme la vraie). Une prévision « oracle » qui connaîtrait
la **vraie moyenne** du simulateur fait encore 10.9 % (validation) et 11.7 % (test) de WAPE.

| | Validation | Test |
|---|---|---|
| Naïve 28 j | 15.0 % | 18.1 % |
| LightGBM | 12.6 % | 13.5 % |
| Oracle (plancher) | 10.9 % | 11.7 % |
| **Part de l'écart réductible comblée** | **≈ 59 %** | **≈ 70 %** |

### Ce qui a fait baisser l'erreur (historique des essais)

| Étape | WAPE validation |
|---|---|
| Naïve 28 j | ≈ 16 % |
| LightGBM sur quantité brute, 24 mois | 14.8 % |
| + cible relative (ratio) + variables dérivées | 14.3 % |
| + **~3 ans d'historique** + base 91 jours | **≈ 12.5 %** |

Essais **non retenus** (pas de gain en validation glissante) : demande de la même semaine l'an dernier,
indice saisonnier de catégorie, moyenne 364 j, identifiant produit comme variable, objectif L2 / Tweedie.
Ces variables restent dans le dataset pour de futurs essais.

### Stabilité (validation glissante, WAPE)

| Période évaluée | LightGBM | Naïve 28 j | Ridge |
|---|---|---|---|
| 26/11/2025 → 06/01/2026 | 15.1 % | 17.3 % | 15.5 % |
| 07/01 → 17/02/2026 | 13.2 % | 17.9 % | 15.8 % |
| 18/02 → 31/03/2026 | 13.6 % | 19.0 % | 17.2 % |

### Par segment (validation, WAPE LightGBM vs naïve 28 j)

| Segment | LightGBM | Naïve 28 j |
|---|---|---|
| Produits lents | 18.7 % | 21.4 % |
| Produits moyens | 14.5 % | 18.8 % |
| Produits rapides | 11.2 % | 12.9 % |
| **Promotion annoncée** | **14.6 %** | **24.0 %** |
| Sans promotion | 12.3 % | 13.8 % |
| Rupture récente | 12.0 % | 14.3 % |

Meilleur que la référence dans **toutes** les catégories et tous les segments.

## 5. Limites connues

- **Sous-estimation** pour la **Papeterie (−12.5 %)** et l'**Électroménager (−6.4 %)** en validation ;
  à l'inverse, légère surestimation pour l'Épicerie (+5.8 %). Pour le risque de rupture, prévoir un **stock de sécurité**.
- **Produits lents** : erreur relative plus forte (18.7 %), le hasard pèse plus sur de petits volumes.
- **Monde simulé** : les règles de demande sont connues et relativement simples.
- **Pas encore testé sous drift** : évalué en phase 9.
- Prévision tous entrepôts confondus ; la dimension entrepôt est prévue pour la version avancée.

## 6. Reproduire

```powershell
python -m smartsupply.generator.bootstrap               # historique du 01/01/2024 à hier
python -m smartsupply.features.build --export           # dataset versionné
python -m smartsupply.models.train                      # compare les modèles sur la validation
python -m smartsupply.models.train --final-test         # + test final (une seule fois)
```

Résultats dans `data/models/<run_id>/` : `model.joblib`, `report.json`, `feature_importance.csv`.
Seed de simulation 42 ; seed LightGBM 42.

## Changement du 21/11/2026 : réapprovisionnement simulé

L'acheteur simulé commandait sur la demande « catalogue » de janvier 2024, figée.
Les produits en croissance finissaient en rupture chronique : taux de service
94 % (2024) -> 87 % (2026), et 79 % pour les produits à plus de +25 %.
Il estime désormais la demande sur les 28 derniers jours observés (comme un ERP).
Résultat, à demande identique (simulation déterministe) : taux de service 96,1 %,
stable d'une année à l'autre et quel que soit le profil de croissance, sans hausse
du stock moyen. Le modèle de prévision n'est pas affecté (test : 13,6 % -> 13,5 %),
car il prévoit la demande, pas les ventes.

## Changement du 21/11/2026 : cycle de vie des clients

- Arrivées : ~0,2 nouveau client Faker par jour (~70 par an), qui démarre à 30 %
  de son rythme d'achat et l'atteint en 60 jours.
- Départs (cachés) : durée de vie exponentielle, ~3 % par an (B2B), 4 % (particulier
  régulier), 7 % (occasionnel). Un client parti ne commande plus ; aucun indicateur
  n'est stocké.
- Règle métier : un client est PERDU après 12 mois sans commande
  (analytics.v_clients, analytics.v_clients_monthly). Observé : ~48 pertes par an
  (~4,5 %), base active sur 12 mois 1 068 -> 1 090 clients.
- La demande suit la base de clients actifs (facteur par entrepôt et catégorie).

Résultats après régénération (test, sept. -> nov. 2026) :

| Modèle | WAPE test | Biais |
|---|---|---|
| **LightGBM** | **12.3 %** | **-0.1 %** |
| Pondérée 7 j / 28 j | 18.2 % | +3.0 % |
| Naïve 28 j | 19.2 % | +4.0 % |

Le modèle s'adapte sans nouvelle variable : il suit le niveau récent de la demande,
qui intègre déjà l'effet des départs et des arrivées.
