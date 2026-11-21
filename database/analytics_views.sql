-- =====================================================================
-- SmartSupply AI - Vues analytiques (lues par Power BI)
-- Power BI ne lit QUE le schéma analytics, jamais les tables raw.
-- Relançable : CREATE OR REPLACE VIEW.
-- Dates : jour métier simulé, en UTC.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1) Ventes par jour x produit x entrepôt (le "grain" le plus fin du BI)
--    demande  = requested_qty  (ce que les clients voulaient)
--    ventes   = fulfilled_qty  (ce qui a été servi)
--    perdues  = lost_sales_qty (ruptures)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_daily_sales AS
SELECT
    (o.ordered_at AT TIME ZONE 'UTC')::date                       AS sales_date,
    oi.product_id,
    p.sku,
    p.name                                                        AS product_name,
    p.category,
    oi.warehouse_id,
    w.name                                                        AS warehouse_name,
    count(DISTINCT o.order_id)                                    AS n_orders,
    count(DISTINCT o.client_id)                                   AS n_clients,
    sum(oi.requested_qty)                                         AS requested_qty,
    sum(oi.fulfilled_qty)                                         AS fulfilled_qty,
    sum(oi.lost_sales_qty)                                        AS lost_sales_qty,
    sum(oi.fulfilled_qty * oi.unit_price)                         AS revenue_eur,
    -- marge brute SIMULÉE : prix payé - coût d'achat catalogue
    sum(oi.fulfilled_qty * (oi.unit_price - p.unit_cost))         AS gross_margin_eur,
    -- manque à gagner estimé des ruptures (au prix payé)
    sum(oi.lost_sales_qty * oi.unit_price)                        AS lost_revenue_eur,
    bool_or(oi.unit_price < p.unit_price)                         AS promotion_active
FROM raw.order_items oi
JOIN raw.orders     o USING (order_id)
JOIN raw.products   p ON p.product_id = oi.product_id
JOIN raw.warehouses w ON w.warehouse_id = oi.warehouse_id
GROUP BY 1, 2, 3, 4, 5, 6, 7;

-- ---------------------------------------------------------------------
-- 2) KPI globaux par jour (page "Vue direction")
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_daily_kpis AS
SELECT
    (o.ordered_at AT TIME ZONE 'UTC')::date                       AS sales_date,
    count(DISTINCT o.order_id)                                    AS n_orders,
    count(DISTINCT o.client_id)                                   AS n_active_clients,
    sum(oi.requested_qty)                                         AS requested_qty,
    sum(oi.fulfilled_qty)                                         AS fulfilled_qty,
    sum(oi.lost_sales_qty)                                        AS lost_sales_qty,
    round(100.0 * sum(oi.fulfilled_qty) / nullif(sum(oi.requested_qty), 0), 2)
                                                                  AS service_rate_pct,
    sum(oi.fulfilled_qty * oi.unit_price)                         AS revenue_eur,
    sum(oi.fulfilled_qty * (oi.unit_price - p.unit_cost))         AS gross_margin_eur
FROM raw.order_items oi
JOIN raw.orders   o USING (order_id)
JOIN raw.products p ON p.product_id = oi.product_id
GROUP BY 1;

-- ---------------------------------------------------------------------
-- 3) Performance par produit (page "Ventes et produits")
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_product_performance AS
WITH totals AS (
    SELECT
        product_id,
        min(sales_date)                         AS first_sale_date,
        max(sales_date)                         AS last_sale_date,
        sum(requested_qty)                      AS requested_qty,
        sum(fulfilled_qty)                      AS fulfilled_qty,
        sum(lost_sales_qty)                     AS lost_sales_qty,
        sum(revenue_eur)                        AS revenue_eur,
        sum(gross_margin_eur)                   AS gross_margin_eur,
        sum(lost_revenue_eur)                   AS lost_revenue_eur,
        count(DISTINCT sales_date) FILTER (WHERE promotion_active) AS promo_days
    FROM analytics.v_daily_sales
    GROUP BY product_id
)
SELECT
    p.product_id, p.sku, p.name AS product_name, p.category, p.supplier_id,
    p.unit_cost, p.unit_price, p.active,
    t.first_sale_date, t.last_sale_date,
    coalesce(t.requested_qty, 0)                AS requested_qty,
    coalesce(t.fulfilled_qty, 0)                AS fulfilled_qty,
    coalesce(t.lost_sales_qty, 0)               AS lost_sales_qty,
    round(100.0 * t.fulfilled_qty / nullif(t.requested_qty, 0), 2) AS service_rate_pct,
    coalesce(t.revenue_eur, 0)                  AS revenue_eur,
    coalesce(t.gross_margin_eur, 0)             AS gross_margin_eur,
    round(100.0 * t.gross_margin_eur / nullif(t.revenue_eur, 0), 2) AS gross_margin_pct,
    coalesce(t.lost_revenue_eur, 0)             AS lost_revenue_eur,
    coalesce(t.promo_days, 0)                   AS promo_days,
    -- rang par chiffre d'affaires (1 = meilleur produit)
    rank() OVER (ORDER BY coalesce(t.revenue_eur, 0) DESC) AS revenue_rank
FROM raw.products p
LEFT JOIN totals t USING (product_id);

-- ---------------------------------------------------------------------
-- 4) État des stocks au dernier jour simulé (page "Stocks et ruptures")
--    Le statut est une RÈGLE simple (couverture de stock vs livraisons attendues),
--    pas une probabilité de rupture : le ML fera mieux en phase 6.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_stock_status AS
WITH today AS (
    SELECT max(simulated_date) AS as_of_date FROM raw.simulation_runs
),
recent AS (
    SELECT oi.product_id, oi.warehouse_id, sum(oi.requested_qty) / 28.0 AS avg_daily_demand_28d
    FROM raw.order_items oi
    JOIN raw.orders o USING (order_id)
    CROSS JOIN today
    WHERE (o.ordered_at AT TIME ZONE 'UTC')::date > today.as_of_date - 28
    GROUP BY 1, 2
),
transit AS (
    SELECT poi.product_id, po.warehouse_id,
           sum(poi.ordered_qty) AS in_transit_qty,
           min(po.expected_at)  AS next_expected_at
    FROM raw.purchase_orders po
    JOIN raw.purchase_order_items poi USING (po_id)
    WHERE po.status = 'in_transit'
    GROUP BY 1, 2
)
SELECT
    today.as_of_date,
    b.product_id, p.sku, p.name AS product_name, p.category,
    b.warehouse_id, w.name AS warehouse_name,
    b.on_hand_qty,
    coalesce(t.in_transit_qty, 0)               AS in_transit_qty,
    t.next_expected_at,
    s.lead_time_days                            AS supplier_lead_time_days,
    round(coalesce(r.avg_daily_demand_28d, 0), 2) AS avg_daily_demand_28d,
    CASE WHEN coalesce(r.avg_daily_demand_28d, 0) = 0 THEN NULL
         ELSE round(b.on_hand_qty / r.avg_daily_demand_28d, 1) END AS days_of_cover,
    CASE
        WHEN b.on_hand_qty = 0 THEN 'rupture'
        -- moins de 7 jours de stock ET aucune livraison attendue avant épuisement
        WHEN r.avg_daily_demand_28d > 0
             AND b.on_hand_qty / r.avg_daily_demand_28d < 7
             AND (t.next_expected_at IS NULL
                  OR t.next_expected_at > today.as_of_date
                                          + floor(b.on_hand_qty / r.avg_daily_demand_28d)::int)
             THEN 'critique'
        WHEN r.avg_daily_demand_28d > 0
             AND b.on_hand_qty / r.avg_daily_demand_28d > 60 THEN 'surstock'
        ELSE 'ok'
    END                                         AS stock_status,
    b.on_hand_qty * p.unit_cost                 AS stock_value_eur
FROM raw.inventory_balances b
JOIN raw.products   p ON p.product_id = b.product_id
JOIN raw.suppliers  s ON s.supplier_id = p.supplier_id
JOIN raw.warehouses w ON w.warehouse_id = b.warehouse_id
LEFT JOIN recent  r ON r.product_id = b.product_id AND r.warehouse_id = b.warehouse_id
LEFT JOIN transit t ON t.product_id = b.product_id AND t.warehouse_id = b.warehouse_id
CROSS JOIN today;

-- ---------------------------------------------------------------------
-- 5) Fiabilité des fournisseurs (page "Fournisseurs")
--    délai annoncé vs délai réellement constaté
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_supplier_reliability AS
SELECT
    s.supplier_id, s.name AS supplier_name, s.country,
    s.lead_time_days                                   AS announced_lead_time_days,
    count(po.po_id)                                    AS n_purchase_orders,
    count(po.po_id) FILTER (WHERE po.status = 'received')   AS n_received,
    count(po.po_id) FILTER (WHERE po.status = 'in_transit') AS n_in_transit,
    round(avg(po.received_at - po.ordered_at), 1)      AS avg_actual_lead_time_days,
    round(avg(po.received_at - po.expected_at), 1)     AS avg_delay_days,
    round(100.0 * count(po.po_id) FILTER (WHERE po.received_at > po.expected_at)
          / nullif(count(po.po_id) FILTER (WHERE po.status = 'received'), 0), 1)
                                                       AS late_rate_pct,
    max(po.received_at - po.expected_at)               AS max_delay_days
FROM raw.suppliers s
LEFT JOIN raw.purchase_orders po ON po.supplier_id = s.supplier_id
GROUP BY s.supplier_id, s.name, s.country, s.lead_time_days;

-- ---------------------------------------------------------------------
-- 6) Réceptions à venir (commandes fournisseurs en cours)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_incoming_receipts AS
SELECT
    po.po_id, po.supplier_id, s.name AS supplier_name,
    po.warehouse_id, w.name AS warehouse_name,
    poi.product_id, p.sku, p.name AS product_name,
    poi.ordered_qty, po.ordered_at, po.expected_at
FROM raw.purchase_orders po
JOIN raw.purchase_order_items poi USING (po_id)
JOIN raw.suppliers  s ON s.supplier_id = po.supplier_id
JOIN raw.warehouses w ON w.warehouse_id = po.warehouse_id
JOIN raw.products   p ON p.product_id = poi.product_id
WHERE po.status = 'in_transit';

-- ---------------------------------------------------------------------
-- 7) Calendrier des promotions
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_promotions AS
SELECT
    pr.promotion_id, pr.product_id, p.sku, p.name AS product_name, p.category,
    pr.announced_at, pr.starts_at, pr.ends_at,
    pr.ends_at - pr.starts_at + 1 AS duration_days,
    round(100 * pr.discount_pct, 0) AS discount_pct
FROM raw.promotions pr
JOIN raw.products p USING (product_id);

-- ---------------------------------------------------------------------
-- 8) Prévisions du modèle (page "Prévisions")
--    Une ligne = un produit, prévu à la fin de as_of_date pour les 7 jours suivants.
--    actual_qty reste vide tant que ces 7 jours ne sont pas terminés.
--    Alerte : stock + réceptions attendues sous 7 jours < demande prévue.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_forecasts AS
SELECT
    f.as_of_date,
    f.horizon_start,
    f.horizon_end,
    f.as_of_date = (SELECT max(as_of_date) FROM ml.forecasts)   AS is_latest,
    f.product_id, p.sku, p.name AS product_name, p.category,
    f.predicted_qty,
    fs.target_demand_next_7d                                    AS actual_qty,
    fs.target_demand_next_7d - f.predicted_qty                  AS error_qty,      -- > 0 : sous-estimé
    abs(fs.target_demand_next_7d - f.predicted_qty)             AS abs_error_qty,
    round(f.predicted_qty * p.unit_price, 2)                    AS predicted_revenue_eur,
    fs.stock_as_of,
    fs.incoming_qty_known_as_of                                 AS incoming_qty,
    fs.stock_as_of + fs.incoming_qty_known_as_of - f.predicted_qty AS projected_stock_end,
    CASE
        WHEN fs.stock_as_of + fs.incoming_qty_known_as_of < f.predicted_qty THEN 'rupture probable'
        WHEN fs.stock_as_of + fs.incoming_qty_known_as_of < 1.5 * f.predicted_qty THEN 'à surveiller'
        ELSE 'ok'
    END                                                         AS forecast_alert,
    f.run_id                                                    AS model_run_id
FROM ml.forecasts f
JOIN raw.products p USING (product_id)
LEFT JOIN ml.feature_snapshots fs
       ON fs.as_of_date = f.as_of_date AND fs.product_id = f.product_id;

-- ---------------------------------------------------------------------
-- 9) Qualité RÉELLE des prévisions, jour de prévision par jour de prévision
--    (seulement quand les 7 jours prévus sont terminés)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_forecast_accuracy AS
SELECT
    as_of_date,
    count(*)                                                    AS n_products,
    sum(predicted_qty)                                          AS predicted_qty,
    sum(actual_qty)                                             AS actual_qty,
    round(100.0 * sum(abs_error_qty) / nullif(sum(actual_qty), 0), 2) AS wape_pct,
    round(100.0 * sum(predicted_qty - actual_qty) / nullif(sum(actual_qty), 0), 2) AS bias_pct
FROM analytics.v_forecasts
WHERE actual_qty IS NOT NULL
GROUP BY as_of_date;

-- ---------------------------------------------------------------------
-- 10) Historique des entraînements nocturnes (sans le modèle lui-même)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_model_runs AS
SELECT
    run_id, trained_at, model_name, data_until, eval_start, eval_end, n_train_rows,
    round(100 * wape::numeric, 2)          AS wape_pct,
    round(100 * baseline_wape::numeric, 2) AS baseline_wape_pct,
    round(100 * bias::numeric, 2)          AS bias_pct,
    round(100 * champion_wape::numeric, 2) AS champion_wape_pct,
    promoted, decision,
    run_id = (SELECT run_id FROM ml.model_runs WHERE promoted ORDER BY trained_at DESC LIMIT 1) AS is_current
FROM ml.model_runs;

-- ---------------------------------------------------------------------
-- 11) Clients et leur statut (page "Clients")
--     Règle MÉTIER : un client est PERDU après 12 mois sans commande.
--     (La simulation ne stocke aucun indicateur « parti » : on l'observe.)
--     à risque = 6 à 12 mois sans commande ; nouveau = inscrit depuis < 90 jours.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_clients AS
WITH today AS (
    SELECT max(simulated_date) AS d FROM raw.simulation_runs
),
lines AS (
    SELECT o.client_id, o.order_id,
           (o.ordered_at AT TIME ZONE 'UTC')::date                  AS order_date,
           oi.fulfilled_qty * oi.unit_price                         AS revenue,
           oi.fulfilled_qty * (oi.unit_price - p.unit_cost)         AS margin
    FROM raw.orders o
    JOIN raw.order_items oi USING (order_id)
    JOIN raw.products p ON p.product_id = oi.product_id
),
o AS (
    SELECT l.client_id,
           min(order_date) AS first_order,
           max(order_date) AS last_order,
           count(DISTINCT order_id) AS n_orders,
           sum(revenue) AS revenue_eur,
           sum(margin)  AS margin_eur,
           -- 12 derniers mois / 12 mois précédents
           sum(revenue) FILTER (WHERE order_date >  today.d - 365)                              AS revenue_12m,
           sum(revenue) FILTER (WHERE order_date <= today.d - 365 AND order_date > today.d - 730) AS revenue_prev_12m,
           sum(margin)  FILTER (WHERE order_date >  today.d - 365)                              AS margin_12m,
           sum(margin)  FILTER (WHERE order_date <= today.d - 365 AND order_date > today.d - 730) AS margin_prev_12m,
           count(DISTINCT order_id) FILTER (WHERE order_date > today.d - 365)                   AS n_orders_12m
    FROM lines l CROSS JOIN today
    GROUP BY l.client_id
),
s AS (
    SELECT c.client_id, c.name, c.segment, c.city,
           (c.created_at AT TIME ZONE 'UTC')::date AS created_date,
           o.first_order, o.last_order,
           coalesce(o.n_orders, 0)          AS n_orders,
           coalesce(o.revenue_eur, 0)       AS revenue_eur,
           coalesce(o.margin_eur, 0)        AS margin_eur,
           coalesce(o.revenue_12m, 0)       AS revenue_12m,
           coalesce(o.revenue_prev_12m, 0)  AS revenue_prev_12m,
           coalesce(o.margin_12m, 0)        AS margin_12m,
           coalesce(o.margin_prev_12m, 0)   AS margin_prev_12m,
           coalesce(o.n_orders_12m, 0)      AS n_orders_12m,
           today.d - coalesce(o.last_order, (c.created_at AT TIME ZONE 'UTC')::date) AS days_without_order,
           today.d
    FROM raw.clients c
    LEFT JOIN o USING (client_id)
    CROSS JOIN today
),
ranked AS (
    SELECT s.*,
           -- part cumulée du CA 12 mois (clients triés du plus gros au plus petit)
           sum(revenue_12m) OVER (ORDER BY revenue_12m DESC, client_id
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
             / nullif(sum(revenue_12m) OVER (), 0)                 AS cumulative_share,
           rank() OVER (ORDER BY revenue_12m DESC)                 AS revenue_rank
    FROM s
)
SELECT client_id, name, segment, city, created_date, first_order, last_order,
       n_orders, revenue_eur, days_without_order,
       CASE
           WHEN days_without_order >= 365 THEN 'perdu'
           WHEN days_without_order >= 180 THEN 'à risque'
           WHEN created_date > d - 90       THEN 'nouveau'
           ELSE 'actif'
       END AS client_status,
       CASE WHEN days_without_order >= 365
            THEN coalesce(last_order, created_date) + 365 END AS lost_date,
       -- valeur et évolution
       margin_eur, revenue_12m, revenue_prev_12m, margin_12m, margin_prev_12m, n_orders_12m,
       round(100 * (revenue_12m - revenue_prev_12m) / nullif(revenue_prev_12m, 0), 1) AS revenue_change_pct,
       round(100 * (margin_12m  - margin_prev_12m)  / nullif(margin_prev_12m, 0), 1)  AS margin_change_pct,
       revenue_rank,
       -- Classe ABC (Pareto) sur le CA des 12 derniers mois
       CASE
           WHEN revenue_12m = 0            THEN 'sans achat (12 mois)'
           WHEN cumulative_share <= 0.80   THEN 'A - meilleurs clients'
           WHEN cumulative_share <= 0.95   THEN 'B - clients moyens'
           ELSE                                 'C - petits clients'
       END AS value_class,
       CASE
           WHEN revenue_prev_12m = 0 AND revenue_12m > 0                       THEN 'nouveau CA'
           WHEN revenue_prev_12m > 0 AND revenue_12m <= 0.8 * revenue_prev_12m THEN 'en baisse'
           WHEN revenue_prev_12m > 0 AND revenue_12m >= 1.2 * revenue_prev_12m THEN 'en hausse'
           WHEN revenue_prev_12m > 0                                           THEN 'stable'
           ELSE 'sans historique'
       END AS revenue_trend,
       -- À suivre : client à risque, ou bon client (A ou B) dont le CA baisse de 20 % ou plus
       (days_without_order BETWEEN 180 AND 364)
         OR (cumulative_share <= 0.95 AND revenue_12m > 0
             AND revenue_prev_12m > 0 AND revenue_12m <= 0.8 * revenue_prev_12m) AS to_follow
FROM ranked;

-- ---------------------------------------------------------------------
-- 12) Évolution mensuelle de la base clients
--     nouveaux = inscriptions du mois ; perdus = clients qui atteignent
--     12 mois sans commande ce mois-ci ; actifs_12m = au moins une commande
--     sur les 12 derniers mois (fin de mois).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_clients_monthly AS
WITH today AS (
    SELECT min(simulated_date) AS first_day, max(simulated_date) AS d FROM raw.simulation_runs
),
months AS (
    SELECT generate_series(date_trunc('month', first_day), date_trunc('month', d), interval '1 month')::date AS month
    FROM today
),
c AS (   -- v_clients calculée UNE seule fois
    SELECT created_date, lost_date FROM analytics.v_clients
),
new_c AS (
    SELECT date_trunc('month', created_date)::date AS month, count(*) AS n
    FROM c, today WHERE created_date >= first_day GROUP BY 1
),
lost_c AS (
    SELECT date_trunc('month', lost_date)::date AS month, count(*) AS n
    FROM c WHERE lost_date IS NOT NULL GROUP BY 1
),
activity AS (
    SELECT DISTINCT client_id, date_trunc('month', ordered_at AT TIME ZONE 'UTC')::date AS month
    FROM raw.orders
),
active AS (
    SELECT m.month, count(DISTINCT a.client_id) AS n
    FROM months m
    JOIN activity a ON a.month BETWEEN (m.month - interval '11 months')::date AND m.month
    GROUP BY m.month
)
SELECT m.month,
       coalesce(n.n, 0) AS new_clients,
       coalesce(l.n, 0) AS lost_clients,
       coalesce(a.n, 0) AS active_clients_12m
FROM months m
LEFT JOIN new_c  n ON n.month = m.month
LEFT JOIN lost_c l ON l.month = m.month
LEFT JOIN active a ON a.month = m.month;

-- ---------------------------------------------------------------------
-- 13) État du cycle automatique (une seule ligne) : « est-ce que tout a tourné ? »
--     Journée attendue = hier (heure de Paris), avec une marge jusqu'à 4 h du matin
--     pour laisser le cycle de 2 h 30 se terminer.
--     health_status : ok / en retard / échec / aucun passage
--     (même règle que smartsupply/health.py, utilisée pour l'alerte Windows)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_pipeline_health AS
WITH now_paris AS (
    SELECT (now() AT TIME ZONE 'Europe/Paris') AS t
),
expected AS (
    SELECT CASE WHEN t::time < time '04:00' THEN t::date - 2 ELSE t::date - 1 END AS expected_day
    FROM now_paris
),
last_run AS (
    SELECT finished_at, status, message FROM ops.pipeline_runs ORDER BY finished_at DESC LIMIT 1
),
last_ok AS (
    SELECT max(finished_at) AS finished_at FROM ops.pipeline_runs WHERE status = 'ok'
),
data AS (
    SELECT (SELECT max(simulated_date) FROM raw.simulation_runs) AS last_day,
           (SELECT max(as_of_date) FROM ml.forecasts)            AS last_forecast,
           (SELECT max(data_until) FROM ml.model_runs)           AS last_training
)
SELECT
    e.expected_day,
    d.last_day                                           AS last_simulated_day,
    d.last_forecast                                      AS last_forecast_day,
    d.last_training                                      AS last_training_data_until,
    lr.finished_at                                       AS last_run_at,
    lr.status                                            AS last_run_status,
    lo.finished_at                                       AS last_success_at,
    round(extract(epoch FROM now() - lo.finished_at) / 3600.0, 1) AS hours_since_success,
    CASE
        WHEN lr.status IS NULL                                     THEN 'aucun passage'
        WHEN lr.status = 'échec'                                   THEN 'échec'
        WHEN d.last_day IS NULL OR d.last_day < e.expected_day     THEN 'en retard'
        WHEN d.last_forecast IS NULL OR d.last_forecast < d.last_day THEN 'en retard'
        ELSE 'ok'
    END                                                  AS health_status,
    CASE
        WHEN lr.status IS NULL THEN 'Le cycle automatique n''a encore jamais tourné.'
        WHEN lr.status = 'échec' THEN 'Dernier passage en échec : ' || coalesce(lr.message, '?')
        WHEN d.last_day IS NULL OR d.last_day < e.expected_day
            THEN 'Données arrêtées au ' || coalesce(to_char(d.last_day, 'DD/MM/YYYY'), '?')
                 || ' (attendu : ' || to_char(e.expected_day, 'DD/MM/YYYY') || ')'
        WHEN d.last_forecast IS NULL OR d.last_forecast < d.last_day
            THEN 'Prévision du ' || to_char(d.last_day, 'DD/MM/YYYY') || ' manquante'
        ELSE 'Tout est à jour (données du ' || to_char(d.last_day, 'DD/MM/YYYY') || ')'
    END                                                  AS health_message
FROM expected e
CROSS JOIN data d
LEFT JOIN last_run lr ON true
LEFT JOIN last_ok  lo ON true;

-- ---------------------------------------------------------------------
-- 14) Surveillance du modèle, jour par jour (Grafana)
--     max_psi : dérive la plus forte du jour ; real_wape : erreur réelle de la
--     prévision faite ce jour-là (connue 7 jours plus tard).
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_monitoring_daily AS
SELECT as_of_date, model_run_id, max_psi, top_feature, n_watch, n_strong, real_wape, real_bias
FROM ml.monitoring_daily;

-- ---------------------------------------------------------------------
-- 15) Dérive de chaque variable, jour par jour (Grafana)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_drift_metrics AS
SELECT as_of_date, feature, psi, level, model_run_id
FROM ml.drift_metrics;

-- ---------------------------------------------------------------------
-- 16) Journal des passages du cycle (Grafana)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW analytics.v_pipeline_runs AS
SELECT run_id, started_at, finished_at,
       round(extract(epoch FROM finished_at - started_at)::numeric, 1) AS duration_s,
       status, days_added, model_status, forecast_status, message
FROM ops.pipeline_runs;

COMMIT;
