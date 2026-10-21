# YuktiCode v2 — System Architecture Reference

> **Role & Purpose**: Technical system reference for the YuktiCode v2 online judge codebase. Prepared for a Senior Backend / Distributed Systems Engineer defending the architecture in a technical interview.

---

## 1. System Overview

YuktiCode v2 is an asynchronous, sandbox-isolated online judge designed with a minimal operational footprint. Unlike conventional judge architectures that introduce message brokers (RabbitMQ/Kafka), caching layers (Redis/Memcached), WebSocket state channels, and dedicated background daemon processes (e.g. Celery beat or Celery workers), YuktiCode v2 unifies persistence, distributed leasing, job queuing, and state recovery directly inside **PostgreSQL 16**.

### Primary User Flows
1. **Submit Code Flow**: A candidate submits source code against a published problem or active contest (`POST /submit`). The backend persists a `PENDING` record in `submissions`, enqueues a `submit` job into `judge_jobs` with priority 10 within the same database transaction, and returns HTTP 202. The client polls `GET /submissions/{id}` until terminal status (`AC`, `WA`, `TLE`, `MLE`, `RE`, `CE`, `SYSTEM_ERROR`).
2. **Custom / Batch Run Flow**: A candidate executes code against sample or ad-hoc test cases (`POST /run` or `POST /run_batch`). The backend validates size constraints, checks concurrency limits (max 5 active jobs per user), inserts a `JudgeJob` without creating a `Submission`, and returns HTTP 202. The frontend polls `GET /runs/{id}`.
3. **Contest Lifecycle & Standings Flow**: Contestants register (`POST /contests/{id}/register`). Contest start/end timestamps gate submission and problem statement access. Leaderboards are queried via `GET /contests/{id}/leaderboard`, where standings are dynamically computed directly from raw submission records using ICPC penalty scoring.
4. **Admin Problem & Test Case Authoring**: Admins create/update problems and test cases, execute single-test dry runs (`POST /admin/problems/{id}/testcases/{id}/run`), and poll output via `GET /admin/run-result/{run_id}` before publishing.

---

## 2. System Architecture & Runtime Topology

```mermaid
flowchart TD
    subgraph Client["Client Tier"]
        Browser["React 19 SPA (Monaco Editor, Allotment)"]
    end

    subgraph Edge["Edge / Reverse Proxy Tier"]
        Nginx["Nginx 1.25 Alpine (:8080)<br/>- Static Asset Caching<br/>- Gzip Compression<br/>- Rate Limiting Header Forwarding<br/>- Proxy to FastAPI :9000"]
    end

    subgraph API["Application Tier (Async I/O)"]
        FastAPI["FastAPI 0.128 + Uvicorn 0.40 (:9000)<br/>- JWT Auth (HS256, Argon2)<br/>- Sliding Window Rate Limiting<br/>- Ingress Payload Validation (Pydantic)<br/>- Contest Gates & Concurrency Guards"]
    end

    subgraph Data["Persistence & Broker Tier"]
        Postgres[("PostgreSQL 16 Alpine<br/>- Application Schemas<br/>- judge_jobs Table (SKIP LOCKED)<br/>- Atomicity & Terminal Verdicts")]
    end

    subgraph Worker["Execution & Worker Tier"]
        WorkerProcess["Worker Daemon (worker.main)<br/>- Lease Claiming (FOR UPDATE SKIP LOCKED)<br/>- Crash Recovery (requeue_expired_jobs)<br/>- Docker API Socket Controller"]
    end

    subgraph Sandboxes["Isolation Tier (Transient Containers)"]
        Box1["Docker Sandbox (Test 1)<br/>read-only root, tmpfs /workspace<br/>net=none, cap-drop ALL, pids=64"]
        BoxN["Docker Sandbox (Test N)<br/>runsc (gVisor) / runc<br/>ulimit fsize 2MB, no-new-privileges"]
    end

    Browser -->|HTTP Requests| Nginx
    Nginx -->|/api/*| FastAPI
    Nginx -->|/*| Browser
    FastAPI -->|asyncpg / SQLAlchemy 2.0| Postgres
    WorkerProcess -->|Claim & Lease Renewal| Postgres
    WorkerProcess -->|/var/run/docker.sock| Sandboxes
```

### Component Breakdown

| Component | Technology | Role & Boundary |
|---|---|---|
| **Edge / Proxy** | Nginx Alpine | Serves React static bundle; terminates HTTP on `:8080`; rewrites `/api/*` to FastAPI `:9000`; sets `X-Real-IP`, security headers (`X-Frame-Options: SAMEORIGIN`, `nosniff`). |
| **API Server** | FastAPI, Uvicorn, Python 3.12 | Enforces authentication, payload limits, contest gate conditions, and user concurrency quotas (`MAX_ACTIVE_JOBS = 5`). Enqueues jobs transactionally into Postgres. |
| **State & Broker** | PostgreSQL 16 | Single source of truth. Stores relational schema, application entities, and acts as the concurrency-safe FIFO/priority task queue (`judge_jobs`). |
| **Judge Worker** | Python 3.12, Docker SDK | Autonomous daemon. Claims queued jobs with `FOR UPDATE SKIP LOCKED`, schedules container executions, parses resource telemetry, writes verdicts, and cleans up stale containers. |
| **Judge Sandbox** | Debian Bookworm Slim / runsc | Ephemeral execution jail. One container per test case. Network-less, read-only root, dropped capabilities, unprivileged UID (`65534`). Expected outputs are never copied inside. |
| **Frontend** | React 19, Vite 7, Monaco Editor | Single-page application. Handles code editing, language switching, batch test runners, and poll loops using monotonic request tokens. |

---

## 3. Technology Stack & Configuration

### Dependencies & Versions
- **Python Runtime**: `3.12-slim` ([`backend/Dockerfile`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/Dockerfile))
- **Web Framework**: `fastapi==0.128.0`, `uvicorn==0.40.0`
- **Data Access**: `sqlalchemy==2.0.52`, `asyncpg==0.31.0`, `alembic==1.18.4`
- **Security & Crypto**: `passlib[argon2]==1.7.4`, `python-jose[cryptography]==3.5.0`
- **Container Control**: `docker==7.1.0`
- **Frontend Stack**: `react@19.2.0`, `vite@7.3.1`, `@monaco-editor/react@4.7.0`, `axios@1.13.6`
- **Test Suite**: `pytest==8.3.4`, `pytest-asyncio==1.3.0`

### Key Configuration Variables ([`backend/server/config.py`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/config.py))

| Environment Variable | Default | Purpose / Operational Implication |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://judge:judge@postgres:5432/judge` | Async connection string for SQLAlchemy engine (`pool_pre_ping=True`). |
| `JWT_SECRET` | *(None — Required)* | HMAC signing key; requires **≥32 characters** at boot or application raises `RuntimeError` (fail-closed). |
| `JWT_ALGORITHM` | `HS256` | Symmetric signature algorithm for access tokens. |
| `JWT_EXPIRE_MINUTES` | `60` | Lifespan of generated bearer tokens. |
| `JOB_LEASE_SECONDS` | `600` (10 min) | Worker lease duration before a stalled or dead worker's job is eligible for recovery. |
| `JOB_MAX_ATTEMPTS` | `3` | Maximum retry attempts before marking a job permanently `failed` (`SYSTEM_ERROR`). |
| `JUDGE_IMAGE` | `cf-clone-v2-judge:latest` | Tag of the inner sandbox container built from [`backend/worker/judger_image/Dockerfile`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger_image/Dockerfile). |
| `JUDGE_RUNTIME` | `auto` | Container runtime engine: detects `runsc` (gVisor) in Docker daemon, falling back to `runc`. |
| `JUDGE_MAX_OUTPUT_BYTES` | `102400` (100 KiB) | Capped read threshold for `stdout`/`stderr`/compile logs before output truncation. |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080` | Allowed browser origins for development proxies. |

---

## 4. API & Interface Design

### Authentication & Authorization Trust Boundaries
- **Unauthenticated**: Registration (`POST /auth/register`), Login (`POST /auth/login`), Problem listing/details (`GET /problems`, `GET /problems/{id}`), Public contest metadata (`GET /contests`, `GET /contests/{id}`).
- **Authenticated (`get_current_user`)**: Code submission (`POST /submit`), code execution (`POST /run`, `POST /run_batch`), submission polling (`GET /submissions/{id}`), contest registration (`POST /contests/{id}/register`), contest standings (`GET /contests/{id}/leaderboard`).
- **Admin Only (`admin_required`)**: Problem CRUD, test case authoring, contest setup, and admin dry-run test polling (`/admin/*`).
- **Owner Isolation**: Users can only query their own runs and submissions. Non-admin requests for records owned by other users return HTTP 404 to avoid leaking ID presence.

### Complete REST API Route Map

```
POST   /auth/register                       -> Register user account (201)
POST   /auth/login                          -> Authenticate with username & password -> Bearer JWT
GET    /auth/me                             -> Current user profile

POST   /submit                              -> Queue code submission against problem (202)
GET    /submissions/{id}                    -> Poll submission execution status & metrics
GET    /submissions                         -> List user's past submissions (descending date)

POST   /run                                 -> Queue single custom code execution (202)
POST   /run_batch                           -> Queue multi-test batch execution (1-20 tests) (202)
GET    /runs/{id}                           -> Poll custom/batch execution result

GET    /problems                            -> List published problems with acceptance stats
GET    /problems/{id}                       -> Fetch published problem statement and sample tests

GET    /contests                            -> List published contests with user registration flag
GET    /contests/{id}                       -> Contest details, problem list, and user solve status
POST   /contests/{id}/register              -> Register current user for contest
GET    /contests/{id}/leaderboard           -> Dynamic ICPC standings computed on read

GET    /admin/problems                      -> [Admin] List all problems (published & drafts)
POST   /admin/problems                      -> [Admin] Create problem draft
GET    /admin/problems/{id}                 -> [Admin] Retrieve problem metadata
PATCH  /admin/problems/{id}                 -> [Admin] Update problem statement/limits/publishing
DELETE /admin/problems/{id}                 -> [Admin] Delete problem and cascading associations
GET    /admin/problems/{id}/testcases       -> [Admin] List test cases for problem
POST   /admin/problems/{id}/testcases       -> [Admin] Create test case
PUT    /admin/problems/{id}/testcases/{tid} -> [Admin] Update test case
DELETE /admin/problems/{id}/testcases/{tid} -> [Admin] Delete test case
POST   /admin/problems/{id}/testcases/{tid}/run -> [Admin] Queue dry run on single test case
GET    /admin/run-result/{run_id}           -> [Admin] Poll dry run result
GET    /admin/contests                      -> [Admin] List all contests
POST   /admin/contests                      -> [Admin] Create contest
GET    /admin/contests/{id}                 -> [Admin] Contest details and problem associations
PATCH  /admin/contests/{id}                 -> [Admin] Update contest metadata/times
DELETE /admin/contests/{id}                 -> [Admin] Delete contest
POST   /admin/contests/{id}/problems        -> [Admin] Associate problem with contest
DELETE /admin/contests/{id}/problems/{pid}  -> [Admin] Remove problem association

GET    /health                              -> Readiness probe (checks `SELECT 1` on Postgres)
```

---

## 5. Data Model & Storage Design

The schema is defined via SQLAlchemy declarative models in [`backend/server/db/models.py`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/db/models.py) and version-controlled via Alembic in [`backend/server/alembic/versions/0001_initial.py`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/alembic/versions/0001_initial.py).

```mermaid
erDiagram
    User ||--o{ Problem : "authors"
    User ||--o{ Submission : "submits"
    User ||--o{ Contest : "creates"
    User ||--o{ ContestRegistration : "registers"
    User ||--o{ JudgeJob : "owns"

    Problem ||--o{ TestCase : "contains"
    Problem ||--o{ Submission : "tested against"
    Problem ||--o{ ContestProblem : "associated with"

    Contest ||--o{ ContestProblem : "includes"
    Contest ||--o{ ContestRegistration : "enrolled"
    Contest ||--o{ Submission : "scoped to"

    Submission ||--o| JudgeJob : "triggered by"
```

### Table Definitions & Indexing Rationale

#### 1. `judge_jobs` (The Transactional Job Queue)
- **Columns**: `id` (VARCHAR(36) UUID PK), `kind` (VARCHAR(20) - `submit`, `run`, `run_batch`), `status` (VARCHAR(20) - `queued`, `running`, `done`, `failed`), `priority` (INT, default 100, submit=10, dry-run=20), `user_id` (FK users.id), `submission_id` (FK submissions.id), `payload` (JSON), `result` (JSON), `attempts` (INT, default 0), `locked_by` (VARCHAR(100)), `locked_until` (TIMESTAMPTZ), `available_at` (TIMESTAMPTZ), `last_error` (TEXT), `created_at` (TIMESTAMPTZ), `updated_at` (TIMESTAMPTZ).
- **Critical Index `ix_judge_jobs_claim`**: `("status", "available_at", "priority", "created_at")`. Enables index-scan execution for the CTE:
  ```sql
  SELECT id FROM judge_jobs
  WHERE status = 'queued' AND available_at <= now()
  ORDER BY priority, available_at, created_at
  FOR UPDATE SKIP LOCKED LIMIT 1;
  ```
- **Index `ix_judge_jobs_user`**: `("user_id", "created_at")`. Supports O(1) checks for `_check_active_jobs` (enforcing max 5 concurrent jobs per user).

#### 2. `submissions`
- **Columns**: `id` (INT PK), `user_id` (FK users.id), `problem_id` (FK problems.id), `language` (VARCHAR(20)), `code` (TEXT), `status` (VARCHAR(20) default `PENDING`), `execution_time_ms` (FLOAT), `peak_memory_mb` (FLOAT), `message` (TEXT), `submitted_at` (TIMESTAMPTZ), `contest_id` (FK contests.id).
- **Composite Index `ix_submissions_contest_user_problem_time`**: `("contest_id", "user_id", "problem_id", "submitted_at")`. Tailor-made for the dynamic ICPC leaderboard query, filtering out-of-window submissions and pre-sorting rows.
- **Index `ix_submissions_user_time`**: `("user_id", "submitted_at")`. Accelerates user submission history pagination.

#### 3. `problems` & `test_cases`
- `problems`: `id`, `title` (UNIQUE), `author_id`, `statement`, `time_limit_ms` (default 2000), `memory_limit_mb` (default 256), `difficulty`, `tags` (JSON string), `is_published` (BOOLEAN).
- `test_cases`: `id`, `problem_id` (FK CASCADE, INDEXED), `input_data` (TEXT), `expected_output` (TEXT), `is_sample` (BOOLEAN), `score` (INT default 10).

#### 4. `contests`, `contest_problems`, `contest_registrations`
- `contest_registrations`: Unique composite constraint `("contest_id", "user_id")`.
- `contest_problems`: Unique composite constraint `("contest_id", "problem_id")`, stores `score` (default 100) and `display_order`.

---

## 6. End-to-End Core Flow Walkthrough

```mermaid
sequenceDiagram
    autonumber
    actor Candidate as Candidate Browser
    participant API as FastAPI (:9000)
    participant DB as PostgreSQL 16
    participant Worker as Worker Daemon
    participant HostDocker as Host Docker Socket
    participant Sandbox as Transient Container

    Candidate->>API: POST /submit {problem_id, language, src_code, contest_id}
    Note over API: 1. Validate payload bounds<br/>2. Contest gate check<br/>3. User active jobs check (<= 5)
    API->>DB: INSERT INTO submissions (status='PENDING')
    API->>DB: INSERT INTO judge_jobs (kind='submit', priority=10, payload=...)
    DB-->>API: commit transaction
    API-->>Candidate: 202 Accepted {submission_id, job_id}

    loop Background Claim Loop (worker.main)
        Worker->>DB: requeue_expired_jobs()
        Worker->>DB: claim_job() via FOR UPDATE SKIP LOCKED
    end
    DB-->>Worker: Return Job Row (status='running', locked_until=now()+600s)

    Worker->>DB: fetch_submission_data(submission_id)
    DB-->>Worker: Return {source, test_cases, limits}

    loop For Each Test Case in Problem
        Worker->>HostDocker: Create Container (net=none, ro-root, tmpfs, ulimits)
        HostDocker-->>Worker: Container ID
        Worker->>Sandbox: exec cat > /workspace/main.{ext} & input.txt
        Worker->>Sandbox: exec /usr/local/bin/run-judge py|cpp|java <time_limit> <mem_limit>
        Sandbox-->>Sandbox: Compile & execute with /usr/bin/time & timeout
        Worker->>Sandbox: exec head -c 102401 stdout.txt & time.txt
        Worker->>HostDocker: container.remove(force=True)
        Note over Worker: Compare stdout with expected.<br/>Break on first non-AC (TLE, MLE, WA, RE).
    end

    alt Judging Completed Normally
        Worker->>DB: complete_job(job_id, result, submission_id)
        Note over DB: UPDATE judge_jobs SET status='done'<br/>UPDATE submissions SET status='AC|WA|TLE' WHERE status='PENDING'
    else Worker Crashes or Exception
        Worker->>DB: fail_job(job_id, error, submission_id)
    end

    Candidate->>API: GET /submissions/{submission_id} (Polled every 1s)
    API->>DB: SELECT status, metrics FROM submissions WHERE id=:id
    DB-->>API: Return {status: 'AC', execution_time_ms: 12.4, peak_memory_mb: 18.2}
    API-->>Candidate: JSON verdict rendered in SubmissionConsole
```

### Critical Code Reference Points
- Submission queuing: [`backend/server/routes.py:130-178`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/routes.py#L130-L178) (`submit`).
- Atomic Claiming CTE: [`backend/server/jobs.py:27-58`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L27-L58) (`claim_job`).
- Safe File Injection via Exec: [`backend/worker/judger.py:78-110`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L78-L110) (`_write_file`).
- Sandbox Construction: [`backend/worker/judger.py:163-277`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L163-L277) (`run_case`).
- Fixed Runner Script: [`backend/worker/judger_image/run-judge:1-55`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger_image/run-judge#L1-L55).
- Atomic Completion Guard: [`backend/server/jobs.py:60-120`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L60-L120) (`complete_job`).

---

## 7. State & Storage Lifecycle

| State Category | Storage Medium | Lifetime | Invalidation / Cleanup Strategy |
|---|---|---|---|
| **User & Problem Data** | Postgres (`users`, `problems`, `test_cases`) | Permanent | Explicit admin deletion via cascade foreign keys. |
| **Submissions** | Postgres (`submissions`) | Permanent | Append-only audit trail; status advances `PENDING` → terminal verdict. |
| **Queued Jobs** | Postgres (`judge_jobs`) | Ephemeral-to-Permanent | Initial state `queued`; claimed to `running`; transitions to `done` or `failed`. |
| **Authentication Tokens** | Client `localStorage` (`access_token`) | 60 minutes (`JWT_EXPIRE_MINUTES`) | Cryptographic expiration (`exp` claim); purged on 401 response interceptor. |
| **Login Rate Limit Counters** | Backend In-Memory (`_login_attempts`) | 300-second sliding window | Expired bucket entries pruned on every attempt; cleared on successful auth. |
| **Judge Workspaces** | Linux RAM (`tmpfs` in containers) | Container lifetime (~1-3s) | Destroyed upon container termination and `tmpfs` unmount. |
| **Orphaned Containers** | Host Docker Engine | Up to 20 minutes | Swept by `reap_orphaned_containers()` filter `cfclone.judge=1` every 300s. |

---

## 8. Concurrency, Scaling & Coordination

### 1. Inter-Instance Coordination without Distributed Locks
YuktiCode v2 eliminates Redis locks (`Redlock`), ZooKeeper, or etcd clusters. Worker coordination is achieved purely through PostgreSQL row-level locks using `SKIP LOCKED`:
- Multiple worker processes (across one or more host machines) execute `claim_job()` concurrently.
- Postgres identifies the highest-priority, oldest available job, locks the row in the CTE, updates its status to `running`, marks the worker ID, and immediately releases the table lock while maintaining the row lock across the atomic transaction.
- Competing workers skip already-locked rows without blocking or serialization delays.

### 2. Guarding against Zombie Worker Overwrites
If Worker A experiences a network partition or heavy swapping, its lease (`locked_until`) will expire. Worker B reclaims the job via `requeue_expired_jobs()`. If Worker A subsequently wakes up and attempts to complete the job:
```python
# backend/server/jobs.py
updated = await db.execute(
    text("""
        UPDATE judge_jobs
        SET status = 'done', result = CAST(:result AS json),
            locked_by = NULL, locked_until = NULL, updated_at = now()
        WHERE id = :job_id AND status = 'running' AND locked_by = :worker_id
    """),
    {"job_id": job_id, "worker_id": worker_id, ...}
)
if not updated.rowcount:
    await db.rollback()
    return
```
Because Worker A's worker ID is no longer present in `locked_by` (or status is no longer `running`), `updated.rowcount == 0`. Worker A rolls back, preventing it from overwriting Worker B's verdict.

### 3. Submission Terminal State Protection
Submissions are guarded by the atomic condition:
```sql
UPDATE submissions
SET status = :status, ...
WHERE id = :submission_id AND status = 'PENDING';
```
Once a submission transitions out of `PENDING`, no subsequent worker retry or expired duplicate execution can alter its historical outcome.

---

## 9. Background Jobs & System Lifecycle

### 1. Startup Sequences
- **FastAPI Backend**:
  1. Bootstrapped via Docker CMD: runs `python3 -m alembic upgrade head` to verify/apply schema migrations before opening listener port.
  2. Verifies `JWT_SECRET` exists and is $\ge 32$ chars.
  3. Boots Uvicorn on `0.0.0.0:9000`.
  4. Readiness probe `GET /health` executes `SELECT 1` on Postgres; Compose halts dependent services until healthy.
- **Worker Process**:
  1. Generates unique identifier: `socket.gethostname() + ":" + str(os.getpid())`.
  2. Calls `judger.ensure_image()`: inspects host Docker daemon for `cf-clone-v2-judge:latest`. If missing, builds image from [`backend/worker/judger_image/Dockerfile`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger_image/Dockerfile).
  3. Connects to Postgres connection pool and enters claim loop.

### 2. Continuous Operations Loop
```python
while True:
    async with Session() as db:
        await requeue_expired_jobs(db)
        job = await claim_job(db, worker_id, JOB_LEASE_SECONDS)
    if job:
        await process_job(job, worker_id)
        continue
    if time.monotonic() - last_reap > REAP_INTERVAL_SECONDS:
        last_reap = time.monotonic()
        await asyncio.to_thread(judger.reap_orphaned_containers)
    await asyncio.sleep(1.0)
```
- **Job Processing**: If work is claimed, it immediately executes without sleeping.
- **Backoff**: If no work is found, workers sleep 1.0s to avoid database polling saturation.
- **Orphan Reaping**: Every 300 seconds, workers inspect Docker containers labeled `cfclone.judge=1` with creation timestamps $>20$ minutes old and issues `container.remove(force=True)`.

---

## 10. Testing, Benchmarks & Measured Metrics

### Test Suite Summary ([`backend/tests/`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/tests))
All 14 tests execute cleanly via pytest:
```
tests/test_auth.py::test_password_hash_roundtrip PASSED                  [  7%]
tests/test_auth.py::test_access_token_contains_subject_and_expiry PASSED [ 14%]
tests/test_judger_core.py::test_compare_outputs_ignores_crlf_and_trailing_blank_lines PASSED [ 21%]
tests/test_judger_core.py::test_compare_outputs_preserves_internal_spaces PASSED [ 28%]
tests/test_judger_core.py::test_files_contain_source_and_input_but_never_expected_output PASSED [ 35%]
tests/test_leaderboard.py::test_only_solved_problems_score_and_penalty_uses_first_ac PASSED [ 42%]
tests/test_leaderboard.py::test_multiple_problems_sum_score_and_penalty PASSED [ 50%]
tests/test_leaderboard.py::test_rank_is_deterministic_for_exact_ties PASSED [ 57%]
tests/test_leaderboard.py::test_unsolved_and_unknown_problem_are_not_ranked PASSED [ 64%]
tests/test_models.py::test_language_allowlist_rejects_unknown_language PASSED [ 71%]
tests/test_models.py::test_source_size_limit_is_enforced PASSED          [ 78%]
tests/test_models.py::test_run_limits_are_bounded PASSED                 [ 85%]
tests/test_models.py::test_batch_requires_between_one_and_twenty_tests PASSED [ 92%]
tests/test_models.py::test_username_validation_and_password_minimum PASSED [100%]
======================== 14 passed, 2 warnings in 0.78s ========================
```

### Measured Constraints & Invariants
- Source code upper bound: **256 KiB** (`262,144` bytes).
- Custom test input/output bound: **64 KiB** (`65,536` bytes).
- Batch runner batch bounds: **1 to 20** tests per request.
- Sandbox container PID ceiling: **64 PIDs**.
- Sandbox container filesystem output ceiling: **2 MiB** (`RLIMIT_FSIZE`).
- Sandbox output buffer capture: **100 KiB** (`102,400` bytes).
- User concurrent jobs quota: **5 active jobs** (`queued` + `running`).

---

## 11. Complete Codebase File Map

```
cf-clone-v2/
├── docker-compose.yml              # Multi-container orchestration (postgres, backend, worker, frontend)
├── .env.example                    # Template for production and test credentials
├── README.md                       # High-level architecture, threat model, and setup instructions
├── backend/
│   ├── Dockerfile                  # API server image (Python 3.12, Alembic migration + Uvicorn)
│   ├── Dockerfile.worker           # Judge worker image (Python 3.12, Docker-in-Docker socket client)
│   ├── pyproject.toml              # Pytest configuration and Python path setup
│   ├── requirements.txt            # Locked Python library dependencies
│   ├── alembic.ini                 # Alembic database migration tool settings
│   ├── seed.py                     # Idempotent DB seeder (admin account, 5 problems, sample test cases)
│   ├── conftest.py                 # Pytest test harness defaults (dummy JWT secrets and DB URL)
│   ├── server/
│   │   ├── __init__.py             # Package marker
│   │   ├── main.py                 # FastAPI application root, middleware, route inclusions, /health
│   │   ├── config.py               # Fail-closed environment parser and global constants
│   │   ├── models.py               # Pydantic ingress validation models (SubmitRequest, RunRequest, etc.)
│   │   ├── auth.py                 # Argon2 password hashing, JWT minting/verification, login rate limiter
│   │   ├── routes.py               # Problem endpoints, submission queuing, custom runs, batch runners
│   │   ├── contests_routes.py      # Contest lifecycle, user registration, and public standings proxy
│   │   ├── leaderboard.py          # Dynamic ICPC ranking logic and tie-breaker sorting
│   │   ├── admin.py                # Admin CRUD for problems, test cases, contests, and dry-run testing
│   │   ├── jobs.py                 # Postgres-backed queue operations (claim, complete, fail, requeue)
│   │   ├── db/
│   │   │   ├── database.py         # Async SQLAlchemy engine and async_sessionmaker setup
│   │   │   └── models.py           # SQLAlchemy ORM declarations (User, Problem, Submission, JudgeJob, etc.)
│   │   └── alembic/                # Database migration scripts and metadata hooks
│   ├── worker/
│   │   ├── __init__.py             # Package marker
│   │   ├── main.py                 # Worker polling loop, job dispatcher, and reap scheduler
│   │   ├── judger.py               # Docker container builder, security spec builder, execution supervisor
│   │   └── judger_image/
│   │       ├── Dockerfile          # Inner judge image (Debian Bookworm, gcc, python3, openjdk)
│   │       └── run-judge           # Sandbox shell runner (time telemetry, ulimits, timeouts, exit codes)
│   └── tests/
│       ├── test_auth.py            # Password hashing & JWT decoding tests
│       ├── test_judger_core.py     # Output normalization & sandbox input isolation tests
│       ├── test_leaderboard.py     # ICPC score calculation, penalty, and tie-breaking tests
│       └── test_models.py          # Ingress bounds and validator rejection tests
└── frontend/
    ├── Dockerfile                  # Multi-stage production build (Node build -> Nginx host)
    ├── nginx.conf                  # Nginx reverse proxy rules, caching headers, /api forwarding
    ├── package.json                # Frontend package dependencies (React 19, Vite, Monaco)
    ├── vite.config.js              # Vite bundler config with local /api dev proxy
    └── src/
        ├── api.js                  # Axios instance with auth interceptors and 401 broadcast
        ├── App.jsx                 # Client-side router layout
        ├── context/AuthContext.jsx # User identity state and token synchronization
        ├── hooks/
        │   ├── useSubmission.js    # Polling hook for submissions with monotonic runTokenRef
        │   └── useRunBatch.js      # Polling hook for batch runs with monotonic runTokenRef
        ├── components/             # Reusable UI elements (Console, Runner, Statement, Nav)
        └── pages/                  # Problem list, detail arena, leaderboard, admin dashboards
```

---

## 12. Architectural Trade-offs Table

| Decision | Chosen Architecture | Rejected Alternative | Engineering Rationale / Trade-off |
|---|---|---|---|
| **Broker Architecture** | Postgres `judge_jobs` table with `FOR UPDATE SKIP LOCKED` | RabbitMQ / Redis Streams / Celery | **Eliminated distributed transactions.** RabbitMQ requires a two-phase commit or outbox pattern to guarantee that saving a submission and enqueuing a job succeed atomically. Postgres guarantees atomic enrollment in a single ACID transaction with zero extra infrastructure. |
| **Client Notification** | Short Polling (HTTP `GET` every 1.0s) | WebSockets / Server-Sent Events (SSE) | **Stateless Edge Simplicity.** Long-lived WebSocket/SSE connections tie up reverse-proxy file descriptors and require stateful worker-to-gateway broadcasting (e.g., Redis Pub/Sub). Polling allows API and proxy tiers to remain completely stateless. Average judge latency is 1-3 seconds, making 1s polling indistinguishable to users. |
| **Standings Calculation** | Dynamic compute-on-read from raw `submissions` | Materialized cache / incremental Redis sorted sets | **Absolute Determinism & Simplicity.** Online judges frequently re-judge submissions or adjust test cases. Incremental caches risk drifting or corrupting standings on re-judges. Computing from indexed submissions guarantees consistency at contest scales ($\le 5,000$ submissions). |
| **Sandbox Isolation** | Host Docker daemon socket + ephemeral containers | Native Linux `chroot` / `nsjail` / `isolate` on worker host | **Operational Portability.** Using Docker containers avoids binding host kernel packages, compilation dependencies, or complex manual cgroup configurations. Works out of the box in standard cloud VMs. Mitigated by `read_only`, `network_mode: none`, dropped capabilities, and optional `runsc` (gVisor). |
| **Code Injection Mechanism** | Exec stream `cat > "$1"` over Docker exec API | Docker `put_archive` (tar streaming) | **Kernel / Daemon Compatibility.** Docker daemon unconditionally rejects `put_archive` when a container root filesystem is mounted `read_only: True`, even if the target directory is an in-memory `tmpfs`. The one-shot exec stream cleanly writes into the tmpfs without relaxing container read-only security. |
| **Secrets Management** | Fail-closed environment variables (boot-time check) | Hardcoded default secrets with fallback warning | **Hardened Production Stance.** Fallback secrets (e.g. `"secret123"`) inevitably leak into production environments. Failing fast on import forces operators to generate a cryptographic key before deployment. |

---

## 13. System Bottlenecks & Production Fixes

| Identified Bottleneck | Underlying Cause | Implemented Code Fix | Verification & Evidence |
|---|---|---|---|
| **Postgres NUL Byte Crash** | User programs outputting binary garbage or NUL (`\x00`) bytes crash PostgreSQL `JSON` columns. | Recursive NUL sanitization via `_json_safe()` in [`backend/server/jobs.py:16-24`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L16-L24). | Strips `\x00` from dictionaries, strings, and lists before passing to SQL JSON serializer. |
| **Zombie Worker Verdict Rewrites** | A worker whose lease expired completes a job after another worker already re-judged it. | Optimistic concurrency check: `WHERE id = :id AND status = 'running' AND locked_by = :worker_id`. | If `updated.rowcount == 0`, transaction issues `await db.rollback()`, discarding stale output. |
| **Read-Only Tar Stream Rejection** | Docker engine blocks `put_archive` into `/workspace` due to `read_only: true` on rootfs. | Replaced `put_archive` with one-shot `cat > "$1"` exec socket stream in [`backend/worker/judger.py:78-110`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L78-L110). | Allows mounting entire rootfs as read-only while safely populating `/workspace` tmpfs. |
| **Client Polling Race Conditions** | Fast tab switches or multiple clicks cause delayed polling responses to overwrite newer runs. | Monotonic request token ref (`runTokenRef.current`) in [`useSubmission.js`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/frontend/src/hooks/useSubmission.js) & [`useRunBatch.js`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/frontend/src/hooks/useRunBatch.js). | Polling responses verify `runTokenRef.current === token` before dispatching React state updates. |
| **Host Disk Exhaustion from Rogue Code** | Malicious code attempting `while(1) printf(...)` or creating huge files. | Dual-layer file size limits: Docker cgroup `Ulimit(name="fsize", soft=2MB, hard=2MB)` + shell `ulimit -f 4096`. | OS terminates process with `SIGXFSZ` (exit code 153/1) when output exceeds 2 MiB. |

---

## 14. One-Page Cheat Sheet (Interview Quick-Reference)

```
========================================================================================
                      YUKTICODE V2 CHEAT SHEET
========================================================================================
Core Paradigm:   Brokerless, minimal online judge. Postgres is queue + database.
Tech Stack:      FastAPI + asyncpg (API), Python + Docker socket (Worker), Nginx + React (UI).

1. THE QUEUE (judge_jobs in Postgres)
   - Claim Query:   SELECT id FROM judge_jobs WHERE status='queued' AND available_at<=now()
                    ORDER BY priority, available_at, created_at FOR UPDATE SKIP LOCKED LIMIT 1;
   - Atomicity:     Enqueuing a submission and its judge job happens in one DB transaction.
   - Crash Lease:   `locked_until` (default 600s). Requeued up to `JOB_MAX_ATTEMPTS` (3).

2. THE SANDBOX (Ephemeral Docker container per test case)
   - Isolation:     --network none, --read-only rootfs, --user 65534:65534, --cap-drop ALL,
                    --security-opt no-new-privileges, --pids-limit 64, mem_limit cgroups.
   - Workspaces:    tmpfs on /workspace (64m, exec), /tmp (64m), /dev/shm (32m, noexec).
   - Secret Leak:   Expected output NEVER enters sandbox; evaluation happens on worker host.
   - Output Cap:    Truncated at 100 KiB (JUDGE_MAX_OUTPUT_BYTES). File capped at 2 MiB.

3. CONCURRENCY & LIMITS
   - Active Quota:  Max 5 concurrent active jobs per user (HTTP 429).
   - Ingress Caps:  Source <= 256 KiB; Custom test <= 64 KiB; Batch tests <= 20.
   - Rate Limit:    Sliding window (10 attempts / 300s) on IP+username for /auth/login.
   - Standings:     Dynamically computed on read from raw submissions (ICPC penalty rules).

4. TERMINAL VERDICTS
   - AC (Accepted), WA (Wrong Answer), TLE (Time Limit Exceeded - 124/143),
     MLE (Memory Limit Exceeded - 137), RE (Runtime Error), CE (Compile Error),
     SYSTEM_ERROR (Infrastructure / lease timeout).
========================================================================================
```
