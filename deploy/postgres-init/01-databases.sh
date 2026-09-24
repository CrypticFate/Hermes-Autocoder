#!/bin/bash
# Runs once on an empty data volume. Separate roles: autocoder owns autocoder, mem0 owns mem0 only.
set -euo pipefail
autocoder_password=$(cat /run/secrets/db_autocoder_password)
mem0_password=$(cat /run/secrets/db_mem0_password)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
     -v autocoder_password="$autocoder_password" -v mem0_password="$mem0_password" <<'SQL'
CREATE ROLE autocoder LOGIN PASSWORD :'autocoder_password';
CREATE ROLE mem0 LOGIN PASSWORD :'mem0_password';
CREATE DATABASE autocoder OWNER autocoder;
CREATE DATABASE mem0 OWNER mem0;
REVOKE ALL ON DATABASE autocoder FROM PUBLIC;
REVOKE ALL ON DATABASE mem0 FROM PUBLIC;
GRANT CONNECT ON DATABASE autocoder TO autocoder;
GRANT CONNECT ON DATABASE mem0 TO mem0;
\connect mem0
CREATE EXTENSION IF NOT EXISTS vector;
SQL
