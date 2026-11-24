"""APIエラー・再試行・レート制限・不正な出力・バックエンド選択。"""
import asyncio
import json

import httpx
import numpy as np
import pytest

from askusers import llm as L

SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


class Flaky:
    name, model = "flaky", "m"

    def __init__(self, outcomes):
        self.outcomes, self.n = list(outcomes), 0

    async def call(self, **kw):
        self.n += 1
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return L.RawResult(o, 10, 5)


async def nosleep(_):
    pass


def mk(raw, **kw):
    sleeps = []

    async def sl(t):
        sleeps.append(t)
    return L.LLM(raw, policy=L.RetryPolicy(base_delay=1, sleep=sl), **kw), sleeps


def run(coro):
    return asyncio.run(coro)


def call(llm):
    return llm.structured(system="s", user="u", schema=SCHEMA)


def test_retry_then_success_counts_usage():
    llm, sleeps = mk(Flaky([L.LLMError("transient", "x"), {"x": 1}]))
    data, attempts = run(call(llm))
    assert data == {"x": 1} and attempts == 2 and len(sleeps) == 1
    assert llm.usage.requests == 2 and llm.usage.retries == 1 and llm.usage.inputTokens == 10


def test_rate_limit_backs_off_longer_and_is_counted():
    llm, sleeps = mk(Flaky([L.LLMError("rate_limit", "429"), L.LLMError("transient", "x"), {"x": 2}]))
    run(call(llm))
    assert llm.usage.rateLimitHits == 1 and sleeps[0] > 3  # rate_limit は 4 倍の待機


def test_retries_are_bounded():
    llm, _ = mk(Flaky([L.LLMError("transient", "x")] * 10))
    with pytest.raises(L.LLMError) as e:
        run(call(llm))
    assert e.value.attempts == 3 and llm.raw.n == 3  # 無制限に再試行しない


def test_fatal_is_not_retried():
    llm, _ = mk(Flaky([L.LLMError("fatal", "auth"), {"x": 1}]))
    with pytest.raises(L.LLMError):
        run(call(llm))
    assert llm.raw.n == 1


def test_schema_violation_is_retried_as_invalid_output():
    llm, _ = mk(Flaky([{"x": "not int"}, {"x": 3}]))
    assert run(call(llm))[0] == {"x": 3}
    llm, _ = mk(Flaky([{"y": 1}] * 3))
    with pytest.raises(L.LLMError) as e:
        run(call(llm))
    assert e.value.kind == "invalid_output"


def test_request_budget_stops_calls():
    llm, _ = mk(Flaky([{"x": 1}] * 5), max_requests=2)
    run(call(llm))
    run(call(llm))
    with pytest.raises(L.LLMError) as e:
        run(call(llm))
    assert e.value.kind == "budget" and llm.raw.n == 2


def test_concurrency_limit():
    state = {"cur": 0, "max": 0}

    class Slow:
        name, model = "s", "m"

        async def call(self, **kw):
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            await asyncio.sleep(0.01)
            state["cur"] -= 1
            return L.RawResult({"x": 1})

    async def go():
        llm = L.LLM(Slow(), concurrency=3)
        await asyncio.gather(*(call(llm) for _ in range(12)))
    run(go())
    assert state["max"] == 3


def _api(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return L.AnthropicAPIRaw("claude-sonnet-5-5", "sk-ant-TESTKEY-0000000000", client)


def test_anthropic_api_success_uses_forced_tool_and_headers():
    seen = {}

    def h(req):
        seen["body"] = json.loads(req.content)
        seen["key"] = req.headers["x-api-key"]
        return httpx.Response(200, json={"content": [{"type": "tool_use", "input": {"x": 1}}],
                                         "usage": {"input_tokens": 7, "output_tokens": 3}})
    res = run(_api(h).call(system="s", user="u", schema=SCHEMA, max_tokens=10, temperature=None))
    assert res.data == {"x": 1} and res.input_tokens == 7
    assert seen["body"]["tool_choice"] == {"type": "tool", "name": "submit"}
    assert "temperature" not in seen["body"] and seen["key"].startswith("sk-ant-")


@pytest.mark.parametrize("status,kind", [(429, "rate_limit"), (529, "rate_limit"), (500, "transient"),
                                         (401, "fatal"), (400, "fatal")])
def test_anthropic_api_error_classification(status, kind):
    with pytest.raises(L.LLMError) as e:
        run(_api(lambda r: httpx.Response(status, text="x")).call(
            system="s", user="u", schema=SCHEMA, max_tokens=1, temperature=None))
    assert e.value.kind == kind


def test_anthropic_api_without_tool_use_is_invalid_output():
    with pytest.raises(L.LLMError) as e:
        run(_api(lambda r: httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}]})).call(
            system="s", user="u", schema=SCHEMA, max_tokens=1, temperature=None))
    assert e.value.kind == "invalid_output"


def test_api_key_never_appears_in_errors():
    with pytest.raises(L.LLMError) as e:
        run(_api(lambda r: httpx.Response(400, text="bad")).call(
            system="s", user="u", schema=SCHEMA, max_tokens=1, temperature=None))
    assert "TESTKEY" not in str(e.value)


def test_embedder_batches_orders_and_retries():
    calls = []

    def h(req):
        body = json.loads(req.content)
        calls.append(len(body["input"]))
        if len(calls) == 1:
            return httpx.Response(429)
        data = [{"index": i, "embedding": [float(len(t)), 1.0]} for i, t in enumerate(body["input"])][::-1]
        return httpx.Response(200, json={"data": data, "usage": {"total_tokens": 9}})
    raw = L.OpenAIEmbeddingRaw("text-embedding-3-small", "sk-TEST", httpx.AsyncClient(transport=httpx.MockTransport(h)))
    emb = L.Embedder(raw, policy=L.RetryPolicy(base_delay=0, sleep=nosleep), batch_size=2)
    out = run(emb.embed(["a", "bb", "ccc"]))
    assert out.shape == (3, 2) and out[:, 0].tolist() == [1.0, 2.0, 3.0]
    assert emb.usage.rateLimitHits == 1 and np.isfinite(out).all()


# ---- Claude Code CLI

def test_cli_output_parsing_structured_and_usage():
    out = json.dumps({"is_error": False, "structured_output": {"x": 1},
                      "usage": {"input_tokens": 2, "cache_creation_input_tokens": 100, "output_tokens": 9},
                      "credential_expires_at": "2026-01-01"})
    r = L.parse_cli_output(out, "", 0)
    assert r.data == {"x": 1} and r.input_tokens == 102 and r.output_tokens == 9


def test_cli_output_rate_limit_and_errors():
    rl = json.dumps({"is_error": True, "result": "5-hour usage limit reached", "api_error_status": 429})
    with pytest.raises(L.LLMError) as e:
        L.parse_cli_output(rl, "", 1)
    assert e.value.kind == "rate_limit"
    with pytest.raises(L.LLMError) as e:
        L.parse_cli_output("not json", "boom", 1)
    assert e.value.kind == "transient"
    with pytest.raises(L.LLMError) as e:
        L.parse_cli_output(json.dumps({"is_error": False, "result": "plain text"}), "", 0)
    assert e.value.kind == "invalid_output"


def test_cli_command_is_isolated_and_has_no_credentials():
    cmd = L.ClaudeCLIRaw("claude-sonnet-5-5").build_cmd("SYS", SCHEMA)
    assert cmd[:3] == ["claude", "-p", "--output-format"] and "--json-schema" in cmd
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--setting-sources") + 1] == ""
    assert "--no-session-persistence" in cmd and cmd[cmd.index("--model") + 1] == "claude-sonnet-5-5"
    assert not any("key" in c.lower() or "token" in c.lower() for c in cmd)


def test_cli_subprocess_with_fake_binary(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\ncat >/dev/null\necho \'{"is_error":false,"structured_output":{"x":5},"usage":{"output_tokens":1}}\'\n')
    fake.chmod(0o755)
    r = run(L.ClaudeCLIRaw("m", str(fake)).call(system="s", user="hello", schema=SCHEMA, max_tokens=1, temperature=None))
    assert r.data == {"x": 5}
    with pytest.raises(L.LLMError) as e:
        run(L.ClaudeCLIRaw("m", str(tmp_path / "missing")).call(system="s", user="u", schema=SCHEMA, max_tokens=1, temperature=None))
    assert e.value.kind == "fatal"


def test_backend_selection_prefers_logged_in_cli_then_api_key():
    which = lambda n: "/bin/claude"  # noqa: E731
    r = L.select_llm_backend("m", env={"ANTHROPIC_API_KEY": "k"}, which=which, logged_in=lambda b: True)
    assert r.name == "claude-cli"
    r = L.select_llm_backend("m", env={"ANTHROPIC_API_KEY": "k"}, which=which, logged_in=lambda b: False)
    assert r.name == "anthropic-api"
    r = L.select_llm_backend("m", env={"ANTHROPIC_API_KEY": "k"}, which=lambda n: None, logged_in=lambda b: True)
    assert r.name == "anthropic-api"
    with pytest.raises(L.BackendUnavailable):
        L.select_llm_backend("m", env={}, which=lambda n: None)
    with pytest.raises(L.BackendUnavailable):
        L.select_llm_backend("m", "claude-cli", env={"ANTHROPIC_API_KEY": "k"}, which=lambda n: None)
    assert L.select_embedder("m", env={}) is None and L.select_embedder("m", env={"OPENAI_API_KEY": "k"})
