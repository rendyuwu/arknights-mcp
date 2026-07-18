"""§T29 envelope tests: §V21 (schema_version), §V22 (200 KB cap), §V23 (typed
status vocabulary + no stack-trace/path leak in errors)."""

from __future__ import annotations

import json

import pytest

from arknights_mcp.mcp.envelopes import (
    MAX_RESPONSE_BYTES,
    SCHEMA_VERSION,
    STATUS_VALUES,
    EnvelopeError,
    Provenance,
    build_envelope,
    error,
    internal_error,
    ok,
    serialized_size,
)


def _prov() -> Provenance:
    return Provenance(server="en", snapshot_id="snap-1", imported_at="2026-07-18T00:00:00+00:00")


# --- §V21: every envelope stamps the stable schema_version, first in the dict ---


def test_ok_envelope_stamps_schema_version_first() -> None:
    env = ok({"stage_code": "4-4"}, provenance=[_prov()])
    payload = env.to_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    # §I field order: schema_version leads the envelope.
    assert next(iter(payload)) == "schema_version"


def test_schema_version_is_stable_string() -> None:
    # A change here is a breaking wire-contract change (§V21 -> bump + ADR).
    assert SCHEMA_VERSION == "0.1"


def test_field_order_matches_interface_contract() -> None:
    env = ok({"a": 1}, provenance=[_prov()], analyzer_version="1")
    assert list(env.to_dict()) == [
        "schema_version",
        "status",
        "data",
        "provenance",
        "limitations",
        "analyzer_version",
    ]


# --- §V22: default response < 200 KB; oversized fails closed to bounded partial ---


def test_under_cap_response_passes_through() -> None:
    env = ok({"note": "small"}, provenance=[_prov()])
    assert env.status == "ok"
    assert serialized_size(env) <= MAX_RESPONSE_BYTES


def test_oversized_response_fails_closed_to_partial() -> None:
    huge = {"blob": "x" * (MAX_RESPONSE_BYTES + 10_000)}
    env = ok(huge, provenance=[_prov()])

    assert env.status == "partial"
    assert env.data == {}  # payload dropped, not emitted oversized
    assert serialized_size(env) <= MAX_RESPONSE_BYTES  # bounded response
    assert any("cap" in limit for limit in env.limitations)
    # Provenance (small, region attribution) is retained through the downgrade.
    assert env.provenance and env.provenance[0].server == "en"


# --- §V23: typed status vocabulary + safe error bodies ---


def test_status_vocabulary_is_exactly_the_ten_specified() -> None:
    expected = {
        "ok",
        "partial",
        "not_found",
        "ambiguous",
        "unsupported_server",
        "data_stale",
        "database_unavailable",
        "schema_incompatible",
        "analysis_unavailable",
        "internal_error",
    }
    assert expected == STATUS_VALUES


def test_build_envelope_rejects_unknown_status() -> None:
    with pytest.raises(EnvelopeError):
        build_envelope("kinda_ok")  # type: ignore[arg-type]


def test_error_requires_a_failure_status() -> None:
    # ``ok``/``partial`` are delivered results, not errors.
    with pytest.raises(EnvelopeError):
        error("ok", "should not be an error")  # type: ignore[arg-type]


def test_error_envelope_carries_typed_status_and_message() -> None:
    env = error("not_found", "no stage matched", suggested_action="run `arknights-mcp sync`")
    payload = env.to_dict()
    assert payload["status"] == "not_found"
    assert payload["data"]["message"] == "no stage matched"
    assert payload["data"]["suggested_action"] == "run `arknights-mcp sync`"


def test_internal_error_leaks_no_trace_or_path() -> None:
    env = internal_error()
    assert env.status == "internal_error"
    serialized = json.dumps(env.to_dict())
    # No local path, module traceback, or exception-repr markers reach the client.
    for marker in ("Traceback", "/home/", "/src/", ".py", "Error(", "line "):
        assert marker not in serialized
