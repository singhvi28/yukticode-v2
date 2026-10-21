import os

# The config module is fail-closed, so every test process needs a secret before
# importing any application module.
os.environ.setdefault("JWT_SECRET", "test-secret-test-secret-test-secret-1234")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://judge:judge@localhost:5432/judge"
)
