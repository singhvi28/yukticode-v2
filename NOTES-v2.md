# YuktiCode v2 — System Architecture Reference

> **Role & Purpose**: Technical system reference for the YuktiCode v2 online judge codebase with production-grade SRE observability and event-driven autoscaling. Prepared for a Senior Backend / Distributed Systems / SRE Engineer defending the architecture in a technical interview.

---

## 1. System Overview

YuktiCode v2 is an asynchronous, sandbox-isolated online judge designed with a minimal operational footprint and production-grade reliability engineering. Unlike conventional judge architectures that introduce message brokers (RabbitMQ/Kafka), caching layers (Redis/Memcached), WebSocket state channels, and dedicated background daemon processes (e.g. Celery beat or Celery workers), YuktiCode v2 unifies persistence, distributed leasing, job queuing, and state recovery directly inside **PostgreSQL 16**.

To support production enterprise reliability, the platform incorporates:
- **RED & Golden Signals Telemetry**: Prometheus metrics middleware exposing HTTP request rate, errors, and p50/p95/p99 duration histograms.
- **Queue Saturation Monitoring**: Dynamic PostgreSQL queue depth gauges (`judge_queue_depth`) and terminal verdict metrics (`judge_submissions_total`).
- **Autonomous Worker Telemetry**: Dedicated Prometheus metrics daemon on port `9100` tracking job duration, compilation times, lease recovery counters, and container reap events.
- **Dashboards as Code**: Instant, zero-touch Grafana provisioning with preconfigured SRE overview panels.
- **Event-Driven Autoscaling (KEDA)**: Kubernetes worker horizontal pod autoscaling directly triggered by the `judge_jobs` backlog query, scaling worker pods from 1 to 10 instances dynamically during traffic bursts.

### Primary User Flows
1. **Submit Code Flow**: A candidate submits source code against a published problem or active contest (`POST /submit`). The backend persists a `PENDING` record in `submissions`, enqueues a `submit` job into `judge_jobs` with priority 10 within the same database transaction, and returns HTTP 202. The client polls `GET /submissions/{id}` until terminal status (`AC`, `WA`, `TLE`, `MLE`, `RE`, `CE`, `SYSTEM_ERROR`).
2. **Custom / Batch Run Flow**: A candidate executes code against sample or ad-hoc test cases (`POST /run` or `POST /run_batch`). The backend validates size constraints, checks concurrency limits (max 5 active jobs per user), inserts a `JudgeJob` without creating a `Submission`, and returns HTTP 202. The frontend polls `GET /runs/{id}`.
3. **Contest Lifecycle & Standings Flow**: Contestants register (`POST /contests/{id}/register`). Contest start/end timestamps gate submission and problem statement access. Leaderboards are queried via `GET /contests/{id}/leaderboard`, where standings are dynamically computed directly from raw submission records using ICPC penalty scoring.
4. **Admin Problem & Test Case Authoring**: Admins create/update problems and test cases, execute single-test dry runs (`POST /admin/problems/{id}/testcases/{id}/run`), and poll output via `GET /admin/run-result/{run_id}` before publishing.
5. **Observability & Autoscaling Flow**: Prometheus scrapes FastAPI (`:9000/metrics`) and workers (`:9100/metrics`) every 5 seconds. Grafana renders the real-time SRE overview. Under contest load, KEDA queries PostgreSQL backlog and scales worker replicas out to handle queue spikes.

---

## 2. System Architecture & Runtime Topology

```mermaid
flowchart TD
    subgraph Client["Client Tier"]
        Browser["React 19 SPA (Monaco Editor, Allotment)"]
        LoadGen["Load Generator (simulate_load.py)"]
    end

    subgraph Edge["Edge / Reverse Proxy Tier"]
        Nginx["Nginx 1.25 Alpine (:8080)<br/>- Static Asset Caching<br/>- Gzip Compression<br/>- Proxy /api/* to FastAPI :9000<br/>- Proxy /api/metrics to FastAPI :9000/metrics"]
    end

    subgraph API["Application Tier (Async I/O)"]
        FastAPI["FastAPI 0.128 + Uvicorn 0.40 (:9000)<br/>- JWT Auth (HS256, Argon2)<br/>- Sliding Window Rate Limiting<br/>- PrometheusMiddleware (RED Metrics)<br/>- Dynamic Queue Telemetry Collector"]
    end

    subgraph Data["Persistence & Broker Tier"]
        Postgres[("PostgreSQL 16 Alpine<br/>- Application Schemas<br/>- judge_jobs Table (SKIP LOCKED)<br/>- Atomicity & Terminal Verdicts")]
    end

    subgraph Worker["Execution & Worker Tier (KEDA Scalable 1..10)"]
        WorkerProcess["Worker Daemon (worker.main)<br/>- Lease Claiming (FOR UPDATE SKIP LOCKED)<br/>- Crash Recovery (requeue_expired_jobs)<br/>- Metrics Server (:9100)<br/>- Docker API Socket Controller"]
    end

    subgraph Sandboxes["Isolation Tier (Transient Containers)"]
        Box1["Docker Sandbox (Test 1)<br/>read-only root, tmpfs /workspace<br/>net=none, cap-drop ALL, pids=64"]
        BoxN["Docker Sandbox (Test N)<br/>runsc (gVisor) / runc<br/>ulimit fsize 2MB, no-new-privileges"]
    end

    subgraph Monitoring["Observability & Autoscaling Tier"]
        Prometheus["Prometheus (:9090)<br/>Scrapes :9000/metrics & :9100/metrics"]
        Grafana["Grafana (:3000)<br/>YuktiCode SRE Dashboard"]
        KEDA["KEDA 2.x Autoscaler (K8s)<br/>Polls backlog (status='queued')<br/>HPA scales worker pods"]
    end

    Browser -->|HTTP Requests| Nginx
    LoadGen -->|Synthetic Submissions| Nginx
    Nginx -->|/api/*| FastAPI
    Nginx -->|/*| Browser
    FastAPI -->|asyncpg / SQLAlchemy 2.0| Postgres
    WorkerProcess -->|Claim & Lease Renewal| Postgres
    WorkerProcess -->|/var/run/docker.sock| Sandboxes
    Prometheus -->|Pull 5s| FastAPI
    Prometheus -->|Pull 5s| WorkerProcess
    Grafana -->|Query Datasource| Prometheus
    KEDA -->|SELECT count* FROM judge_jobs| Postgres
    KEDA -.->|Scale replicas 1..10| WorkerProcess
```

### Component Breakdown

| Component | Technology | Role & Boundary |
|---|---|---|
| **Edge / Proxy** | Nginx Alpine | Serves React static bundle; terminates HTTP on `:8080`; rewrites `/api/*` to FastAPI `:9000`; routes `/api/metrics` to `/metrics`; sets security headers (`X-Frame-Options: SAMEORIGIN`, `nosniff`). |
| **API Server** | FastAPI, Uvicorn, Python 3.12 | Enforces authentication, payload limits, contest gate conditions, and user concurrency quotas (`MAX_ACTIVE_JOBS = 5`). Enqueues jobs transactionally. Exposes RED signals and queue gauges at `/metrics`. |
| **State & Broker** | PostgreSQL 16 | Single source of truth. Stores relational schema, application entities, and acts as the concurrency-safe FIFO/priority task queue (`judge_jobs`). |
| **Judge Worker** | Python 3.12, Docker SDK | Autonomous daemon. Claims queued jobs with `FOR UPDATE SKIP LOCKED`, schedules container executions, parses resource telemetry, writes verdicts, cleans up stale containers, and exposes Prometheus metrics on `:9100`. |
| **Judge Sandbox** | Debian Bookworm Slim / runsc | Ephemeral execution jail. One container per test case. Network-less, read-only root, dropped capabilities, unprivileged UID (`65534`). Expected outputs are never copied inside. |
| **Observability** | Prometheus 2.51 & Grafana 10.4 | Pull-based metrics gathering on 5s intervals. Dashboards-as-code visualizing API RPS/latencies, PostgreSQL queue depth, worker throughput, verdict breakdown, and lease expirations. |
| **Autoscaler (K8s)** | KEDA 2.x (PostgreSQL Trigger) | Event-driven scaler evaluating `judge_jobs` queue backlog ($5\text{ jobs / pod}$, min 1, max 10) to scale worker pods dynamically before CPU saturation occurs. |
| **Frontend** | React 19, Vite 7, Monaco Editor | Single-page application. Handles code editing, language switching, batch test runners, and poll loops using monotonic request tokens. |

---

## 3. Technology Stack & Configuration

### Dependencies & Versions
- **Python Runtime**: `3.12-slim` ([`backend/Dockerfile`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/Dockerfile))
- **Web Framework**: `fastapi==0.128.0`, `uvicorn==0.40.0`
- **Data Access**: `sqlalchemy==2.0.52`, `asyncpg==0.31.0`, `alembic==1.18.4`
- **Security & Crypto**: `passlib[argon2]==1.7.4`, `python-jose[cryptography]==3.5.0`
- **Container Control**: `docker==7.1.0`
- **Observability**: `prometheus-client==0.21.1`, `prom/prometheus:v2.51.0`, `grafana/grafana:10.4.1`
- **Autoscaling / K8s**: KEDA 2.x, Kubernetes 1.28+, `scripts/simulate_load.py`
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
- **Unauthenticated**: Registration (`POST /auth/register`), Login (`POST /auth/login`), Problem listing/details (`GET /problems`, `GET /problems/{id}`), Public contest metadata (`GET /contests`, `GET /contests/{id}`), Health probe (`GET /health`), Metrics inspection (`GET /metrics`, `/api/metrics`).
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
GET    /metrics                             -> Prometheus scrape endpoint (RED signals + queue depth)
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

## 8. SRE Observability & Telemetry Architecture

### 1. The Four Golden Signals & RED Method
YuktiCode v2 implements the **RED method** (Rate, Errors, Duration) for synchronous API traffic via [`backend/server/metrics.py`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/metrics.py):
- **Rate**: `http_requests_total` (`method`, `endpoint`, `status_code`) counts all incoming requests.
- **Errors**: HTTP 4xx and 5xx counters indicate client errors and infrastructure failures.
- **Duration**: `http_request_duration_seconds` histogram evaluates latency across 10 buckets (`0.005s` to `5.0s`), enabling calculation of p50, p95, and p99 percentiles in Prometheus.

### 2. Queue Saturation & Broker Gauges
Because PostgreSQL acts as the job broker, traditional host metrics fail to capture processing backlog. On every scrape of `GET /metrics`:
- `judge_queue_depth`: Reports active row counts partitioned by `status` (`queued`, `running`, `done`, `failed`).
- `judge_submissions_total`: Reports terminal verdict distribution (`AC`, `WA`, `TLE`, `MLE`, `CE`, `RE`, `SYSTEM_ERROR`).

### 3. Worker Daemon Telemetry
Each worker daemon runs a lightweight Prometheus server on port `9100` ([`backend/worker/metrics.py`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/metrics.py)):
- `judge_worker_jobs_total`: Counter partitioned by `kind` (`submit`, `run`, `run_batch`) and `status`.
- `judge_worker_job_duration_seconds`: Histogram measuring execution duration by job type.
- `judge_worker_active_jobs`: Instantaneous gauge (1 if actively executing/compiling, 0 if idling).
- `judge_worker_reaped_containers_total`: Tracks cleanup of leaked Docker containers.
- `judge_worker_requeued_leases_total`: Tracks recovery events where expired locks were recycled back into the queue.

### 4. Dashboards as Code (Grafana Provisioning)
The monitoring stack starts automatically alongside the application via [`docker-compose.yml`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/docker-compose.yml):
- **Prometheus**: Runs on `:9090`, scraping `backend:9000` and `worker:9100` on 5-second intervals.
- **Grafana**: Runs on `:3000` with anonymous viewer access enabled. The pre-provisioned dashboard ([`monitoring/grafana/dashboards/judge_sre_overview.json`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/monitoring/grafana/dashboards/judge_sre_overview.json)) visualizes API request rates, latency distributions, queue backlog timelines, worker throughput, and verdict distribution.

---

## 9. Event-Driven Autoscaling with KEDA (Kubernetes)

### 1. The Autoscaling Dilemma: CPU/Memory vs. Queue Depth
Standard Kubernetes Horizontal Pod Autoscalers (HPA) rely on CPU or Memory metrics. For an online judge, this creates severe operational failure modes:
1. **Compilation Spikes vs. True Load**: Compiling a single C++ program utilizes 100% of a CPU core for 800ms. CPU-based HPA sees a 100% spike and scales out aggressively, creating pod thrashing for a single submission.
2. **Post-Batch Starvation**: Once submissions enter the queue, worker CPU remains low while awaiting container initialization. CPU HPA fails to scale out early enough, causing submission latency to spiral.

### 2. KEDA PostgreSQL Scaler Configuration
YuktiCode v2 deploys **KEDA (Kubernetes Event-driven Autoscaling)** ([`deploy/k8s/keda-scaledobject.yaml`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/deploy/k8s/keda-scaledobject.yaml)) to drive worker scaling directly from queue backlog:

$$\text{Desired Replicas} = \left\lceil \frac{\text{Queued Jobs}}{\text{targetQueryValue}} \right\rceil$$

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: judge-worker-autoscaler
  namespace: yukti-code
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: judge-worker
  minReplicaCount: 1
  maxReplicaCount: 10
  pollingInterval: 5
  cooldownPeriod: 30
  triggers:
    - type: postgresql
      metadata:
        connectionFromEnv: DATABASE_URL_PG
        query: "SELECT count(*) FROM judge_jobs WHERE status = 'queued' AND available_at <= now();"
        targetQueryValue: "5"
        activationTargetQueryValue: "1"
```
- **Scale Threshold**: 1 worker pod per 5 queued jobs.
- **Min / Max Replicas**: Bounds worker capacity between 1 and 10 pods.
- **Cooldown (30s)**: Prevents scale-down oscillation while jobs transition between execution stages.

### 3. Load Simulation & Scale Testing
The CLI tool [`scripts/simulate_load.py`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/scripts/simulate_load.py) injects concurrent submissions against `/api/submit`. Running:
```bash
python3 scripts/simulate_load.py --count 40 --concurrency 10
```
Creates an instant backlog of 40 jobs, triggering KEDA to scale worker replicas from 1 to 8 pods within 10 seconds, draining the queue and scaling back down gracefully.

---

## 10. Concurrency, Scaling & Coordination

### 1. Inter-Instance Coordination without Distributed Locks
YuktiCode v2 eliminates Redis locks (`Redlock`), ZooKeeper, or etcd clusters. Worker coordination is achieved purely through PostgreSQL row-level locks using `SKIP LOCKED`:
- Multiple worker processes (across one or more host machines) execute `claim_job()` concurrently.
- Postgres identifies the highest-priority, oldest available job, locks the row in the CTE, updates its status to `running`, marks the worker ID, and immediately releases the table lock while maintaining the row lock across the atomic transaction.
- Competing workers skip already-locked rows without blocking or serialization delays.

### 2. Guarding against Zombie Worker Overwrites
If Worker A experiences a network partition or heavy swapping, its lease (`locked_until`) will expire. Worker B reclaims the job via `requeue_expired_jobs()`. If Worker A subsequently wakes up and attempts to complete the job:
```python
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

## 11. Background Jobs & System Lifecycle

### 1. Startup Sequences
- **FastAPI Backend**:
  1. Bootstrapped via Docker CMD: runs `python3 -m alembic upgrade head` to verify/apply schema migrations before opening listener port.
  2. Verifies `JWT_SECRET` exists and is $\ge 32$ chars.
  3. Registers `PrometheusMiddleware` and mounts `/metrics`.
  4. Boots Uvicorn on `0.0.0.0:9000`.
  5. Readiness probe `GET /health` executes `SELECT 1` on Postgres; Compose halts dependent services until healthy.
- **Worker Process**:
  1. Generates unique identifier: `socket.gethostname() + ":" + str(os.getpid())`.
  2. Starts Prometheus HTTP server on `0.0.0.0:9100`.
  3. Calls `judger.ensure_image()`: inspects host Docker daemon for `cf-clone-v2-judge:latest`. If missing, builds image from [`backend/worker/judger_image/Dockerfile`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger_image/Dockerfile).
  4. Connects to Postgres connection pool and enters claim loop.

### 2. Continuous Operations Loop
```python
while True:
    async with Session() as db:
        requeued = await requeue_expired_jobs(db)
        if requeued:
            WORKER_REQUEUED_LEASES_TOTAL.inc(len(requeued))
        job = await claim_job(db, worker_id, JOB_LEASE_SECONDS)
    if job:
        await process_job(job, worker_id)
        continue
    if time.monotonic() - last_reap > REAP_INTERVAL_SECONDS:
        last_reap = time.monotonic()
        removed = await asyncio.to_thread(judger.reap_orphaned_containers)
        if removed:
            WORKER_REAPED_CONTAINERS_TOTAL.inc(removed)
    await asyncio.sleep(1.0)
```

---

## 12. Testing, Benchmarks & Measured Metrics

### Test Suite Summary ([`backend/tests/`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/tests))
All 31 tests execute cleanly via pytest:
```
tests/test_auth.py ..                                                    [  6%]
tests/test_judger_core.py ...                                            [ 16%]
tests/test_leaderboard.py ....                                           [ 29%]
tests/test_metrics.py ...........                                        [ 64%]
tests/test_models.py .....                                               [ 80%]
tests/test_observability_provisioning.py ......                          [100%]
======================== 31 passed, 2 warnings in 1.01s ========================
```

### Measured Constraints & Invariants
- Source code upper bound: **256 KiB** (`262,144` bytes).
- Custom test input/output bound: **64 KiB** (`65,536` bytes).
- Batch runner batch bounds: **1 to 20** tests per request.
- Sandbox container PID ceiling: **64 PIDs**.
- Sandbox container filesystem output ceiling: **2 MiB** (`RLIMIT_FSIZE`).
- Sandbox output buffer capture: **100 KiB** (`102,400` bytes).
- User concurrent jobs quota: **5 active jobs** (`queued` + `running`).
- KEDA Queue Target: **5 queued jobs per worker pod**.

---

## 13. Complete Codebase File Map

```
cf-clone-v2/
├── docker-compose.yml              # Multi-container orchestration (postgres, backend, worker, frontend, prometheus, grafana)
├── .env.example                    # Template for production and test credentials
├── README.md                       # High-level architecture, threat model, and setup instructions
├── NOTES-v2.md                     # Complete System Architecture Reference
├── NOTES1-v2.md                    # Engineering & Distributed Systems Concepts Deep-Dive
├── NOTES2-v2.md                    # Senior Systems & SRE Interview Defense Gauntlet
├── scripts/
│   └── simulate_load.py            # High-concurrency load generator & real-time queue drainage monitor
├── monitoring/
│   ├── prometheus/
│   │   └── prometheus.yml          # Scrape config for backend (:9000) and worker (:9100)
│   └── grafana/
│       ├── provisioning/
│       │   ├── datasources/datasources.yml # Automated Prometheus datasource config
│       │   └── dashboards/dashboards.yml   # File-based dashboard provider config
│       └── dashboards/
│           └── judge_sre_overview.json     # Preconfigured SRE dashboard (RED, queue depth, throughput)
├── deploy/
│   └── k8s/
│       ├── README.md               # 320-line Kubernetes & KEDA operations manual
│       ├── namespace.yaml          # yukti-code namespace definition
│       ├── postgres.yaml           # PostgreSQL StatefulSet, headless Service, PVC
│       ├── backend.yaml            # FastAPI Deployment & Service with scrape annotations
│       ├── worker.yaml             # Worker Deployment mounting /var/run/docker.sock with port 9100
│       ├── frontend.yaml           # Nginx SPA Deployment & NodePort Service (:30080)
│       └── keda-scaledobject.yaml  # KEDA PostgreSQL trigger scaling worker deployment
├── backend/
│   ├── Dockerfile                  # API server image (Python 3.12, Alembic migration + Uvicorn)
│   ├── Dockerfile.worker           # Judge worker image (Python 3.12, Docker socket client, EXPOSE 9100)
│   ├── pyproject.toml              # Pytest configuration and Python path setup
│   ├── requirements.txt            # Locked Python library dependencies (includes prometheus-client)
│   ├── alembic.ini                 # Alembic database migration tool settings
│   ├── seed.py                     # Idempotent DB seeder (admin account, 5 problems, sample test cases)
│   ├── conftest.py                 # Pytest test harness defaults (dummy JWT secrets and DB URL)
│   ├── server/
│   │   ├── __init__.py             # Package marker
│   │   ├── main.py                 # FastAPI application root, middleware, route inclusions, /health, /metrics
│   │   ├── metrics.py              # Prometheus RED middleware, queue gauges, and /metrics collector
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
│   │   ├── main.py                 # Worker polling loop, job dispatcher, metrics server (:9100), and reap scheduler
│   │   ├── metrics.py              # Worker Prometheus counters, histograms, and active job gauges
│   │   ├── judger.py               # Docker container builder, security spec builder, execution supervisor
│   │   └── judger_image/
│   │       ├── Dockerfile          # Inner judge image (Debian Bookworm, gcc, python3, openjdk)
│   │       └── run-judge           # Sandbox shell runner (time telemetry, ulimits, timeouts, exit codes)
│   └── tests/
│       ├── test_auth.py            # Password hashing & JWT decoding tests
│       ├── test_judger_core.py     # Output normalization & sandbox input isolation tests
│       ├── test_leaderboard.py     # ICPC score calculation, penalty, and tie-breaking tests
│       ├── test_metrics.py         # Ingress RED and PostgreSQL queue depth metrics tests
│       ├── test_models.py          # Ingress bounds and validator rejection tests
│       └── test_observability_provisioning.py # Config validation for Prometheus, Grafana, and KEDA
└── frontend/
    ├── Dockerfile                  # Multi-stage production build (Node build -> Nginx host)
    ├── nginx.conf                  # Nginx reverse proxy rules, /api proxy, and /api/metrics forwarding
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

## 14. Architectural Trade-offs Table

| Decision | Chosen Architecture | Rejected Alternative | Engineering Rationale / Trade-off |
|---|---|---|---|
| **Broker Architecture** | Postgres `judge_jobs` table with `FOR UPDATE SKIP LOCKED` | RabbitMQ / Redis Streams / Celery | **Eliminated distributed transactions.** RabbitMQ requires a two-phase commit or outbox pattern to guarantee that saving a submission and enqueuing a job succeed atomically. Postgres guarantees atomic enrollment in a single ACID transaction with zero extra infrastructure. |
| **Worker Autoscaling** | KEDA PostgreSQL Trigger on Queue Backlog | Horizontal Pod Autoscaler (HPA) on CPU/Memory | **Leading indicator vs. lagging indicator.** Code compilation creates short 100% CPU spikes even with 1 job, causing false-positive HPA thrashing. Measuring queue depth directly predicts latency degradation before node resources saturate. |
| **Telemetry Transport** | Pull-based Prometheus scraping (`:9000` & `:9100`) | Push-based telemetry (StatsD / Pushgateway) | **Scraper isolation and zero egress backpressure.** Push-based telemetry blocks application threads or requires memory buffers if the metrics sink slows down. Pull scraping ensures telemetry collection cannot impact judge execution. |
| **Client Notification** | Short Polling (HTTP `GET` every 1.0s) | WebSockets / Server-Sent Events (SSE) | **Stateless Edge Simplicity.** Long-lived WebSocket/SSE connections tie up reverse-proxy file descriptors and require stateful worker-to-gateway broadcasting (e.g., Redis Pub/Sub). Polling allows API and proxy tiers to remain completely stateless. Average judge latency is 1-3 seconds, making 1s polling indistinguishable to users. |
| **Standings Calculation** | Dynamic compute-on-read from raw `submissions` | Materialized cache / incremental Redis sorted sets | **Absolute Determinism & Simplicity.** Online judges frequently re-judge submissions or adjust test cases. Incremental caches risk drifting or corrupting standings on re-judges. Computing from indexed submissions guarantees consistency at contest scales ($\le 5,000$ submissions). |
| **Sandbox Isolation** | Host Docker daemon socket + ephemeral containers | Native Linux `chroot` / `nsjail` / `isolate` on worker host | **Operational Portability.** Using Docker containers avoids binding host kernel packages, compilation dependencies, or complex manual cgroup configurations. Works out of the box in standard cloud VMs. Mitigated by `read_only`, `network_mode: none`, dropped capabilities, and optional `runsc` (gVisor). |
| **Code Injection Mechanism** | Exec stream `cat > "$1"` over Docker exec API | Docker `put_archive` (tar streaming) | **Kernel / Daemon Compatibility.** Docker daemon unconditionally rejects `put_archive` when a container root filesystem is mounted `read_only: True`, even if the target directory is an in-memory `tmpfs`. The one-shot exec stream cleanly writes into the tmpfs without relaxing container read-only security. |

---

## 15. One-Page Cheat Sheet (Interview Quick-Reference)

```
========================================================================================
                      YUKTICODE V2 CHEAT SHEET (WITH SRE TELEMETRY)
========================================================================================
Core Paradigm:   Brokerless, minimal online judge. Postgres is queue + database.
Tech Stack:      FastAPI + asyncpg (API), Python + Docker socket (Worker), Nginx + React (UI).
Observability:   Prometheus (:9090), Grafana (:3000), FastAPI /metrics (:9000), Worker (:9100).
Autoscaling:     KEDA on Postgres queue depth (1 pod per 5 jobs, min 1, max 10).

1. THE QUEUE (judge_jobs in Postgres)
   - Claim Query:   SELECT id FROM judge_jobs WHERE status='queued' AND available_at<=now()
                    ORDER BY priority, available_at, created_at FOR UPDATE SKIP LOCKED LIMIT 1;
   - Atomicity:     Enqueuing a submission and its judge job happens in one DB transaction.
   - Crash Lease:   `locked_until` (default 600s). Requeued up to `JOB_MAX_ATTEMPTS` (3).

2. SRE OBSERVABILITY & TELEMETRY
   - RED Metrics:   http_requests_total, http_request_duration_seconds (p50/p95/p99).
   - Queue Gauges:  judge_queue_depth{status="queued|running|done|failed"}, judge_submissions_total.
   - Worker Port:   Dedicated Prometheus server on :9100 (judge_worker_jobs_total, duration).
   - Dashboards:    Pre-provisioned Grafana SRE Overview (Ingress, Queue Backlog, Verdicts).

3. EVENT-DRIVEN AUTOSCALING (KEDA)
   - Trigger:       PostgreSQL query: SELECT count(*) FROM judge_jobs WHERE status='queued'...
   - Target Metric: targetQueryValue: "5" -> Replicas = ceil(Queue / 5).
   - Resilience:    30s cooldown prevents thrashing; database connection pooling protects PG.

4. THE SANDBOX (Ephemeral Docker container per test case)
   - Isolation:     --network none, --read-only rootfs, --user 65534:65534, --cap-drop ALL,
                    --security-opt no-new-privileges, --pids-limit 64, mem_limit cgroups.
   - Workspaces:    tmpfs on /workspace (64m, exec), /tmp (64m), /dev/shm (32m, noexec).
   - Secret Leak:   Expected output NEVER enters sandbox; evaluation happens on worker host.
   - Output Cap:    Truncated at 100 KiB (JUDGE_MAX_OUTPUT_BYTES). File capped at 2 MiB.

5. TERMINAL VERDICTS
   - AC (Accepted), WA (Wrong Answer), TLE (Time Limit Exceeded - 124/143),
     MLE (Memory Limit Exceeded - 137), RE (Runtime Error), CE (Compile Error),
     SYSTEM_ERROR (Infrastructure / lease timeout).
========================================================================================
```
