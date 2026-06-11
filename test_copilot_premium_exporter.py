"""Tests for copilot_premium_exporter."""

from __future__ import annotations

import json
import re
from pathlib import Path

import responses
from prometheus_client import CollectorRegistry

from copilot_premium_exporter import CopilotPremiumCollector, ExporterConfig

SAMPLE_RESPONSE = {
    "timePeriod": {"year": 2026, "month": 6},
    "usageItems": [
        {
            "product": "Copilot",
            "sku": "Copilot Premium Request",
            "model": "GPT-5",
            "unitType": "requests",
            "pricePerUnit": 0.04,
            "grossQuantity": 100,
            "grossAmount": 4.0,
            "discountQuantity": 0,
            "discountAmount": 0.0,
            "netQuantity": 100,
            "netAmount": 4.0,
        },
        {
            "product": "Copilot",
            "sku": "Copilot Premium Request",
            "model": "Claude Sonnet 4.6",
            "unitType": "requests",
            "pricePerUnit": 0.01,
            "grossQuantity": 250,
            "grossAmount": 2.5,
            "discountQuantity": 50,
            "discountAmount": 0.5,
            "netQuantity": 200,
            "netAmount": 2.0,
        },
    ],
}

EMPTY_RESPONSE = {"timePeriod": {"year": 2026, "month": 6}, "usageItems": []}

# Match any month param in the URL
ENT_URL_PATTERN = re.compile(
    r"https://api\.github\.com/enterprises/test-ent"
    r"/settings/billing/premium_request/usage\?year=\d+&month=\d+"
)


def _config(**overrides: object) -> ExporterConfig:
    defaults = dict(token="test-token", enterprise="test-ent", cache_ttl=900, http_timeout=5)
    defaults.update(overrides)
    return ExporterConfig(**defaults)  # type: ignore[arg-type]


def _collect_as_dict(collector: CopilotPremiumCollector) -> dict[str, list]:
    """Run collect() and return {metric_name: [samples...]}."""
    result: dict[str, list] = {}
    for family in collector.collect():
        result[family.name] = list(family.samples)
    return result


def _mock_all_months(response_json, status=200):
    """Register a callback that returns the same response for any month query."""
    def callback(request):
        return (status, {}, json.dumps(response_json))

    responses.add_callback(
        responses.GET,
        re.compile(
            r"https://api\.github\.com/enterprises/test-ent"
            r"/settings/billing/premium_request/usage"
        ),
        callback=callback,
        content_type="application/json",
    )


class TestConstructor:
    def test_wires_up_config(self) -> None:
        config = _config()
        registry = CollectorRegistry()
        collector = CopilotPremiumCollector(config, registry=registry)
        assert collector._config.token == "test-token"
        assert collector._config.enterprise == "test-ent"
        assert collector._config.cache_ttl == 900

    def test_session_has_auth_header(self) -> None:
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        assert "Bearer test-token" in collector._session.headers["Authorization"]

    def test_session_has_api_version(self) -> None:
        collector = CopilotPremiumCollector(
            _config(api_version="2024-01-01"), registry=CollectorRegistry()
        )
        assert collector._session.headers["X-GitHub-Api-Version"] == "2024-01-01"


class TestLoadConfig:
    def test_loads_from_json(self, tmp_path: Path, monkeypatch) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "github_enterprise": "loaded-ent",
            "cache_ttl_seconds": 600,
            "log_level": "DEBUG",
        }))
        monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
        config = ExporterConfig.load(config_path=cfg_file)
        assert config.enterprise == "loaded-ent"
        assert config.cache_ttl == 600
        assert config.log_level == "DEBUG"
        assert config.token == "tok-123"

    def test_missing_enterprise_exits(self, tmp_path: Path, monkeypatch) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({}))
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        try:
            ExporterConfig.load(config_path=cfg_file)
            assert False, "Should have raised SystemExit"
        except SystemExit:
            pass

    def test_missing_token_exits(self, tmp_path: Path, monkeypatch) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"github_enterprise": "ent"}))
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        try:
            ExporterConfig.load(config_path=cfg_file)
            assert False, "Should have raised SystemExit"
        except SystemExit:
            pass


class TestCollectWithData:
    @responses.activate
    def test_yields_expected_families(self) -> None:
        _mock_all_months(SAMPLE_RESPONSE)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        assert "github_premium_request_usage_gross_quantity" in metrics
        assert "github_premium_request_usage_net_amount" in metrics
        assert "github_premium_request_scrape_success" in metrics
        assert "github_premium_request_last_scrape_timestamp_seconds" in metrics

    @responses.activate
    def test_correct_label_values(self) -> None:
        _mock_all_months(SAMPLE_RESPONSE)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        gross_qty_samples = metrics["github_premium_request_usage_gross_quantity"]
        # Each month returns 2 models, 3 months fetched = up to 6, but same data deduped per month
        gpt5 = [s for s in gross_qty_samples if s.labels["model"] == "GPT-5"]
        assert len(gpt5) >= 1
        assert gpt5[0].value == 100.0
        assert gpt5[0].labels["type"] == "enterprise"
        assert gpt5[0].labels["name"] == "test-ent"
        assert gpt5[0].labels["year"] == "2026"
        assert gpt5[0].labels["month"] == "6"
        assert gpt5[0].labels["product"] == "Copilot"

    @responses.activate
    def test_discount_values(self) -> None:
        _mock_all_months(SAMPLE_RESPONSE)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        disc_qty = metrics["github_premium_request_usage_discount_quantity"]
        claude = [s for s in disc_qty if s.labels["model"] == "Claude Sonnet 4.6"]
        assert len(claude) >= 1
        assert claude[0].value == 50.0

    @responses.activate
    def test_scrape_success_is_one(self) -> None:
        _mock_all_months(SAMPLE_RESPONSE)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)
        assert metrics["github_premium_request_scrape_success"][0].value == 1.0


class TestCacheTTL:
    @responses.activate
    def test_two_scrapes_within_ttl_make_one_set_of_calls(self) -> None:
        _mock_all_months(SAMPLE_RESPONSE)
        collector = CopilotPremiumCollector(_config(cache_ttl=600), registry=CollectorRegistry())

        list(collector.collect())
        first_call_count = len(responses.calls)

        list(collector.collect())
        # No additional calls — cache hit
        assert len(responses.calls) == first_call_count

    @responses.activate
    def test_scrape_after_ttl_expires_makes_new_calls(self) -> None:
        _mock_all_months(SAMPLE_RESPONSE)
        collector = CopilotPremiumCollector(_config(cache_ttl=0), registry=CollectorRegistry())

        list(collector.collect())
        first_call_count = len(responses.calls)

        list(collector.collect())
        # TTL=0 means always stale, so new calls are made
        assert len(responses.calls) > first_call_count


class TestEmptyUsageItems:
    @responses.activate
    def test_empty_items_yields_no_per_model_series(self) -> None:
        _mock_all_months(EMPTY_RESPONSE)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        assert len(metrics["github_premium_request_usage_gross_quantity"]) == 0
        # scrape_success is 0 when no months have data
        assert metrics["github_premium_request_scrape_success"][0].value == 0.0


class TestAuthFailure:
    @responses.activate
    def test_401_sets_scrape_success_to_zero(self) -> None:
        _mock_all_months({"message": "Bad credentials"}, status=401)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)
        assert metrics["github_premium_request_scrape_success"][0].value == 0.0

    @responses.activate
    def test_401_increments_failure_counter(self) -> None:
        _mock_all_months({"message": "Bad credentials"}, status=401)
        registry = CollectorRegistry()
        collector = CopilotPremiumCollector(_config(), registry=registry)
        list(collector.collect())

        value = registry.get_sample_value("github_premium_request_scrape_failures_total")
        assert value >= 1.0

    @responses.activate
    def test_403_also_fails(self) -> None:
        _mock_all_months({"message": "Forbidden"}, status=403)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)
        assert metrics["github_premium_request_scrape_success"][0].value == 0.0


class TestNotFound:
    @responses.activate
    def test_404_does_not_crash(self) -> None:
        _mock_all_months({"message": "Not Found"}, status=404)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        assert "github_premium_request_usage_gross_quantity" in metrics
        assert len(metrics["github_premium_request_usage_gross_quantity"]) == 0
