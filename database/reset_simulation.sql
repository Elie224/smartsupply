-- Efface TOUTE l'activité simulée pour repartir de zéro.
-- Conserve les données de référence (fournisseurs, produits, clients, entrepôts).
-- TRUNCATE n'est pas bloqué par le trigger append-only (qui vise UPDATE/DELETE) :
-- c'est volontaire, réservé au développement.
BEGIN;
TRUNCATE
    raw.stock_movements,
    raw.inventory_balances,
    raw.order_items,
    raw.orders,
    raw.purchase_order_items,
    raw.purchase_orders,
    raw.promotions,
    raw.simulation_runs
RESTART IDENTITY;
-- Le dataset ML dérive de l'activité : on le vide aussi (il sera reconstruit).
TRUNCATE ml.feature_snapshots;
-- Prévisions et modèles appris sur l'ancienne activité : ils n'ont plus de sens.
TRUNCATE ml.drift_metrics, ml.monitoring_daily, ml.forecasts, ml.model_runs;
-- Journal du cycle automatique.
TRUNCATE ops.pipeline_runs RESTART IDENTITY;
-- Clients inscrits PENDANT la simulation (les clients de référence sont créés avant 2024).
DELETE FROM raw.clients WHERE created_at >= '2024-01-01';
SELECT setval(pg_get_serial_sequence('raw.clients', 'client_id'),
              greatest((SELECT max(client_id) FROM raw.clients), 1));
COMMIT;
