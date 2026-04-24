#!/usr/bin/env python3
"""Wipe an Alexa account's smart home state: devices, groups, scenes, routines.

Default run is a dry-run that only lists what would be deleted. Pass --yes to
actually delete. Deletion order is routines → scenes → groups → devices so
references resolve cleanly.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import alexa_client
import endpoints
from endpoints import DeleteError, Entity

KINDS_ORDER = ("routines", "scenes", "groups", "devices")
KINDS_DISPLAY = {"devices": "Devices", "groups": "Groups", "scenes": "Scenes", "routines": "Routines"}


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--yes", action="store_true", help="actually delete (default: dry-run)")
    p.add_argument(
        "--only",
        default=",".join(KINDS_ORDER),
        help=f"comma-separated subset of {sorted(KINDS_ORDER)} (default: all)",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "setup.yaml",
        help="path to setup.yaml (default: ./setup.yaml)",
    )
    return p.parse_args(argv)


def _parse_only(value: str) -> list[str]:
    kinds = [k.strip() for k in value.split(",") if k.strip()]
    unknown = [k for k in kinds if k not in KINDS_ORDER]
    if unknown:
        raise SystemExit(f"--only: unknown kind(s): {', '.join(unknown)}")
    # Preserve the fixed deletion order regardless of how the user wrote them.
    return [k for k in KINDS_ORDER if k in kinds]


def _list_all(ctx: alexa_client.ClientContext, kinds: list[str]) -> dict[str, list[Entity]]:
    return {k: endpoints.KIND_LIST[k](ctx) for k in kinds}


def _print_inventory(inventory: dict[str, list[Entity]]) -> None:
    for kind in KINDS_ORDER:
        if kind not in inventory:
            continue
        items = inventory[kind]
        print(f"\n{KINDS_DISPLAY[kind]} ({len(items)}):")
        for it in items:
            extra = f"  {it.extra}" if it.extra else ""
            print(f"  - {it.name:<30} [{it.id}]{extra}")


def _delete_all(ctx: alexa_client.ClientContext, inventory: dict[str, list[Entity]]) -> tuple[int, int]:
    total = sum(len(v) for v in inventory.values())
    if total == 0:
        print("\nNothing to delete.")
        return 0, 0
    kinds_with_items = [k for k in KINDS_ORDER if inventory.get(k)]
    print(f"\nAbout to DELETE {total} entities across {len(kinds_with_items)} kinds.")
    print("Starting in 5 seconds — Ctrl-C to abort.")
    time.sleep(5)

    deleted = 0
    failed = 0
    for kind in KINDS_ORDER:
        items = inventory.get(kind) or []
        if not items:
            continue
        print(f"\n--- deleting {KINDS_DISPLAY[kind].lower()} ({len(items)}) ---")
        delete_fn = endpoints.KIND_DELETE[kind]
        for it in items:
            try:
                delete_fn(ctx, it.id)
                deleted += 1
                print(f"  ✓ deleted {it.name}")
            except DeleteError as exc:
                failed += 1
                print(f"  ✗ failed  {it.name}: {exc}")
            except Exception as exc:  # noqa: BLE001 — one bad item shouldn't abort the run
                failed += 1
                print(f"  ✗ error   {it.name}: {exc!r}")
    return deleted, failed


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not args.config.exists():
        raise SystemExit(f"config not found: {args.config} — copy setup.example.yaml to setup.yaml")
    kinds = _parse_only(args.only)

    config = alexa_client.load_config(args.config)
    print(f"Logging in as {config.email} ({config.alexa_url}) …")
    ctx = alexa_client.login(config)
    print(f"Logged in as {ctx.email} ({ctx.base_url})")

    print("Listing entities …")
    inventory = _list_all(ctx, kinds)
    _print_inventory(inventory)

    if not args.yes:
        print("\nDRY RUN — re-run with --yes to delete.")
        return 0

    deleted, failed = _delete_all(ctx, inventory)
    total = deleted + failed
    print(f"\nDeleted {deleted} of {total}. {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
