-- First-boot local/CI role only. Existing volumes require explicit provisioning.
-- The migration owner grants application-table permissions after schema creation.
\getenv runtime_password APP_RUNTIME_PASSWORD
CREATE ROLE app_runtime LOGIN PASSWORD :'runtime_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
