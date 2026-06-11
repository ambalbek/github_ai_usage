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

# The exporter calls the premium_request endpoint without query params.
PREMIUM_URL = (
    "https://api.github.com/enterprises/test-ent"
    "/settings/billing/premium_request/usage"
)
# Fallback general billing endpoint.
GENERAL_URL = (
    "https://api.github.com/enterprises/test-ent"
    "/settings/billing/usage"
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


class TestConstructor:
    def test_wires_up_config(self) -> None:
        config = _config()
        registry = CollectorRegistry()
        collector = CopilotPremiumCollector(config, registry=registry)
        assert collector._config.token == "test-token"
        assert collector._config.enterprise == "test-ent"
        assert collector._config.entity_type == "enterprise"
        assert collector._config.entity_name == "test-ent"
        assert collector._config.cache_ttl == 900

    def test_session_has_auth_header(self) -> None:
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        assert "Bearer test-token" in collector._session.headers["Authorization"]

    def test_session_has_api_version(self) -> None:
        collector = CopilotPremiumCollector(
            _config(api_version="2024-01-01"), registry=CollectorRegistry()
        )
        assert collector._session.headers["X-GitHub-Api-Version"] == "2024-01-01"

    def test_accept_header_is_github_json(self) -> None:
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        assert collector._session.headers["Accept"] == "application/vnd.github+json"


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

    def test_loads_organization(self, tmp_path: Path, monkeypatch) -> None:
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"github_organization": "my-org"}))
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        config = ExporterConfig.load(config_path=cfg_file)
        assert config.organization == "my-org"
        assert config.entity_type == "organization"

    def test_missing_both_exits(self, tmp_path: Path, monkeypatch) -> None:
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
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        assert "github_premium_request_usage_gross_quantity" in metrics
        assert "github_premium_request_usage_net_amount" in metrics
        assert "github_premium_request_usage_price_per_unit" in metrics
        assert "github_premium_request_scrape_success" in metrics
        assert "github_premium_request_last_scrape_timestamp_seconds" in metrics

    @responses.activate
    def test_correct_label_values(self) -> None:
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        gross_qty_samples = metrics["github_premium_request_usage_gross_quantity"]
        assert len(gross_qty_samples) == 2  # Two models

        gpt5 = [s for s in gross_qty_samples if s.labels["model"] == "GPT-5"]
        assert len(gpt5) == 1
        assert gpt5[0].value == 100.0
        assert gpt5[0].labels["type"] == "enterprise"
        assert gpt5[0].labels["name"] == "test-ent"
        assert gpt5[0].labels["year"] == "2026"
        assert gpt5[0].labels["month"] == "6"
        assert gpt5[0].labels["product"] == "Copilot"

    @responses.activate
    def test_all_seven_metric_values(self) -> None:
        """Verify every usage metric maps to the correct JSON field."""
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        # Check GPT-5 values across all metrics
        def gpt5_value(metric_name: str) -> float:
            samples = metrics[metric_name]
            match = [s for s in samples if s.labels["model"] == "GPT-5"]
            assert len(match) == 1, f"Expected 1 GPT-5 sample for {metric_name}, got {len(match)}"
            return match[0].value

        assert gpt5_value("github_premium_request_usage_gross_quantity") == 100.0
        assert gpt5_value("github_premium_request_usage_net_quantity") == 100.0
        assert gpt5_value("github_premium_request_usage_discount_quantity") == 0.0
        assert gpt5_value("github_premium_request_usage_gross_amount") == 4.0
        assert gpt5_value("github_premium_request_usage_net_amount") == 4.0
        assert gpt5_value("github_premium_request_usage_discount_amount") == 0.0
        assert gpt5_value("github_premium_request_usage_price_per_unit") == 0.04

    @responses.activate
    def test_discount_values(self) -> None:
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        disc_qty = metrics["github_premium_request_usage_discount_quantity"]
        claude = [s for s in disc_qty if s.labels["model"] == "Claude Sonnet 4.6"]
        assert len(claude) == 1
        assert claude[0].value == 50.0

    @responses.activate
    def test_scrape_success_is_one(self) -> None:
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)
        assert metrics["github_premium_request_scrape_success"][0].value == 1.0


class TestFallbackEndpoint:
    @responses.activate
    def test_falls_back_to_general_when_premium_empty(self) -> None:
        """When premium_request returns empty usageItems, try general billing."""
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        general_response = {
            "usageItems": [
                {
                    "product": "Copilot",
                    "sku": "Copilot Premium Request",
                    "unitType": "requests",
                    "quantity": 500,
                    "grossAmount": 20.0,
                    "netAmount": 18.0,
                    "discountAmount": 2.0,
                    "pricePerUnit": 0.04,
                },
                {
                    "product": "Actions",
                    "sku": "Actions Minutes",
                    "unitType": "minutes",
                    "quantity": 1000,
                    "grossAmount": 10.0,
                    "netAmount": 10.0,
                    "discountAmount": 0.0,
                    "pricePerUnit": 0.01,
                },
            ],
        }
        responses.get(GENERAL_URL, json=general_response, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        # Should only pick up Copilot items, not Actions
        gross_qty = metrics["github_premium_request_usage_gross_quantity"]
        assert len(gross_qty) == 1
        assert gross_qty[0].labels["product"] == "Copilot"
        assert gross_qty[0].labels["model"] == ""  # General endpoint has no model

    @responses.activate
    def test_no_fallback_when_premium_has_data(self) -> None:
        """Should NOT call general endpoint when premium_request has data."""
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        list(collector.collect())

        # Only the premium_request endpoint should have been called
        assert len(responses.calls) == 1
        assert "premium_request" in responses.calls[0].request.url


class TestCacheTTL:
    @responses.activate
    def test_two_scrapes_within_ttl_make_one_call(self) -> None:
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(cache_ttl=600), registry=CollectorRegistry())

        list(collector.collect())
        first_count = len(responses.calls)
        assert first_count == 1

        list(collector.collect())
        assert len(responses.calls) == first_count  # Cache hit, no new calls

    @responses.activate
    def test_scrape_after_ttl_expires_makes_new_call(self) -> None:
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(cache_ttl=0), registry=CollectorRegistry())

        list(collector.collect())
        assert len(responses.calls) == 1

        list(collector.collect())
        assert len(responses.calls) == 2  # TTL=0 means always expired


class TestEmptyUsageItems:
    @responses.activate
    def test_empty_everywhere_yields_no_series(self) -> None:
        """Both endpoints return empty — no usage metrics, scrape_success=0."""
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        assert len(metrics["github_premium_request_usage_gross_quantity"]) == 0
        assert metrics["github_premium_request_scrape_success"][0].value == 0.0


class TestAuthFailure:
    @responses.activate
    def test_401_sets_scrape_success_to_zero(self) -> None:
        responses.get(PREMIUM_URL, json={"message": "Bad credentials"}, status=401)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)
        assert metrics["github_premium_request_scrape_success"][0].value == 0.0

    @responses.activate
    def test_401_increments_failure_counter(self) -> None:
        responses.get(PREMIUM_URL, json={"message": "Bad credentials"}, status=401)
        registry = CollectorRegistry()
        collector = CopilotPremiumCollector(_config(), registry=registry)
        list(collector.collect())

        value = registry.get_sample_value("github_premium_request_scrape_failures_total")
        assert value >= 1.0

    @responses.activate
    def test_403_also_fails(self) -> None:
        responses.get(PREMIUM_URL, json={"message": "Forbidden"}, status=403)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)
        assert metrics["github_premium_request_scrape_success"][0].value == 0.0


class TestNotFound:
    @responses.activate
    def test_404_does_not_crash(self) -> None:
        responses.get(PREMIUM_URL, json={"message": "Not Found"}, status=404)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        assert "github_premium_request_usage_gross_quantity" in metrics
        assert len(metrics["github_premium_request_usage_gross_quantity"]) == 0
