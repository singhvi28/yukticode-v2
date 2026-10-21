"""Single configuration module.

Secrets are required. There is deliberately no fallback JWT secret and no
default admin password: starting with a known secret is worse than failing.
"""

import os


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is required. Copy .env.example to .env and set a real value."
        )
    return value


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://judge:judge@postgres:5432/judge",
)

JWT_SECRET = _required("JWT_SECRET")
if len(JWT_SECRET) < 32:
    raise RuntimeError("JWT_SECRET must be at least 32 characters long")

JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))

# The worker holds a job lease for this long. A crashed worker's job is
# returned to the queue after the lease expires.
JOB_LEASE_SECONDS = int(os.getenv("JOB_LEASE_SECONDS", "600"))
JOB_MAX_ATTEMPTS = int(os.getenv("JOB_MAX_ATTEMPTS", "3"))

# Docker judge settings. The image is built from worker/judger_image/Dockerfile.
JUDGE_IMAGE = os.getenv("JUDGE_IMAGE", "cf-clone-v2-judge:latest")
JUDGE_RUNTIME = os.getenv("JUDGE_RUNTIME", "auto")
JUDGE_MAX_OUTPUT_BYTES = int(os.getenv("JUDGE_MAX_OUTPUT_BYTES", "102400"))

# Slugs/languages accepted by the API. Keep this list short and explicit.
ALLOWED_LANGUAGES = {"py", "cpp", "java"}

# Comma-separated browser origins for local development. Nginx serves the
# production frontend same-origin, so CORS is only needed for `npm run dev`.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080",
    ).split(",")
    if origin.strip()
]
