import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from server.config import JWT_ALGORITHM, JWT_EXPIRE_MINUTES, JWT_SECRET
from server.db.database import get_db_session
from server.db.models import User
from server.models import UserCreate, UserLogin, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")
oauth2_scheme_optional = OAuth2PasswordBearer(
    tokenUrl="/auth/login", auto_error=False
)

# A small, single-process login limiter. It is intentionally not distributed;
# the app has one backend process. For a larger deployment use a shared store.
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_MAX_ATTEMPTS = 10
_login_attempts: dict[str, deque[float]] = defaultdict(deque)
_login_lock = asyncio.Lock()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=JWT_EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _user_from_token(token: str, db: AsyncSession) -> User | None:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        username = payload.get("sub")
        if not username:
            return None
    except JWTError:
        return None
    result = await db.execute(select(User).where(User.username == username))
    return result.scalars().first()


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db_session),
) -> User:
    user = await _user_from_token(token, db)
    if user is None or not user.is_active:
        raise _credentials_exception()
    return user


async def get_current_user_optional(
    token: str | None = Depends(oauth2_scheme_optional),
    db: AsyncSession = Depends(get_db_session),
) -> User | None:
    if not token:
        return None
    user = await _user_from_token(token, db)
    if user is None or not user.is_active:
        return None
    return user


async def _check_login_rate_limit(key: str) -> None:
    now = time.monotonic()
    async with _login_lock:
        bucket = _login_attempts[key]
        while bucket and now - bucket[0] > _LOGIN_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= _LOGIN_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts; try again later.",
            )


async def _record_failed_login(key: str) -> None:
    async with _login_lock:
        _login_attempts[key].append(time.monotonic())


async def _clear_login_attempts(key: str) -> None:
    async with _login_lock:
        _login_attempts.pop(key, None)


def _login_key(request: Request, username: str) -> str:
    # Nginx overwrites X-Real-IP with the actual peer address; the backend is
    # not published to the host, so this header is trustworthy in production.
    client = request.headers.get("x-real-ip") or (
        request.client.host if request.client else "unknown"
    )
    return f"{client}:{username.lower()}"


@router.post("/register", response_model=UserResponse, status_code=201)
async def register_user(user_in: UserCreate, db: AsyncSession = Depends(get_db_session)):
    stmt = select(User).where(
        (User.username == user_in.username) | (User.email == user_in.email)
    )
    existing = (await db.execute(stmt)).scalars().first()
    if existing:
        raise HTTPException(
            status_code=409, detail="Username or email already registered"
        )

    user = User(
        username=user_in.username,
        email=user_in.email,
        hashed_password=get_password_hash(user_in.password),
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="Username or email already registered"
        )
    await db.refresh(user)
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        is_active=user.is_active,
        is_admin=user.is_admin,
    )


@router.post("/login")
async def login_user(
    user_in: UserLogin,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
):
    key = _login_key(request, user_in.username)
    await _check_login_rate_limit(key)

    result = await db.execute(select(User).where(User.username == user_in.username))
    user = result.scalars().first()
    if not user or not verify_password(user_in.password, user.hashed_password):
        await _record_failed_login(key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    await _clear_login_attempts(key)
    token = create_access_token({"sub": user.username, "id": user.id})
    return {"access_token": token, "token_type": "bearer", "user_id": user.id}


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    return UserResponse(
        id=current_user.id,
        username=current_user.username,
        email=current_user.email,
        is_active=current_user.is_active,
        is_admin=current_user.is_admin,
    )
