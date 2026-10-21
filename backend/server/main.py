from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from server.admin import router as admin_router
from server.auth import router as auth_router
from server.config import CORS_ORIGINS
from server.contests_routes import router as contests_router
from server.db.database import async_session_maker
from server.routes import router

app = FastAPI(title="YuktiCode v2", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(router)
app.include_router(contests_router)
app.include_router(admin_router)


@app.get("/health")
async def health():
    # A process that cannot reach Postgres is not healthy enough to receive
    # traffic from Nginx or to unblock the worker's `depends_on` chain.
    async with async_session_maker() as session:
        await session.execute(text("SELECT 1"))
    return {"status": "ok"}
