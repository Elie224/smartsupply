# Démonstration : retour arrière automatique du modèle

*Générée le 22/11/2026 à 12:42 par `python -m smartsupply.models.demo_rollback`, sur la vraie base (données au 21/11/2026).*

Chaque nuit, avant de prévoir, le garde-fou (`smartsupply.models.guard`) contrôle le modèle en service. Ici on **sabote volontairement** le modèle, on le laisse **publier sa prévision** (le pire cas), puis on lance **le vrai garde-fou**, le même code que la nuit.

Le modèle saboté annonce les mêmes scores que le bon modèle : la panne arrive après l'évaluation, elle est invisible pour la règle de mise en service. Seul le garde-fou peut la voir.

## Résultat

| Panne | Contrôle qui l'attrape | Détecté | Retiré, précédent remis | Prévision rétablie | Durée du contrôle |
|---|---|---|---|---|---|
| Bug d'unité : toutes les prévisions multipliées par 4 | demande totale comparée à la méthode naïve | oui | oui → 20261122T013019Z | identique à l’origine | 1.1 s |
| Dérive discrète : prévisions gonflées de 60 %, plausibles à l'œil | erreur sur les 28 derniers jours connus | oui | oui → 20261122T013019Z | identique à l’origine | 1.1 s |
| Bug de signe : un produit sur cinq prévu en négatif | valeurs négatives | oui | oui → 20261122T013019Z | identique à l’origine | 0.6 s |

Modèle en service pendant toute la démonstration, en dehors des pannes : `20261122T013019Z`. Après chaque panne, la base est remise exactement dans son état d'origine.

## Panne « x4 » — Bug d'unité : toutes les prévisions multipliées par 4

Déroulé :

- `  0.42 s` modèle saboté DEMO_X4 mis en service à la place de 20261122T013019Z
- `  1.13 s` prévision du 21/11 par le modèle saboté : publiée : 50 produits, 18620 unités sur 7 jours
- `  2.27 s` garde-fou : retour arrière automatique : DEMO_X4 -> 20261122T013019Z — demande totale prévue à 3.74 x la méthode naïve
- `  2.51 s` service rétabli : modèle 20261122T013019Z en service, prévision du 21/11 refaite par le garde-fou (4655 unités)
- `  2.54 s` base remise exactement dans son état d'origine

Raison enregistrée dans la base : **automatique : demande totale prévue à 3.74 x la méthode naïve**

Demande prévue sur 7 jours :

| | Avant la panne | Modèle saboté | Après le garde-fou |
|---|---:|---:|---:|
| **Total (50 produits)** | 4 655 | 18 620 | 4 655 |
| SKU-00003 Boissons Non #3 | 347 | 1389 | 347 |
| SKU-00037 Boissons Entretenir #37 | 338 | 1351 | 338 |
| SKU-00018 Boissons Paupière #18 | 278 | 1111 | 278 |
| SKU-00046 Papeterie Lettre #46 | 212 | 849 | 212 |
| SKU-00011 Bricolage Marchand #11 | 200 | 800 | 200 |

Modèles retenus juste après le garde-fou :

```
Modèle             Entraîné le       Données au    WAPE  Naïve  État
DEMO_X4            22/11/2026 11:41 2099-12-31   12.5%  19.1%  retiré le 22/11 11:41 (automatique : demande totale prévue à 3.74 x la méthode naïve)
20261122T013019Z   22/11/2026 01:30 2026-11-14   12.5%  19.1%  EN SERVICE
20261121T125301Z   21/11/2026 12:53 2026-11-13   12.4%  19.1%  ancien
```

## Panne « subtile » — Dérive discrète : prévisions gonflées de 60 %, plausibles à l'œil

Déroulé :

- `  0.45 s` modèle saboté DEMO_SUBTILE mis en service à la place de 20261122T013019Z
- `  1.21 s` prévision du 21/11 par le modèle saboté : publiée : 50 produits, 7447 unités sur 7 jours
- `  2.30 s` garde-fou : retour arrière automatique : DEMO_SUBTILE -> 20261122T013019Z — moins bon que la méthode naïve sur les 28 derniers jours (59.4% > 19.2%)
- `  2.60 s` service rétabli : modèle 20261122T013019Z en service, prévision du 21/11 refaite par le garde-fou (4655 unités)
- `  2.62 s` base remise exactement dans son état d'origine

Raison enregistrée dans la base : **automatique : moins bon que la méthode naïve sur les 28 derniers jours (59.4% > 19.2%)**

Demande prévue sur 7 jours :

| | Avant la panne | Modèle saboté | Après le garde-fou |
|---|---:|---:|---:|
| **Total (50 produits)** | 4 655 | 7 447 | 4 655 |
| SKU-00003 Boissons Non #3 | 347 | 556 | 347 |
| SKU-00037 Boissons Entretenir #37 | 338 | 540 | 338 |
| SKU-00018 Boissons Paupière #18 | 278 | 444 | 278 |
| SKU-00046 Papeterie Lettre #46 | 212 | 340 | 212 |
| SKU-00011 Bricolage Marchand #11 | 200 | 320 | 200 |

Modèles retenus juste après le garde-fou :

```
Modèle             Entraîné le       Données au    WAPE  Naïve  État
DEMO_SUBTILE       22/11/2026 11:41 2099-12-31   12.5%  19.1%  retiré le 22/11 11:41 (automatique : moins bon que la méthode naïve sur les 28 derniers jours (59.4% > 19.2%))
20261122T013019Z   22/11/2026 01:30 2026-11-14   12.5%  19.1%  EN SERVICE
20261121T125301Z   21/11/2026 12:53 2026-11-13   12.4%  19.1%  ancien
```

## Panne « negative » — Bug de signe : un produit sur cinq prévu en négatif

Déroulé :

- `  0.33 s` modèle saboté DEMO_NEGATIVE mis en service à la place de 20261122T013019Z
- `  1.14 s` prévision du 21/11 par le modèle saboté : refusée par la base (quantité négative interdite) : aucune prévision ce jour
- `  1.74 s` garde-fou : retour arrière automatique : DEMO_NEGATIVE -> 20261122T013019Z — prédictions négatives (10 produit(s))
- `  2.77 s` service rétabli : modèle 20261122T013019Z en service, prévision du 21/11 faite par la suite du cycle (4655 unités)
- `  2.79 s` base remise exactement dans son état d'origine

Raison enregistrée dans la base : **automatique : prédictions négatives (10 produit(s))**

Demande prévue sur 7 jours :

| | Avant la panne | Modèle saboté | Après le garde-fou |
|---|---:|---:|---:|
| **Total (50 produits)** | 4 655 | — | 4 655 |
| SKU-00003 Boissons Non #3 | 347 | — | 347 |
| SKU-00037 Boissons Entretenir #37 | 338 | — | 338 |
| SKU-00018 Boissons Paupière #18 | 278 | — | 278 |
| SKU-00046 Papeterie Lettre #46 | 212 | — | 212 |
| SKU-00011 Bricolage Marchand #11 | 200 | — | 200 |

Modèles retenus juste après le garde-fou :

```
Modèle             Entraîné le       Données au    WAPE  Naïve  État
DEMO_NEGATIVE      22/11/2026 11:41 2099-12-31   12.5%  19.1%  retiré le 22/11 11:42 (automatique : prédictions négatives (10 produit(s)))
20261122T013019Z   22/11/2026 01:30 2026-11-14   12.5%  19.1%  EN SERVICE
20261121T125301Z   21/11/2026 12:53 2026-11-13   12.4%  19.1%  ancien
```

## Ce qui se passe en vrai, la nuit

- Le garde-fou tourne **avant** la prévision : un modèle défectueux est en général retiré avant d'avoir publié quoi que ce soit. La démonstration montre le pire cas.
- Le retrait déclenche l'alerte e-mail `RetourArriereAutomatique` ; MLflow (alias `champion`), le site public et l'API suivent le modèle remis en service dans la suite du cycle.
- Si aucun modèle précédent ne passe les contrôles, le cycle s'arrête **avant** de prévoir (alerte `CycleEnEchec`) : mieux vaut pas de prévision qu'une prévision fausse.
- Ici, rien de tout cela n'est déclenché : la démonstration remet la base dans son état d'origine en quelques secondes et ne touche ni MLflow, ni le site, ni Prometheus.

Pour la refaire : `python -m smartsupply.models.demo_rollback` (hors du créneau de 2h30).
