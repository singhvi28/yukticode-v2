# YuktiCode v2

A deliberately smaller rewrite of the online judge. Postgres is the only stateful
service: it stores application data *and* the job queue. There is no RabbitMQ, no
Redis, no WebSocket/SSE stream, and no separate reaper process.

```
Browser
  │
  ▼
Nginx :8080                       (frontend container)
  ├── /api/*  ────────────────►  FastAPI :9000  ──────┐
  └── /*      ────────────────►  React SPA            │
                                                      ▼
                                                   Postgres
                                                  (data + judge_jobs)
                                                        ▲
                                                        │ FOR UPDATE SKIP LOCKED
                                                        │
                                              ┌─────────┴─────────┐
                                              │  worker container  │
                                              │  claims one job   │
                                              └─────────┬─────────┘
                                                        │ Docker socket
                                                        ▼
                                        one fresh, read-only, network-less
                                        container per test case
```

## What changed from v1

- **One queue:** `judge_jobs` in Postgres. Workers claim jobs with
  `SELECT … FOR UPDATE SKIP LOCKED`; leases recover from crashes.
- **One worker service** handles submits, single runs, batch runs, and admin
  dry-runs. The old submit/run/reaper split is gone.
- **Polling frontend:** submissions and runs are polled with `GET /submissions/{id}`
  and `GET /runs/{id}`. Contest standings are polled too.
- **Auth is required** for every code-execution endpoint. Jobs are owned by the
  user who created them and cannot be read by another user.
- **Fail-closed secrets:** `JWT_SECRET` is required at import time and must be
  32+ characters. The seed script requires `ADMIN_PASSWORD` when it creates the
  first admin.
- **Hardened judge:** one container per test, `--network none`, read-only root,
  tmpfs workspace, dropped capabilities, no-new-privileges, PID/CPU/memory/file
  limits, capped output, fixed runner script, and expected output never enters
  the container.
- **Deterministic standings** are computed from `submissions` at read time.
- **Alembic** owns the schema; `seed.py` is idempotent and never drops tables.

## Quick start (Docker Compose)

Requirements: Docker Engine with Compose v2. The worker needs access to the host
Docker daemon so it can create the per-test containers.

```bash
cd cf-clone-v2

cp .env.example .env
# Edit .env:
#   POSTGRES_PASSWORD=<url-safe password>
#   JWT_SECRET=$(openssl rand -hex 32)
#   ADMIN_PASSWORD=<admin password>

docker compose up --build -d

# The backend applies Alembic migrations on startup. After it is healthy:
docker compose exec backend python seed.py

open http://localhost:8080
```

The first submission may take a little longer because the worker builds the inner
judge image (`cf-clone-v2-judge:latest`) on first use. You can build it ahead of
time:

```bash
docker compose exec worker python -c \
  "from worker import judger; judger.ensure_image(judger._get_client())"
```

### gVisor (recommended for public deployments)

If the host has the `runsc` runtime registered, leave `JUDGE_RUNTIME=auto` and the
worker will use it. Otherwise it falls back to plain runc. The other container
restrictions still apply.

## Local development

Backend:

```bash
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export JWT_SECRET="$(openssl rand -hex 32)"
export DATABASE_URL="postgresql+asyncpg://judge:judge@localhost:5432/judge"
python -m alembic upgrade head
ADMIN_PASSWORD=change-me python seed.py
uvicorn server.main:app --reload --port 9000
```

Worker (needs the Docker socket and a reachable Postgres):

```bash
cd backend
export JWT_SECRET="$(openssl rand -hex 32)"
export DATABASE_URL="postgresql+asyncpg://judge:judge@localhost:5432/judge"
export PYTHONPATH="$PWD"
python -m worker.main
```

Frontend:

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173, proxies /api to :9000
```

## API summary

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/auth/register` | Create an account |
| `POST` | `/auth/login` | JSON `{username, password}` → bearer token |
| `GET` | `/auth/me` | Current user |
| `POST` | `/submit` | Auth + queue a submission |
| `GET` | `/submissions/{id}` | Poll one submission |
| `GET` | `/submissions` | Current user's submissions |
| `POST` | `/run` | Auth + single custom run |
| `POST` | `/run_batch` | Auth + up to 20 custom tests |
| `GET` | `/runs/{id}` | Poll single run or batch |
| `GET` | `/problems`, `/problems/{id}` | Published problems |
| `GET` | `/contests` | Public list; registration info for the current user |
| `GET` | `/contests/{id}` | Public detail + per-user progress |
| `POST` | `/contests/{id}/register` | Auth |
| `GET` | `/contests/{id}/leaderboard` | Auth; computed from submissions |
| `*` | `/admin/*` | Admin-only CRUD and dry-run polling |

Job states are `queued` → `running` → `done`/`failed`. A submission is `PENDING`
until the worker writes its verdict.

## Security notes

- Do not expose the Docker socket to anything except the worker. A process with
  the socket can control the host Docker daemon.
- Use a unique `JWT_SECRET`; leaking it lets an attacker mint tokens.
- Set a strong `ADMIN_PASSWORD`; it is only read when the admin is first created.
- The judge image contains compilers/interpreters, not the expected outputs.
- The API limits source size, test size, language choices, active jobs per user,
  login attempts, and judge output size.

## Tests

```bash
cd backend
JWT_SECRET="$(openssl rand -hex 32)" python -m pytest -q
```

Frontend:

```bash
cd frontend
npm ci
npm run build
```
