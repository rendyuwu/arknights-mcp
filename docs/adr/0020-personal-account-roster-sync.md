# ADR 0020: Personal Yostar account roster sync — CLI login/sync on the owner's machine, PostgreSQL roster store, read-only roster tools

- **Status:** Accepted
- **Date:** 2026-09-29
- **Founder decision(s):** D10 reversed by the owner on 2026-09-29. Roster
  storage is allowed: opt-in via CLI, for one owner account, into the owner's
  own PostgreSQL database. Also changed:
  - SPEC §C line 23 (the "⊥ game login | roster storage" constraint);
  - the PRD §2 rows "Game-server login: Excluded" and "User roster storage:
    Deferred";
  - the PRD §6.2 exclusions "Game account login" and "Player account
    synchronization or roster storage";
  - PRD §10.9 rule 8, PRD §17.5 bullet 1 ("No Arknights credentials or player
    account identifiers") and PRD §27 rule 8 ("Never request game credentials
    or add direct game-server authentication").
  - For the account roster only: the PRD §2 rows "Data store: SQLite" and
    "Runtime data access: SQLite only", PRD §4.2 principle 4 ("User-facing
    tools read SQLite only"), PRD §9.2 question path ("Opens only the
    validated SQLite database in read-only mode"), and ADR 0003's
    "User-facing MCP tools read SQLite only". Game data keeps all of them
    unchanged.
  - Unchanged: D3 (allowlisted sync only), D7 (manual CLI; user questions
    never trigger sync), D13, D15.
  - The owner signed off that the protocol constants used below (the u8 sign
    key, the Yostar salt, and the SDK header values) are taken from the
    GPL-3.0 `ashleney/ArkPRTS` repository as interoperability facts only, with
    no ArkPRTS code copied.
- **Invariants:** §V1, §V2, §V5, §V12, §V15, §V16, §V17, §V19, §V27, §V28

## Context

The owner wants their own Arknights (Yostar, en) progress available to the
tools this project already serves: which operators they own, at what elite,
level, potential, skill level and mastery, which modules they have unlocked
and equipped, which skins and items they hold. D10 excluded exactly this, and
for good reason at the time: any game login is a ban-risk action, and this
project's serving host (a VPS) has a different public IP than the owner's
home connection, so a login from the VPS risks the account.

Doing this safely means separating two things that used to be one machine:
the machine that is allowed to talk to Yostar (the owner's home PC, on the
owner's normal IP) and the machine that serves MCP tools (the VPS). The
roster the home PC produces has to reach the VPS without the VPS ever
holding a Yostar credential or making a Yostar request itself.

There is no first-party Arknights API. The login flow below (email + a
one-time code exchanged for a long-lived session token, then that token used
to open a fresh game session) is the flow the `ashleney/ArkPRTS` project
documents against the live Yostar/Arknights servers. That repository's
`LICENSE` file is GPL-3.0 even though its packaging metadata claims MIT; this
project is Apache-2.0 and its own dependency audit refuses copyleft code, so
no code from that project is used. The protocol details — a handful of host
names, request shapes, and constant key/salt values needed to compute request
signatures — are treated as facts about how the Yostar/Arknights servers
work, not as copied implementation, and a small stdlib-only client is written
from scratch against them.

## Decision

(a) A CLI-only command group, `arknights-mcp account`, with five actions:
`login`, `sync`, `status`, `logout`, `purge`. None of the five is ever
registered as an MCP tool (§V28).

(b) The Yostar/Arknights client is this project's own code, built on the
standard library only, using ArkPRTS purely as a protocol reference. No
ArkPRTS code is copied and no ArkPRTS dependency is added; the GPL-3.0
LICENSE file vs. MIT packaging-metadata conflict in that upstream repository
is the reason a dependency was rejected rather than accepted.

(c) Every request the account client makes is checked against a host+path
allowlist before it is sent:
- `*.arknights.global`: `/config/prod/official/network_config`,
  `/user/v1/getToken`, `/account/login`, `/account/syncData`;
- `*.yostarplat.com`: `/yostar/send-code`, `/yostar/get-auth`,
  `/user/login`;
- `*.yo-star.com`: `/official/Android/version`.

No asset host and no game-data endpoint is reachable through this client;
the allowlist is a host+path pair check, so a response that tries to point a
later request at a different path on an allowlisted host still cannot reach
anything outside this list.

(d) The email address and the one-time code are used once, at `account
login`, and are never stored. What comes back — a Yostar account id and a
long-lived session token — is written to
`$XDG_CONFIG_HOME/arknights-mcp/yostar_session.json`, mode 600, on the
machine that ran the command (the owner's home PC). The machine that serves
MCP tools never holds that file and no MCP process reads it. `account sync`
reuses the saved token so only the first login needs an OTP.

(e) The roster lands in a PostgreSQL table, `account_roster`: one row per
server holding the whole allowlisted roster as JSON text. `account sync`
deletes and re-inserts that row inside one transaction, so a reader always
sees either the old roster or the new one, and a failed sync leaves the old
row in place. MCP tools read this table on every call; there is no
in-process caching of the roster across calls.

(f) Provenance for the roster is the `account_roster` row itself:
`snapshot_id`, `synced_at`, `transform_version`, `content_hash`. There is no
per-record provenance stamp, because one row is one snapshot of the whole
roster (§V17 applies at that scope).

(g) The account roster is not a source-registry entry (§V27): it is the
owner's personal data, not a shared dataset, and it does not participate in
`FIELD_POLICY_VERSION` (which only feeds the shared game-data build's
content hash).

(h) Only the `en` (Yostar) server is supported. There is no Hypergryph
(`cn`) login flow to build against.

(i) Retention: the roster row is kept until the next successful sync or
until `account purge` removes it. `account logout` only deletes the saved
session token; it does not touch the roster.

(j) Preventing the account from being signed out of a running game session
(a "kick") is procedural, not just a warning in a help string:
- `account sync` is its own command, entirely separate from the game-data
  `arknights-mcp sync`; neither one triggers the other.
- Both `account login` and `account sync` refuse to run when standard input
  is not a terminal, so neither can be scheduled by cron, a timer, or any
  other unattended job.
- Both commands print a notice and wait for the owner to confirm, at the
  terminal, that Arknights is fully closed on every device using the
  account, before sending a single request.
- A sync is one short burst of requests with no retries; the intermediate
  tokens and the fresh session secret live only in local variables of that
  one call, are never written anywhere, and every connection is closed
  immediately after its response.
- Between runs, nothing in this project talks to the game servers at all.

(k) Topology: `account login` and `account sync` run on the owner's home PC,
so every Yostar and game-server request leaves from the owner's normal home
IP; the VPS never logs in to Yostar. The VPS runs the MCP server and a
PostgreSQL container (`account-db`, a compose profile) that publishes its
port on the VPS's own loopback interface only. The home PC reaches that port
through an SSH local port forward, which carries database traffic only —
never Yostar traffic.

(l) Two PostgreSQL roles are created once, by a database-initialization
script: a writer role, used only by the CLI on the home PC, and a
SELECT-only reader role with read-only transactions by default, used only by
the serving process. Both roles' connection URLs are supplied through one
environment variable and are never printed or logged; any error naming a
database problem names only the exception's class, never the URL, host,
user, or password.

(m) Before sending anything to Yostar, `account login` and `account sync`
first prove the account database is reachable and writable. This means a
host holding only the reader URL, or no URL at all — the VPS — fails before
any Yostar request is made, even if someone runs the login command there by
mistake.

(n) The account database is reached through SQLAlchemy Core, with the
`pg8000` driver (pure Python, BSD-3-Clause) in production. `psycopg` /
`psycopg2` are LGPL and fail this project's copyleft dependency audit;
`asyncpg` is async-only, and this project's SQLAlchemy usage is synchronous.
A plain `sqlite:///` URL is also accepted, so the test suite can exercise the
whole account-store code path without a real PostgreSQL server.

## What does NOT change

- Game-data builds. `arknights-mcp sync` keeps pulling public GitHub
  mirrors into versioned SQLite files under `data/builds/`; none of that
  moves to PostgreSQL, and none of it touches the account database.
- D3 (allowlisted-source sync only) and D7 (sync/import are manual CLI
  actions; answering a user's question never triggers one) — the account
  commands add a second, separate manual CLI action, they do not relax
  either rule.
- D13 (no assumption of reuse permission from a public repository) and D15
  (OAuth/OIDC remote auth) are untouched.
- Every MCP tool stays read-only and stays off the CLI-only admin surface;
  the three new account tools follow the same read-only, typed-envelope
  contract as every existing tool.
- The rule that user-facing tools never make a network request at query
  time (§V1): the account tools read a database, the same shape of
  operation as reading the game-data SQLite file, not a request to Yostar.

## Consequences

- Using a third-party-documented login flow against Yostar's servers
  carries some ongoing ToS / account-action risk that this project cannot
  eliminate, only reduce through the confirmation gate and the CLI-only,
  manual-trigger design.
- Each `account sync` opens a new game-server session as a new device; an
  Arknights app that is still open or backgrounded on the same account will
  probably be signed out on its next request. This has not been verified
  against a second device. The close-the-game confirmation step reduces the
  chance of this happening mid-session but cannot detect whether the app is
  actually closed — it relies on the owner's word.
- How long the saved Yostar session token remains valid has not been
  checked; if Yostar eventually rejects it, `account sync` fails with a
  message pointing back at `account login`.
- On a deployment where the account database is reachable, anyone holding
  the reader credential, or root on the database host, can read the synced
  roster directly; there is no additional per-tool access scope beyond the
  OAuth/OIDC scopes this project already enforces for remote access.
- The SSH local forward must be open while `account sync` runs on the home
  PC; if it is not, the command fails at the writability check before
  sending anything to Yostar.
- When the account database is unreachable, all three account tools answer
  a typed `database_unavailable` result; every other tool is unaffected.
