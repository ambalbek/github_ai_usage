"""Tests for copilot_premium_exporter."""

from __future__ import annotations

import json
from pathlib import Path

import responses
from prometheus_client import CollectorRegistry

from unittest.mock import MagicMock, patch

from copilot_premium_exporter import CopilotPremiumCollector, ElasticsearchSender, ExporterConfig

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
        months_back=0,
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
    def test_calls_all_three_endpoints(self) -> None:
        """All three endpoints are always called."""
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        # All three endpoints called
        assert len(responses.calls) == 3
        urls = [c.request.url for c in responses.calls]
        assert any("ai_credit" in u for u in urls)
        assert any("premium_request" in u for u in urls)
        assert any(u.endswith("/usage") or "/usage?" in u for u in urls)

        # Data is deduplicated (same model/sku) so still 2 unique models
        gross = metrics["github_premium_request_usage_gross_quantity"]
        assert len(gross) == 2

    @responses.activate
    def test_premium_request_empty_still_calls_general(self) -> None:
        """If ai_credit has data but premium_request is empty, general is still called."""
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        assert len(responses.calls) == 3

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
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)

        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        gross = metrics["github_premium_request_usage_gross_quantity"]
        assert len(gross) == 2


class TestCollectWithData:
    @responses.activate
    def test_correct_label_values(self) -> None:
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)
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
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)
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
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)
        collector = CopilotPremiumCollector(_config(), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)
        assert metrics["github_premium_request_scrape_success"][0].value == 1.0


class TestCacheTTL:
    @responses.activate
    def test_cache_hit(self) -> None:
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)
        collector = CopilotPremiumCollector(_config(cache_ttl=600), registry=CollectorRegistry())

        list(collector.collect())
        first_count = len(responses.calls)

        list(collector.collect())
        assert len(responses.calls) == first_count  # Cache hit, no new calls

    @responses.activate
    def test_cache_expired(self) -> None:
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)
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


class TestMultiMonth:
    @responses.activate
    def test_fetches_current_and_previous_month(self) -> None:
        """With months_back=1, fetches both current and previous month."""
        current_resp = {
            "timePeriod": {"year": 2026, "month": 7},
            "usageItems": [
                {"product": "Copilot", "sku": "Copilot AI Credits", "model": "GPT-5",
                 "unitType": "ai-credits", "pricePerUnit": 0.01,
                 "grossQuantity": 200, "grossAmount": 2.0,
                 "discountQuantity": 0, "discountAmount": 0.0,
                 "netQuantity": 200, "netAmount": 2.0},
            ],
        }
        previous_resp = {
            "timePeriod": {"year": 2026, "month": 6},
            "usageItems": [
                {"product": "Copilot", "sku": "Copilot AI Credits", "model": "GPT-5",
                 "unitType": "ai-credits", "pricePerUnit": 0.01,
                 "grossQuantity": 400, "grossAmount": 4.0,
                 "discountQuantity": 0, "discountAmount": 0.0,
                 "netQuantity": 400, "netAmount": 4.0},
            ],
        }
        # responses library matches URL regardless of query params;
        # use callbacks to return different data based on month param.
        def ai_credit_callback(request):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(request.url).query)
            month = qs.get("month", [""])[0]
            if month == "6":
                return (200, {}, json.dumps(previous_resp))
            return (200, {}, json.dumps(current_resp))

        responses.add_callback(responses.GET, AI_CREDIT_URL, callback=ai_credit_callback)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)

        collector = CopilotPremiumCollector(
            _config(months_back=1), registry=CollectorRegistry())
        metrics = _collect_as_dict(collector)

        gross = metrics["github_premium_request_usage_gross_quantity"]
        # Two separate entries: one for month 7, one for month 6
        assert len(gross) == 2
        values = sorted([s.value for s in gross])
        assert values == [200.0, 400.0]
        months = sorted([s.labels["month"] for s in gross])
        assert months == ["6", "7"]

    def test_months_to_fetch_wraps_year(self) -> None:
        """January with months_back=1 should return Jan + Dec of previous year."""
        from unittest.mock import patch
        from datetime import datetime as dt

        with patch("copilot_premium_exporter.datetime") as mock_dt:
            mock_dt.now.return_value = dt(2026, 1, 15)
            mock_dt.side_effect = lambda *a, **kw: dt(*a, **kw)
            months = CopilotPremiumCollector._months_to_fetch(1)
            assert (2026, 1) in months
            assert (2025, 12) in months

    def test_months_to_fetch_zero(self) -> None:
        """months_back=0 returns only current month."""
        months = CopilotPremiumCollector._months_to_fetch(0)
        assert len(months) == 1


class TestElasticsearchSender:
    def test_to_doc_structure(self) -> None:
        item = {
            "_type": "enterprise", "_name": "test-ent",
            "_year": "2026", "_month": "6",
            "product": "Copilot", "sku": "Copilot AI Credits",
            "model": "GPT-5", "unitType": "ai-credits",
            "grossQuantity": 400, "netQuantity": 400,
            "discountQuantity": 0, "grossAmount": 4.0,
            "netAmount": 4.0, "discountAmount": 0.0, "pricePerUnit": 0.01,
        }
        doc = ElasticsearchSender._to_doc(item)
        assert doc["@timestamp"] == "2026-06-01T00:00:00Z"
        assert doc["entity"]["type"] == "enterprise"
        assert doc["entity"]["name"] == "test-ent"
        assert doc["billing"]["product"] == "Copilot"
        assert doc["billing"]["sku"] == "Copilot AI Credits"
        assert doc["billing"]["model"] == "GPT-5"
        assert doc["billing"]["unit_type"] == "ai-credits"
        assert doc["billing"]["gross_quantity"] == 400
        assert doc["billing"]["net_amount"] == 4.0
        assert doc["billing"]["price_per_unit"] == 0.01

    @patch("copilot_premium_exporter.Elasticsearch")
    def test_send_calls_bulk(self, mock_es_cls) -> None:
        mock_client = MagicMock()
        mock_es_cls.return_value = mock_client
        config = _config(
            elasticsearch_url="https://es:9200",
            elasticsearch_api_key="test-key",
            elasticsearch_index="ds-copilot-billing",
            elasticsearch_enabled=True,
        )
        sender = ElasticsearchSender(config)
        items = [{
            "_type": "enterprise", "_name": "test-ent",
            "_year": "2026", "_month": "6",
            "product": "Copilot", "sku": "AI Credits", "model": "GPT-5",
            "unitType": "ai-credits", "grossQuantity": 100,
            "netQuantity": 100, "grossAmount": 1.0, "netAmount": 1.0,
            "discountQuantity": 0, "discountAmount": 0.0, "pricePerUnit": 0.01,
        }]
        with patch("copilot_premium_exporter.es_bulk", return_value=(1, [])) as mock_bulk:
            sender.send(items)
            mock_bulk.assert_called_once()
            actions = mock_bulk.call_args[0][1]
            assert len(actions) == 1
            assert actions[0]["_index"] == "ds-copilot-billing"
            assert actions[0]["_source"]["@timestamp"] == "2026-06-01T00:00:00Z"

    def test_send_empty_items_noop(self) -> None:
        with patch("copilot_premium_exporter.Elasticsearch"):
            config = _config(
                elasticsearch_url="https://es:9200",
                elasticsearch_api_key="test-key",
                elasticsearch_index="ds-copilot-billing",
                elasticsearch_enabled=True,
            )
            sender = ElasticsearchSender(config)
            with patch("copilot_premium_exporter.es_bulk") as mock_bulk:
                sender.send([])
                mock_bulk.assert_not_called()

    @responses.activate
    def test_es_failure_does_not_break_collect(self) -> None:
        """ES send failure should not prevent Prometheus metrics from being returned."""
        responses.get(AI_CREDIT_URL, json=SAMPLE_RESPONSE, status=200)
        responses.get(PREMIUM_URL, json=EMPTY_RESPONSE, status=200)
        responses.get(GENERAL_URL, json={"usageItems": []}, status=200)

        mock_sender = MagicMock()
        mock_sender.send.side_effect = Exception("ES connection refused")

        collector = CopilotPremiumCollector(
            _config(), registry=CollectorRegistry(), es_sender=mock_sender)
        metrics = _collect_as_dict(collector)

        # Metrics still returned despite ES failure
        assert len(metrics["github_premium_request_usage_gross_quantity"]) == 2
        assert metrics["github_premium_request_scrape_success"][0].value == 1.0
        mock_sender.send.assert_called_once()


class TestElasticsearchConfig:
    def test_es_disabled_by_default(self) -> None:
        config = _config()
        assert config.elasticsearch_enabled is False

    def test_es_enabled_when_configured(self) -> None:
        config = _config(
            elasticsearch_url="https://es:9200",
            elasticsearch_api_key="test-key",
            elasticsearch_enabled=True,
        )
        assert config.elasticsearch_enabled is True

    def test_load_es_config(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "github_enterprise": "my-ent",
            "elasticsearch_url": "https://es:9200",
            "elasticsearch_index": "my-index",
        }))
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("ES_API_KEY", "my-api-key")
        config = ExporterConfig.load(config_path=cfg)
        assert config.elasticsearch_url == "https://es:9200"
        assert config.elasticsearch_api_key == "my-api-key"
        assert config.elasticsearch_index == "my-index"
        assert config.elasticsearch_enabled is True

    def test_load_es_disabled_without_key(self, tmp_path: Path, monkeypatch) -> None:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({
            "github_enterprise": "my-ent",
            "elasticsearch_url": "https://es:9200",
        }))
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.delenv("ES_API_KEY", raising=False)
        config = ExporterConfig.load(config_path=cfg)
        assert config.elasticsearch_enabled is False


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
