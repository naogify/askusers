"""SSR Scorer: SSR 対象質問の自由回答を Embedding し、確率分布と期待スコアにする。"""

from __future__ import annotations

import numpy as np

from . import ssr
from .llm import Embedder, LLMError
from .models import ExecError, Question, Response, SSRResult, SSRSetResult


async def score_question(q: Question, responses: list[Response], embedder: Embedder,
                         errors: list[ExecError]) -> int:
    """responses の ssr を埋める。計算できた件数を返す。"""
    targets = [r for r in responses if r.status == "completed" and isinstance(r.answer, str)
               and r.answer.strip()]
    if not targets:
        return 0
    anchors_txt = [s for aset in q.anchorSets for s in aset.statements]
    try:
        vecs = await embedder.embed(anchors_txt + [r.answer for r in targets])  # type: ignore[misc]
    except LLMError as e:
        errors.append(ExecError(stage="ssr", questionId=q.id, message=f"Embedding 取得失敗: {e}",
                                attempts=e.attempts))
        return 0
    n_anchor = len(anchors_txt)
    anchor_sets = [vecs[i * 5 : (i + 1) * 5].T for i in range(len(q.anchorSets))]
    resp_vecs = vecs[n_anchor:]
    done = 0
    for r, v in zip(targets, resp_vecs, strict=True):
        try:
            pmfs, per_set = ssr.get_response_pmfs(np.array([v]), anchor_sets)
        except ssr.SSRError as e:
            errors.append(ExecError(stage="ssr", questionId=q.id, userId=r.userId,
                                    targetId=r.targetId, message=f"SSR 計算不能: {e}"))
            continue
        pmf = pmfs[0]
        r.ssr = SSRResult(
            distribution=[float(x) for x in pmf], expectedScore=ssr.expected_score(pmf),
            perSet=[SSRSetResult(setId=a.id, distribution=[float(x) for x in p[0]])
                    for a, p in zip(q.anchorSets, per_set, strict=True)],
        )
        done += 1
    return done
