from prometheus_client import Counter, Gauge, Histogram

WORKER_JOBS_TOTAL = Counter(
    "judge_worker_jobs_total",
    "Total jobs processed by worker daemon",
    ["kind", "status"],
)

WORKER_JOB_DURATION_SECONDS = Histogram(
    "judge_worker_job_duration_seconds",
    "Duration of job processing by kind",
    ["kind"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

WORKER_REAPED_CONTAINERS_TOTAL = Counter(
    "judge_worker_reaped_containers_total",
    "Total orphaned containers cleaned up by worker",
)

WORKER_REQUEUED_LEASES_TOTAL = Counter(
    "judge_worker_requeued_leases_total",
    "Total expired leases reclaimed by worker",
)

WORKER_ACTIVE_JOBS = Gauge(
    "judge_worker_active_jobs",
    "Whether the worker is currently processing a job (1 or 0)",
)
