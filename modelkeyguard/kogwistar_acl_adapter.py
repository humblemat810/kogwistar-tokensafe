from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Literal

from .kogwistar_import_guard import enforce_installed_kogwistar_only

ACLMode = Literal["private", "shared", "scope", "group", "public"]


@dataclass(frozen=True)
class AdapterInfo:
    backend: str
    detail: str


class MiniACLGraph:
    """Small compatibility graph with the same methods used by this app.

    The production path uses Kogwistar ACLGraph. This class keeps the repository
    runnable in a clean sandbox and mirrors Kogwistar's public/private/shared/
    scope/group decision semantics used by ModelKeyGuard.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str, str | None], list[Any]] = {}

    def add_record(self, **kwargs: Any) -> Any:
        target = type(
            "ACLTarget",
            (),
            {
                "truth_graph": kwargs["truth_graph"],
                "entity_id": kwargs["entity_id"],
                "grain": kwargs.get("grain", "node"),
                "target_item_id": kwargs.get("target_item_id"),
            },
        )()
        record = type(
            "ACLRecord",
            (),
            {
                "target": target,
                "version": kwargs["version"],
                "mode": kwargs["mode"],
                "created_by": kwargs.get("created_by"),
                "owner_id": kwargs.get("owner_id"),
                "security_scope": kwargs.get("security_scope"),
                "shared_with_principals": tuple(kwargs.get("shared_with_principals", ())),
                "shared_with_groups": tuple(kwargs.get("shared_with_groups", ())),
                "source_ids": tuple(kwargs.get("source_ids", ())),
                "derivation_type": kwargs.get("derivation_type"),
                "supersedes_version": kwargs.get("supersedes_version"),
                "tombstoned": kwargs.get("tombstoned", False),
            },
        )()
        key = (
            kwargs["truth_graph"],
            kwargs.get("grain", "node"),
            kwargs["entity_id"],
            kwargs.get("target_item_id"),
        )
        self._records.setdefault(key, []).append(record)
        return record

    def latest_record(self, *, truth_graph: str, entity_id: str, grain: str | None = None, target_item_id: str | None = None) -> Any | None:
        grains = [grain] if grain is not None else ["node", "edge", "artifact", "span", "grounding", "document"]
        found: list[Any] = []
        for g in grains:
            found.extend(self._records.get((truth_graph, g, entity_id, target_item_id), []))
        active = [r for r in found if not getattr(r, "tombstoned", False)]
        pool = active or found
        if not pool:
            return None
        rank = {"public": 0, "group": 1, "shared": 2, "scope": 3, "private": 4}
        return max(pool, key=lambda r: (r.version, rank.get(r.mode, 99)))

    def decide(
        self,
        *,
        truth_graph: str,
        entity_id: str,
        principal_id: str,
        grain: str | None = None,
        target_item_id: str | None = None,
        principal_groups: Iterable[str] = (),
        security_scope: str | None = None,
    ) -> Any:
        record = self.latest_record(truth_graph=truth_graph, entity_id=entity_id, grain=grain, target_item_id=target_item_id)
        def decision(visible: bool, reason: str) -> Any:
            return type("ACLDecision", (), {"visible": visible, "record": record, "reason": reason})()
        if record is None:
            return decision(False, "no_acl_record")
        if record.tombstoned:
            return decision(False, "tombstoned")
        if record.mode == "public":
            return decision(True, "public")
        if record.owner_id and principal_id == record.owner_id:
            return decision(True, "owner")
        if record.mode == "private":
            return decision(False, "private")
        if record.mode == "scope":
            ok = bool(security_scope) and security_scope == record.security_scope
            return decision(ok, "scope_match" if ok else "scope_mismatch")
        if record.mode == "shared":
            ok = principal_id in record.shared_with_principals
            return decision(ok, "principal_share" if ok else "not_shared")
        if record.mode == "group":
            ok = bool(set(principal_groups) & set(record.shared_with_groups))
            return decision(ok, "group_share" if ok else "not_shared")
        return decision(False, "unknown_mode")


def load_acl_graph() -> tuple[Any, AdapterInfo]:
    store_kind = (os.environ.get("MODELKEYGUARD_STORE", "") or "").strip().lower()
    require_installed = os.environ.get("MODELKEYGUARD_USE_INSTALLED_KOGWISTAR", "0") == "1" or store_kind == "kogwistar_postgres"

    if require_installed:
        try:
            enforce_installed_kogwistar_only()
            from kogwistar.acl.graph import ACLGraph  # type: ignore
            return ACLGraph(), AdapterInfo("kogwistar-package", "from kogwistar.acl.graph import ACLGraph")
        except Exception as exc:
            if store_kind == "kogwistar_postgres":
                raise RuntimeError("kogwistar_postgres requires installed Kogwistar ACLGraph (no compat fallback).") from exc
            return MiniACLGraph(), AdapterInfo("compat", f"installed Kogwistar ACL unavailable: {exc!r}")

    return MiniACLGraph(), AdapterInfo("compat", "standalone MiniACLGraph; set MODELKEYGUARD_USE_INSTALLED_KOGWISTAR=1 to use installed Kogwistar ACLGraph")
