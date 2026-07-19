"""Unit-test fixtures.

``built_distributions`` builds the wheel + sdist exactly once per session (the
build shells out and is slow) and shares them with every test that inspects a
release artifact -- the packaging smoke (§T47) and the release audit (§T49).
"""

from __future__ import annotations

import pytest
from tests.support import BuiltDistributions, build_distributions


@pytest.fixture(scope="session")
def built_distributions(tmp_path_factory: pytest.TempPathFactory) -> BuiltDistributions:
    outdir = tmp_path_factory.mktemp("dist")
    return build_distributions(outdir)
