"""List and delete Alexa smart home entities.

Amazon killed /api/phoenix (returns HTTP 299 empty) in 2024/2025. The current
source of truth is /api/behaviors/entities?skillId=amzn1.ask.1p.smarthome,
which returns a flat array of everything: appliances, scenes, and groups,
classified by providerData.categoryType.

Devices are the exception. /api/behaviors/entities identifies an appliance by a
bare UUID, but DELETE /api/phoenix/appliance/<id> expects the *legacy*
applianceId (e.g. "AAA_SonarCloudService_<uuid>_7"). Handed a UUID it does not
recognise, that endpoint answers HTTP 200 with an empty body and does nothing -
so deletes appear to succeed while the account is unchanged. Devices are
therefore listed through /nexus/v1/graphql, the same query the Alexa app uses,
which returns legacyAppliance.applianceId alongside the friendly name.

Groups also appear at /api/phoenix/group with their legacy `groupId` form
(amzn1.HomeAutomation.ApplianceGroup.<accountId>.<uuid>) — that's still the
right URL for DELETE.

Routines are at /api/behaviors/v2/automations.
"""

from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from alexa_client import ClientContext

_RATE_LIMIT_SLEEP = 0.3
_MAX_RETRIES_429 = 3
_ENTITIES_PATH = "/api/behaviors/entities?skillId=amzn1.ask.1p.smarthome"
_GRAPHQL_PATH = "/nexus/v1/graphql"

_GRAPHQL_ENDPOINTS_QUERY = """
query CustomerSmartHome {
    endpoints(endpointsQueryParams: { paginationParams: { disablePagination: true } }) {
        items {
            friendlyName
            legacyAppliance {
                applianceId
                friendlyDescription
                manufacturerName
            }
        }
    }
}
"""


@dataclass
class Entity:
    id: str
    name: str
    extra: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class DeleteError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


def list_devices(ctx: ClientContext) -> list[Entity]:
    """Devices, identified by the legacy applianceId that DELETE requires.

    Uses /nexus/v1/graphql rather than /api/behaviors/entities: the latter's
    UUIDs are silently ignored by the delete endpoint (see module docstring).
    """
    out: list[Entity] = []
    for item in _graphql_endpoints(ctx):
        legacy = item.get("legacyAppliance") or {}
        appliance_id = legacy.get("applianceId")
        if not appliance_id:
            # Nothing we can delete without it; skip rather than pretend.
            continue
        manufacturer = (legacy.get("manufacturerName") or "").strip()
        description = (legacy.get("friendlyDescription") or "").strip()
        extra = "; ".join(x for x in (manufacturer, description) if x)
        out.append(
            Entity(
                id=appliance_id,
                name=item.get("friendlyName") or legacy.get("friendlyName") or "(unnamed)",
                extra=extra,
                raw=item,
            )
        )
    return out


def list_scenes(ctx: ClientContext) -> list[Entity]:
    return [_entity_from_behavior(e) for e in _entities(ctx) if _category(e) == "SCENE"]


def list_groups(ctx: ClientContext) -> list[Entity]:
    """Use /api/phoenix/group — it returns the legacy `groupId` needed for DELETE."""
    data = _get_json(ctx, "/api/phoenix/group")
    groups = data.get("applianceGroups") or []
    out: list[Entity] = []
    for g in groups:
        group_id = g.get("groupId") or g.get("groupIdentifier")
        if not group_id:
            continue
        out.append(
            Entity(
                id=group_id,
                name=g.get("name") or "(unnamed group)",
                extra=f"{len(g.get('applianceIds') or [])} members",
                raw=g,
            )
        )
    return out


def list_routines(ctx: ClientContext) -> list[Entity]:
    data = _get_json(ctx, "/api/behaviors/v2/automations?limit=200")
    items = data if isinstance(data, list) else (data.get("automations") or [])
    out: list[Entity] = []
    for r in items:
        aid = r.get("automationId")
        if not aid:
            continue
        status = r.get("status") or ""
        out.append(
            Entity(
                id=aid,
                name=(r.get("name") or "(unnamed routine)").strip(),
                extra=status.lower(),
                raw=r,
            )
        )
    return out


# ---------- DELETE ----------

def delete_device(ctx: ClientContext, entity_id: str) -> None:
    _delete(ctx, f"/api/phoenix/appliance/{urllib.parse.quote(entity_id, safe='')}")


def delete_scene(ctx: ClientContext, entity_id: str) -> None:
    _delete(ctx, f"/api/phoenix/appliance/{urllib.parse.quote(entity_id, safe='')}")


def delete_group(ctx: ClientContext, group_id: str) -> None:
    _delete(ctx, f"/api/phoenix/group/{urllib.parse.quote(group_id, safe='')}")


def delete_routine(ctx: ClientContext, automation_id: str) -> None:
    _delete(ctx, f"/api/behaviors/v2/automations/{urllib.parse.quote(automation_id, safe='')}")


KIND_LIST: dict[str, Callable[[ClientContext], list[Entity]]] = {
    "devices": list_devices,
    "groups": list_groups,
    "scenes": list_scenes,
    "routines": list_routines,
}

KIND_DELETE: dict[str, Callable[[ClientContext, str], None]] = {
    "devices": delete_device,
    "groups": delete_group,
    "scenes": delete_scene,
    "routines": delete_routine,
}


# ---------- internals ----------

_ENTITIES_CACHE_KEY = "_entities_cache"
_GRAPHQL_CACHE_KEY = "_graphql_cache"


def _graphql_endpoints(ctx: ClientContext) -> list[dict]:
    cache = getattr(ctx, _GRAPHQL_CACHE_KEY, None)
    if cache is not None:
        return cache
    resp = ctx.session.post(
        f"{ctx.base_url}{_GRAPHQL_PATH}",
        headers={"Content-Type": "application/json; charset=utf-8"},
        json={"query": _GRAPHQL_ENDPOINTS_QUERY},
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"POST {_GRAPHQL_PATH} returned {resp.status_code}: {resp.text[:300]}"
        )
    try:
        data = resp.json()
    except Exception:
        raise SystemExit(
            f"POST {_GRAPHQL_PATH} returned non-JSON: {resp.text[:300]}"
        )
    if data.get("errors"):
        raise SystemExit(f"{_GRAPHQL_PATH} errors: {data['errors']}")
    items = ((data.get("data") or {}).get("endpoints") or {}).get("items") or []
    setattr(ctx, _GRAPHQL_CACHE_KEY, items)
    return items


def invalidate_caches(ctx: ClientContext) -> None:
    """Drop cached listings so the next call re-reads from the server."""
    for key in (_ENTITIES_CACHE_KEY, _GRAPHQL_CACHE_KEY):
        if hasattr(ctx, key):
            delattr(ctx, key)


def _entities(ctx: ClientContext) -> list[dict]:
    cache = getattr(ctx, _ENTITIES_CACHE_KEY, None)
    if cache is not None:
        return cache
    data = _get_json(ctx, _ENTITIES_PATH)
    entities = data if isinstance(data, list) else []
    setattr(ctx, _ENTITIES_CACHE_KEY, entities)
    return entities


def _category(e: dict) -> str:
    return (e.get("providerData") or {}).get("categoryType") or ""


def _entity_from_behavior(e: dict) -> Entity:
    name = e.get("displayName") or "(unnamed)"
    desc = e.get("description") or ""
    availability = e.get("availability") or ""
    extra = f"{desc}; {availability.lower()}" if availability else desc
    return Entity(id=e.get("id") or "", name=name, extra=extra, raw=e)


def _get_json(ctx: ClientContext, path: str) -> Any:
    resp = ctx.session.get(f"{ctx.base_url}{path}", timeout=20)
    try:
        return resp.json()
    except Exception:
        raise SystemExit(
            f"GET {path} returned non-JSON (status {resp.status_code}, "
            f"content-type {resp.headers.get('content-type', '?')}).\n"
            f"First 400 bytes of body:\n{resp.text[:400]!r}\n"
            f"Final URL after redirects: {resp.url}"
        )


def _delete(ctx: ClientContext, path: str) -> None:
    delay = _RATE_LIMIT_SLEEP
    for attempt in range(_MAX_RETRIES_429 + 1):
        resp = ctx.session.delete(f"{ctx.base_url}{path}", timeout=20)
        if 200 <= resp.status_code < 300:
            time.sleep(_RATE_LIMIT_SLEEP)
            return
        if resp.status_code == 429 and attempt < _MAX_RETRIES_429:
            time.sleep(delay)
            delay *= 2
            continue
        raise DeleteError(resp.status_code, resp.text)
