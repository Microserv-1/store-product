from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import threading
import time
import uuid
from typing import Optional
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from prometheus_client import Counter, Histogram
from pydantic import BaseModel, Field
import redis
from sqlalchemy import Numeric, String, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .common import identity, instrument, logger

engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True, pool_size=5, max_overflow=5)
Session = sessionmaker(engine, expire_on_commit=False)

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "true").lower() == "true"
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "120"))

CACHE_HITS = Counter("product_cache_hits_total", "Total Redis cache hits in Product service", ["operation"])
CACHE_MISSES = Counter("product_cache_misses_total", "Total Redis cache misses in Product service", ["operation"])
CACHE_BYPASSES = Counter("product_cache_bypasses_total", "Total cache bypasses in Product service", ["operation"])
CACHE_EVICTIONS = Counter("product_cache_evictions_total", "Total cache evictions in Product service", ["reason"])
DB_QUERY_DURATION = Histogram("product_db_query_duration_seconds", "Database query duration in Product service", ["operation"])


class InMemoryCacheBackend:
    """Thread-safe TTL cache backend used when REDIS_URL starts with memory:// for self-contained unit tests."""

    def __init__(self):
        self._data: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> Optional[str]:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if now >= expires_at:
                del self._data[key]
                return None
            return value

    def setex(self, key: str, ttl: int, value: str) -> None:
        with self._lock:
            self._data[key] = (value, time.monotonic() + ttl)

    def delete(self, *keys: str) -> int:
        removed = 0
        with self._lock:
            for k in keys:
                if k in self._data:
                    del self._data[k]
                    removed += 1
        return removed

    def scan_iter(self, match: str = "*"):
        import fnmatch
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (_, exp) in self._data.items() if now >= exp]
            for k in expired:
                del self._data[k]
            keys = [k for k in self._data.keys() if fnmatch.fnmatch(k, match)]
        for k in keys:
            yield k


def build_cache_client(url: str):
    if url.startswith("memory://"):
        return InMemoryCacheBackend()
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=1.5,
        socket_timeout=1.5,
    )


cache_client = build_cache_client(REDIS_URL)
cache_stats = {"hits": 0, "misses": 0, "bypasses": 0, "evictions": 0}
stats_lock = threading.Lock()


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    unit: Mapped[str] = mapped_column(String(40))


@asynccontextmanager
async def lifespan(app):
    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SELECT pg_advisory_xact_lock(20261110)"))
        Base.metadata.create_all(connection)
    yield
    engine.dispose()


app = FastAPI(title="Store Product", lifespan=lifespan)
instrument(app)


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    price: float = Field(gt=0, le=1_000_000_000)
    unit: str = Field(min_length=1, max_length=40)


def round_money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def serialize_product(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "price": float(round_money(product.price)),
        "unit": product.unit,
    }


def should_use_cache(request: Request) -> bool:
    if not CACHE_ENABLED:
        return False
    ctrl = request.headers.get("x-cache-control", "").lower()
    if "no-cache" in ctrl or "bypass" in ctrl:
        return False
    q_cache = request.query_params.get("cache", "").lower()
    if q_cache in ("false", "0", "no", "bypass", "no-cache"):
        return False
    return True


def cache_get(key: str, operation: str) -> Optional[dict | list]:
    try:
        raw = cache_client.get(key)
        if raw is not None:
            CACHE_HITS.labels(operation=operation).inc()
            with stats_lock:
                cache_stats["hits"] += 1
            return json.loads(raw)
        CACHE_MISSES.labels(operation=operation).inc()
        with stats_lock:
            cache_stats["misses"] += 1
        return None
    except Exception as exc:
        logger.warning(json.dumps({"service": "product", "event": "cache_read_error", "error": str(exc)}))
        CACHE_BYPASSES.labels(operation=operation).inc()
        with stats_lock:
            cache_stats["bypasses"] += 1
        return None


def cache_set(key: str, payload: dict | list, ttl: Optional[int] = None) -> None:
    try:
        cache_client.setex(key, ttl or CACHE_TTL_SECONDS, json.dumps(payload))
    except Exception as exc:
        logger.warning(json.dumps({"service": "product", "event": "cache_write_error", "error": str(exc)}))


def cache_invalidate(product_id: Optional[str] = None, reason: str = "mutation") -> None:
    try:
        keys_to_delete = ["products:list:all"]
        if product_id:
            keys_to_delete.append(f"products:id:{product_id}")
        for search_key in cache_client.scan_iter(match="products:list:search:*"):
            keys_to_delete.append(search_key)
        if keys_to_delete:
            cache_client.delete(*keys_to_delete)
        CACHE_EVICTIONS.labels(reason=reason).inc()
        with stats_lock:
            cache_stats["evictions"] += 1
    except Exception as exc:
        logger.warning(json.dumps({"service": "product", "event": "cache_invalidate_error", "error": str(exc)}))


@app.get("/ready")
def ready():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    redis_ok = True
    if CACHE_ENABLED:
        try:
            cache_client.ping()
        except Exception:
            redis_ok = False
    return {"status": "ready", "redis_connected": redis_ok}


@app.get("/products/cache/stats")
def get_cache_stats(_account_id: str = Depends(identity)):
    redis_ok = False
    try:
        redis_ok = bool(cache_client.ping())
    except Exception:
        redis_ok = False
    with stats_lock:
        hits = cache_stats["hits"]
        misses = cache_stats["misses"]
        bypasses = cache_stats["bypasses"]
        evictions = cache_stats["evictions"]
    total = hits + misses
    return {
        "cache_enabled": CACHE_ENABLED,
        "redis_connected": redis_ok,
        "ttl_seconds": CACHE_TTL_SECONDS,
        "hits": hits,
        "misses": misses,
        "bypasses": bypasses,
        "evictions": evictions,
        "hit_ratio": round(hits / total, 4) if total > 0 else 0.0,
    }


@app.post("/products", status_code=201)
def create_product(body: ProductIn, _account_id: str = Depends(identity)):
    name = body.name.strip()
    unit = body.unit.strip()
    if not name or not unit:
        raise HTTPException(422, "Product name and unit must not be blank")
    price_dec = round_money(body.price)
    if price_dec <= Decimal("0.00"):
        raise HTTPException(422, "Product price must be greater than zero")
    product = Product(id=str(uuid.uuid4()), name=name, price=price_dec, unit=unit)
    with DB_QUERY_DURATION.labels(operation="insert").time():
        with Session() as session:
            session.add(product)
            session.commit()
            result = serialize_product(product)
    if CACHE_ENABLED:
        cache_set(f"products:id:{product.id}", result)
        cache_invalidate(product_id=None, reason="create")
    return result


@app.get("/products")
def list_products(
    request: Request,
    response: Response,
    name: Optional[str] = Query(default=None, max_length=120),
    _account_id: str = Depends(identity),
):
    normalized = name.strip().lower() if name and name.strip() else None
    cache_key = f"products:list:search:{normalized}" if normalized else "products:list:all"

    if should_use_cache(request):
        cached = cache_get(cache_key, operation="list")
        if cached is not None:
            response.headers["X-Cache"] = "HIT"
            return cached
        cache_header = "MISS"
    else:
        CACHE_BYPASSES.labels(operation="list").inc()
        with stats_lock:
            cache_stats["bypasses"] += 1
        cache_header = "BYPASS"

    with DB_QUERY_DURATION.labels(operation="select_list").time():
        with Session() as session:
            stmt = select(Product).order_by(Product.name, Product.id)
            if normalized:
                stmt = stmt.where(Product.name.ilike(f"%{normalized}%"))
            rows = session.scalars(stmt).all()
            payload = [serialize_product(p) for p in rows]

    if cache_header == "MISS":
        cache_set(cache_key, payload)
    response.headers["X-Cache"] = cache_header
    return payload


@app.get("/products/{product_id}")
def get_product(
    product_id: str,
    request: Request,
    response: Response,
    _account_id: str = Depends(identity),
):
    cache_key = f"products:id:{product_id}"

    if should_use_cache(request):
        cached = cache_get(cache_key, operation="get_by_id")
        if cached is not None:
            response.headers["X-Cache"] = "HIT"
            return cached
        cache_header = "MISS"
    else:
        CACHE_BYPASSES.labels(operation="get_by_id").inc()
        with stats_lock:
            cache_stats["bypasses"] += 1
        cache_header = "BYPASS"

    with DB_QUERY_DURATION.labels(operation="select_by_id").time():
        with Session() as session:
            product = session.get(Product, product_id)
            if product is None:
                raise HTTPException(404, "Product not found")
            payload = serialize_product(product)

    if cache_header == "MISS":
        cache_set(cache_key, payload)
    response.headers["X-Cache"] = cache_header
    return payload


@app.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: str, _account_id: str = Depends(identity)):
    with DB_QUERY_DURATION.labels(operation="delete").time():
        with Session() as session:
            product = session.get(Product, product_id)
            if product is not None:
                session.delete(product)
                session.commit()
    cache_invalidate(product_id=product_id, reason="delete")
    return Response(status_code=204)
