import json
import logging
import os
import time
import uuid
from fastapi import FastAPI, HTTPException, Request
from prometheus_fastapi_instrumentator import Instrumentator
import jwt

SERVICE = os.environ.get("SERVICE_NAME", "product")
SECRET = os.environ["JWT_SECRET"]
if len(SECRET) < 32:
    raise RuntimeError("JWT_SECRET must contain at least 32 characters")
ISSUER = "store-auth"
AUDIENCE = "store-api"
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(SERVICE)


def identity(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = jwt.decode(
            token,
            SECRET,
            algorithms=["HS256"],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "sub"]},
        )
        uuid.UUID(claims["sub"])
        return claims["sub"]
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})


def instrument(app: FastAPI):
    @app.middleware("http")
    async def log_request(request: Request, call_next):
        started = time.monotonic()
        request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Served-By"] = os.environ.get("HOSTNAME", SERVICE)
        route = request.scope.get("route")
        logger.info(
            json.dumps(
                {
                    "service": SERVICE,
                    "request_id": request_id,
                    "method": request.method,
                    "route": getattr(route, "path", "unmatched"),
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            )
        )
        return response

    Instrumentator().instrument(app).expose(app, include_in_schema=False)

    @app.get("/health-check")
    def health():
        return {"status": "ok", "service": SERVICE}
