import pytest

from scripts.traffic import local_checkout_url, parse_mix, scenario_plan


def test_local_checkout_url_rejects_external_and_credentialed_targets() -> None:
    assert local_checkout_url("http://127.0.0.1:8000") == (
        "http://127.0.0.1:8000/checkout"
    )
    assert local_checkout_url("http://localhost:8000/checkout") == (
        "http://localhost:8000/checkout"
    )
    with pytest.raises(ValueError, match="localhost"):
        local_checkout_url("https://example.com")
    with pytest.raises(ValueError, match="credentials"):
        local_checkout_url("http://user:password@127.0.0.1:8000")


def test_scenario_plan_is_finite_complete_and_deterministic() -> None:
    weights = parse_mix("normal=50,slow=20,error=15,out-of-stock=15")

    first = scenario_plan(20, weights, seed=2026)
    second = scenario_plan(20, weights, seed=2026)

    assert first == second
    assert len(first) == 20
    assert first.count("normal") == 10
    assert first.count("slow") == 4
    assert first.count("error") == 3
    assert first.count("out-of-stock") == 3
