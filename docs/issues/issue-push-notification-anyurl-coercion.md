# Bug: Push notification config stores Pydantic AnyUrl/enum objects instead of plain strings

**Affects:** `src/core/tools/media_buy_create.py` — `_create_media_buy_impl()`  
**Introduced by:** affinity-main commit `63c35c93` ("fix: adcopy-gen e2e fixes")  
**Status:** Fixed on `affinity-main`, **not yet ported to `feat/tmp-integration`**

---

## Problem

When a `push_notification_config` dict is passed to `_create_media_buy_impl`, the values
extracted from it may be Pydantic `AnyUrl` objects (for `url`) or enum instances (for
`auth_type`, `credentials`) rather than plain Python `str`. SQLAlchemy's `String` column
type does not accept these objects and raises a `StatementError` at flush time.

### Affected code (before fix)

```python
# src/core/tools/media_buy_create.py ~line 1690
url = push_notification_config.get("url")          # may be AnyUrl, not str
authentication = push_notification_config.get("authentication", {})
schemes = authentication.get("schemes", []) if authentication else []
auth_type = schemes[0] if schemes else None         # may be an enum, not str
credentials = authentication.get("credentials") if authentication else None  # may be AnyUrl/enum
```

### Fix (already on affinity-main)

```python
url = push_notification_config.get("url")
if url is not None:
    url = str(url)          # coerce AnyUrl → str for SQLAlchemy compatibility

schemes = authentication.get("schemes", []) if authentication else []
auth_type = str(schemes[0]) if schemes else None    # coerce enum → str

credentials = authentication.get("credentials") if authentication else None
if credentials is not None:
    credentials = str(credentials)                  # coerce AnyUrl/enum → str
```

## Proposed solution

Cherry-pick or manually apply the three `str()` coercions from affinity-main commit `63c35c93`
into `feat/tmp-integration` at the same location in `_create_media_buy_impl`.

The change is 6 lines and has no side effects — `str()` on a plain `str` is a no-op.

## Reproduction

1. Call `create_media_buy` via A2A with a `push_notification_config` whose `url` is a
   Pydantic `AnyUrl` instance (e.g. from a validated A2A request model).
2. Observe `sqlalchemy.exc.StatementError: (builtins.TypeError) SQLite/Postgres String
   column received AnyUrl` at the `session.flush()` call inside the push notification
   config registration block.
