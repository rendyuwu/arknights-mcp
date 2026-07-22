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

_Last reviewed: 2026-07-22._

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

## `arknights_global_official_news` — announcement metadata (v0.2 M9), **enabled**

- **Owner / maintainer:** Official Arknights (Global) — Hypergryph / Yostar.
- **Canonical URL:** https://www.arknights.global/
- **Feed endpoint (verified 2026-07-21, SPEC §V61):** `https://ark-us-static-online.yo-star.com/announce/Android/announcement.meta.json` — the client `network_config` key `an` value with the canonical `Android` platform substituted for `{0}` (`IOS` returns identical content). Distinct first-party host per region so en/cn are never mixed (§V5).
- **Source type / regions:** first-party announcement website; region `en`.
- **Fields / domains consumed (metadata-only maximum scope, D14; SPEC §V56):** `announce_id`, `title`, `date`, `url`, `category`, `region` — nothing else. The real feed names three of these differently (`day`+`month` → `date`, `webUrl` → `url`, `group` → `category`, field-mapped by the importer, §V61); those source keys are int/enum/name strings, never prose. Never the article body, HTML, prose, promotional images, image URLs, or full copied announcements.
- **Adapter / transform versions:** `official_news` adapter (metadata-only maximum scope).
- **License / permission status:** first-party copyrighted website; metadata-only maximum scope, M9 policy review recorded (see below).
- **Private-hosting status:** deferred.
- **Redistribution status:** prohibited.
- **Required attribution:** "Announcement dates from official Arknights channels © Hypergryph / Yostar."
- **Contact / issue URL:** official channels.
- **Enabled:** yes — enabled 2026-07-21 (§V56 flip, M9 review satisfied D14). Metadata-only remains the PERMANENT ceiling regardless of enabled state; widening requires a new review. The `sync` ride-along fetches this feed only when the source id is in `[sync].enabled_sources` and `[sync.arknights_global_official_news].feed_url` is set (§T106/§T107).
- **Current snapshot commit / retrieved at:** n/a.
- **Last reviewed:** 2026-07-21 (M9 source policy review, D14 — metadata-only scope confirmed; full announcement body prohibited; feed endpoint + field-map verified, §V61).

## `arknights_cn_official_news` — announcement metadata (v0.2 M9), **enabled**

- **Owner / maintainer:** Official Arknights (CN) — Hypergryph.
- **Canonical URL:** https://ak.hypergryph.com/
- **Feed endpoint (verified 2026-07-21, SPEC §V61):** `https://ak-conf.hypergryph.com/config/prod/announce_meta/Android/announcement.meta.json` — distinct first-party host from the Global feed so en/cn are never mixed (§V5).
- **Source type / regions:** first-party announcement website; region `cn`.
- **Fields / domains consumed (metadata-only maximum scope, D14; SPEC §V56):** `announce_id`, `title`, `date`, `url`, `category`, `region` — same metadata-only posture as the Global news source above, including the §V61 field-map (`day`+`month` → `date`, `webUrl` → `url`, `group` → `category`). Never the article body, HTML, prose, images, or image URLs.
- **Adapter / transform versions:** `official_news` adapter (metadata-only maximum scope).
- **License / permission status:** first-party copyrighted website; metadata-only maximum scope, M9 policy review recorded (see below).
- **Private-hosting status:** deferred.
- **Redistribution status:** prohibited.
- **Required attribution:** "Announcement dates from official Arknights channels © Hypergryph."
- **Contact / issue URL:** official channels.
- **Enabled:** yes — enabled 2026-07-21 (same posture + ride-along gate as the Global news source).
- **Current snapshot commit / retrieved at:** n/a.
- **Last reviewed:** 2026-07-21 (M9 source policy review, D14 — metadata-only scope confirmed; full announcement body prohibited; feed endpoint + field-map verified, §V61).

## `arknights_extra_locale_names` — extra-locale NAME aliases (v0.2 M10), **enabled**

- **Owner / maintainer:** ArknightsAssets / ArknightsGamedata project.
- **Canonical URL:** https://github.com/ArknightsAssets/ArknightsGamedata
- **Source type / regions:** structured game-data snapshot; NAME locales `jp`, `kr`. These are NAME locales, **not** fact regions (§V57): a jp/kr NAME is attached as a locale-tagged search alias onto the *existing* en/cn entity that shares its `game_id`, and an alias match returns the entity's OWN en/cn region facts — jp/kr never become fact regions.
- **Fields / domains consumed (NAME-only, §V57/§V18):** the canonical `name` from `character_table.json` (operators) and `enemy_handbook_table.json` (enemies) — nothing else. A machine-translated `description` / prose is never read or stored (D6 forbids bulk MT).
- **Adapter / transform versions:** adapter `extra_locale_aliases` v0.1 (CLI-only network fetch — HTTPS-only, endpoint allowlist, size / JSON-depth / node-count / redirect caps; never invoked at query time, §V1). Field policy / transform v1.
- **License / permission status:** no explicit dataset license assumed; not granted, cautious private MVP (same posture as the primary snapshot).
- **Private-hosting status:** permitted private, authenticated, with attribution and kill switch.
- **Redistribution status:** prohibited.
- **Required attribution:** "Game data snapshots courtesy of the ArknightsAssets/ArknightsGamedata project. Arknights game content © Hypergryph / Yostar."
- **Contact / issue URL:** https://github.com/ArknightsAssets/ArknightsGamedata/issues
- **Enabled:** yes — enabled 2026-07-21 (importer + FTS rebuild + `search_entities` `locale` param landed T99/T100/T101; `sync` ride-along wired T109). The ride-along fetches a locale only when the source id is in `[sync].enabled_sources` **and** a `[sync.arknights_extra_locale_names].base_url` is configured for that locale (there is NO shipped default URL, so an unconfigured install fetches nothing).
- **Current snapshot commit / retrieved at:** n/a.
- **Last reviewed:** 2026-07-21.

## `arknights_game_resource` — image URL references (v0.3 M12), **disabled by default**

- **Owner / maintainer:** yuanyan3060 (community mirror `ArknightsGameResource`).
- **Canonical URL:** https://github.com/yuanyan3060/ArknightsGameResource
- **Source type / regions:** image-asset URL reference (query-time DERIVED links, no import); regions `en`, `cn`. The `game_id` used to build a link is already region-scoped, so en/cn are never mixed (§V5).
- **Fields / domains consumed:** none — nothing is imported. Image URLs are DERIVED at response-build time from a `game_id` already stored from the primary snapshot (operator portrait/avatar/skin from `operators.game_id`, enemy sprite from `enemies.game_id`). The database holds no bytes and no URL; the server performs no fetch/HEAD/GET/validation at import or query time (§V1/§V24/§V63). See [ADR 0008](docs/adr/0008-art-asset-url-references.md).
- **Adapter / transform versions:** n/a (no adapter, no importer, no transform — pure query-time derivation).
- **License / permission status:** the mirror's **AGPL-3.0 license covers its code only**; the referenced art assets are self-declared Yostar/Hypergryph copyright, "for learning and exchange only, content subject to removal upon request." No permission is assumed (D13). Referenced under a private, non-commercial posture with immediate takedown.
- **Private-hosting status:** private, non-commercial only — **never public** (D4/§C). Cannot be enabled for any non-loopback/public deployment by a single flag.
- **Redistribution status:** reference-link only — **never bytes**. No artwork, image bytes, or repository code enters any release artifact or the database (§V16 airtight; zero AGPL code intake keeps the copyleft out of the Apache-2.0 tree).
- **Required attribution:** "Image URL references courtesy of the yuanyan3060/ArknightsGameResource mirror (repository code AGPL-3.0). Referenced Arknights art assets © Hypergryph / Yostar, for learning and exchange only, removed on request."
- **Contact / issue URL:** https://github.com/yuanyan3060/ArknightsGameResource/issues
- **Enabled:** no — OFF by default. When enabled, `get_operator`/`get_enemy` carry an additive optional `image_refs` list; disabling stops emitting references (kill switch, §V28/§V20). Takedown is a config flip with nothing to purge.
- **Current snapshot commit / retrieved at:** n/a — nothing is imported, so there is no snapshot commit for this source.
- **Last reviewed:** 2026-07-22 (M12, ADR 0008 — founder-approved private + non-commercial URL references only; image bytes remain excluded).

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
