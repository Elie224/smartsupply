# Dictionnaire de données — SmartSupply AI

*Généré le 22/11/2026 par `python scripts/build_data_dictionary.py` à partir de la base et des commentaires de `database/*.sql` : ne pas modifier à la main.*

**Données 100 % synthétiques.** Conventions : montants en EUR (`numeric(12,2)`), dates avec fuseau (`timestamptz`) ; `created_at` = date **métier simulée**, `ingested_at` = instant **réel** d'insertion ; remises stockées en fraction (0,20 = 20 %). Les vues `analytics` ne copient aucune donnée.

## Sommaire

- **raw** — Données brutes écrites par le simulateur (le « système opérationnel » de l'entreprise fictive). [clients](#rawclients), [inventory_balances](#rawinventory_balances), [order_items](#raworder_items), [orders](#raworders), [products](#rawproducts), [promotions](#rawpromotions), [purchase_order_items](#rawpurchase_order_items), [purchase_orders](#rawpurchase_orders), [simulation_runs](#rawsimulation_runs), [stock_movements](#rawstock_movements), [suppliers](#rawsuppliers), [warehouses](#rawwarehouses)
- **ml** — Machine learning : variables du modèle, entraînements, prévisions, surveillance. [drift_metrics](#mldrift_metrics), [feature_snapshots](#mlfeature_snapshots), [forecasts](#mlforecasts), [model_runs](#mlmodel_runs), [monitoring_daily](#mlmonitoring_daily), [training_datasets](#mltraining_datasets)
- **ops** — Exploitation : journal du cycle automatique. [pipeline_runs](#opspipeline_runs)
- **analytics** — Vues de lecture (aucune donnée copiée) pour Power BI, Grafana, l'API et le site. [v_clients](#analyticsv_clients), [v_clients_monthly](#analyticsv_clients_monthly), [v_daily_kpis](#analyticsv_daily_kpis), [v_daily_sales](#analyticsv_daily_sales), [v_drift_metrics](#analyticsv_drift_metrics), [v_forecast_accuracy](#analyticsv_forecast_accuracy), [v_forecasts](#analyticsv_forecasts), [v_incoming_receipts](#analyticsv_incoming_receipts), [v_model_runs](#analyticsv_model_runs), [v_monitoring_daily](#analyticsv_monitoring_daily), [v_pipeline_health](#analyticsv_pipeline_health), [v_pipeline_runs](#analyticsv_pipeline_runs), [v_product_performance](#analyticsv_product_performance), [v_promotions](#analyticsv_promotions), [v_stock_status](#analyticsv_stock_status), [v_supplier_reliability](#analyticsv_supplier_reliability)

## Schéma `raw`

Données brutes écrites par le simulateur (le « système opérationnel » de l'entreprise fictive).

### raw.clients

*Table — 1 198 lignes*

Clients B2B / B2C

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `client_id` | bigint | oui | clé | Client (raw.clients). |
| `name` | text | oui |  | Nom. |
| `segment` | text | oui |  | B2B (entreprise) ou B2C (particulier). |
| `city` | text | oui |  | Ville. |
| `created_at` | timestamp with time zone | oui |  | date d'inscription simulée |
| `ingested_at` | timestamp with time zone | oui |  | Instant réel d'insertion en base (horloge du PC, pas la date simulée). |

Règles vérifiées par la base :

- `CHECK ((segment = ANY (ARRAY['B2B'::text, 'B2C'::text])))`

### raw.inventory_balances

*Table — 150 lignes*

Solde courant (= somme du journal) Matérialisé pour éviter de resommer tout le journal à chaque lecture.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `product_id` | bigint | oui | clé | Produit (raw.products). |
| `warehouse_id` | bigint | oui | clé | Entrepôt (raw.warehouses). |
| `on_hand_qty` | integer | oui |  | Unités en stock dans l'entrepôt. |
| `updated_at` | timestamp with time zone | oui |  | horloge simulée |

Règles vérifiées par la base :

- `CHECK ((on_hand_qty >= 0))`

### raw.order_items

*Table — 336 488 lignes*

La demande des clients

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `order_item_id` | bigint | oui | clé | Identifiant de la ligne de commande. |
| `order_id` | bigint | oui | → raw.orders | Commande client (raw.orders). |
| `product_id` | bigint | oui | → raw.products | Produit (raw.products). |
| `warehouse_id` | bigint | oui | → raw.warehouses | Entrepôt (raw.warehouses). |
| `requested_qty` | integer | oui |  | DEMANDE (cible du ML) |
| `fulfilled_qty` | integer | oui |  | VENTE réellement servie |
| `lost_sales_qty` | integer | oui |  | demande perdue (rupture) |
| `unit_price` | numeric(12,2) | oui |  | prix payé, promo incluse (EUR) |
| `ingested_at` | timestamp with time zone | oui |  | Instant réel d'insertion en base (horloge du PC, pas la date simulée). |

Règles vérifiées par la base :

- `CHECK (((fulfilled_qty >= 0) AND (fulfilled_qty <= requested_qty)))`
- `CHECK ((lost_sales_qty = (requested_qty - fulfilled_qty)))`
- `CHECK ((unit_price > (0)::numeric))`
- `CHECK ((requested_qty > 0))`

### raw.orders

*Table — 255 236 lignes*

La demande des clients

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `order_id` | bigint | oui | clé | Commande client (raw.orders). |
| `client_id` | bigint | oui | → raw.clients | Client (raw.clients). |
| `ordered_at` | timestamp with time zone | oui |  | Date de la commande (simulée). |
| `status` | text | oui |  | fulfilled (tout livré) / partial (en partie) / unfulfilled (rien, rupture). |
| `channel` | text | oui |  | Canal de vente : web, store (magasin) ou b2b_portal. |
| `ingested_at` | timestamp with time zone | oui |  | Instant réel d'insertion en base (horloge du PC, pas la date simulée). |

Règles vérifiées par la base :

- `CHECK ((channel = ANY (ARRAY['web'::text, 'store'::text, 'b2b_portal'::text])))`
- `CHECK ((status = ANY (ARRAY['fulfilled'::text, 'partial'::text, 'unfulfilled'::text])))`

### raw.products

*Table — 50 lignes*

Catalogue

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `product_id` | bigint | oui | clé | Produit (raw.products). |
| `sku` | text | oui |  | Référence article. |
| `name` | text | oui |  | Nom. |
| `category` | text | oui |  | Catégorie du produit. |
| `unit_cost` | numeric(12,2) | oui |  | prix d'achat (EUR) |
| `unit_price` | numeric(12,2) | oui |  | prix de vente catalogue (EUR) |
| `supplier_id` | bigint | oui | → raw.suppliers | Fournisseur (raw.suppliers). |
| `created_at` | timestamp with time zone | oui |  | date de lancement simulée |
| `active` | boolean | oui |  | Produit encore vendu. |
| `ingested_at` | timestamp with time zone | oui |  | Instant réel d'insertion en base (horloge du PC, pas la date simulée). |

Règles vérifiées par la base :

- `CHECK ((unit_cost > (0)::numeric))`
- `CHECK ((unit_price >= unit_cost))`
- `UNIQUE (sku)`

### raw.promotions

*Table — 518 lignes*

Raw.promotions announced_at = jour où la promo devient connue. Servira à l'anti-fuite : une feature à la date D ne pourra utiliser que les promos annoncées <= D.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `promotion_id` | bigint | oui | clé | Identifiant de la promotion. |
| `product_id` | bigint | oui | → raw.products | Produit (raw.products). |
| `starts_at` | date | oui |  | Début de la promotion. |
| `ends_at` | date | oui |  | Fin de la promotion. |
| `discount_pct` | numeric(4,3) | oui |  | Remise, en fraction (0,20 = 20 %). |
| `announced_at` | date | oui |  | Date à laquelle la promotion est connue (une promotion n'est utilisée par le modèle qu'une fois annoncée). |
| `ingested_at` | timestamp with time zone | oui |  | Instant réel d'insertion en base (horloge du PC, pas la date simulée). |

Règles vérifiées par la base :

- `CHECK ((announced_at <= starts_at))`
- `CHECK ((ends_at >= starts_at))`
- `CHECK (((discount_pct > (0)::numeric) AND (discount_pct <= 0.7)))`

### raw.purchase_order_items

*Table — 9 554 lignes*

Réapprovisionnements

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `po_item_id` | bigint | oui | clé | Identifiant de la ligne de commande fournisseur. |
| `po_id` | bigint | oui | → raw.purchase_orders | Commande fournisseur (raw.purchase_orders). |
| `product_id` | bigint | oui | → raw.products | Produit (raw.products). |
| `ordered_qty` | integer | oui |  | Unités commandées. |
| `received_qty` | integer |  |  | Unités reçues (vide tant que la commande est en transit). |
| `unit_cost` | numeric(12,2) | oui |  | Coût d'achat unitaire (EUR). |
| `ingested_at` | timestamp with time zone | oui |  | Instant réel d'insertion en base (horloge du PC, pas la date simulée). |

Règles vérifiées par la base :

- `CHECK ((unit_cost > (0)::numeric))`
- `CHECK ((ordered_qty > 0))`
- `CHECK (((received_qty IS NULL) OR ((received_qty >= 0) AND (received_qty <= ordered_qty))))`

### raw.purchase_orders

*Table — 8 216 lignes*

Réapprovisionnements

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `po_id` | bigint | oui | clé | Commande fournisseur (raw.purchase_orders). |
| `supplier_id` | bigint | oui | → raw.suppliers | Fournisseur (raw.suppliers). |
| `warehouse_id` | bigint | oui | → raw.warehouses | Entrepôt (raw.warehouses). |
| `ordered_at` | date | oui |  | Date de la commande (simulée). |
| `expected_at` | date | oui |  | date promise (délai fournisseur) |
| `received_at` | date |  |  | date réelle, NULL tant que non reçue |
| `status` | text | oui |  | in_transit (en route) ou received (reçue). |
| `ingested_at` | timestamp with time zone | oui |  | Instant réel d'insertion en base (horloge du PC, pas la date simulée). |

Règles vérifiées par la base :

- `CHECK ((expected_at > ordered_at))`
- `CHECK (((received_at IS NULL) OR (received_at > ordered_at)))`
- `CHECK ((status = ANY (ARRAY['in_transit'::text, 'received'::text])))`
- `CHECK (((status = 'received'::text) = (received_at IS NOT NULL)))`

### raw.simulation_runs

*Table — 1 055 lignes*

Un enregistrement par jour simulé. UNIQUE(simulated_date) = garantie qu'un jour n'est jamais simulé deux fois, même si le générateur est relancé après une coupure.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `run_id` | bigint | oui | clé | Identifiant du passage de simulation. |
| `seed` | integer | oui |  | Graine aléatoire (rejouer la simulation à l'identique). |
| `mode` | text | oui |  | Origine : bootstrap (historique), once, stream, pipeline (cycle de nuit) ou scenario. |
| `simulated_date` | date | oui |  | Journée simulée (jamais deux fois la même). |
| `started_at` | timestamp with time zone | oui |  | Début. |
| `finished_at` | timestamp with time zone | oui |  | Fin. |
| `status` | text | oui |  | success ou failed. |

Règles vérifiées par la base :

- `CHECK ((mode = ANY (ARRAY['bootstrap'::text, 'once'::text, 'stream'::text, 'pipeline'::text, 'scenario'::text])))`
- `CHECK ((status = ANY (ARRAY['success'::text, 'failed'::text])))`
- `UNIQUE (simulated_date)`

### raw.stock_movements

*Table — 333 851 lignes*

Journal APPEND-ONLY de toutes les entrées/sorties Le stock d'un produit = somme de ses quantity_delta.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `movement_id` | bigint | oui | clé | Identifiant du mouvement de stock. |
| `event_time` | timestamp with time zone | oui |  | horloge métier simulée |
| `product_id` | bigint | oui | → raw.products | Produit (raw.products). |
| `warehouse_id` | bigint | oui | → raw.warehouses | Entrepôt (raw.warehouses). |
| `movement_type` | text | oui |  | initial (stock de départ), receipt (réception fournisseur) ou sale (vente). |
| `quantity_delta` | integer | oui |  | Variation de stock : + entrée, − sortie. |
| `source_id` | bigint |  |  | order_item_id ou po_item_id selon le type |
| `ingested_at` | timestamp with time zone | oui |  | horloge réelle |

Règles vérifiées par la base :

- `CHECK ((((movement_type = 'sale'::text) AND (quantity_delta < 0)) OR ((movement_type = ANY (ARRAY['initial'::text, 'receipt'::text])) AND (quantity_delta > 0))))`
- `CHECK ((movement_type = ANY (ARRAY['initial'::text, 'receipt'::text, 'sale'::text])))`

### raw.suppliers

*Table — 10 lignes*

Fournisseurs Créée en premier car raw.products y fait référence.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `supplier_id` | bigint | oui | clé | Fournisseur (raw.suppliers). |
| `name` | text | oui |  | Nom. |
| `country` | text | oui |  | Pays. |
| `lead_time_days` | integer | oui |  | délai annoncé de livraison |
| `reliability_score` | numeric(4,3) | oui |  | 0.000 = jamais à l'heure, 1.000 = toujours |
| `ingested_at` | timestamp with time zone | oui |  | Instant réel d'insertion en base (horloge du PC, pas la date simulée). |

Règles vérifiées par la base :

- `CHECK (((lead_time_days >= 1) AND (lead_time_days <= 120)))`
- `CHECK (((reliability_score >= (0)::numeric) AND (reliability_score <= (1)::numeric)))`

### raw.warehouses

*Table — 3 lignes*

Entrepôts

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `warehouse_id` | bigint | oui | clé | Entrepôt (raw.warehouses). |
| `name` | text | oui |  | Nom. |
| `city` | text | oui |  | Ville. |
| `capacity` | integer | oui |  | capacité en unités stockables |
| `ingested_at` | timestamp with time zone | oui |  | Instant réel d'insertion en base (horloge du PC, pas la date simulée). |

Règles vérifiées par la base :

- `CHECK ((capacity > 0))`
- `UNIQUE (name)`

## Schéma `ml`

Machine learning : variables du modèle, entraînements, prévisions, surveillance.

### ml.drift_metrics

*Table — 20 lignes*

Surveillance du modèle en service (une ligne par journée) ml.drift_metrics   : dérive de chaque variable (PSI) entre la période d'entraînement du modèle (1 an) et les 28 derniers jours ; ml.monitoring_daily: résumé du jour (dérive) + erreur RÉELLE de la prévision faite ce jour-là, connue 7 jours plus tard. Les colonnes *_logged indiquent ce qui a déjà été recopié dans MLflow.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `as_of_date` | date | oui | clé | Jour de la mesure. |
| `feature` | text | oui | clé | Variable du modèle. |
| `psi` | double precision | oui |  | Population Stability Index : < 0,10 stable, 0,10-0,25 à surveiller, > 0,25 forte. |
| `level` | text | oui |  | stable / à surveiller / forte (seuils du PSI). |
| `reference_start` | date | oui |  | Début de la période de référence (année d'entraînement du modèle). |
| `reference_end` | date | oui |  | Fin de la période de référence. |
| `current_start` | date | oui |  | Début de la période récente comparée (28 jours). |
| `model_run_id` | text | oui | → ml.model_runs | Modèle en service ce jour-là. |
| `computed_at` | timestamp with time zone | oui |  | Instant du calcul. |

Règles vérifiées par la base :

- `CHECK ((level = ANY (ARRAY['stable'::text, 'à surveiller'::text, 'forte'::text])))`
- `CHECK ((psi >= (0)::double precision))`

### ml.feature_snapshots

*Table — 51 400 lignes*

Une ligne = un produit observé à la fin du jour as_of_date. Toutes les variables n'utilisent QUE des informations connues à as_of_date. La cible (demande J+1..J+7) reste NULL tant que ces 7 jours ne sont pas passés.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `as_of_date` | date | oui | clé | Jour D : la ligne décrit le produit à la fin de ce jour, avec seulement ce qui était connu. |
| `product_id` | bigint | oui | clé | Produit (raw.products). |
| `category` | text | oui |  | Catégorie du produit. |
| `supplier_id` | bigint | oui |  | Fournisseur (raw.suppliers). |
| `demand_lag_1` | integer | oui |  | demande du jour D |
| `demand_lag_7` | integer | oui |  | demande du jour D-6 |
| `demand_lag_14` | integer | oui |  | demande du jour D-13 |
| `demand_lag_28` | integer | oui |  | demande du jour D-27 |
| `demand_rolling_mean_7` | double precision | oui |  | Demande moyenne des 7 derniers jours (D-6 à D). |
| `demand_rolling_mean_28` | double precision | oui |  | Demande moyenne des 28 derniers jours. |
| `demand_rolling_std_28` | double precision | oui |  | Écart-type de la demande sur 28 jours (régularité). |
| `lost_sales_rolling_7` | integer | oui |  | Unités demandées mais non servies (ruptures) sur 7 jours. |
| `price_as_of` | numeric(12,2) | oui |  | Prix de vente catalogue (EUR, avant remise ; la remise est dans discount_pct). |
| `promotion_active` | boolean | oui |  | Promotion en cours le jour D. |
| `discount_pct` | double precision | oui |  | Remise du jour D, en fraction (0,20 = 20 %). |
| `promo_days_next_7` | integer | oui |  | Jours de promotion déjà annoncés sur D+1..D+7. |
| `promo_max_discount_next_7` | double precision | oui |  | Plus forte remise annoncée sur D+1..D+7 (fraction). |
| `weekday` | smallint | oui |  | 0 = lundi |
| `month` | smallint | oui |  | Mois de D (1 à 12). |
| `day_of_year` | smallint | oui |  | Jour de l'année de D (1 à 366). |
| `is_weekend` | boolean | oui |  | D est un samedi ou un dimanche. |
| `stock_as_of` | integer | oui |  | Stock disponible à la fin de D, tous entrepôts. |
| `incoming_qty_known_as_of` | integer | oui |  | en transit, attendu d'ici D+7 |
| `supplier_lead_time_known_as_of` | integer | oui |  | Délai de livraison du fournisseur connu à D (jours). |
| `target_demand_next_7d` | integer |  |  | CIBLE : unités demandées de D+1 à D+7. NULL tant que ces 7 jours ne sont pas passés. |
| `built_at` | timestamp with time zone | oui |  | Instant du calcul de la ligne. |
| `demand_rolling_mean_91` | double precision |  |  | Demande moyenne des 91 derniers jours (tendance de fond). |
| `demand_rolling_mean_364` | double precision |  |  | NULL si < 6 mois d'historique |
| `demand_next7_last_year` | double precision |  |  | Demande de la semaine correspondante l'an dernier (jours D-363 à D-357) : saisonnalité annuelle. |
| `category_season_index` | double precision |  |  | Saisonnalité de la catégorie à cette période de l'année (1 = normal). |
| `category_trend_7_28` | double precision |  |  | Tendance récente de la catégorie : moyenne 7 jours / moyenne 28 jours. |

### ml.forecasts

*Table — 50 lignes*

Prévisions faites chaque nuit, JAMAIS modifiées ensuite as_of_date = dernière journée connue ; la prévision couvre les 7 jours suivants.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `as_of_date` | date | oui | clé | Dernière journée connue au moment de la prévision. |
| `product_id` | bigint | oui | clé | Produit (raw.products). |
| `horizon_start` | date | oui |  | Premier jour prévu (as_of_date + 1). |
| `horizon_end` | date | oui |  | Dernier jour prévu (as_of_date + 7). |
| `predicted_qty` | integer | oui |  | Unités demandées prévues sur les 7 jours. |
| `run_id` | text | oui | → ml.model_runs | Modèle qui a fait la prévision (ml.model_runs). |
| `created_at` | timestamp with time zone | oui |  | Instant où la prévision a été enregistrée. |

Règles vérifiées par la base :

- `CHECK (((horizon_start = (as_of_date + 1)) AND (horizon_end = (as_of_date + 7))))`
- `CHECK ((predicted_qty >= 0))`

### ml.model_runs

*Table — 1 lignes*

Un entraînement nocturne = une ligne (registre des modèles) Le modèle EN PLACE = le dernier entraînement mis en service (promoted) et non retiré. Le modèle lui-même est stocké dans la base (model_blob) : il est disponible aussi bien pour le pipeline Docker que pour les commandes lancées sur le PC.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `run_id` | text | oui | clé | Identifiant de l'entraînement (horodatage UTC), aussi tag MLflow smartsupply_run_id. |
| `trained_at` | timestamp with time zone | oui |  | Date de l'entraînement. |
| `model_name` | text | oui |  | Type de modèle (lightgbm). |
| `data_until` | date | oui |  | dernière date avec cible connue |
| `eval_start` | date | oui |  | fenêtre d'évaluation (8 semaines) |
| `eval_end` | date | oui |  | Fin de la fenêtre d'évaluation (8 semaines). |
| `n_train_rows` | integer | oui |  | Lignes utilisées pour l'entraînement évalué. |
| `wape` | double precision | oui |  | scores du nouveau modèle sur la fenêtre |
| `mae` | double precision | oui |  | Erreur absolue moyenne sur la fenêtre d'évaluation (unités). |
| `bias` | double precision | oui |  | Biais sur la fenêtre d'évaluation (> 0 : surestimation). |
| `baseline_wape` | double precision | oui |  | méthode naïve 28 j, même fenêtre |
| `champion_run_id` | text |  |  | modèle en place au moment de la décision |
| `champion_wape` | double precision |  |  | Erreur du modèle en place au moment de la décision. |
| `promoted` | boolean | oui |  | Mis en service (règle : bat la méthode naïve, \|biais\| ≤ 5 %, pas pire que le modèle en place). |
| `decision` | text | oui |  | raison, en clair |
| `model_blob` | bytea |  |  | modèle sérialisé (seulement s'il est retenu) |
| `params` | jsonb |  |  | Réglages LightGBM utilisés (JSON). |
| `withdrawn_at` | timestamp with time zone |  |  | Retour arrière : date de retrait du service (NULL = jamais retiré). |
| `withdrawn_reason` | text |  |  | Retour arrière : raison du retrait. |

Règles vérifiées par la base :

- `CHECK (((NOT promoted) OR (model_blob IS NOT NULL)))`
- `UNIQUE (data_until)`

### ml.monitoring_daily

*Table — 1 lignes*

Surveillance du modèle en service (une ligne par journée) ml.drift_metrics   : dérive de chaque variable (PSI) entre la période d'entraînement du modèle (1 an) et les 28 derniers jours ; ml.monitoring_daily: résumé du jour (dérive) + erreur RÉELLE de la prévision faite ce jour-là, connue 7 jours plus tard. Les colonnes *_logged indiquent ce qui a déjà été recopié dans MLflow.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `as_of_date` | date | oui | clé | Jour de surveillance (jour de la prévision). |
| `model_run_id` | text |  | → ml.model_runs | Modèle en service ce jour-là. |
| `max_psi` | double precision |  |  | PSI le plus élevé du jour. |
| `top_feature` | text |  |  | Variable qui a le PSI le plus élevé. |
| `n_watch` | integer |  |  | variables « à surveiller » (PSI 0,1 à 0,25) |
| `n_strong` | integer |  |  | variables en dérive forte (PSI > 0,25) |
| `drift_logged` | boolean | oui |  | Dérive déjà recopiée dans MLflow. |
| `real_wape` | double precision |  |  | erreur réelle de la prévision de ce jour (7 jours après) |
| `real_bias` | double precision |  |  | Biais réel de la prévision de ce jour (> 0 : surestimation). |
| `real_logged` | boolean | oui |  | Erreur réelle déjà recopiée dans MLflow. |
| `updated_at` | timestamp with time zone | oui |  | Dernière mise à jour. |

### ml.training_datasets

*Table — 5 lignes*

Registre des jeux de données exportés (versionnage)

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `dataset_id` | text | oui | clé | features_<date max>_<empreinte du contenu> |
| `created_at` | timestamp with time zone | oui |  | Date de l'export. |
| `as_of_min` | date | oui |  | Première journée du jeu de données. |
| `as_of_max` | date | oui |  | Dernière journée du jeu de données. |
| `n_rows` | integer | oui |  | Nombre de lignes. |
| `n_products` | integer | oui |  | Nombre de produits. |
| `feature_columns` | text[] | oui |  | Variables incluses. |
| `target_column` | text | oui |  | Colonne cible. |
| `content_hash` | text | oui |  | identique si les données sont identiques |
| `file_path` | text | oui |  | Fichier exporté (data/datasets). |
| `code_version` | text | oui |  | Version du code qui a produit le fichier. |
| `simulation_seed` | integer |  |  | Graine de la simulation. |

## Schéma `ops`

Exploitation : journal du cycle automatique.

### ops.pipeline_runs

*Table — 16 lignes*

Journal du cycle automatique (un passage = une ligne) Sert à savoir, sans ouvrir Prefect, si le cycle tourne bien : vue analytics.v_pipeline_health (Power BI) et alerte Windows (smartsupply.health).

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `run_id` | bigint | oui | clé | Identifiant du passage. |
| `flow_run_id` | text |  |  | identifiant Prefect (lien avec l'interface) |
| `started_at` | timestamp with time zone | oui |  | Début. |
| `finished_at` | timestamp with time zone | oui |  | Fin. |
| `status` | text | oui |  | ok ou échec. |
| `days_added` | integer | oui |  | Journées simulées pendant le passage. |
| `model_status` | text |  |  | Résultat de l'étape modèle (mis en service, refusé, déjà fait...). |
| `forecast_status` | text |  |  | Résultat de l'étape prévision. |
| `message` | text |  |  | résumé, ou l'erreur en cas d'échec |

Règles vérifiées par la base :

- `CHECK ((status = ANY (ARRAY['ok'::text, 'échec'::text])))`

## Schéma `analytics`

Vues de lecture (aucune donnée copiée) pour Power BI, Grafana, l'API et le site.

### analytics.v_clients

*Vue — lue par : Power BI (Clients)*

Clients et leur statut (page "Clients") Règle MÉTIER : un client est PERDU après 12 mois sans commande. (La simulation ne stocke aucun indicateur « parti » : on l'observe.) à risque = 6 à 12 mois sans commande ; nouveau = inscrit depuis < 90 jours.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `client_id` | bigint |  |  | Client (raw.clients). |
| `name` | text |  |  | Nom. |
| `segment` | text |  |  | B2B (entreprise) ou B2C (particulier). |
| `city` | text |  |  | Ville. |
| `created_date` | date |  |  | Date d'inscription. |
| `first_order` | date |  |  | Première commande. |
| `last_order` | date |  |  | Dernière commande. |
| `n_orders` | bigint |  |  | Nombre de commandes. |
| `revenue_eur` | numeric |  |  | Chiffre d'affaires (EUR, remises comprises). |
| `days_without_order` | integer |  |  | Jours depuis la dernière commande (ou l'inscription). |
| `client_status` | text |  |  | perdu (≥ 12 mois sans commande) / à risque (6 à 12 mois) / nouveau (< 90 jours) / actif. |
| `lost_date` | date |  |  | Date à laquelle le client est devenu perdu. |
| `margin_eur` | numeric |  |  | Marge brute totale (EUR). |
| `revenue_12m` | numeric |  |  | CA des 12 derniers mois (EUR). |
| `revenue_prev_12m` | numeric |  |  | CA des 12 mois précédents (EUR). |
| `margin_12m` | numeric |  |  | Marge des 12 derniers mois (EUR). |
| `margin_prev_12m` | numeric |  |  | Marge des 12 mois précédents (EUR). |
| `n_orders_12m` | bigint |  |  | Commandes sur 12 mois. |
| `revenue_change_pct` | numeric |  |  | Évolution du CA sur un an, en %. |
| `margin_change_pct` | numeric |  |  | Évolution de la marge sur un an, en %. |
| `revenue_rank` | bigint |  |  | Rang selon le CA (1 = le plus gros). |
| `value_class` | text |  |  | Classe ABC sur le CA 12 mois : A = 80 % du CA, B = 15 % suivants, C = le reste. |
| `revenue_trend` | text |  |  | en hausse (≥ +20 %) / en baisse (≤ −20 %) / stable / nouveau CA / sans historique. |
| `to_follow` | boolean |  |  | À suivre : client à risque, ou client A/B dont le CA baisse d'au moins 20 %. |

### analytics.v_clients_monthly

*Vue — lue par : Power BI (Clients)*

Évolution mensuelle de la base clients nouveaux = inscriptions du mois ; perdus = clients qui atteignent 12 mois sans commande ce mois-ci ; actifs_12m = au moins une commande sur les 12 derniers mois (fin de mois).

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `month` | date |  |  | Mois. |
| `new_clients` | bigint |  |  | Clients inscrits dans le mois. |
| `lost_clients` | bigint |  |  | Clients devenus perdus dans le mois (12 mois sans commande). |
| `active_clients_12m` | bigint |  |  | Clients avec au moins une commande sur les 12 mois précédents. |

### analytics.v_daily_kpis

*Vue — lue par : Power BI (Vue direction), site public*

KPI globaux par jour (page "Vue direction")

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `sales_date` | date |  |  | Jour des ventes. |
| `n_orders` | bigint |  |  | Nombre de commandes. |
| `n_active_clients` | bigint |  |  | Clients ayant commandé ce jour-là. |
| `requested_qty` | bigint |  |  | Unités demandées par les clients. |
| `fulfilled_qty` | bigint |  |  | Unités réellement livrées. |
| `lost_sales_qty` | bigint |  |  | Unités demandées mais non livrées (rupture). |
| `service_rate_pct` | numeric |  |  | Taux de service : unités livrées / unités demandées, en %. |
| `revenue_eur` | numeric |  |  | Chiffre d'affaires (EUR, remises comprises). |
| `gross_margin_eur` | numeric |  |  | Marge brute = CA − coût d'achat des unités vendues (EUR). |

### analytics.v_daily_sales

*Vue — lue par : Power BI*

Ventes par jour x produit x entrepôt (le "grain" le plus fin du BI) demande  = requested_qty  (ce que les clients voulaient) ventes   = fulfilled_qty  (ce qui a été servi) perdues  = lost_sales_qty (ruptures)

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `sales_date` | date |  |  | Jour des ventes. |
| `product_id` | bigint |  |  | Produit (raw.products). |
| `sku` | text |  |  | Référence article. |
| `product_name` | text |  |  | Nom du produit. |
| `category` | text |  |  | Catégorie du produit. |
| `warehouse_id` | bigint |  |  | Entrepôt (raw.warehouses). |
| `warehouse_name` | text |  |  | Nom de l'entrepôt. |
| `n_orders` | bigint |  |  | Nombre de commandes. |
| `n_clients` | bigint |  |  | Clients distincts. |
| `requested_qty` | bigint |  |  | Unités demandées par les clients. |
| `fulfilled_qty` | bigint |  |  | Unités réellement livrées. |
| `lost_sales_qty` | bigint |  |  | Unités demandées mais non livrées (rupture). |
| `revenue_eur` | numeric |  |  | Chiffre d'affaires (EUR, remises comprises). |
| `gross_margin_eur` | numeric |  |  | Marge brute = CA − coût d'achat des unités vendues (EUR). |
| `lost_revenue_eur` | numeric |  |  | CA perdu à cause des ruptures (unités non livrées × prix). |
| `promotion_active` | boolean |  |  | Une promotion était en cours ce jour-là. |

### analytics.v_drift_metrics

*Vue — lue par : Grafana, site public*

Dérive de chaque variable, jour par jour (Grafana)

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `as_of_date` | date |  |  | Dernière journée connue au moment de la prévision. |
| `feature` | text |  |  | Variable du modèle. |
| `psi` | double precision |  |  | Population Stability Index : < 0,10 stable, 0,10-0,25 à surveiller, > 0,25 forte. |
| `level` | text |  |  | stable / à surveiller / forte (seuils du PSI). |
| `model_run_id` | text |  |  | Modèle qui a fait la prévision (ml.model_runs). |

### analytics.v_forecast_accuracy

*Vue — lue par : Power BI, site public*

Qualité RÉELLE des prévisions, jour de prévision par jour de prévision (seulement quand les 7 jours prévus sont terminés)

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `as_of_date` | date |  |  | Dernière journée connue au moment de la prévision. |
| `n_products` | bigint |  |  | Nombre de produits. |
| `predicted_qty` | bigint |  |  | Unités demandées prévues sur les 7 jours. |
| `actual_qty` | bigint |  |  | Unités réellement demandées sur les 7 jours (vide tant qu'ils ne sont pas passés). |
| `wape_pct` | numeric |  |  | Erreur en % : somme des écarts absolus / demande totale. |
| `bias_pct` | numeric |  |  | Biais en % (> 0 : le modèle surestime). |

### analytics.v_forecasts

*Vue — lue par : Power BI (Prévisions), API, site public, Prometheus*

Prévisions du modèle (page "Prévisions") Une ligne = un produit, prévu à la fin de as_of_date pour les 7 jours suivants. actual_qty reste vide tant que ces 7 jours ne sont pas terminés. Alerte : stock + réceptions attendues sous 7 jours < demande prévue.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `as_of_date` | date |  |  | Dernière journée connue au moment de la prévision. |
| `horizon_start` | date |  |  | Premier jour prévu (as_of_date + 1). |
| `horizon_end` | date |  |  | Dernier jour prévu (as_of_date + 7). |
| `is_latest` | boolean |  |  | Ligne de la prévision la plus récente (celle du jour). |
| `product_id` | bigint |  |  | Produit (raw.products). |
| `sku` | text |  |  | Référence article. |
| `product_name` | text |  |  | Nom du produit. |
| `category` | text |  |  | Catégorie du produit. |
| `predicted_qty` | integer |  |  | Unités demandées prévues sur les 7 jours. |
| `actual_qty` | integer |  |  | Unités réellement demandées sur les 7 jours (vide tant qu'ils ne sont pas passés). |
| `error_qty` | integer |  |  | > 0 : sous-estimé |
| `abs_error_qty` | integer |  |  | Écart absolu prévu / réel (unités), quand le réel est connu. |
| `predicted_revenue_eur` | numeric |  |  | CA prévu sur 7 jours = demande prévue × prix catalogue (EUR). |
| `stock_as_of` | integer |  |  | Stock disponible à la fin de as_of_date, tous entrepôts. |
| `incoming_qty` | integer |  |  | Réceptions fournisseurs attendues sur les 7 jours prévus. |
| `projected_stock_end` | integer |  |  | Stock projeté en fin de semaine = stock + réceptions − demande prévue. |
| `forecast_alert` | text |  |  | ok / à surveiller (stock < 1,5 × demande prévue) / rupture probable (stock < demande prévue). |
| `model_run_id` | text |  |  | Modèle qui a fait la prévision (ml.model_runs). |

### analytics.v_incoming_receipts

*Vue — lue par : Power BI*

Réceptions à venir (commandes fournisseurs en cours)

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `po_id` | bigint |  |  | Commande fournisseur (raw.purchase_orders). |
| `supplier_id` | bigint |  |  | Fournisseur (raw.suppliers). |
| `supplier_name` | text |  |  | Nom du fournisseur. |
| `warehouse_id` | bigint |  |  | Entrepôt (raw.warehouses). |
| `warehouse_name` | text |  |  | Nom de l'entrepôt. |
| `product_id` | bigint |  |  | Produit (raw.products). |
| `sku` | text |  |  | Référence article. |
| `product_name` | text |  |  | Nom du produit. |
| `ordered_qty` | integer |  |  | Unités commandées. |
| `ordered_at` | date |  |  | Date de la commande (simulée). |
| `expected_at` | date |  |  | Date de livraison annoncée. |

### analytics.v_model_runs

*Vue — lue par : Power BI, Grafana, site public*

Historique des entraînements nocturnes (sans le modèle lui-même)

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `run_id` | text |  |  | Modèle qui a fait la prévision (ml.model_runs). |
| `trained_at` | timestamp with time zone |  |  | Date de l'entraînement. |
| `model_name` | text |  |  | Type de modèle (lightgbm). |
| `data_until` | date |  |  | dernière date avec cible connue |
| `eval_start` | date |  |  | fenêtre d'évaluation (8 semaines) |
| `eval_end` | date |  |  | Fin de la fenêtre d'évaluation (8 semaines). |
| `n_train_rows` | integer |  |  | Lignes utilisées pour l'entraînement évalué. |
| `wape_pct` | numeric |  |  | Erreur en % : somme des écarts absolus / demande totale. |
| `baseline_wape_pct` | numeric |  |  | Erreur de la méthode naïve (moyenne 28 jours), même fenêtre, en %. |
| `bias_pct` | numeric |  |  | Biais en % (> 0 : le modèle surestime). |
| `champion_wape_pct` | numeric |  |  | Erreur du modèle en place au moment de la décision, en %. |
| `promoted` | boolean |  |  | Mis en service (règle : bat la méthode naïve, \|biais\| ≤ 5 %, pas pire que le modèle en place). |
| `decision` | text |  |  | raison, en clair |
| `is_current` | boolean |  |  | Modèle actuellement en service (dernier retenu et non retiré). |
| `withdrawn_at` | timestamp with time zone |  |  | Retour arrière : date de retrait du service (NULL = jamais retiré). |
| `withdrawn_reason` | text |  |  | Retour arrière : raison du retrait. |

### analytics.v_monitoring_daily

*Vue — lue par : Grafana, site public*

Surveillance du modèle, jour par jour (Grafana) max_psi : dérive la plus forte du jour ; real_wape : erreur réelle de la prévision faite ce jour-là (connue 7 jours plus tard).

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `as_of_date` | date |  |  | Dernière journée connue au moment de la prévision. |
| `model_run_id` | text |  |  | Modèle qui a fait la prévision (ml.model_runs). |
| `max_psi` | double precision |  |  | PSI le plus élevé du jour. |
| `top_feature` | text |  |  | Variable qui a le PSI le plus élevé. |
| `n_watch` | integer |  |  | variables « à surveiller » (PSI 0,1 à 0,25) |
| `n_strong` | integer |  |  | variables en dérive forte (PSI > 0,25) |
| `real_wape` | double precision |  |  | erreur réelle de la prévision de ce jour (7 jours après) |
| `real_bias` | double precision |  |  | Biais réel de la prévision de ce jour (> 0 : surestimation). |

### analytics.v_pipeline_health

*Vue — lue par : Power BI, alerte Windows, site public*

État du cycle automatique (une seule ligne) : « est-ce que tout a tourné ? » Journée attendue = hier (heure de Paris), avec une marge jusqu'à 4 h du matin pour laisser le cycle de 2 h 30 se terminer. health_status : ok / en retard / échec / aucun passage (même règle que smartsupply/health.py, utilisée pour l'alerte Windows)

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `expected_day` | date |  |  | Journée qui devrait déjà être simulée (hier, ou avant-hier avant 4 h). |
| `last_simulated_day` | date |  |  | Dernière journée simulée. |
| `last_forecast_day` | date |  |  | Jour de la dernière prévision. |
| `last_training_data_until` | date |  |  | Données du dernier entraînement (jusqu'au). |
| `last_run_at` | timestamp with time zone |  |  | Fin du dernier passage du cycle. |
| `last_run_status` | text |  |  | Résultat du dernier passage (ok / échec). |
| `last_success_at` | timestamp with time zone |  |  | Fin du dernier passage réussi. |
| `hours_since_success` | numeric |  |  | Heures depuis la dernière réussite. |
| `health_status` | text |  |  | ok / en retard / échec / aucun passage. |
| `health_message` | text |  |  | Explication en clair et consigne à suivre. |

### analytics.v_pipeline_runs

*Vue — lue par : Grafana*

Journal des passages du cycle (Grafana)

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `run_id` | bigint |  |  | Modèle qui a fait la prévision (ml.model_runs). |
| `started_at` | timestamp with time zone |  |  | Début. |
| `finished_at` | timestamp with time zone |  |  | Fin. |
| `duration_s` | numeric |  |  | Durée du passage, en secondes. |
| `status` | text |  |  | ok ou échec. |
| `days_added` | integer |  |  | Journées simulées pendant le passage. |
| `model_status` | text |  |  | Résultat de l'étape modèle (mis en service, refusé, déjà fait...). |
| `forecast_status` | text |  |  | Résultat de l'étape prévision. |
| `message` | text |  |  | résumé, ou l'erreur en cas d'échec |

### analytics.v_product_performance

*Vue — lue par : Power BI*

Performance par produit (page "Ventes et produits")

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `product_id` | bigint |  |  | Produit (raw.products). |
| `sku` | text |  |  | Référence article. |
| `product_name` | text |  |  | Nom du produit. |
| `category` | text |  |  | Catégorie du produit. |
| `supplier_id` | bigint |  |  | Fournisseur (raw.suppliers). |
| `unit_cost` | numeric(12,2) |  |  | Coût d'achat unitaire (EUR). |
| `unit_price` | numeric(12,2) |  |  | prix de vente catalogue (EUR) |
| `active` | boolean |  |  | Produit encore vendu. |
| `first_sale_date` | date |  |  | Première vente. |
| `last_sale_date` | date |  |  | Dernière vente. |
| `requested_qty` | numeric |  |  | Unités demandées par les clients. |
| `fulfilled_qty` | numeric |  |  | Unités réellement livrées. |
| `lost_sales_qty` | numeric |  |  | Unités demandées mais non livrées (rupture). |
| `service_rate_pct` | numeric |  |  | Taux de service : unités livrées / unités demandées, en %. |
| `revenue_eur` | numeric |  |  | Chiffre d'affaires (EUR, remises comprises). |
| `gross_margin_eur` | numeric |  |  | Marge brute = CA − coût d'achat des unités vendues (EUR). |
| `gross_margin_pct` | numeric |  |  | Marge brute / CA, en %. |
| `lost_revenue_eur` | numeric |  |  | CA perdu à cause des ruptures (unités non livrées × prix). |
| `promo_days` | bigint |  |  | Jours de promotion. |
| `revenue_rank` | bigint |  |  | Rang selon le CA (1 = le plus gros). |

### analytics.v_promotions

*Vue — lue par : Power BI*

Calendrier des promotions

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `promotion_id` | bigint |  |  | Identifiant de la promotion. |
| `product_id` | bigint |  |  | Produit (raw.products). |
| `sku` | text |  |  | Référence article. |
| `product_name` | text |  |  | Nom du produit. |
| `category` | text |  |  | Catégorie du produit. |
| `announced_at` | date |  |  | Date à laquelle la promotion est connue (une promotion n'est utilisée par le modèle qu'une fois annoncée). |
| `starts_at` | date |  |  | Début de la promotion. |
| `ends_at` | date |  |  | Fin de la promotion. |
| `duration_days` | integer |  |  | Durée, en jours. |
| `discount_pct` | numeric |  |  | Remise, en fraction (0,20 = 20 %). |

### analytics.v_stock_status

*Vue — lue par : Power BI*

État des stocks au dernier jour simulé (page "Stocks et ruptures") Le statut est une RÈGLE simple (couverture de stock vs livraisons attendues), pas une probabilité de rupture : le ML fera mieux en phase 6.

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `as_of_date` | date |  |  | Dernière journée connue au moment de la prévision. |
| `product_id` | bigint |  |  | Produit (raw.products). |
| `sku` | text |  |  | Référence article. |
| `product_name` | text |  |  | Nom du produit. |
| `category` | text |  |  | Catégorie du produit. |
| `warehouse_id` | bigint |  |  | Entrepôt (raw.warehouses). |
| `warehouse_name` | text |  |  | Nom de l'entrepôt. |
| `on_hand_qty` | integer |  |  | Unités en stock dans l'entrepôt. |
| `in_transit_qty` | bigint |  |  | Unités commandées au fournisseur, pas encore reçues. |
| `next_expected_at` | date |  |  | Prochaine livraison attendue. |
| `supplier_lead_time_days` | integer |  |  | Délai annoncé par le fournisseur (jours). |
| `avg_daily_demand_28d` | numeric |  |  | Demande moyenne par jour sur 28 jours. |
| `days_of_cover` | numeric |  |  | Jours de stock restants au rythme actuel (vide si aucune demande). |
| `stock_status` | text |  |  | rupture (stock nul) / critique (< 7 jours et pas de livraison à temps) / surstock (> 60 jours) / ok. |
| `stock_value_eur` | numeric |  |  | Valeur du stock au coût d'achat (EUR). |

### analytics.v_supplier_reliability

*Vue — lue par : Power BI*

Fiabilité des fournisseurs (page "Fournisseurs") délai annoncé vs délai réellement constaté

| Colonne | Type | Obligatoire | Clé | Description |
|---|---|---|---|---|
| `supplier_id` | bigint |  |  | Fournisseur (raw.suppliers). |
| `supplier_name` | text |  |  | Nom du fournisseur. |
| `country` | text |  |  | Pays. |
| `announced_lead_time_days` | integer |  |  | Délai annoncé par le fournisseur (jours). |
| `n_purchase_orders` | bigint |  |  | Commandes passées au fournisseur. |
| `n_received` | bigint |  |  | Commandes reçues. |
| `n_in_transit` | bigint |  |  | Commandes en transit. |
| `avg_actual_lead_time_days` | numeric |  |  | Délai réellement constaté, en moyenne (jours). |
| `avg_delay_days` | numeric |  |  | Retard moyen par rapport au délai annoncé (jours). |
| `late_rate_pct` | numeric |  |  | Part des commandes livrées en retard, en %. |
| `max_delay_days` | integer |  |  | Plus gros retard constaté (jours). |
