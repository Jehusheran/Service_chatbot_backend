cat > entrypoint.sh <<'ENTRY'
#!/usr/bin/env bash
set -euo pipefail

host="${DB_HOST:-db}"
port="${DB_PORT:-5432}"
user="${POSTGRES_USER:-postgres}"

echo "Waiting for postgres at ${host}:${port}..."
for i in {1..60}; do
  if pg_isready -h "${host}" -p "${port}" -U "${user}" >/dev/null 2>&1; then
    echo "Postgres is ready."
    break
  fi
  echo "Postgres not ready yet... (${i})"
  sleep 1
done

# Attempt sync DB init if available
python - <<'PY'
import importlib
try:
    db_mod = importlib.import_module("app.db")
    if hasattr(db_mod, "init_db_sync"):
        print("Running init_db_sync() ...")
        db_mod.init_db_sync()
    else:
        if hasattr(db_mod, "init_db"):
            import asyncio
            print("Running async init_db() ...")
            res = db_mod.init_db()
            if hasattr(res, "__await__"):
                asyncio.get_event_loop().run_until_complete(res)
except Exception as e:
    print("DB init step failed (continuing):", e)
PY

if [ "${FLASK_ENV:-production}" = "development" ]; then
  export FLASK_APP=app.main
  flask run --host="${FLASK_RUN_HOST:-0.0.0.0}" --port="${FLASK_RUN_PORT:-4000}"
else
  exec gunicorn --bind "${FLASK_RUN_HOST:-0.0.0.0}:${FLASK_RUN_PORT:-4000}" "app.main:app" --workers 3 --threads 4 --log-level info
fi
ENTRY

chmod +x entrypoint.sh
