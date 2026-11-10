# Store Product Service

Serviço de catálogo de produtos (`POST /products`, `GET /products`, `GET /products/{id}`, `DELETE /products/{id}`) em Python/FastAPI com persistência em PostgreSQL e **Cache Redis (Cache-Aside)** como tratamento de gargalo da Pessoa B.

## Endpoints

| Método | Rota | Autenticação | Resposta |
|---|---|---|---|
| `POST` | `/products` | Bearer JWT | `201 Created`: `id`, `name`, `price`, `unit` |
| `GET` | `/products` | Bearer JWT | `200 OK`: lista de produtos (suporta `?name=` com `ILIKE`) |
| `GET` | `/products/{id}` | Bearer JWT | `200 OK`: produto por ID; `404` se inexistente |
| `DELETE` | `/products/{id}` | Bearer JWT | `204 No Content` e invalidação imediata do cache |
| `GET` | `/products/cache/stats` | Bearer JWT | `200 OK`: métricas de hits, misses, bypasses, evictions e hit ratio |
| `GET` | `/health-check` | Público | Saúde do processo |
| `GET` | `/ready` | Público | Prontidão do banco e conectividade Redis |
| `GET` | `/metrics` | Público (interno) | Métricas Prometheus |

## Tratamento de Gargalo — Cache Redis

- Consultas `GET /products` e `GET /products/{id}` consultam o Redis antes do PostgreSQL (`X-Cache: HIT` vs `X-Cache: MISS`).
- Criação (`POST /products`) e exclusão (`DELETE /products/{id}`) invalidam imediatamente as chaves afetadas no Redis, garantindo consistência após remoção.
- O cabeçalho `X-Cache-Control: no-cache` permite comparar latência e vazão com e sem cache no mesmo ambiente.
