-- setup the schema
DROP SCHEMA IF EXISTS risk_platform CASCADE;

CREATE SCHEMA risk_platform;
SET search_path TO risk_platform;
SELECT
    current_database() AS database_name,
    current_schema() AS active_schema;