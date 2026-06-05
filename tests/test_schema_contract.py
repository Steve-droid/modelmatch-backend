"""S3 contract snapshot: the wire contract is camelCase, and it's pinned.

The golden map below is the snapshot — a deliberate, reviewable record of every
field each schema exposes (by its camelCase alias). If a schema changes, this test
fails until the snapshot is updated on purpose, so the API contract never drifts
silently. It also proves no snake_case name leaks onto the wire.
"""

from decimal import Decimal

import pytest

from app import schemas

# alias (camelCase) field names each schema exposes on the wire.
CONTRACT = {
    "UserCreate": {"email", "password"},
    "UserOut": {"id", "email"},
    "ModelCreate": {"name", "vendor", "pricePerMtok", "dataPolicy"},
    "ModelOut": {"id", "name", "vendor", "pricePerMtok", "dataPolicy"},
    "HarnessOut": {"id", "name", "vendor"},
    "BenchmarkOut": {"id", "name", "taskType"},
    "BenchmarkResultCreate": {
        "modelId", "benchmarkId", "harnessId", "taskType", "score", "metric",
        "costPerMtok", "contextWindow", "source", "sourceDocumentId", "measuredAt",
    },
    "BenchmarkResultOut": {
        "id", "modelId", "benchmarkId", "harnessId", "taskType", "score", "metric",
        "costPerMtok", "contextWindow", "source", "sourceDocumentId", "measuredAt",
    },
    "CatalogRowIn": {
        "model", "vendor", "benchmark", "metric", "score", "costPerMtok", "harness",
        "harnessVendor", "taskType", "contextWindow", "source", "measuredAt",
    },
    "CatalogRowOut": {
        "id", "model", "vendor", "benchmark", "metric", "score", "costPerMtok",
        "harness", "harnessVendor", "taskType", "contextWindow", "source", "measuredAt",
    },
}


@pytest.mark.parametrize("name, expected", CONTRACT.items())
def test_schema_exposes_exactly_the_camelcase_contract(name, expected):
    schema_cls = getattr(schemas, name)
    properties = set(schema_cls.model_json_schema(by_alias=True)["properties"])
    assert properties == expected


def test_dump_emits_camelcase_and_no_snake_case_leaks():
    out = schemas.ModelOut(
        id=1, name="Claude Haiku", vendor="Anthropic",
        price_per_mtok=Decimal("0.25"), data_policy="private",
    )
    dumped = out.model_dump(by_alias=True)
    assert dumped["pricePerMtok"] == Decimal("0.25")
    assert dumped["dataPolicy"] == "private"
    assert not any("_" in key for key in dumped), f"snake_case leaked: {dumped}"


def test_populate_by_name_allows_snake_case_construction():
    # We construct with python (snake_case) names internally; aliases are wire-only.
    result = schemas.BenchmarkResultCreate(model_id=1, benchmark_id=2, cost_per_mtok=Decimal("3"))
    assert result.model_id == 1
    assert result.model_dump(by_alias=True)["modelId"] == 1
