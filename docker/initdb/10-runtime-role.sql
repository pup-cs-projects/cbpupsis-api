-- Development-only credentials for the restricted API/worker role.
CREATE ROLE app_runtime LOGIN PASSWORD 'app_runtime_local_only';
GRANT CONNECT ON DATABASE app TO app_runtime;
