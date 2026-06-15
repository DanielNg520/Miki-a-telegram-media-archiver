# Phase 8: Interoperability and Security

Phase 8 provides a transport-neutral, versioned integration dispatcher. It does not open a network
port. A future HTTP, Unix-socket, or plugin adapter can pass raw request bytes and authentication
metadata to `IntegrationService` without changing security or business rules.

## Contract Version 1

Requests contain `version`, a caller-generated `request_id`, `operation`, and object `data`.
Responses repeat the version and request ID, include `ok`, and return either `result` or a stable
`error.code` and `error.message`. Bodies are limited to 64 KiB.

## Operations and Scopes

- `route.preview` requires `submit` and resolves text without copying media.
- `library.search` requires `search` and returns bounded Telegram index references.
- `audit.list` requires `admin` and returns bounded audit metadata.

## Client Configuration

Clients are supplied through `INTEGRATION_CLIENTS_JSON`:

```json
[
  {
    "client_id": "catalog-service",
    "secret": "use-a-long-random-secret",
    "scopes": ["search"],
    "requests_per_minute": 30
  }
]
```

Secrets must contain at least 16 characters and remain environment-only.

## Signatures

An adapter supplies the client ID, Unix timestamp, unique nonce, hexadecimal HMAC-SHA256 signature,
and exact raw JSON bytes. Canonical signed bytes are:

```text
<timestamp>\n<nonce>\n<raw body>
```

Timestamps must be within `INTEGRATION_SIGNATURE_TTL`, default 300 seconds. Signature comparison is
constant-time. Authenticated nonces are atomically claimed and cannot be replayed.

## Quotas and Audit

Each client has an atomic fixed-window request quota. Audit events cover accepted and denied
integration requests, topic and route changes, manager changes, sorting outcomes, retrieval
submission/completion/cancellation, and dead-letter retries.

Audit details exclude secrets, signatures, raw captions, and raw search terms. Telegram
administrators may inspect recent events with:

```text
/audit_log [limit]
```

## Transport Boundary

No HTTP server is enabled in Phase 8. This avoids sharing the single SQLite connection with an
uncoordinated server thread and avoids exposing a port before TLS/proxy deployment decisions exist.
A later HTTP adapter must preserve exact raw body bytes and call this dispatcher.
