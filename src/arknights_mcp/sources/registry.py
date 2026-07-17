"""Machine-readable source-registry loader (SPEC §V27; PRD Section 10.1).

Loads ``config/data_sources.toml`` into typed entries and enforces registry
completeness for every enabled source. Exposes a public-safe projection for the
``get_data_sources`` tool that excludes secrets, local filesystem paths, OAuth
config, and takedown correspondence.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Mandatory fields every enabled source must populate (§V27 / PRD 10.1). The
# active snapshot commit/version is tracked at runtime (source_snapshots), not
# in this static registry.
_MANDATORY_FOR_ENABLED = (
    "owner_name",
    "canonical_url",
    "source_type",
    "purpose",
    "license_status",
    "permission_status",
    "attribution_text",
    "last_reviewed_at",
)

# Fields excluded from the public-safe view exposed by get_data_sources (§V27).
_INTERNAL_ONLY_FIELDS = frozenset({"policy_notes", "private_hosting_status"})


class RegistryError(ValueError):
    """Raised when the source registry is missing, malformed, or incomplete."""


class SourceRegistryEntry(BaseModel):
    """One source's registry record (mirrors the ``data_sources`` table)."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    display_name: str = ""
    owner_name: str = ""
    canonical_url: str = ""
    source_type: str = ""
    regions: list[str] = Field(default_factory=list)
    purpose: str = ""
    fields_consumed: list[str] = Field(default_factory=list)
    adapter_version: str = ""
    transform_version: str = ""
    license_identifier: str = ""
    license_status: str = ""
    permission_status: str = ""
    private_hosting_status: str = ""
    redistribution_status: str = ""
    attribution_text: str = ""
    contact_url: str = ""
    policy_notes: str = ""
    enabled: bool = False
    last_reviewed_at: str = ""

    def missing_mandatory_fields(self) -> list[str]:
        """Mandatory fields that are empty (only meaningful for enabled sources)."""
        missing: list[str] = []
        for field in _MANDATORY_FOR_ENABLED:
            value = getattr(self, field)
            if isinstance(value, str) and not value.strip():
                missing.append(field)
        if not self.regions:
            missing.append("regions")
        return missing

    def public_view(self) -> dict[str, Any]:
        """Public-safe projection for get_data_sources (§V27).

        Excludes internal-only fields (policy notes, private-hosting posture).
        The registry holds no secrets, local paths, or OAuth config by design.
        """
        data = self.model_dump()
        for field in _INTERNAL_ONLY_FIELDS:
            data.pop(field, None)
        return data


class SourceRegistry(BaseModel):
    """The full set of registered sources, keyed by ``source_id``."""

    model_config = ConfigDict(extra="forbid")

    entries: dict[str, SourceRegistryEntry] = Field(default_factory=dict)

    def get(self, source_id: str) -> SourceRegistryEntry | None:
        return self.entries.get(source_id)

    def enabled(self) -> list[SourceRegistryEntry]:
        return [e for e in self.entries.values() if e.enabled]

    def public_registry(self) -> list[dict[str, Any]]:
        """Public-safe registry list for get_data_sources, sorted by source_id."""
        return [self.entries[sid].public_view() for sid in sorted(self.entries)]

    def assert_complete(self) -> None:
        """Enforce §V27: every enabled source has all mandatory fields."""
        problems: list[str] = []
        for source_id, entry in sorted(self.entries.items()):
            if entry.enabled:
                missing = entry.missing_mandatory_fields()
                if missing:
                    problems.append(f"{source_id}: missing {', '.join(missing)}")
        if problems:
            raise RegistryError("incomplete source registry (§V27): " + "; ".join(problems))


def load_source_registry(path: str | Path, *, validate: bool = True) -> SourceRegistry:
    """Load ``config/data_sources.toml`` into a :class:`SourceRegistry`.

    Each top-level TOML table is a source keyed by its ``source_id``. When
    ``validate`` is true (default), completeness for enabled sources is enforced.
    """
    p = Path(path)
    if not p.is_file():
        raise RegistryError(f"source registry not found: {p.name}")
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    entries: dict[str, SourceRegistryEntry] = {}
    for source_id, table in raw.items():
        if not isinstance(table, dict):
            raise RegistryError(f"registry entry {source_id!r} is not a table")
        entries[source_id] = SourceRegistryEntry(source_id=source_id, **table)
    registry = SourceRegistry(entries=entries)
    if validate:
        registry.assert_complete()
    return registry
