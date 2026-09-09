# P38o public-demo controls

Public registration is capped at `MAX_REGISTERED_USERS=700` **total accounts**, including
pre-existing/seeded accounts. `0` closes new registrations. Existing password and Google logins
continue at capacity. This is not a LinkedIn-membership check or a storage/traffic ceiling.
The public frontend explains that this is a temporary demo and accounts/data may disappear at
teardown or rebuild; there is no new backup or retention promise.

## Admission and permissions

- Password signup, first Google login, and the demo user creator share the same PostgreSQL
  transaction advisory lock before inserting a user. Identity is rechecked after waiting;
  only committed accounts count. Password hashing and Google verification happen outside that lock.
- Capacity rejection is HTTP 409 with `detail.code=registration_capacity_reached` and a human
  message. Registration/login DTOs never accept an operator permission. Additional fields cannot
  grant it. Passwords have a 1024-character bound; edge auth bodies are capped separately.
- Migration `f3a4b5c6d7e8` adds `user.is_operator` default false, including existing users.
  Operator permission gates chat GET/POST and catalog POST/ingestion before paid dependencies.
  Chat still requires ownership of the selected project. Catalog reads and owned BYOK CI flows
  retain their prior access rules. `CHAT_ENABLED=false` disables chat even for the operator.
- `/auth/me` returns `chatEnabled`, derived on the server, with `Cache-Control: no-store`.
  Clients must hide chat until explicitly enabled; hiding it is not the authorization mechanism.
- Authentication POSTs share one Postgres token bucket across all hosts/workers/replicas.
  `AUTH_REQUESTS_PER_MINUTE=120` refills it at two requests/second; `AUTH_BURST=4` permits at most
  four immediately available tokens. Signup + chained login and Google challenge + verification
  each consume two requests. Rejections return 429 with Retry-After; storage failures return 503.
  The bucket stores one row and no IPs/emails. Edge per-IP limits provide additional fairness.
  `AUTH_RATE_LIMIT_ENABLED=false` is for isolated fixture tests, not the public deployment.
- `modelmatch_admission_rejections_total{reason="capacity|rate|unavailable"}` and existing HTTP
  status/latency metrics feed the application dashboard. No user/IP labels or credentials.

## Grant/revoke the one operator

After review and approved release, verify the intended existing account's ID and exact stored
email against Steve's signed-in `/auth/me` and owned demo projects. Do not select the first account
or automatically promote an email supplied during registration. The CLI has no public HTTP route:

```sh
python -m app.auth.operator --user-id VERIFIED_ID --expected-email VERIFIED_EMAIL --grant
python -m app.auth.operator --user-id VERIFIED_ID --expected-email VERIFIED_EMAIL --revoke
```

Run inside the approved backend execution context with its ordinary DB configuration. The
identity pair must match, grants are serialized, and a second operator must first be revoked.
The command prints only user ID/action. Never put live credentials in this document or command
output. No operator has been granted on the live stack as part of local implementation.

## Database pool sizing

Each worker has at most `DB_POOL_SIZE + DB_MAX_OVERFLOW = 5 + 5` app connections and
`CHAT_DB_POOL_SIZE + CHAT_DB_MAX_OVERFLOW = 2 + 2` chat connections. Two Gunicorn workers therefore
use at most 28 per backend pod; two pods would use at most 56, leaving headroom under Postgres's
100-connection limit for replication/monitoring/migrations. Pools grow on demand. Pool wait is
bounded by `DB_POOL_TIMEOUT=5` seconds. Recalculate this budget before changing worker/replica count.
700 registered users do not require 700 database connections.

## Verification and safe release

`tests/test_public_access.py` covers final-slot mixed concurrency, duplicate Google identities,
rollback, existing-user migration, direct paid-route denial before provider construction,
capability forgery/revocation, cross-host aggregate throttling, refill, and storage failure.
Existing chat tests explicitly grant operator status to fixture users and still prove ownership,
grounding, token caps and persistence. The rest of the auth/CI/savings contracts remain exercised.

The local-only capacity harness is `python -m ci.public_access_load`: `--seed` populates 700
synthetic accounts and 50 projects with 30 runs each; subsequent probes accept at most 50 clients
and 300 seconds. It refuses non-loopback targets, non-fake LLMs and a database name other than
`modicum_p38o_capacity`. Run migrations explicitly before seed. No reseeding of the live DB.

Release order: migration-only job on the new backend image (seeds disabled) → backend permission
and cap enforcement → verified operator grant → frontend capability UI → F5 ingress configuration
and Grafana panels. Verify all replicas and actual generated NGINX config before public promotion.
Choose fresh release tags after review; no image tags or live settings were changed locally.

Frontend rollback may keep the protected backend. Never roll backend/schema back to an
unrestricted version while the public API remains reachable. Keep the cap/permissions or block
the affected public endpoints first. Closing signup alone does not protect paid routes from
already registered users. Permission revocation is immediate on the next request; a stale tab
receives 403 and removes chat. The hourly model token cap stays in force for operator use.
