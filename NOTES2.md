# YuktiCode v2 — Interviewer Q&A Gauntlet

> **Role & Tone**: You are a Staff Software Engineer defending the technical design of the YuktiCode v2 online judge in a senior system design and backend engineering interview. Your responses cite real code, provide concrete math, defend decisions against alternatives, and openly discuss weaknesses and production limits.

---

## Theme 1: Architecture & Technology Choices

### Q1: Why did you use PostgreSQL as a message broker instead of RabbitMQ, Kafka, or Redis Streams?
**A:** The primary architectural driver was **atomic transactional enrollment**. In an online judge, creating a submission record and dispatching it for evaluation must be an all-or-nothing operation. If we used RabbitMQ, we would face the dual-write problem: if the database insert succeeds but the broker publish fails due to a network blip or socket reset, the submission is permanently stranded in `PENDING`. Solving that with an external broker requires a Two-Phase Commit (2PC) or an Outbox table coupled with a CDC tailer (Debezium), adding massive infrastructure complexity.

By using the `judge_jobs` table in PostgreSQL with `SELECT ... FOR UPDATE SKIP LOCKED` ([`backend/server/jobs.py:30-51`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L30-L51)), inserting into `submissions` and inserting into `judge_jobs` happens within the **exact same ACID transaction** in [`backend/server/routes.py:160-176`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/routes.py#L160-L176). If anything fails, the entire transaction rolls back. Furthermore, PostgreSQL eliminates the operational overhead of running, clustering, securing, and backing up an independent RabbitMQ or Kafka cluster for a system processing tens of submissions per second.

### Q2: Why did you choose short polling over WebSockets or Server-Sent Events (SSE)?
**A:** We evaluated WebSockets and SSE and deliberately rejected them for three reasons:
1. **Edge Simplicity & Statelessness**: WebSockets are stateful TCP connections. To support WebSockets in a horizontally scaled backend, we would need a pub/sub backplane (like Redis Pub/Sub) so that when Worker 3 finishes judging on Node B, it can publish an event that reaches the specific API container on Node A holding the candidate’s WebSocket connection.
2. **Proxy Descriptor Overhead**: In a competition arena with 5,000 students, 5,000 open WebSocket connections consume persistent socket descriptors and memory across our Nginx reverse proxy.
3. **Execution Latency Profile**: Code compilation and execution against multiple test cases typically takes between 1.0 and 3.0 seconds. A client polling every 1,000ms ([`frontend/src/hooks/useSubmission.js:4`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/frontend/src/hooks/useSubmission.js#L4)) will notice a verdict within 0 to 500ms of completion on average, which feels instantaneous to a human candidate. Polling over standard HTTP/1.1 with `Keep-Alive` keeps our API server 100% stateless and trivially cacheable at the gateway.

### Q3: Why mount the host Docker socket into the worker container rather than using Firejail, nsjail, or standard chroots?
**A:** It was a deliberate trade-off between **operational portability** and **least privilege**:
- Using low-level Linux tools like `nsjail` or `isolate` requires installing language compilers (`g++`, `default-jdk`, `python3`) directly onto the worker host machine, along with specific kernel cgroup configurations and setuid binaries. This makes automated testing, CI/CD, and multi-cloud deployment brittle.
- Running inside Docker gives us a completely self-contained, reproducible compiler toolchain packaged in [`backend/worker/judger_image/Dockerfile`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger_image/Dockerfile).
- Mounting `/var/run/docker.sock` ([`docker-compose.yml:57`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/docker-compose.yml#L57)) means the worker can command the host daemon to spin up sibling containers. We explicitly document this as a known trust boundary: the worker container itself must be trusted, but the sibling containers it spawns are completely locked down. For hostile public deployments, we configure `JUDGE_RUNTIME=runsc` ([`backend/server/config.py:38`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/config.py#L38)) to run user code under Google gVisor.

### Q4: Why is there no Redis cache for problem statements or user profiles?
**A:** In YuktiCode v2, problem statements change rarely, and PostgreSQL already excels at caching hot read data in RAM through its buffer pool (`shared_buffers`). Adding Redis to cache problem queries would create the classic **cache invalidation problem**: whenever an admin edits a problem statement, modifies time limits, or publishes a draft problem, we would need reliable cache eviction hooks. If an invalidation message is dropped, candidates see stale statements or outdated sample tests. Querying PostgreSQL directly with indexed reads ([`backend/server/routes.py:35-46`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/routes.py#L35-L46)) takes $<1\text{ ms}$, rendering a dedicated caching service an unnecessary operational liability.

---

## Theme 2: Concurrency, Race Conditions & Distributed Transactions

### Q5: What happens if two workers query `judge_jobs` at the exact same microsecond?
**A:** PostgreSQL’s concurrency engine guarantees that they will never claim the same job. The claim query in [`backend/server/jobs.py:30-51`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L30-L51) uses:
```sql
SELECT id FROM judge_jobs
WHERE status = 'queued' AND available_at <= now()
ORDER BY priority, available_at, created_at
FOR UPDATE SKIP LOCKED LIMIT 1;
```
When Worker 1 enters the transaction, Postgres locks the row index entry for the highest priority job. When Worker 2 executes simultaneously, `SKIP LOCKED` instructs the engine not to wait for Worker 1 to commit, but to immediately skip that locked row and lock the next row matching the filter. Both workers receive distinct job IDs in parallel without blocking each other.

### Q6: What happens if Worker A claims a job, gets delayed by an OS swap storm or long GC pause, its lease expires, Worker B claims and finishes it, and then Worker A suddenly wakes up and tries to complete it?
**A:** Worker A’s write is safely rejected, and its transaction is rolled back. Look at [`backend/server/jobs.py:69-94`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L69-L94):
```python
updated = await db.execute(
    text("""
        UPDATE judge_jobs
        SET status = 'done', result = CAST(:result AS json),
            locked_by = NULL, locked_until = NULL, updated_at = now()
        WHERE id = :job_id
          AND status = 'running'
          AND locked_by = :worker_id
    """),
    {"job_id": job_id, "worker_id": worker_id, ...}
)
if not updated.rowcount:
    await db.rollback()
    return
```
When Worker B reclaimed the expired job via `requeue_expired_jobs()`, it set `locked_by` to Worker B’s identifier (or Worker B already marked it `done`). When Worker A wakes up and sends its update with `locked_by = 'worker_a'`, the `WHERE` clause fails to match any rows. `updated.rowcount` is 0. Worker A executes `await db.rollback()`. Furthermore, [`submissions`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L106) has the guard `WHERE id = :submission_id AND status = 'PENDING'`. Because Worker B already transitioned the submission to its terminal status, Worker A cannot overwrite Worker B's verdict.

### Q7: What prevents a malicious user from spamming the `/submit` endpoint with a script and starving the judge queue?
**A:** We enforce an active-job concurrency quota in the application layer before enqueuing ([`backend/server/routes.py:94-110`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/routes.py#L94-L110)):
```python
async def _check_active_jobs(db: AsyncSession, user: User) -> None:
    if user.is_admin:
        return
    active = (
        await db.execute(
            select(func.count(JudgeJob.id)).where(
                JudgeJob.user_id == user.id,
                JudgeJob.status.in_(("queued", "running")),
            )
        )
    ).scalar_one()
    if active >= MAX_ACTIVE_JOBS:  # MAX_ACTIVE_JOBS = 5
        raise HTTPException(status_code=429, detail="Too many queued jobs; wait for one to finish.")
```
This is backed by the composite index `ix_judge_jobs_user` on `(user_id, created_at)` ([`backend/server/db/models.py:181`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/db/models.py#L181)). A single user can have at most 5 unfinished jobs in the pipeline; the 6th submission is immediately rejected with HTTP 429 without touching the queue.

### Q8: What happens if two concurrent requests attempt to register the same user for the same contest at the exact same instant?
**A:** The database enforces absolute data integrity via a unique composite constraint ([`backend/server/db/models.py:139`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/db/models.py#L139)):
```python
UniqueConstraint("contest_id", "user_id", name="uq_contest_registration")
```
In [`backend/server/contests_routes.py:168-174`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/contests_routes.py#L168-L174), the commit is wrapped in a `try...except IntegrityError` block. If two requests slip past the initial existence check, one transaction commits successfully while the second triggers a unique constraint violation on Postgres, catches `IntegrityError`, rolls back, and returns HTTP 400 (`"You are already registered for this contest"`).

---

## Theme 3: Failure Modes, Crashes & Recovery

### Q9: What happens if a judge worker crashes (e.g., host OOM kill or hardware power loss) while executing a test case?
**A:** The system recovers automatically through distributed leases. 
1. When the worker originally claimed the job, it set `locked_until = now() + 600s` ([`backend/server/jobs.py:45`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L45)).
2. As soon as the dead worker stops renewing leases, the surviving workers (or a restarted worker container) invoke `requeue_expired_jobs(db)` on their next loop iteration ([`backend/worker/main.py:163`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/main.py#L163)).
3. `requeue_expired_jobs()` finds all jobs where `status = 'running' AND locked_until < now()`. If `attempts < 3`, it clears the lock, sets `status = 'queued'`, and makes it available immediately ([`backend/server/jobs.py:175-186`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L175-L186)).
4. If a job has crashed 3 consecutive times (e.g., a "poison pill" submission triggering a kernel panic), it transitions to `status = 'failed'`, writes `last_error = 'worker lease expired after max attempts'`, and marks the submission as `SYSTEM_ERROR` ([`backend/server/jobs.py:194-233`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L194-L233)).

### Q10: When a worker dies mid-execution, what happens to the Docker container it started? Does it leak forever on the host?
**A:** No. We prevent container leakage through two complementary mechanisms:
1. **Normal Flow**: Every execution in [`backend/worker/judger.py:296-302`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L296-L302) wraps container destruction in a `finally: container.remove(force=True)` block.
2. **Crash Recovery Sweeper**: All judge containers are created with a specific Docker label: `"labels": {"cfclone.judge": "1"}` ([`backend/worker/judger.py:204`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L204)). Every 300 seconds, the worker daemon calls `reap_orphaned_containers()` ([`backend/worker/judger.py:396-422`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L396-L422)). It queries Docker for all containers matching that label and forcibly removes any container whose creation timestamp is older than 20 minutes.

### Q11: What happens if PostgreSQL crashes or becomes unreachable?
**A:** The system fails closed and blocks traffic gracefully:
- **API Server**: The `/health` endpoint executes `SELECT 1` on Postgres ([`backend/server/main.py:28-34`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/main.py#L28-L34)). If Postgres fails, `/health` returns HTTP 500, causing Docker Compose / Kubernetes readiness probes to stop routing ingress traffic to that backend container.
- **Worker Daemon**: The main loop in [`backend/worker/main.py:179-182`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/main.py#L179-L182) catches database connection exceptions, logs an error, sleeps for 2.0 seconds with exponential reconnect attempts via SQLAlchemy’s `pool_pre_ping=True`, and resumes claiming once the database recovers. No state in memory is corrupted.

### Q12: What happens if a submission contains an infinite loop?
**A:** It is intercepted and terminated cleanly by the Linux kernel `timeout` utility inside the sandbox runner script ([`backend/worker/judger_image/run-judge:52-54`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger_image/run-judge#L52-L54)):
```sh
/usr/bin/time -f "MEM:%M CPU:%U+%S" -o /workspace/time.txt \
  /usr/bin/timeout -k 1 "$limit_s" /bin/sh -c "$cmd" \
  < /workspace/input.txt > /workspace/stdout.txt 2> /workspace/stderr.txt
echo $? > /workspace/exit_code
```
`/usr/bin/timeout` sends `SIGTERM` when `$limit_s` expires. If the process does not terminate within 1 second, `-k 1` sends uncatchable `SIGKILL`. The exit code is written to `/workspace/exit_code`. In [`backend/worker/judger.py:260-264`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L260-L264), exit codes 124 and 143 are mapped to `TLE` ("Time Limit Exceeded"). The container is then destroyed.

---

## Theme 4: Sandboxing, Security & Abuse Prevention

### Q13: How do you prevent a malicious submission from fork bombing (`while(1) fork();`) and exhausting host PIDs?
**A:** We configure a strict PID cgroup limit directly on the container invocation ([`backend/worker/judger.py:187`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L187)):
```python
"pids_limit": 64
```
The Linux kernel will refuse any `clone()` or `fork()` syscall once the total number of processes and threads in that container’s PID namespace reaches 64, returning `-EAGAIN`. The host operating system's process table remains completely untouched and stable.

### Q14: How do you prevent a candidate from reading the expected test case output?
**A:** Through strict **Zero-Knowledge Information Hiding**. The expected output is never passed into the sandbox container. Look at [`_files()`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L70-L75):
```python
def _files(source: str, stdin: str, language: str) -> list[tuple[str, bytes]]:
    return [
        (_SOURCE_FILES[language], (source or "").encode("utf-8")),
        ("input.txt", (stdin or "").encode("utf-8")),
    ]
```
The only files populated in `/workspace` are the candidate's source code and `input.txt`. The expected output resides solely in the worker process memory on the host. Once the container finishes writing `/workspace/stdout.txt`, the worker extracts the stdout and performs string comparison (`compare_outputs()`) out-of-band on the host. Even if user code gains complete root access inside the container, there is no expected output file to discover.

### Q15: Why did you write files into the container via an exec stream (`cat > "$1"`) instead of using Docker's standard `put_archive` API?
**A:** Because Docker’s engine has an internal limitation: if a container is created with `read_only: True` ([`backend/worker/judger.py:183`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L183)), the daemon’s `put_archive` API endpoint unconditionally rejects uploads with a 500/400 error, even if the destination path is a writable `tmpfs` volume like `/workspace`!
To avoid relaxing `read_only: True` (which would allow writes to `/usr` or `/bin`), we implemented [`_write_file()`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L78-L110):
```python
exec_id = container.client.api.exec_create(
    container.id,
    ["/bin/sh", "-c", 'cat > "$1"', "sh", path],
    stdin=True, stdout=False, stderr=False,
)["Id"]
stream = container.client.api.exec_start(exec_id, socket=True)
stream._sock.sendall(data)
stream._sock.shutdown(socket.SHUT_WR)
```
This executes `cat > path` inside the container and streams the bytes over an exec socket directly into the writable `tmpfs`, keeping the rootfs strictly read-only.

### Q16: How do you prevent a program from filling up the host hard drive with `while(1) printf("A");`?
**A:** We enforce three nested defensive barriers:
1. **Container Ulimit `fsize`**: Docker passes `Ulimit(name="fsize", soft=2MB, hard=2MB)` ([`backend/worker/judger.py:195`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L195)). The Linux kernel delivers `SIGXFSZ` to any process attempting to write a file larger than 2 MiB.
2. **Shell `ulimit -f`**: In [`backend/worker/judger_image/run-judge:14`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger_image/run-judge#L14), `ulimit -f 4096` enforces the exact same 2 MiB block ceiling inside the container shell.
3. **Capped Output Read**: When the host worker reads the resulting `stdout.txt`, it never reads the whole file into RAM. It invokes `head -c 102401` ([`backend/worker/judger.py:119-125`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L119-L125)). If the file exceeds `JUDGE_MAX_OUTPUT_BYTES` (100 KiB), the worker immediately truncates it and assigns verdict `RE` ("output exceeded the output limit").

### Q17: What prevents SSRF or local network exfiltration (e.g. curling AWS metadata `169.254.169.254` or connecting to the Postgres database)?
**A:** Complete network namespace isolation via `"network_mode": "none"` ([`backend/worker/judger.py:182`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L182)). In Linux, this leaves the container without an `eth0` network interface. The only interface inside the network namespace is a disconnected loopback device. Any socket `connect()` system call immediately fails with `ENETUNREACH` (Network unreachable). The code cannot communicate with Postgres, Docker daemon, or the Internet.

### Q18: What prevents privilege escalation via setuid binaries inside the container?
**A:** Two explicit configurations:
1. `"security_opt": ["no-new-privileges"]` ([`backend/worker/judger.py:186`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L186)), which invokes the kernel prctl `PR_SET_NO_NEW_PRIVS`. This tells the Linux kernel that no execution path may grant the process more privileges than parent caller, effectively disabling setuid/setgid bits.
2. `"cap_drop": ["ALL"]` ([`backend/worker/judger.py:185`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger.py#L185)), stripping all 41 Linux capabilities.
3. `"user": "65534:65534"` (`nobody:nogroup`), ensuring that processes execute under an unprivileged user ID.

---

## Theme 5: Scalability, Performance & Resource Limits

### Q19: What breaks first in YuktiCode v2 if load increases by 10×, 100×, or 1000×?
**A:** Let's trace the quantitative failure modes across scaling tiers:

#### At 10× Load (~50 submissions/sec, 500 active users):
- **First Bottleneck**: Worker host CPU & Docker container creation overhead. Spawning a fresh Docker container takes ~150–300ms of Docker daemon overhead (namespace setup, cgroup allocation, tmpfs mounting). One worker machine running 4-8 parallel tests will saturate the Docker daemon socket lock.
- **Remedy**: Horizontally scale the judge worker service across 5-10 worker nodes. Since workers pull jobs with `SKIP LOCKED`, adding worker nodes requires zero architectural changes.

#### At 100× Load (~500 submissions/sec, 5,000 concurrent pollers):
- **First Bottleneck**: PostgreSQL polling and connection saturation. 5,000 clients polling `/submissions/{id}` every 1s generate 5,000 HTTP req/sec and 5,000 SQL queries/sec. While indexed, this puts pressure on Postgres connection pooling and CPU.
- **Second Bottleneck**: Table bloat on `judge_jobs`. 500 inserts and updates per second produce 1,000 dead MVCC tuples per second, necessitating aggressive autovacuuming.
- **Remedy**: Introduce PgBouncer in transaction pooling mode. Add an edge cache or short-TTL (500ms) gateway cache for terminal submission statuses so finalized submissions do not hit Postgres.

#### At 1000× Load (~5,000 submissions/sec, 50,000 concurrent users):
- **Fundamental Architectural Limit**: The single PostgreSQL relational queue breaks down under disk write IOPS.
- **Remedy**: Migrate the queue tier to a partitioned broker (Apache Kafka or AWS SQS) or an in-memory queue (Redis Streams), and adopt an Outbox pattern for submission writes. Replace transient Docker container creation with a warm pool of long-running, isolated micro-sandboxes (e.g. AWS Firecracker microVMs or persistent gVisor processes managed by `containerd`).

### Q20: How many concurrent submissions can a single 8-core, 16 GB RAM worker host handle? Let's do the math.
**A:** Let's calculate based on resource constraints:
1. **CPU Bound**: A submission is allocated 1 CPU core (`nano_cpus: 1_000_000_000`). On an 8-core host, running more than 6-7 concurrent compilation/execution containers will lead to CPU starvation, context switching overhead, and artificial `TLE` (Time Limit Exceeded) verdicts. Safe ceiling: **6 concurrent test containers**.
2. **Memory Bound**: Each sandbox tmpfs is capped at $64\text{ MB} + 64\text{ MB} + 32\text{ MB} = 160\text{ MB}$, plus the problem limit (typically 256 MB). Worst-case peak memory per container is $\sim 416\text{ MB}$. Six containers require $\sim 2.5\text{ GB}$ of RAM, well within the 16 GB host capacity.
3. **Execution Throughput**: Assuming an average submission has 5 test cases, each running for 200ms, plus 250ms container startup/teardown overhead per test:
   $$\text{Time per test} = 0.2\text{s} + 0.25\text{s} = 0.45\text{s}$$
   $$\text{Total time per submission} = 5 \times 0.45\text{s} = 2.25\text{s}$$
   $$\text{Throughput per worker host} = \frac{6 \text{ concurrent executions}}{2.25\text{s}} \approx 2.66\text{ submissions/sec} \approx 160\text{ submissions/minute}$$
To handle 1,000 submissions per minute, we need at least $\lceil 1000 / 160 \rceil = 7$ worker nodes.

### Q21: What is the computational complexity of the dynamic leaderboard query, and when does it degrade?
**A:** In [`backend/server/leaderboard.py:75-97`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/leaderboard.py#L75-L97), the database query filters submissions by:
```sql
WHERE contest_id = :id AND submitted_at >= start AND submitted_at < end AND status != 'PENDING'
ORDER BY user_id, problem_id, submitted_at, id
```
Thanks to the composite index `ix_submissions_contest_user_problem_time` (`contest_id, user_id, problem_id, submitted_at`), the query executes an **Index Scan** with complexity:
$$O(K \log N)$$
where $N$ is total submissions in the database and $K$ is the number of submissions in that specific contest.
The in-memory ranking algorithm [`rank_submissions()`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/leaderboard.py#L20-L65) performs a single linear pass over the $K$ sorted rows ($O(K)$) to resolve first AC and penalties, followed by a sort of the $U$ unique registered users:
$$O(U \log U)$$
**When it degrades**: For a typical contest with $U = 1,000$ users and $K = 5,000$ submissions, $K \log N + U \log U$ takes $\approx 5\text{ ms}$ on modern hardware. However, if a massive contest has $U = 50,000$ users and $K = 250,000$ submissions, calculating this on every user poll will exhaust backend CPU. At that scale, standings must be computed asynchronously into a materialized view or cached with a 10-second debounce.

---

## Theme 6: System Design & Production Weaknesses

### Q22: What are the most honest architectural weaknesses of this system, and what would you change before deploying to 50,000 university students?
**A:** An honest engineering assessment reveals three primary weaknesses:
1. **Single Point of Failure at Host Docker Daemon**: Because the worker communicates with the host Docker engine via `/var/run/docker.sock`, if a rogue kernel crash or Docker daemon deadlock occurs, all judging on that host freezes. In production, I would replace Docker-in-Docker socket control with **isolated microVM runtimes** (such as AWS Firecracker) managed via containerd or Nomad.
2. **Postgres Connection Exhaustion under Swarm Worker Scaling**: Currently, each worker creates its own async SQLAlchemy engine with connection pools ([`backend/worker/main.py:67-68`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/main.py#L67-L68)). If we spin up 50 worker processes across a cluster, each holding 5-10 pooled connections, PostgreSQL’s connection ceiling (`max_connections=100`) will be exhausted quickly. I would introduce **PgBouncer** in transaction pooling mode in front of PostgreSQL.
3. **In-Memory Login Rate Limiting**: The login rate limiter in [`backend/server/auth.py:30`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/auth.py#L30) is stored in an in-memory dictionary `_login_attempts`. If we scale the FastAPI backend to multiple container replicas behind Nginx, each replica maintains its own isolated counter bucket, weakening brute-force defense. I would move the sliding-window state to Redis or use Nginx's native `limit_req_zone`.

### Q23: Why do you compile code inside the transient container rather than on the worker host before launching the container?
**A:** Because **compilation is arbitrary code execution**. Many developers assume compiling C++ (`g++`) or Java (`javac`) is safe. It is not:
- In C++, C++11/20 constexpr metaprogramming can trigger infinite recursion and compiler hangs (`#include </dev/urandom>` or template bombs).
- Malicious `#include` directives can attempt to read sensitive host files (e.g., `#include "/etc/shadow"` inside an error directive).
- Modern compilers support plugins and arbitrary command execution.
Compiling inside the untrusted sandbox ([`backend/worker/judger_image/run-judge:19-37`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/worker/judger_image/run-judge#L19-L37)) under strict timeouts, memory limits, and dropped network privileges ensures that compiler exploits never touch the host worker environment.

### Q24: What happens if a candidate submits code that emits raw binary null bytes (`\x00`) in its output?
**A:** This is a classic PostgreSQL trap: PostgreSQL `json` and `text` data types reject string values containing C-style NUL (`\x00`) characters, raising an internal exception. If unhandled, persisting the result fails, causing the worker loop to crash and the job to stall.
In YuktiCode v2, we created `_json_safe()` in [`backend/server/jobs.py:16-24`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/jobs.py#L16-L24):
```python
def _json_safe(value):
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
```
Before `complete_job()` or `fail_job()` serializes the result dictionary to SQL, it recursively strips all NUL bytes, guaranteeing seamless JSON persistence in PostgreSQL.

### Q25: Why does the seed script require `ADMIN_PASSWORD` explicitly instead of generating a default password like `admin123`?
**A:** In [`backend/seed.py:279-283`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/seed.py#L279-L283), the script enforces:
```python
admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
if not admin_password:
    raise RuntimeError("ADMIN_PASSWORD is required when creating the first admin user")
```
Default credentials are one of the most common vectors for initial system compromise (OWASP Top 10: Security Misconfiguration). Hardcoding or defaulting to `admin:admin` inevitably results in staging or production systems being spun up with well-known passwords. By failing closed, the system forces the DevOps engineer or developer to intentionally provide a secure password before an administrative account can exist.

### Q26: Why are test cases deleted via `cascade="all, delete-orphan"` when a Problem is deleted, but Contest submissions set `contest_id = NULL`?
**A:** This reflects real-world domain requirements:
- **Test Cases**: Test cases are intrinsically owned by a specific problem. If a problem is deleted, its test cases have no independent domain meaning and must be cleaned up to avoid orphaned data (`cascade="all, delete-orphan"` in [`backend/server/db/models.py:59`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/db/models.py#L59)).
- **Submissions**: Submissions represent student academic work and historical activity. If a contest is archived or deleted, candidates still expect to see their historical submissions in their profile history ([`GET /submissions`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/routes.py#L353-L379)). Therefore, `contest_id` on the `submissions` table uses `ondelete="SET NULL"` ([`backend/server/db/models.py:108`](file:///home/singhvi28/Desktop/dev/cf-clone-v2/backend/server/db/models.py#L108)), preserving user submission records while unlinking them from the deleted contest.

---

## 27. Summary Defense: Why YuktiCode v2 is Senior-Level Engineering

> *"YuktiCode v2 is not simple because its author lacked ambition; it is simple because its author understood the cost of distributed systems complexity. By extracting maximum leverage from PostgreSQL’s transactional primitives (`FOR UPDATE SKIP LOCKED`, ACID enqueuing, composite index scans) and Linux kernel namespace isolation, YuktiCode v2 eliminates entire categories of dual-write bugs, cache invalidation failures, and broker operational headaches. It delivers an online judge that is secure, deterministic, resilient to worker failure, and maintainable by a single engineer."*
