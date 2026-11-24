"""LLM / Embedding の呼び出し層。

- 生成: 既定は利用者がログイン済みの Claude Code CLI (`claude -p`)。使えない場合のみ
  ANTHROPIC_API_KEY で Anthropic API を使う。askusers はサブスクリプションの認証情報を読まない。
- Embedding: OpenAI 互換 API（論文は text-embedding-3-small）。OPENAI_API_KEY が必要。
- 再試行・同時実行数・リクエスト上限・利用量集計は `LLM` / `Embedder` ラッパーで一元管理する。
  生の呼び出し（Raw*）を差し替えればテストでモックできる。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx
import jsonschema
import numpy as np

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


class LLMError(Exception):
    """kind: rate_limit / transient / invalid_output / fatal / budget"""

    def __init__(self, kind: str, message: str, attempts: int = 1):
        super().__init__(message)
        self.kind = kind
        self.attempts = attempts


@dataclass
class RawResult:
    data: dict
    input_tokens: int = 0
    output_tokens: int = 0


class RawLLM(Protocol):
    name: str
    model: str

    async def call(
        self, *, system: str, user: str, schema: dict, max_tokens: int, temperature: float | None
    ) -> RawResult: ...


class RawEmbedder(Protocol):
    model: str

    async def call(self, texts: list[str]) -> tuple[list[list[float]], int]: ...


@dataclass
class Usage:
    requests: int = 0
    retries: int = 0
    rateLimitHits: int = 0
    inputTokens: int = 0
    outputTokens: int = 0
    embeddingRequests: int = 0
    embeddingTokens: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 2.0
    max_delay: float = 60.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def delay(self, attempt: int, kind: str) -> float:
        base = self.base_delay * (4 if kind == "rate_limit" else 1) * (2 ** (attempt - 1))
        return min(self.max_delay, base) * (0.75 + random.random() * 0.5)


@dataclass
class LLM:
    raw: RawLLM
    concurrency: int = 4
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    max_requests: int | None = None
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.concurrency)
        self.name = self.raw.name
        self.model = self.raw.model

    async def structured(
        self,
        *,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int = 8192,
        temperature: float | None = None,
    ) -> tuple[dict, int]:
        """構造化 JSON を取得して (data, attempts) を返す。失敗時は LLMError(attempts=n)。"""
        last: LLMError | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            if self.max_requests is not None and self.usage.requests >= self.max_requests:
                raise LLMError("budget", "リクエスト上限 (--max-requests) に達しました", attempt)
            self.usage.requests += 1
            if attempt > 1:
                self.usage.retries += 1
            try:
                async with self._sem:
                    res = await self.raw.call(
                        system=system,
                        user=user,
                        schema=schema,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                self.usage.inputTokens += res.input_tokens
                self.usage.outputTokens += res.output_tokens
                try:
                    jsonschema.validate(res.data, schema)
                except jsonschema.ValidationError as e:
                    raise LLMError("invalid_output", f"LLM 出力がスキーマに違反: {e.message[:200]}")
                return res.data, attempt
            except LLMError as e:
                e.attempts = attempt
                last = e
                if e.kind in ("fatal", "budget"):
                    raise
                if e.kind == "rate_limit":
                    self.usage.rateLimitHits += 1
                if attempt < self.policy.max_attempts:
                    await self.policy.sleep(self.policy.delay(attempt, e.kind))
        assert last is not None
        raise last


@dataclass
class Embedder:
    raw: RawEmbedder
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    usage: Usage = field(default_factory=Usage)
    batch_size: int = 128
    concurrency: int = 4

    def __post_init__(self) -> None:
        self.model = self.raw.model
        self._sem = asyncio.Semaphore(self.concurrency)

    async def _batch(self, texts: list[str]) -> list[list[float]]:
        last: LLMError | None = None
        for attempt in range(1, self.policy.max_attempts + 1):
            self.usage.embeddingRequests += 1
            try:
                async with self._sem:
                    vecs, tokens = await self.raw.call(texts)
                self.usage.embeddingTokens += tokens
                return vecs
            except LLMError as e:
                e.attempts = attempt
                last = e
                if e.kind == "fatal":
                    raise
                if e.kind == "rate_limit":
                    self.usage.rateLimitHits += 1
                if attempt < self.policy.max_attempts:
                    await self.policy.sleep(self.policy.delay(attempt, e.kind))
        assert last is not None
        raise last

    async def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, 0))
        chunks = [texts[i : i + self.batch_size] for i in range(0, len(texts), self.batch_size)]
        parts = await asyncio.gather(*(self._batch(c) for c in chunks))
        return np.array([v for p in parts for v in p], dtype=float)


# ---------------------------------------------------------------- Claude Code CLI

RATE_LIMIT_HINTS = ("rate limit", "usage limit", "limit reached", "overloaded", "429", "too many")


def classify_failure(text: str, status: int | None = None) -> str:
    low = text.lower()
    if status == 429 or any(h in low for h in RATE_LIMIT_HINTS):
        return "rate_limit"
    if status is not None and status >= 500:
        return "transient"
    return "transient"


class ClaudeCLIRaw:
    """`claude -p` を子プロセスとして呼ぶ。トークン等の認証情報には触れない（CLI に任せる）。"""

    name = "claude-cli"

    def __init__(self, model: str, binary: str = "claude", timeout: float = 600.0):
        self.model = model
        self.binary = binary
        self.timeout = timeout

    def build_cmd(self, system: str, schema: dict) -> list[str]:
        return [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, ensure_ascii=False),
            "--model",
            self.model,
            "--tools",
            "",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--system-prompt",
            system,
        ]

    async def call(
        self, *, system: str, user: str, schema: dict, max_tokens: int, temperature: float | None
    ) -> RawResult:
        # 利用者の CLAUDE.md 等の影響を避けるため空の一時ディレクトリで実行する
        with tempfile.TemporaryDirectory(prefix="askusers-") as cwd:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.build_cmd(system, schema),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                )
            except OSError as e:
                raise LLMError("fatal", f"claude CLI を起動できません: {e}") from e
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(user.encode("utf-8")), timeout=self.timeout
                )
            except TimeoutError as e:
                proc.kill()
                await proc.wait()
                raise LLMError("transient", "claude CLI がタイムアウトしました") from e
        return parse_cli_output(out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                                proc.returncode)


def parse_cli_output(out: str, err: str, returncode: int | None) -> RawResult:
    try:
        obj = json.loads(out)
    except json.JSONDecodeError:
        kind = classify_failure(out + err)
        raise LLMError(kind, f"claude CLI の出力を解釈できません (exit={returncode}): {(err or out)[:200]}")
    if obj.get("is_error") or returncode not in (0, None):
        text = str(obj.get("result") or err or "")
        kind = classify_failure(text, obj.get("api_error_status"))
        raise LLMError(kind, f"claude CLI エラー: {text[:200]}")
    data = obj.get("structured_output")
    if not isinstance(data, dict):
        result = obj.get("result")
        try:
            data = json.loads(result) if isinstance(result, str) else None
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        raise LLMError("invalid_output", "claude CLI が構造化出力を返しませんでした")
    usage = obj.get("usage") or {}
    inp = int(usage.get("input_tokens", 0)) + int(usage.get("cache_creation_input_tokens", 0)) + int(
        usage.get("cache_read_input_tokens", 0)
    )
    return RawResult(data, inp, int(usage.get("output_tokens", 0)))


# ---------------------------------------------------------------- Anthropic API


class AnthropicAPIRaw:
    name = "anthropic-api"

    def __init__(self, model: str, api_key: str, client: httpx.AsyncClient | None = None):
        self.model = model
        self._key = api_key  # ログ・成果物には出さない
        self._client = client or httpx.AsyncClient(timeout=300)

    async def call(
        self, *, system: str, user: str, schema: dict, max_tokens: int, temperature: float | None
    ) -> RawResult:
        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": "submit", "description": "Submit the structured result.",
                       "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "submit"},
        }
        if temperature is not None:
            body["temperature"] = temperature
        headers = {
            "x-api-key": self._key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        try:
            r = await self._client.post(ANTHROPIC_URL, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise LLMError("transient", f"Anthropic API 通信エラー: {type(e).__name__}") from e
        if r.status_code == 429 or r.status_code == 529:
            raise LLMError("rate_limit", f"Anthropic API {r.status_code}")
        if r.status_code >= 500:
            raise LLMError("transient", f"Anthropic API {r.status_code}")
        if r.status_code in (401, 403):
            raise LLMError("fatal", f"Anthropic API 認証エラー {r.status_code}")
        if r.status_code >= 400:
            raise LLMError("fatal", f"Anthropic API {r.status_code}: {r.text[:200]}")
        obj = r.json()
        for block in obj.get("content", []):
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                u = obj.get("usage", {})
                return RawResult(block["input"], u.get("input_tokens", 0), u.get("output_tokens", 0))
        raise LLMError("invalid_output", "Anthropic API が tool_use を返しませんでした")


class OpenAIEmbeddingRaw:
    def __init__(self, model: str, api_key: str, client: httpx.AsyncClient | None = None):
        self.model = model
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=120)

    async def call(self, texts: list[str]) -> tuple[list[list[float]], int]:
        try:
            r = await self._client.post(
                OPENAI_EMBED_URL,
                json={"model": self.model, "input": texts},
                headers={"Authorization": f"Bearer {self._key}"},
            )
        except httpx.HTTPError as e:
            raise LLMError("transient", f"Embedding API 通信エラー: {type(e).__name__}") from e
        if r.status_code == 429:
            raise LLMError("rate_limit", "Embedding API 429")
        if r.status_code >= 500:
            raise LLMError("transient", f"Embedding API {r.status_code}")
        if r.status_code >= 400:
            raise LLMError("fatal", f"Embedding API {r.status_code}")
        obj = r.json()
        items = sorted(obj["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in items], int(obj.get("usage", {}).get("total_tokens", 0))


# ---------------------------------------------------------------- 選択


class BackendUnavailable(Exception):
    pass


def claude_cli_logged_in(binary: str = "claude") -> bool:
    """`claude auth status` の loggedIn だけを見る（他の情報は読まない・保存しない）。"""
    try:
        p = subprocess.run([binary, "auth", "status"], capture_output=True, text=True, timeout=30)
        return bool(json.loads(p.stdout).get("loggedIn"))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, AttributeError):
        return False


def select_llm_backend(
    model: str,
    preference: str = "auto",
    env: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    logged_in: Callable[[str], bool] = claude_cli_logged_in,
) -> RawLLM:
    env = os.environ if env is None else env
    binary = which("claude")
    api_key = env.get("ANTHROPIC_API_KEY")
    if preference in ("auto", "claude-cli") and binary and logged_in(binary):
        return ClaudeCLIRaw(model, binary)
    if preference == "claude-cli":
        raise BackendUnavailable(
            "Claude Code CLI が見つからないかログインしていません。`claude` を入れて `claude auth login` するか、"
            "ANTHROPIC_API_KEY を設定して --backend anthropic-api を使ってください。"
        )
    if preference in ("auto", "anthropic-api") and api_key:
        return AnthropicAPIRaw(model, api_key)
    raise BackendUnavailable(
        "LLM を使えません。ログイン済みの Claude Code CLI (`claude`) か ANTHROPIC_API_KEY が必要です。"
    )


def select_embedder(model: str, env: dict[str, str] | None = None) -> RawEmbedder | None:
    env = os.environ if env is None else env
    key = env.get("OPENAI_API_KEY")
    return OpenAIEmbeddingRaw(model, key) if key else None
