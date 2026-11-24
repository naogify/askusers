"""調査パイプライン: 取得 → 設計 → ペルソナ → 回答 → SSR → 定性分析 → 集計/比較 → レポート → 保存。"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, aggregate, designer, personas, qualitative, report, runner, scorer, store
from . import prompts as P
from .limitations import COMPARE_LIMITATION, LIMITATIONS
from .llm import LLM, Embedder
from .models import (
    Analysis,
    ExecError,
    Execution,
    Research,
    Response,
    Survey,
    SurveyData,
    Target,
)


@dataclass
class RunConfig:
    targets: list[Target]
    audience: str | None
    objective: str | None
    count: int
    output_dir: Path
    mode: str = "survey"
    no_ssr: bool = False
    settings: dict = field(default_factory=dict)
    run_id: str | None = None


class Progress:
    """進捗は stderr に出す（--format json でも標準出力を汚さない）。"""

    def __init__(self, quiet: bool = False, stream=None):
        self.quiet = quiet
        self.stream = stream or sys.stderr
        self.survey_total = 0
        self.survey_done = 0
        self.survey_failed = 0

    def step(self, msg: str) -> None:
        if not self.quiet:
            print(f"✓ {msg}", file=self.stream, flush=True)

    def info(self, msg: str) -> None:
        if not self.quiet:
            print(f"  {msg}", file=self.stream, flush=True)

    def user_done(self, ok: bool) -> None:
        self.survey_done += 1
        self.survey_failed += (not ok)
        if not self.quiet and (self.survey_done % 5 == 0 or self.survey_done == self.survey_total):
            print(f"  … 回答取得 {self.survey_done}/{self.survey_total}"
                  f"（失敗あり {self.survey_failed}）", file=self.stream, flush=True)


def new_run_id() -> str:
    return "run-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def assemble(users, targets, survey: Survey, out: runner.RunnerOutput) -> list[Response]:
    resp: list[Response] = []
    n = 0
    for u in users:
        for q in survey.questions:
            tids = [None] if q.phase == "before_exposure" else [t.id for t in targets]
            for tid in tids:
                r = out.responses.get((u.id, tid, q.id))
                if r is None:
                    continue
                n += 1
                r.id = f"response-{n:04d}"
                resp.append(r)
    return resp


def count_users_completed(users, responses: list[Response]) -> int:
    bad = {r.userId for r in responses if r.status == "failed"}
    have = {r.userId for r in responses}
    return len([u for u in users if u.id in have and u.id not in bad])


async def run(cfg: RunConfig, llm: LLM, embedder: Embedder | None, progress: Progress) -> SurveyData:
    warnings: list[str] = []
    errors: list[ExecError] = []
    allow_ssr = embedder is not None and not cfg.no_ssr
    objective = cfg.objective or designer.DEFAULT_OBJECTIVE
    out_json = cfg.output_dir / "data.json"
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    design = await designer.design_survey(llm, cfg.targets, cfg.audience, objective, allow_ssr)
    survey = design.survey
    progress.step(f"Questionnaire created ({len(survey.questions)} questions)")
    pdesign = await personas.design_personas(llm, design.audience, objective)
    users = await personas.generate_personas(llm, pdesign, design.audience, objective, cfg.count, warnings)
    progress.step(f"Synthetic users generated ({len(users)})")

    models = {"generation": llm.model, "generationBackend": llm.name}
    if embedder is not None:
        models["embedding"] = embedder.model
    exec_ = Execution(
        models=models, prompts={k: v for k, v in P.SYSTEM.items()},
        settings={**cfg.settings, "ssrEpsilon": 0.0, "ssrTemperature": 1.0, "ssrAnchorSets": 3,
                  "noSsr": cfg.no_ssr}, toolVersion=__version__)
    data = SurveyData(
        runId=cfg.run_id or new_run_id(), createdAt=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        mode=cfg.mode, status="running",  # type: ignore[arg-type]
        targets=cfg.targets,
        research=Research(audience=design.audience, objective=objective,
                          audienceInferred=cfg.audience is None, objectiveInferred=cfg.objective is None,
                          requestedUserCount=cfg.count, completedUserCount=0),
        survey=survey, personaDesign=pdesign, users=users, responses=[], analysis=Analysis(),
        execution=exec_,
        limitations=LIMITATIONS + ([COMPARE_LIMITATION] if cfg.mode == "compare" else []),
    )
    store.save_checkpoint(data, out_json)

    # --- アンケート実施
    labels = {a.key: a.label for a in pdesign.attributeSchema}
    progress.survey_total = len(users)
    done_count = 0

    def on_done(uid: str, ok: bool) -> None:
        nonlocal done_count
        progress.user_done(ok)
        done_count += 1

    out = await runner.run_survey(llm, survey, users, cfg.targets, labels, on_done)
    data.responses = assemble(users, cfg.targets, survey, out)
    errors += out.errors
    data.research.completedUserCount = count_users_completed(users, data.responses)
    store.save_checkpoint(data, out_json)
    nfail = sum(r.status == "failed" for r in data.responses)
    progress.step(f"Survey completed ({data.research.completedUserCount}/{len(users)} users"
                  + (f", {nfail} responses failed)" if nfail else ")"))

    # --- SSR
    if allow_ssr:
        assert embedder is not None
        for q in survey.questions:
            if q.analysisMethod == "ssr":
                rs = [r for r in data.responses if r.questionId == q.id]
                await scorer.score_question(q, rs, embedder, errors)
    # --- 定性分析
    theme_no = 1
    tids = [t.id for t in cfg.targets]
    for q in survey.questions:
        if q.analysisMethod == "qualitative":
            rs = [r for r in data.responses if r.questionId == q.id]
            qt = [None] if q.phase == "before_exposure" else tids
            themes = await qualitative.analyze_question(llm, q, rs, qt, theme_no, errors)
            theme_no += len(themes)
            data.analysis.themes += themes
    data.analysis.questions = [aggregate.summarize_question(data, q) for q in survey.questions]
    if cfg.mode == "compare":
        data.analysis.comparison = aggregate.build_comparison(data)
    progress.step("Responses analyzed")
    store.save_checkpoint(data, out_json)

    # --- レポート
    await report.generate_report(llm, data, errors, warnings)
    progress.step("Report generated")

    # --- 仕上げ
    exec_.errors = errors
    exec_.warnings = warnings
    exec_.usage = {**llm.usage.as_dict(), **{k: v for k, v in (embedder.usage.as_dict().items()
                                                               if embedder else []) if k.startswith("embedding")}}
    ok_resp = sum(r.status == "completed" for r in data.responses)
    if ok_resp == 0:
        data.status = "failed"
    elif errors:
        data.status = "partial"
    else:
        data.status = "completed"
    return data
