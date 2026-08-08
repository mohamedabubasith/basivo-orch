#!/bin/sh
# Wait for Postgres, apply migrations, then hand off to the CMD.
#
# Migrations run here rather than in a separate job because this deployment is
# a single instance with a single API container: there is no second replica to
# race with. The moment a second one exists this must move to a one-shot task,
# or two containers will try to take the same Alembic lock on boot.
set -eu

echo "waiting for the database…"
attempt=0
until python -c "
import asyncio, os, sys
from sqlalchemy.ext.asyncio import create_async_engine

async def probe() -> None:
    engine = create_async_engine(os.environ['DATABASE_URL'])
    async with engine.connect():
        pass
    await engine.dispose()

try:
    asyncio.run(probe())
except Exception as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
"; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 60 ]; then
        echo "database never became reachable" >&2
        exit 1
    fi
    sleep 2
done

echo "applying migrations…"
alembic upgrade head

echo "starting the api"
exec "$@"
