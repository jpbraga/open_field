"""Resource slots. Policy `reject`: a request that finds no free slot is refused, never queued.

A resource (e.g. `gpu`) has N slots, a user setting (1 by default). Every job of an `exclusive` service
takes one slot. Calls of a `shared` service (LLM proxies) take one slot together, up to max_concurrency
calls. Generation tools started outside the daemon take a slot each. Free memory is checked separately
by the job manager before a job may take a slot.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .config import ServiceSpec


@dataclass
class Holder:
    service: str
    mode: str
    owners: set[str] = field(default_factory=set)
    since: float = field(default_factory=time.time)


class Locks:
    def __init__(self, capacity: Callable[[str], int] = lambda r: 1) -> None:
        self._lock = threading.Lock()
        self.capacity = capacity
        self.held: dict[str, list[Holder]] = {}

    def try_acquire(self, spec: ServiceSpec, owner: str,
                    external: Optional[dict[str, int]] = None) -> Optional[dict[str, Any]]:
        """Take a slot of every resource the service declares, or none. Returns None on success,
        else the conflict. `external`: slots taken by tools outside the daemon, per resource."""
        external = external or {}
        with self._lock:
            plan: list[tuple[str, Optional[Holder]]] = []
            for r in spec.resources:
                holders = self.held.get(r, [])
                join = None
                if spec.lock == "shared":
                    join = next((h for h in holders if h.service == spec.id and h.mode == "shared"), None)
                    if join and len(join.owners) >= spec.max_concurrency:
                        return self._conflict(r, holders, external.get(r, 0),
                                              f"{spec.id} already has {len(join.owners)} calls running (its limit)")
                if join:
                    plan.append((r, join))
                    continue
                cap = self.capacity(r)
                used = len(holders) + external.get(r, 0)
                if used >= cap:
                    names = [h.service for h in holders] + ["a tool outside the daemon"] * external.get(r, 0)
                    if cap == 1 and holders and holders[0].service == spec.id and spec.lock != "shared":
                        reason = f"{spec.id} is already running a job"
                    elif cap == 1:
                        reason = f"{r} is in use by {names[0]}"
                    else:
                        reason = f"all {cap} {r} slots are in use ({', '.join(names)})"
                    return self._conflict(r, holders, external.get(r, 0), reason)
                plan.append((r, None))
            for r, join in plan:
                if join:
                    join.owners.add(owner)
                else:
                    self.held.setdefault(r, []).append(Holder(spec.id, spec.lock, {owner}))
            return None

    def _conflict(self, r: str, holders: list[Holder], external: int, reason: str) -> dict[str, Any]:
        return {"resource": r, "slots": self.capacity(r), "used": len(holders) + external,
                "services": [h.service for h in holders], "external": external,
                "owners": sorted(o for h in holders for o in h.owners), "reason": reason}

    def release(self, spec: ServiceSpec, owner: str) -> None:
        with self._lock:
            for r in spec.resources:
                holders = self.held.get(r, [])
                for h in list(holders):
                    if owner in h.owners:
                        h.owners.discard(owner)
                        if not h.owners:
                            holders.remove(h)
                if r in self.held and not holders:
                    del self.held[r]

    def used(self, r: str) -> int:
        with self._lock:
            return len(self.held.get(r, []))

    def snapshot(self, resources: list[str]) -> dict[str, Any]:
        with self._lock:
            return {r: {"slots": self.capacity(r), "used": len(self.held.get(r, [])),
                        "holders": [{"service": h.service, "mode": h.mode, "calls": len(h.owners), "since": h.since}
                                    for h in self.held.get(r, [])]}
                    for r in resources}
