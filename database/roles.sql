-- =====================================================================
-- Rôle LECTURE SEULE pour Power BI (README section 16).
-- Il ne voit que le schéma analytics : impossible de lire ou modifier raw.
--
-- Le mot de passe n'est PAS écrit ici : on le passe en paramètre.
--   psql ... -v reader_password='TonMotDePasse' -f /database/roles.sql
-- =====================================================================

-- Crée le rôle s'il n'existe pas (\gexec exécute la requête générée).
SELECT format('CREATE ROLE powerbi_reader LOGIN PASSWORD %L', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'powerbi_reader')
\gexec

-- Si le rôle existe déjà, on met simplement le mot de passe à jour.
ALTER ROLE powerbi_reader WITH LOGIN PASSWORD :'reader_password';

GRANT CONNECT ON DATABASE smartsupply TO powerbi_reader;
GRANT USAGE ON SCHEMA analytics TO powerbi_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO powerbi_reader;
-- Les futures vues analytics seront aussi lisibles automatiquement.
ALTER DEFAULT PRIVILEGES IN SCHEMA analytics GRANT SELECT ON TABLES TO powerbi_reader;

-- Par sécurité : aucun accès aux données brutes.
REVOKE ALL ON SCHEMA raw FROM powerbi_reader;
