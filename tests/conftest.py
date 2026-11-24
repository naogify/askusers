from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from askusers import pipeline, store
from askusers.llm import LLM, Embedder, LLMError, RawResult, RetryPolicy
from askusers.mock import MockRawEmbedder, MockRawLLM
from askusers.models import SurveyData
from askusers.target import load_file

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


class RecordingLLM(MockRawLLM):
    """モック LLM。呼び出しを記録し、fail(task, user) で故障を注入できる。"""

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []  # (task, system, user)
        self.fail = None

    async def call(self, *, system, user, schema, max_tokens, temperature):
        from askusers.mock import _task

        task = _task(system)
        self.calls.append((task, system, user))
        if self.fail:
            exc = self.fail(task, user)
            if exc:
                raise exc
        return await super().call(system=system, user=user, schema=schema,
                                  max_tokens=max_tokens, temperature=temperature)


def make_llm(raw=None, **kw) -> LLM:
    return LLM(raw or RecordingLLM(), policy=RetryPolicy(base_delay=0, sleep=_nosleep), **kw)


async def _nosleep(_):
    return None


def run_pipeline(tmp_path: Path, targets, *, count=6, mode="survey", llm=None, embedder="default",
                 no_ssr=False, audience="中小企業の経営者", objective="利用したいか") -> SurveyData:
    llm = llm or make_llm()
    emb = Embedder(MockRawEmbedder()) if embedder == "default" else embedder
    cfg = pipeline.RunConfig(targets, audience, objective, count, tmp_path, mode=mode, no_ssr=no_ssr,
                             run_id="run-test")
    return asyncio.run(pipeline.run(cfg, llm, emb, pipeline.Progress(quiet=True)))


@pytest.fixture
def idea(tmp_path):
    p = tmp_path / "idea.md"
    p.write_text("# アイデア\n請求書を自動整理するサービス。月額1万円。\n", encoding="utf-8")
    return p


@pytest.fixture
def two_docs(tmp_path):
    a = tmp_path / "before-v1.md"
    b = tmp_path / "after-v2.md"
    a.write_text("# 説明\nAPIの使い方。\n", encoding="utf-8")
    b.write_text("# 説明\n【改善】具体例つきでAPIの使い方を説明。\n", encoding="utf-8")
    ta, tb = load_file(str(a), "target-001"), load_file(str(b), "target-002")
    ta.role, tb.role = "before", "after"
    return ta, tb


@pytest.fixture(scope="session")
def sample() -> SurveyData:
    return store.load(EXAMPLES / "mock-survey" / "data.json")


@pytest.fixture(scope="session")
def sample_compare() -> SurveyData:
    return store.load(EXAMPLES / "mock-compare" / "data.json")


@pytest.fixture
def survey_run(tmp_path, idea) -> SurveyData:
    return run_pipeline(tmp_path, [load_file(str(idea))])


__all__ = ["RecordingLLM", "make_llm", "run_pipeline", "LLMError", "RawResult", "json"]
