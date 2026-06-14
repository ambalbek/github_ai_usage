"""Tests for copilot_premium_exporter."""

from __future__ import annotations

import json
from pathlib import Path

import responses
from prometheus_client import CollectorRegistry

from copilot_premium_exporter import CopilotPremiumCollector, ExporterConfig

SAMPLE_RESPONSE = {
    "timePeriod": {"year": 2026, "month": 6},
    "usageItems": [
        {
            "product": "Copilot",
            "sku": "Copilot AI Credits",
            "model": "GPT-5",
            "unitType": "ai-credits",
            "pricePerUnit": 0.01,
            "grossQuantity": 400,
            "grossAmount": 4.0,
            "discountQuantity": 0,
            "discountAmount": 0.0,
            "netQuantity": 400,
            "netAmount": 4.0,
        },
        {
            "product": "Copilot",
            "sku": "Copilot AI Credits",
            "model": "Claude Sonnet 4.6",
            "unitType": "ai-credits",
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

BASE = "https://api.github.com/enterprises/test-ent/settings/billing"
AI_CREDIT_URL = f"{BASE}/ai_credit/usage"
PREMIUM_URL = f"{BASE}/premium_request/usage"
GENERAL_URL = f"{BASE}/usage"


def _config(**overrides: object) -> ExporterConfig:
    defaults = dict(
        token="test-token",
        enterprises=["test-ent"],
        organizations=[],
        cache_ttl=900,
        http_timeout=5,
    )
    defaults.update(overrides)
    return ExporterConfig(**defaults)  # type: ignore[arg-type]


def _collect_as_dict(collector: CopilotPremiumCollector) -> dict[str, list]:
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
        assert collector._config.enterprises == ["test-ent"]

    def test_session_headers(self) -> None:
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        assert "Bearer test-token" in collector._session.headers["Authorization"]
        assert collector._session.headers["Accept"] == "application/vnd.github+json"


class TestLoadConfig:
    def test_loads_enterprise(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"github_enterprise": "my-ent", "cache_ttl_seconds": 600}))
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        config = ExporterConfig.load(config_path=cfg)
        assert config.enterprises == ["my-ent"]
        assert config.cache_ttl == 600

    def test_loads_organization(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"github_organization": "my-org"}))
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        config = ExporterConfig.load(config_path=cfg)
        assert config.organizations == ["my-org"]

    def test_missing_both_exits(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({}))
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        try:
            ExporterConfig.load(config_path=cfg)
            assert False
        except SystemExit:
            pass

    def test_missing_token_exits(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"github_enterprise": "e"}))
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        try:
            ExporterConfig.load(config_path=cfg)
            assert False
        except SystemExit:
            pass


class TestEndpointPriority:
    @responses.activate
    def test_calls_both_ai_credit_and_premium_request(self) -> None:
        """Both ai_credit and premium_request are called to get all data."""
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        # Both endpoints called
        assert len(responses.calls) == 2
        urls = [c.request.url for c in responses.calls]
        assert any("ai_credit" in u for u in urls)
        assert any("premium_request" in u for u in urls)

        # Data is deduplicated (same model/sku) so still 2 unique models
        gross = metrics["github_premium_request_usage_gross_quantity"]
        assert len(gross) == 2

    @responses.activate
    def test_falls_back_to_premium_request(self) -> None:
        """If ai_credit returns empty, try premium_request."""
        responses.get(AI_CREDIT_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        assert len(responses.calls) == 2
        assert "ai_credit" in responses.calls[0].request.url
        assert "premium_request" in responses.calls[1].request.url

        gross = metrics["github_premium_request_usage_gross_quantity"]
        assert len(gross) == 2

    @responses.activate
    def test_falls_back_to_general_usage(self) -> None:
        """If both ai_credit and premium_request are empty, use general."""
        responses.get(AI_CREDIT_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        general_resp = {
            "usageItems": [
                {
                    "product": "Copilot",
                    "sku": "Copilot AI Credits",
                    "model": "GPT-5",
                    "unitType": "ai-credits",
                    "quantity": 100,
                    "grossAmount": 1.0,
                    "netAmount": 1.0,
                    "discountAmount": 0.0,
                    "pricePerUnit": 0.01,
                },
                {
                    "product": "Actions",
                    "sku": "Actions Minutes",
                    "unitType": "minutes",
                    "quantity": 5000,
                    "grossAmount": 50.0,
                    "netAmount": 50.0,
                    "discountAmount": 0.0,
                    "pricePerUnit": 0.01,
                },
            ]
        }
        responses.get(GENERAL_URL, json=general_resp, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        assert len(responses.calls) == 3
        # Only Copilot items, not Actions
        gross = metrics["github_premium_request_usage_gross_quantity"]
        assert len(gross) == 1

    @responses.activate
    def test_ai_credit_404_tries_premium_request(self) -> None:
        """If ai_credit returns 404, still try premium_request."""
        responses.get(AI_CREDIT_URL, json={"message": "Not Found"}, status=404)
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        gross = metrics["github_premium_request_usage_gross_quantity"]
        assert len(gross) == 2


class TestCollectWithData:
    @responses.activate
    def test_correct_label_values(self) -> None:
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        gross_qty = metrics["github_premium_request_usage_gross_quantity"]
        gpt5 = [s for s in gross_qty if s.labels["model"] == "GPT-5"]
        assert len(gpt5) == 1
        assert gpt5[0].value == 400.0
        assert gpt5[0].labels["type"] == "enterprise"
        assert gpt5[0].labels["name"] == "test-ent"
        assert gpt5[0].labels["year"] == "2026"
        assert gpt5[0].labels["month"] == "6"
        assert gpt5[0].labels["product"] == "Copilot"

    @responses.activate
    def test_all_metric_values(self) -> None:
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        def gpt5(name: str) -> float:
            return [s for s in metrics[name] if s.labels["model"] == "GPT-5"][0].value

        assert gpt5("github_premium_request_usage_gross_quantity") == 400.0
        assert gpt5("github_premium_request_usage_net_quantity") == 400.0
        assert gpt5("github_premium_request_usage_gross_amount") == 4.0
        assert gpt5("github_premium_request_usage_net_amount") == 4.0
        assert gpt5("github_premium_request_usage_price_per_unit") == 0.01

    @responses.activate
    def test_scrape_success_is_one(self) -> None:
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)
        assert metrics["github_premium_request_scrape_success"][0].value == 1.0


class TestCacheTTL:
    @responses.activate
    def test_cache_hit(self) -> None:
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(cache_ttl=600), registry=CollectorRegistry())

        list(collector.collect())
        first_count = len(responses.calls)

        list(collector.collect())
        assert len(responses.calls) == first_count  # Cache hit, no new calls

    @responses.activate
    def test_cache_expired(self) -> None:
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        collector = CopilotPremiumCollector(_config(cache_ttl=0), registry=CollectorRegistry())

        list(collector.collect())
        first_count = len(responses.calls)

        list(collector.collect())
        assert len(responses.calls) > first_count  # TTL expired, new calls made


class TestAuthFailure:
    @responses.activate
    def test_401_all_endpoints(self) -> None:
        responses.get(AI_CREDIT_URL, json={"message": "Bad credentials"}, status=401)
        responses.get(PREMIUM_URL, json={"message": "Bad credentials"}, status=401)
        responses.get(GENERAL_URL, json={"message": "Bad credentials"}, status=401)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)
        assert metrics["github_premium_request_scrape_success"][0].value == 0.0

    @responses.activate
    def test_failure_counter_increments(self) -> None:
        responses.get(AI_CREDIT_URL, json={"message": "err"}, status=401)
        responses.get(PREMIUM_URL, json={"message": "err"}, status=401)
        responses.get(GENERAL_URL, json={"message": "err"}, status=401)

        registry = CollectorRegistry()
        collector = CopilotPremiumCollector(_config(), registry=registry)
        list(collector.collect())

        value = registry.get_sample_value("github_premium_request_scrape_failures_total")
        assert value >= 1.0


class TestEmptyUsageItems:
    @responses.activate
    def test_all_empty(self) -> None:
        responses.get(AI_CREDIT_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        assert len(metrics["github_premium_request_usage_gross_quantity"]) == 0
        # Still success=1 because the API responded OK, just no usage
        assert metrics["github_premium_request_scrape_success"][0].value == 1.0


class TestExcludeSkus:
    @responses.activate
    def test_seat_licenses_excluded(self) -> None:
        responses.get(AI_CREDIT_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        general_resp = {
            "usageItems": [
                {"product": "Copilot", "sku": "Copilot Business", "unitType": "seats",
                 "quantity": 50, "grossAmount": 950.0, "netAmount": 950.0,
                 "discountAmount": 0, "pricePerUnit": 19.0},
                {"product": "Copilot", "sku": "Copilot AI Credits", "model": "GPT-5",
                 "unitType": "ai-credits", "quantity": 100, "grossAmount": 1.0,
                 "netAmount": 1.0, "discountAmount": 0, "pricePerUnit": 0.01},
            ]
        }
        responses.get(GENERAL_URL, json=general_resp, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        gross = metrics["github_premium_request_usage_gross_amount"]
        # Only AI Credits (1.0), not seat licenses (950.0)
        assert len(gross) == 1
        assert gross[0].value == 1.0
