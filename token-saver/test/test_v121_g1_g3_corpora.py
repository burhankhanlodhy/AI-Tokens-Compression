import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmark.run_e2e_v121_ab import load_corpus
from proxy import tool_result_optimizer
from proxy.counting import count_text
from proxy.tool_protocol import minify_tool_schema

FIXTURES = ROOT / "benchmark" / "fixtures"


def test_harness_loads_and_checksum_verifies_g1_and_g3_corpora():
    for filename, segment in (("v121_g1_tool_heavy.json", "tool"),
                              ("v121_g3_result_heavy.json", "result")):
        scenarios, digest = load_corpus(FIXTURES / filename)
        assert scenarios
        assert {scenario["segment"] for scenario in scenarios} == {segment}
        pin = (FIXTURES / f"{filename}.sha256").read_text().split()[0]
        assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == pin
        assert digest == pin


def test_g1_fixture_exercises_existing_allow_list_target_shapes():
    scenarios, _ = load_corpus(FIXTURES / "v121_g1_tool_heavy.json")
    assert len(scenarios) >= 3
    tools = [tool for scenario in scenarios for tool in scenario.get("tools", [])]
    assert tools
    params = [p for tool in tools for p in
              tool["function"]["parameters"]["properties"].values()]
    assert any(len(param.get("description", "")) >= 70 for param in params)
    assert any("\n" in json.dumps(tool["function"]["parameters"], indent=2)
               for tool in tools)
    example = tools[0]
    properties = example["function"]["parameters"]["properties"]
    assert all(len(properties[name]["description"]) > 50 for name in
               ("customer_id", "include_archived", "page_size", "page_cursor"))
    minimized = minify_tool_schema(example)
    minimized_properties = minimized["function"]["parameters"]["properties"]
    assert "description" not in minimized_properties["email"]
    assert "description" in minimized_properties["customer_id"]
    assert minimized_properties["customer_id"]["description"] == properties["customer_id"]["description"]


def test_g3_fixture_contains_over_cap_shapes_boundary_and_repeated_result():
    scenarios, _ = load_corpus(FIXTURES / "v121_g3_result_heavy.json")
    scenarios_by_id = {s["id"]: s for s in scenarios}
    assert scenarios_by_id["v121-g3-ls-noisy-24000"]["messages"][1]["tool_calls"][0]["function"]["name"] == "list_files"
    assert scenarios_by_id["v121-g3-repeated-logs-70000"]["messages"][1]["tool_calls"][0]["function"]["name"] == "get_logs"
    scenarios = list(scenarios_by_id.values())
    repeated = scenarios_by_id["v121-g3-repeated-listing-24000"]
    first = scenarios_by_id[repeated["repeated_result_of"]]
    assert repeated["messages"] == first["messages"]
    contents = {s["id"]: next(m["content"] for m in s["messages"]
                               if m["role"] == "tool") for s in scenarios}
    assert any("ls -la" in text for text in contents.values())
    assert any(text.lstrip().startswith("{") and '"data"' in text
               for text in contents.values())
    assert any("Traceback (most recent call last)" in text for text in contents.values())
    assert any(s.get("repeated_result_of") for s in scenarios)
    sizes = {s["id"]: len(contents[s["id"]]) for s in scenarios}
    tokens = {scenario_id: count_text(text, "gpt-4o-mini")
              for scenario_id, text in contents.items()}
    assert all(5_000 < tokens[s["id"]] and sizes[s["id"]] <= 80_000
               for s in scenarios if s.get("size_class") != "at-cap-boundary")
    boundary = [s for s in scenarios if s.get("size_class") == "at-cap-boundary"]
    assert boundary and 4_900 <= tokens[boundary[0]["id"]] <= 5_100


def test_g3_listing_and_identical_repeat_exercise_filter_and_result_cache(monkeypatch):
    scenarios, _ = load_corpus(FIXTURES / "v121_g3_result_heavy.json")
    listing = next(s for s in scenarios if s["id"] == "v121-g3-ls-noisy-24000")
    message = next(m for m in listing["messages"] if m["role"] == "tool")
    calls = []
    real_filter = tool_result_optimizer.filter_result_by_type

    def record_filter(content, result_type):
        calls.append(result_type)
        return real_filter(content, result_type)

    monkeypatch.setattr(tool_result_optimizer, "filter_result_by_type", record_filter)
    tool_result_optimizer.clear_result_cache()
    args = {"tool_name": "list_files"}
    first = tool_result_optimizer.optimize_tool_result(
        message, tool_call_args=args, max_tokens=5_000,
        filtering=True, cache_enabled=True)
    repeated = tool_result_optimizer.optimize_tool_result(
        message, tool_call_args=args, max_tokens=5_000,
        filtering=True, cache_enabled=True)
    assert "node_modules/" not in first["content"]
    assert "irrelevant file entries filtered" in first["content"]
    assert repeated["content"] == first["content"]
    assert calls == ["file_listing"]


def test_loader_rejects_checksum_mismatch(tmp_path):
    corpus = tmp_path / "corrupt.json"
    corpus.write_text("[]\n")
    corpus.with_suffix(".json.sha256").write_text("0" * 64 + "\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_corpus(corpus)
