"""Probe DELETE endpoints against a single entity to confirm the right URL shape.

Usage:
    python diagnose.py <entity_id>

The entity is deleted if one of the candidates returns 2xx. Pick something you
don't mind losing — a throwaway scene is ideal.
"""

from __future__ import annotations

import sys
import urllib.parse

import alexa_client


CANDIDATE_PATHS = [
    "/api/phoenix/appliance/{id}",
    "/api/behaviors/entities/{id}",
    "/api/phoenix/entity/{id}",
    "/api/smarthome/v2/entities/{id}",
    "/api/smarthome/v1/presentation/entities/{id}",
    "/api/phoenix/registration/{id}",
]


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python diagnose.py <entity_id>", file=sys.stderr)
        return 2
    entity_id = sys.argv[1]
    quoted = urllib.parse.quote(entity_id, safe="")

    ctx = alexa_client.login(alexa_client.load_config("setup.yaml"))
    print(f"\nProbing DELETE endpoints for entity id: {entity_id}\n")

    for tmpl in CANDIDATE_PATHS:
        path = tmpl.replace("{id}", quoted)
        resp = ctx.session.delete(f"{ctx.base_url}{path}", timeout=15)
        body = (resp.text or "").replace("\n", " ")[:140]
        print(f"  {resp.status_code:3d} DELETE {path:60s} -> {body!r}")
        if 200 <= resp.status_code < 300:
            print(f"\n*** SUCCESS on {tmpl} — entity {entity_id} is gone. ***")
            return 0

    print("\nAll candidates failed. Paste this output back.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
