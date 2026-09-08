"""The one exit-code table, pinned. A Jenkins stage branches on these numbers."""

from agent import errors
from agent.errors import (
    AgentConfigError,
    CeilingExceeded,
    MalformedFindings,
    ModelRefused,
    ProviderError,
    AgentError,
    looks_like_refusal,
)


def test_table_values():
    assert errors.EXIT_PASS == 0
    assert errors.EXIT_GATE_FAIL == 1
    assert errors.EXIT_MALFORMED == 2
    assert errors.EXIT_REFUSED == 3
    assert errors.EXIT_CONFIG == 4
    assert errors.EXIT_CEILING == 124
    assert 0 not in errors.NON_PASS_CODES and len(errors.NON_PASS_CODES) == 5


def test_every_error_maps_to_a_non_pass_code():
    for exc in (MalformedFindings("x"), ModelRefused("x"), CeilingExceeded("token", "x"),
                AgentConfigError("x"), ProviderError("x")):
        assert isinstance(exc, AgentError)
        assert exc.exit_code in errors.NON_PASS_CODES
    assert ModelRefused("x").exit_code == 3
    assert CeilingExceeded("step", "x").exit_code == 124
    assert errors.TokenCeilingExceeded is CeilingExceeded   # v1 name still importable


def test_refusal_detector():
    assert looks_like_refusal("I cannot fulfill this request.")
    assert looks_like_refusal("Sorry, I'm unable to assist with a security audit.")
    assert not looks_like_refusal('{"version":"1.0.0","results":[]}')
    assert not looks_like_refusal("")
