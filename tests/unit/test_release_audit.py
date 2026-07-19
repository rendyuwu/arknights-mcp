"""§T49 M5 release audit (§V16): the distributable artifacts carry code + policy
only.

§V16 forbids a release from shipping a raw snapshot, a prebuilt DB, artwork,
audio, story script, voice line, wiki/community prose, or a full announcement
body. ADR 0004 + ``NOTICE`` fix the release scope: "code, schema, migrations,
tests, and parsers only". So the *minimal* test fixtures are in-scope, but a raw
upstream dump (MBs) or a ``*.sqlite`` is not.

This audit builds the real wheel + sdist and proves, fail-closed:

* the wheel is code + package resources + dist metadata only (allowlist);
* the code-license files (``LICENSE`` / ``NOTICE``) ship in the wheel metadata;
* neither artifact carries a prebuilt DB, a promoted build, a raw-snapshot dir,
  or any binary game content (art / audio / video / asset bundle);
* every bundled data JSON is minimal, not a raw full dump (size cap);
* the sdist ships the complete policy / legal file set.

The build runs offline in the default gate (see :func:`build_distributions`).
"""

from __future__ import annotations

from tests.support import MAX_DATA_JSON_BYTES, REQUIRED_POLICY_FILES, BuiltDistributions

# --- Forbidden-in-any-release-artifact patterns (§V16) -----------------------

#: A promoted / prebuilt database is never distributed -- users build their own.
_DATABASE_SUFFIXES = (".sqlite", ".sqlite3", ".db")

#: Binary game content: artwork, audio, video, and Unity/asset-bundle payloads.
_GAME_BINARY_SUFFIXES = (
    # artwork
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tga",
    ".psd",
    ".svg",
    # audio
    ".wav",
    ".mp3",
    ".ogg",
    ".flac",
    ".acb",
    ".awb",
    ".fsb",
    ".bank",
    # video
    ".mp4",
    ".mov",
    ".webm",
    # Unity / asset bundles
    ".ab",
    ".bundle",
    ".assetbundle",
    ".unity3d",
    ".bytes",
    ".ress",
)

#: Path segments that mean "a promoted build" or "a raw acquired snapshot" --
#: git-ignored working dirs that must never reach an artifact (ADR 0004).
_FORBIDDEN_PATH_SEGMENTS = ("snapshot", "snapshots")


def _forbidden_reason(path: str) -> str | None:
    """Return why ``path`` is forbidden in a release artifact, or ``None``."""
    lower = path.lower()
    if lower.endswith(_DATABASE_SUFFIXES):
        return "prebuilt/promoted database"
    if lower.endswith(_GAME_BINARY_SUFFIXES):
        return "binary game content"
    if path == "data/current.json" or path.startswith("data/builds/"):
        return "promoted build / manifest"
    segments = path.split("/")
    if any(seg in _FORBIDDEN_PATH_SEGMENTS for seg in segments):
        return "raw-snapshot directory"
    return None


def _all_members(dists: BuiltDistributions) -> dict[str, dict[str, int]]:
    return {"wheel": dists.wheel_sizes, "sdist": dists.sdist_sizes}


def test_wheel_is_code_and_metadata_only(built_distributions: BuiltDistributions) -> None:
    # Allowlist (fail-closed): a stray data file added to the package later has
    # no matching rule and trips this. The wheel is the installable artifact, so
    # it gets the tightest §V16 gate.
    def allowed(name: str) -> bool:
        return (
            name.endswith(".py")
            or (name.startswith("arknights_mcp/migrations/") and name.endswith(".sql"))
            or name == "arknights_mcp/py.typed"
            or ".dist-info/" in name
        )

    stray = sorted(name for name in built_distributions.wheel_sizes if not allowed(name))
    assert not stray, f"wheel carries non-code/non-metadata members: {stray}"


def test_wheel_ships_license_and_notice(built_distributions: BuiltDistributions) -> None:
    # V16: Apache-2.0 covers project code only; the NOTICE scoping must travel
    # with the artifact so the license boundary is legible to any consumer.
    names = built_distributions.wheel_sizes
    assert any(n.endswith(".dist-info/licenses/LICENSE") for n in names)
    assert any(n.endswith(".dist-info/licenses/NOTICE") for n in names)


def test_no_prebuilt_db_or_game_binary_in_artifacts(
    built_distributions: BuiltDistributions,
) -> None:
    # V16: neither the wheel nor the sdist may carry a database, a promoted
    # build, a raw-snapshot dir, or binary game content.
    offenders: list[str] = []
    for artifact, members in _all_members(built_distributions).items():
        for name in members:
            reason = _forbidden_reason(name)
            if reason is not None:
                offenders.append(f"{artifact}:{name} ({reason})")
    assert not offenders, f"forbidden bundled data in release artifacts: {sorted(offenders)}"


def test_bundled_json_data_is_minimal_not_raw_dump(
    built_distributions: BuiltDistributions,
) -> None:
    # V16 / §T15: fixtures may ship (ADR 0004 releases include tests), but each
    # must be a minimal fixture, never a raw full-dump snapshot. Size is the
    # cheap, content-agnostic proxy: a real upstream table is orders of
    # magnitude larger than the cap.
    oversize = sorted(
        f"{name} ({size} bytes)"
        for name, size in built_distributions.sdist_sizes.items()
        if name.endswith(".json") and size > MAX_DATA_JSON_BYTES
    )
    assert not oversize, (
        f"bundled JSON exceeds the minimal-fixture cap ({MAX_DATA_JSON_BYTES} bytes); "
        f"looks like a raw dump: {oversize}"
    )


def test_sdist_ships_complete_policy_file_set(built_distributions: BuiltDistributions) -> None:
    # V16: the source release carries the full policy / legal set so the data
    # boundary, takedown path, and privacy stance travel with the code.
    members = built_distributions.sdist_sizes
    missing = [name for name in REQUIRED_POLICY_FILES if name not in members]
    assert not missing, f"sdist missing required policy files: {missing}"
