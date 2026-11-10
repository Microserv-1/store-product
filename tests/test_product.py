import os
import time
import uuid

os.environ["DATABASE_URL"] = "sqlite:///./test_product.db"
os.environ["JWT_SECRET"] = "product-unit-test-secret-with-32-plus-bytes"
os.environ["REDIS_URL"] = "memory://local"
os.environ["CACHE_TTL_SECONDS"] = "2"
os.environ["CACHE_ENABLED"] = "true"

from fastapi.testclient import TestClient
import jwt
from app.main import app, cache_client


def make_token(sub: str | None = None, secret: str | None = None) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub or str(uuid.uuid4()),
            "iss": "store-auth",
            "aud": "store-api",
            "iat": now,
            "exp": now + 600,
        },
        secret or os.environ["JWT_SECRET"],
        algorithm="HS256",
    )


def test_product_crud_auth_and_cache_lifecycle():
    with TestClient(app) as client:
        assert client.get("/health-check").status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.get("/products").status_code == 401
        assert client.post("/products", json={"name": "Tomato", "price": 10.12, "unit": "kg"}).status_code == 401

        bad_token = make_token(secret="wrong-secret-with-at-least-32-characters!!")
        assert client.get("/products", headers={"Authorization": f"Bearer {bad_token}"}).status_code == 401

        auth = {"Authorization": f"Bearer {make_token()}"}

        # Create products
        r1 = client.post("/products", json={"name": "Tomato", "price": 10.12, "unit": "kg"}, headers=auth)
        assert r1.status_code == 201
        p1 = r1.json()
        assert p1["name"] == "Tomato"
        assert p1["price"] == 10.12
        assert p1["unit"] == "kg"
        assert uuid.UUID(p1["id"])

        r2 = client.post("/products", json={"name": "Cheese", "price": 0.62, "unit": "slice"}, headers=auth)
        assert r2.status_code == 201
        p2 = r2.json()

        # Invalid inputs rejected
        assert client.post("/products", json={"name": "", "price": 10, "unit": "kg"}, headers=auth).status_code == 422
        assert client.post("/products", json={"name": "Milk", "price": -1, "unit": "L"}, headers=auth).status_code == 422

        # List products: first call is MISS, second call is HIT
        list_miss = client.get("/products", headers=auth)
        assert list_miss.status_code == 200
        assert list_miss.headers.get("X-Cache") == "MISS"
        assert len(list_miss.json()) >= 2

        list_hit = client.get("/products", headers=auth)
        assert list_hit.status_code == 200
        assert list_hit.headers.get("X-Cache") == "HIT"
        assert list_hit.json() == list_miss.json()

        # Search by name (LIKE)
        search_res = client.get("/products?name=tom", headers=auth)
        assert search_res.status_code == 200
        assert any(item["id"] == p1["id"] for item in search_res.json())
        assert all("tom" in item["name"].lower() for item in search_res.json())

        # Get by ID: already pre-warmed on create (HIT), or after eviction is MISS -> HIT
        cache_client.delete(f"products:id:{p1['id']}")
        get_miss = client.get(f"/products/{p1['id']}", headers=auth)
        assert get_miss.status_code == 200
        assert get_miss.headers.get("X-Cache") == "MISS"
        assert get_miss.json() == p1

        get_hit = client.get(f"/products/{p1['id']}", headers=auth)
        assert get_hit.status_code == 200
        assert get_hit.headers.get("X-Cache") == "HIT"
        assert get_hit.json() == p1

        # Bypass header forces DB read
        get_bypass = client.get(
            f"/products/{p1['id']}",
            headers={**auth, "X-Cache-Control": "no-cache"},
        )
        assert get_bypass.status_code == 200
        assert get_bypass.headers.get("X-Cache") == "BYPASS"

        # TTL expiration test (CACHE_TTL_SECONDS=2)
        time.sleep(2.1)
        get_after_ttl = client.get(f"/products/{p1['id']}", headers=auth)
        assert get_after_ttl.status_code == 200
        assert get_after_ttl.headers.get("X-Cache") == "MISS"

        # Cache stats endpoint
        stats = client.get("/products/cache/stats", headers=auth)
        assert stats.status_code == 200
        stats_body = stats.json()
        assert stats_body["hits"] >= 2
        assert stats_body["misses"] >= 2

        # Delete product -> 204 and immediate cache invalidation
        del_res = client.delete(f"/products/{p1['id']}", headers=auth)
        assert del_res.status_code == 204
        assert client.get(f"/products/{p1['id']}", headers=auth).status_code == 404
        remaining_ids = [item["id"] for item in client.get("/products", headers=auth).json()]
        assert p1["id"] not in remaining_ids
        assert p2["id"] in remaining_ids
