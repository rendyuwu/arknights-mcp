#!/bin/sh
# First start of an empty account-db volume only (ADR 0020). Creates:
#   arknights_writer -- CLI `account sync|purge` on the sync machine
#   arknights_reader -- `serve`: SELECT only, read-only transactions by default
# To change a password later: `docker compose ... exec account-db psql -U arknights_admin
#   -d arknights_account -c "ALTER ROLE arknights_reader PASSWORD '...'"`.
: "${ARKNIGHTS_ACCOUNT_WRITER_PASSWORD:?set it in deploy/docker/account-db.env}"
: "${ARKNIGHTS_ACCOUNT_READER_PASSWORD:?set it in deploy/docker/account-db.env}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v writer_pw="$ARKNIGHTS_ACCOUNT_WRITER_PASSWORD" \
  -v reader_pw="$ARKNIGHTS_ACCOUNT_READER_PASSWORD" <<'SQL'
CREATE ROLE arknights_writer LOGIN PASSWORD :'writer_pw';
CREATE ROLE arknights_reader LOGIN PASSWORD :'reader_pw';
ALTER ROLE arknights_reader SET default_transaction_read_only = on;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO arknights_writer;
GRANT USAGE ON SCHEMA public TO arknights_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE arknights_writer IN SCHEMA public
  GRANT SELECT ON TABLES TO arknights_reader;
SQL
