"""askusers CLI。

  askusers <target> [--who ...] [--ask ...]       調査を実行
  askusers compare <before> <after> [...]         改善前後の比較
  askusers render <data.json> [--output DIR]      data.json から HTML/Markdown/CSV を再生成（LLM 不使用）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import webbrowser
from pathlib import Path

from . import __version__, outputs, pipeline, store, target
from .llm import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_MODEL,
    LLM,
    BackendUnavailable,
    Embedder,
    LLMError,
    RetryPolicy,
    select_embedder,
    select_llm_backend,
)
from .mock import MockRawEmbedder, MockRawLLM

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_TARGET, EXIT_INCOMPLETE, EXIT_CONFIG = 0, 1, 2, 3, 4, 5


class CliError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="askusers",
        description="合成ユーザーにアンケート調査を行い、改善のヒントを得る。Ask users. Build better.",
        usage="askusers <target> [--who AUDIENCE] [--ask OBJECTIVE]\n"
              "       askusers compare <before> <after> [--who ...] [--ask ...]\n"
              "       askusers render <data.json> [--output DIR]",
    )
    p.add_argument("args", nargs="*", help="調査対象（ファイル/URL）。compare / render はサブコマンド")
    p.add_argument("--who", help="調査対象者を自然言語で指定（既定: AIが推定）")
    p.add_argument("--ask", help="調査目的を自然言語で指定（既定: 総合的な評価と改善点）")
    p.add_argument("--count", type=int, default=100, help="合成ユーザーの人数（既定 100）")
    p.add_argument("--output", help="出力ディレクトリ（既定: askusers-results/<run-id>）")
    p.add_argument("--format", choices=["text", "json"], default="text", help="標準出力の形式")
    p.add_argument("--open", action="store_true", help="完了後に survey.html をブラウザで開く")
    p.add_argument("--model", default=os.environ.get("ASKUSERS_MODEL", DEFAULT_MODEL), help="生成モデル")
    p.add_argument("--backend", choices=["auto", "claude-cli", "anthropic-api"], default="auto",
                   help="生成の呼び出し方式。auto: ログイン済み claude CLI → ANTHROPIC_API_KEY")
    p.add_argument("--embedding-model", default=os.environ.get("ASKUSERS_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL))
    p.add_argument("--concurrency", type=int, help="LLM の同時実行数（既定: claude CLI 4 / API 8）")
    p.add_argument("--max-requests", type=int, default=3000, help="LLM リクエスト数の上限（再試行を含む）")
    p.add_argument("--no-ssr", action="store_true",
                   help="Embedding API キー無しで続行: SSR を使わず定性分析・選択肢集計のみで調査する")
    p.add_argument("--mock", action="store_true", help="オフラインのデモ実行（偽の LLM。結果に意味は無い）")
    p.add_argument("--version", action="version", version=f"askusers {__version__}")
    return p


def estimate(n: int, n_targets: int, target_chars: int) -> dict:
    """実行前の規模見積もり（確定料金ではない）。質問数は 7 問・SSR 1 問・定性 3 問を仮定した概算。"""
    llm_req = 2 + math.ceil(n / 10) + n * (1 + n_targets) + 3 * (1 + math.ceil(n / 20)) + 1
    emb_req = math.ceil((n * n_targets + 15) / 128)
    in_tok = n * (1 + n_targets) * (900 + (target_chars // 3 if n_targets else 0)) + 20000
    out_tok = n * (1 + n_targets) * 350 + 6000
    return {"llmRequests": llm_req, "embeddingRequests": emb_req,
            "inputTokens": in_tok, "outputTokens": out_tok}


def make_clients(a: argparse.Namespace) -> tuple[LLM, Embedder | None, str]:
    policy = RetryPolicy()
    if a.mock:
        return (LLM(MockRawLLM(), concurrency=a.concurrency or 8, policy=RetryPolicy(base_delay=0),
                    max_requests=a.max_requests),
                None if a.no_ssr else Embedder(MockRawEmbedder()), "mock")
    try:
        raw = select_llm_backend(a.model, a.backend)
    except BackendUnavailable as e:
        raise CliError(EXIT_CONFIG, str(e)) from e
    emb_raw = select_embedder(a.embedding_model)
    if emb_raw is None and not a.no_ssr:
        raise CliError(
            EXIT_CONFIG,
            "SSR（数値化）には Embedding API が必要ですが OPENAI_API_KEY が設定されていません。\n"
            "Claude のサブスクリプションでは Embedding を提供していないため、API キーが必要です。\n"
            "  - OPENAI_API_KEY を設定する（論文と同じ text-embedding-3-small を使用）\n"
            "  - または --no-ssr を付けて、SSR を使わない（定性分析・選択肢集計のみの）調査にする",
        )
    conc = a.concurrency or (4 if raw.name == "claude-cli" else 8)
    return (LLM(raw, concurrency=conc, policy=policy, max_requests=a.max_requests),
            Embedder(emb_raw, policy=policy) if emb_raw else None, raw.name)


def summary_json(d, out: Path) -> dict:
    qmap = {q.id: q for q in d.survey.questions}
    scores = []
    for s in d.analysis.questions:
        if s.method == "ssr":
            for p in s.perTarget:
                if p.ssr and p.ssr.meanScore is not None:
                    scores.append({"questionId": s.questionId, "question": qmap[s.questionId].text,
                                   "targetId": p.targetId, "meanScore": round(p.ssr.meanScore, 3),
                                   "distribution": [round(x, 4) for x in p.ssr.distribution or []],
                                   "validN": p.ssr.validN})
    return {
        "runId": d.runId, "status": d.status, "synthetic": True, "mode": d.mode,
        "audience": d.research.audience, "audienceInferred": d.research.audienceInferred,
        "objective": d.research.objective, "objectiveInferred": d.research.objectiveInferred,
        "users": {"requested": d.research.requestedUserCount, "completed": d.research.completedUserCount},
        "questions": len(d.survey.questions), "summary": d.analysis.summary,
        "conclusion": d.analysis.conclusion, "scores": scores,
        "findings": [{"id": f.id, "title": f.title} for f in d.analysis.findings],
        "recommendations": [{"id": r.id, "priority": r.priority, "title": r.title}
                            for r in d.analysis.recommendations],
        "errors": len(d.execution.errors), "warnings": d.execution.warnings,
        "results": {f: str(out / f) for f in outputs.FILES},
        "disclaimer": d.limitations[0],
    }


def cmd_render(a: argparse.Namespace) -> int:
    if len(a.args) != 2:
        raise CliError(EXIT_USAGE, "使い方: askusers render <data.json> [--output DIR]")
    src = Path(a.args[1])
    try:
        d = store.load(src)
    except store.DataError as e:
        raise CliError(EXIT_ERROR, str(e)) from e
    out = Path(a.output) if a.output else src.parent
    outputs.render_all(d, out)
    print("\n".join(str(out / f) for f in outputs.FILES[:3]))
    return EXIT_OK


def cmd_run(a: argparse.Namespace) -> int:
    compare = bool(a.args) and a.args[0] == "compare"
    srcs = a.args[1:] if compare else a.args
    if compare and len(srcs) != 2:
        raise CliError(EXIT_USAGE, "使い方: askusers compare <before> <after> [--who ...] [--ask ...]")
    if not compare and len(srcs) != 1:
        raise CliError(EXIT_USAGE, "使い方: askusers <target> [--who ...] [--ask ...]（--help 参照）")
    if a.count < 1:
        raise CliError(EXIT_USAGE, "--count は 1 以上にしてください")
    targets = []
    for i, s in enumerate(srcs, 1):
        try:
            t = target.load_target(s, f"target-{i:03d}")
        except target.TargetError as e:
            raise CliError(EXIT_TARGET, f"評価対象を取得できません: {e}") from e
        if compare:
            t.role = "before" if i == 1 else "after"
        targets.append(t)

    llm, embedder, backend_name = make_clients(a)
    est = estimate(a.count, len(targets), sum(len(t.content) for t in targets))
    if est["llmRequests"] > a.max_requests:
        raise CliError(EXIT_CONFIG, f"推定リクエスト数 {est['llmRequests']} が上限 --max-requests {a.max_requests} を超えます。"
                                    "--count を減らすか上限を上げてください。")
    out = Path(a.output) if a.output else Path("askusers-results") / (rid := pipeline.new_run_id())
    rid = out.name if a.output else rid
    progress = pipeline.Progress()
    print("askusers\n", file=sys.stderr)
    print(f"Target: {' vs '.join(srcs)}", file=sys.stderr)
    print(f"Audience: {a.who or '(AIが推定)'}", file=sys.stderr)
    print(f"Objective: {a.ask or '総合的な評価と改善点'}", file=sys.stderr)
    print(f"Synthetic users: {a.count}", file=sys.stderr)
    print(f"LLM: {llm.model} via {backend_name}"
          + (f" ／ Embedding: {embedder.model}" if embedder else " ／ SSR なし (--no-ssr)"), file=sys.stderr)
    print(f"推定規模（確定料金ではありません）: LLM 約{est['llmRequests']}リクエスト"
          f"・Embedding 約{est['embeddingRequests']}リクエスト・トークン 入力約{est['inputTokens']:,}/出力約{est['outputTokens']:,}。"
          "claude CLI 利用時は Claude の利用上限（レート制限）を消費します。", file=sys.stderr)
    print("注意: 調査対象・入力内容は外部の LLM（Claude / Embedding API）に送信されます。"
          "個人情報・機密情報を含めないでください。\n", file=sys.stderr)

    cfg = pipeline.RunConfig(
        targets=targets, audience=a.who, objective=a.ask, count=a.count, output_dir=out,
        mode="compare" if compare else "survey", no_ssr=a.no_ssr, run_id=rid,
        settings={"count": a.count, "concurrency": llm.concurrency, "maxRequests": a.max_requests,
                  "maxAttempts": llm.policy.max_attempts, "format": a.format})
    try:
        d = asyncio.run(pipeline.run(cfg, llm, embedder, progress))
    except LLMError as e:
        raise CliError(EXIT_ERROR, f"LLM 呼び出しに失敗しました: {e}") from e
    except Exception as e:  # noqa: BLE001
        from .designer import DesignError
        from .personas import PersonaError
        if isinstance(e, DesignError | PersonaError):
            raise CliError(EXIT_ERROR, str(e)) from e
        raise
    outputs.write_all(d, out)
    code = EXIT_OK if d.status == "completed" else EXIT_INCOMPLETE
    if a.open:
        webbrowser.open((out / "survey.html").resolve().as_uri())
    if a.format == "json":
        print(json.dumps(summary_json(d, out), ensure_ascii=False, indent=2))
    else:
        print(f"\nSummary:\n{d.analysis.summary}\n")
        if d.status != "completed":
            print(f"⚠ 調査は不完全です（status: {d.status}、エラー {len(d.execution.errors)} 件）。"
                  "詳細は data.json の execution.errors を参照。\n")
        print("Results:\n" + "\n".join(f"  {out / f}" for f in outputs.FILES))
        print(f"\n{d.limitations[0]}")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        a = parser.parse_args(argv)
        if not a.args:
            parser.print_usage(sys.stderr)
            raise CliError(EXIT_USAGE, "調査対象（ファイルまたは URL）を指定してください")
        if a.args[0] == "render":
            return cmd_render(a)
        return cmd_run(a)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    except SystemExit as e:  # argparse の usage エラー(2)・--help(0)
        return int(e.code or 0)
    except KeyboardInterrupt:
        print("中断しました（data.json に途中経過が残っている場合があります）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
