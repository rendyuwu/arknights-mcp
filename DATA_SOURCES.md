# Data Sources

This is the human-readable mirror of the machine-readable source registry
(`config/data_sources.toml`). It is kept in sync with that file and with the
`get_data_sources` MCP tool and the `arknights://sources` resource. It is
updated whenever a source is enabled, disabled, purged, reviewed, or has its
attribution changed (see [`TAKEDOWN_POLICY.md`](TAKEDOWN_POLICY.md)).

Every enabled source records all of the following (PRD Section 10.1; SPEC §V27):
`source_id`, owner/maintainer, canonical URL, source type and regions, exact
fields/domains consumed, adapter and transform versions, license/permission
status, private-hosting status, redistribution status, required attribution
text, contact/issue URL, date last reviewed, enabled/disabled state, and the
current snapshot commit/version and retrieval time.

**Cautious posture (D13; PRD Section 10.9):** neither a public repository, nor
an attribution offer, nor a takedown offer is treated as permission to reuse.
No raw snapshots or prebuilt databases are distributed. Every source has a
removal mechanism.

_Last reviewed: 2026-07-20._

---

## `arknights_assets_gamedata` — **PRIMARY, enabled**

- **Owner / maintainer:** ArknightsAssets / ArknightsGamedata (community-maintained snapshot repository).
- **Canonical URL:** https://github.com/ArknightsAssets/ArknightsGamedata
- **Source type / regions:** structured multi-region game-data snapshots (JSON); regions `en`, `cn`.
- **Fields / domains consumed (allowlisted gameplay fields only):** selected fields from `character_table.json`, `skill_table.json`, `uniequip_table.json`, `uniequip_data.json`, `battle_equip_table.json`, `enemy_handbook_table.json`, `enemy_database.json`, `stage_table.json`, `zone_table.json`, `range_table.json`, and level files under the `levels` tree. See [`DATA_POLICY.md`](DATA_POLICY.md) for the exact field allowlist. Prose, art, audio, and story content are excluded.
- **Adapter / transform versions:** adapter `arknights_assets` v0.1; field policy / transform v1.
- **License / permission status:** No explicit dataset license is assumed; imported game content may also be governed by the game's rights holders. Status is *recorded, not assumed* and remains under review.
- **Private-hosting status:** permitted for a private, authenticated, non-commercial instance with attribution, field minimization, no database download, and a source kill switch.
- **Redistribution status:** prohibited — code-only releases; no bundled snapshots or prebuilt databases.
- **Required attribution:** "Game data snapshots courtesy of the ArknightsAssets/ArknightsGamedata project. Arknights game content © Hypergryph / Yostar."
- **Contact / issue URL:** https://github.com/ArknightsAssets/ArknightsGamedata/issues
- **Enabled:** yes (private MVP).
- **Current snapshot commit / retrieved at:** none imported into this repository (populated per import; the repository never commits snapshots).

## `kengxxiao_gamedata` — CN parser validation, **disabled in production**

- **Owner / maintainer:** Kengxxiao / ArknightsGameData.
- **Canonical URL:** https://github.com/Kengxxiao/ArknightsGameData
- **Source type / regions:** structured CN game-data snapshots (JSON); region `cn`.
- **Fields / domains consumed:** selected CN entities used only to cross-check the primary parser. Never a runtime dependency; never silently overrides primary values. Discrepancies become a validation report requiring investigation.
- **Adapter / transform versions:** validator `kengxxiao_validator` v0.1.
- **License / permission status:** No explicit dataset license is assumed; recorded, not assumed.
- **Private-hosting status:** not hosted; development/CI only.
- **Redistribution status:** prohibited.
- **Required attribution:** "CN validation data courtesy of the Kengxxiao/ArknightsGameData project."
- **Contact / issue URL:** https://github.com/Kengxxiao/ArknightsGameData/issues
- **Enabled:** no (development / CI validation only; disabled by default in production).
- **Current snapshot commit / retrieved at:** n/a.

## `penguin_statistics` — drop-rate statistics (v0.2 M8), **disabled by default**

- **Owner / maintainer:** Penguin Statistics.
- **Canonical URL:** https://penguin-stats.io
- **Source type / regions:** observed drop-rate statistics via the documented Penguin Statistics v2 API; regions `en`, `cn` (penguin server `US`/`Global` → `en`, `CN` → `cn`; `JP`/`KR` are out of scope and dropped, not mislabelled).
- **Fields / domains consumed (allowlisted endpoints only):** `result/matrix` (drop matrix), `stages`, and `items` metadata. No prose, art, or other content. Cached drop observations record server, sample size, fetched time, expiry, penguin snapshot id, region, and attribution.
- **Adapter / transform versions:** adapter `penguin_statistics` v0.1 (CLI-only network fetch — HTTPS-only, endpoint allowlist, size / JSON-depth / node-count / redirect caps; never invoked at query time). Field policy / transform not yet defined (drop importer lands with `get_stage_drops`).
- **License / permission status:** CC BY-NC 4.0 per its API documentation; attribution required; **non-commercial only**.
- **Private-hosting status:** permitted for a private, non-commercial instance with attribution.
- **Redistribution status:** prohibited — code-only releases; no bundled drop caches.
- **Required attribution:** "Drop-rate statistics © Penguin Statistics, licensed CC BY-NC 4.0."
- **Contact / issue URL:** https://penguin-stats.io
- **Enabled:** no — the adapter and registry entry exist, but the source stays disabled by default until the drop importer and `get_stage_drops` tool are complete and the enablement is reviewed.
- **Current snapshot commit / retrieved at:** n/a (populated per import).

## `arknights_global_official_news` — future announcement metadata, **disabled**

- **Owner / maintainer:** Official Arknights (Global) — Hypergryph / Yostar.
- **Canonical URL:** https://www.arknights.global/
- **Source type / regions:** first-party announcement website; region `en`.
- **Fields / domains consumed:** none in v0.1. If enabled later, metadata only — title, publication time, event start/end, region, announcement type, and canonical link. Never article bodies, promotional images, or full copied announcements.
- **Adapter / transform versions:** `official_news` adapter (disabled; metadata-only maximum scope).
- **License / permission status:** first-party copyrighted website; metadata-only policy pending implementation review.
- **Private-hosting status:** deferred.
- **Redistribution status:** prohibited.
- **Required attribution:** "Announcement dates from official Arknights channels © Hypergryph / Yostar."
- **Contact / issue URL:** official channels.
- **Enabled:** no (disabled by default; remains disabled until the core importer is stable and the source policy is reviewed).
- **Current snapshot commit / retrieved at:** n/a.

## `arknights_cn_official_news` — future announcement metadata, **disabled**

- **Owner / maintainer:** Official Arknights (CN) — Hypergryph.
- **Canonical URL:** https://ak.hypergryph.com/
- **Source type / regions:** first-party announcement website; region `cn`.
- **Fields / domains consumed:** none in v0.1 (same metadata-only posture as the Global news source above).
- **Adapter / transform versions:** `official_news` adapter (disabled).
- **License / permission status:** first-party copyrighted website; metadata-only policy pending review.
- **Private-hosting status:** deferred.
- **Redistribution status:** prohibited.
- **Required attribution:** "Announcement dates from official Arknights channels © Hypergryph."
- **Contact / issue URL:** official channels.
- **Enabled:** no.
- **Current snapshot commit / retrieved at:** n/a.

## `local_snapshot` — user-supplied snapshot adapter, **enabled (adapter)**

- **Owner / maintainer:** the local operator (user-supplied files).
- **Canonical URL:** n/a (local filesystem).
- **Source type / regions:** user-supplied directory tree matching the primary snapshot layout; regions `en`, `cn`.
- **Fields / domains consumed:** same allowlist as `arknights_assets_gamedata`.
- **Adapter / transform versions:** adapter `local_snapshot` v0.1; field policy / transform v1.
- **License / permission status:** inherits the legal status of the underlying source. Local possession is not proof of permission.
- **Private-hosting status:** personal import only, with mandatory provenance fields.
- **Redistribution status:** prohibited.
- **Required attribution:** inherits the attribution of the underlying source.
- **Contact / issue URL:** n/a.
- **Enabled:** yes (adapter available for personal import).
- **Current snapshot commit / retrieved at:** populated per import.
