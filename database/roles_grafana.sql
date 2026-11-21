-- =====================================================================
-- Rôle LECTURE SEULE pour Grafana : comme Power BI, il ne voit que le
-- schéma analytics (vues), jamais les tables raw / ml / ops directement.
--
-- Le mot de passe n'est PAS écrit ici : c'est celui de GRAFANA_DB_PASSWORD (.env).
--   docker compose exec postgres psql -U smartsupply -d smartsupply \
--       -v grafana_password='LeMêmeQueDansEnv' -f /database/roles_grafana.sql
-- =====================================================================

SELECT format('CREATE ROLE grafana_reader LOGIN PASSWORD %L', :'grafana_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader')
\gexec

ALTER ROLE grafana_reader WITH LOGIN PASSWORD :'grafana_password';

GRANT CONNECT ON DATABASE smartsupply TO grafana_reader;
GRANT USAGE ON SCHEMA analytics TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO grafana_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO grafana_reader;

REVOKE ALL ON SCHEMA raw FROM grafana_reader;
-- Une requête lourde ne peut pas bloquer la base plus de 30 secondes.
ALTER ROLE grafana_reader SET statement_timeout = '30s';
