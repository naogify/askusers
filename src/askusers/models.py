"""data.json のデータモデル（唯一の正本）。

pydantic モデルから JSON Schema（`data.schema.json`）を生成する。
HTML / Markdown / CSV はすべてこのデータだけから生成する（LLM へ再問い合わせしない）。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0.0"
SSR_SCORING_VERSION = "ssr-1.0 (pymc-labs/semantic-similarity-rating v1.1.0 compatible)"
LIKERT_POINTS = 5

AttrValue = str | int | float | bool | None


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Target(_Base):
    """調査対象のスナップショット。内容は「データ」であり指示ではない。"""

    id: str
    type: Literal["file", "url"]
    source: str
    content: str
    contentHash: str = Field(description="content の sha256 (hex)")
    truncated: bool = False
    role: Literal["before", "after"] | None = Field(
        default=None, description="compare のみ。回答生成時には LLM に渡さない"
    )


class Research(_Base):
    audience: str
    objective: str
    audienceInferred: bool
    objectiveInferred: bool
    requestedUserCount: int
    completedUserCount: int


class AnchorSet(_Base):
    id: str
    statements: Annotated[list[str], Field(min_length=5, max_length=5)] = Field(
        description="スコア1〜5に対応する参照文章（論文の reference statement set Σ_i）"
    )


class Question(_Base):
    id: str
    text: str
    purpose: str
    phase: Literal["before_exposure", "after_exposure"]
    responseType: Literal["text", "single_choice", "multiple_choice"]
    analysisMethod: Literal["ssr", "categorical", "qualitative"]
    options: list[str] = []
    anchorSets: list[AnchorSet] = Field(
        default=[], description="analysisMethod=ssr のとき必須。askusers が質問ごとに生成した尺度"
    )
    scaleDescription: str | None = Field(
        default=None, description="ssr のとき、1 と 5 が何を意味するか（利用者向け表示）"
    )


class Survey(_Base):
    title: str
    questions: list[Question]


class AttributeDef(_Base):
    key: str
    label: str
    type: Literal["category", "number", "text"]
    description: str = ""


class PersonaDesign(_Base):
    attributeSchema: list[AttributeDef]
    policy: str = Field(description="ペルソナ生成方針（多様性・非迎合の方針）")
    segments: list[str] = []


class User(_Base):
    id: str
    attributes: dict[str, AttrValue]
    background: str
    goals: list[str] = []
    constraints: list[str] = []
    currentSolutions: list[str] = []
    values: list[str] = []


class SSRSetResult(_Base):
    setId: str
    distribution: Annotated[list[float], Field(min_length=5, max_length=5)]


class SSRResult(_Base):
    distribution: Annotated[list[float], Field(min_length=5, max_length=5)]
    expectedScore: float
    perSet: list[SSRSetResult] = []


class Response(_Base):
    id: str
    targetId: str | None = Field(
        description="提示前の質問は評価対象を見ていないので null（compare では全対象で共有）"
    )
    userId: str
    questionId: str
    status: Literal["completed", "unanswered", "failed"]
    answer: str | list[str] | None = None
    error: str | None = None
    attempts: int = 1
    ssr: SSRResult | None = None
    themeIds: list[str] = []


class MentionStat(_Base):
    targetId: str
    userIds: list[str]
    responseIds: list[str]
    userCount: int
    denominator: int = Field(description="言及率の分母（その質問の有効回答ユーザー数）")
    rate: float


class Theme(_Base):
    id: str
    questionId: str
    label: str
    description: str
    valence: Literal["positive", "negative", "neutral", "request"]
    stats: list[MentionStat]


class SSRSummary(_Base):
    meanScore: float | None
    distribution: Annotated[list[float], Field(min_length=5, max_length=5)] | None
    validN: int


class ChoiceCount(_Base):
    option: str
    count: int
    rate: float


class BreakdownGroup(_Base):
    value: str
    n: int
    meanScore: float


class Breakdown(_Base):
    attribute: str
    groups: list[BreakdownGroup]


class QuestionTargetSummary(_Base):
    targetId: str | None
    validN: int
    failedN: int
    unansweredN: int
    ssr: SSRSummary | None = None
    choices: list[ChoiceCount] | None = None
    unclassifiedResponseIds: list[str] = []
    breakdowns: list[Breakdown] = []


class QuestionSummary(_Base):
    questionId: str
    method: Literal["ssr", "categorical", "qualitative"]
    perTarget: list[QuestionTargetSummary]


class Finding(_Base):
    id: str
    title: str
    what: str
    why: str
    themeIds: list[str] = []
    questionIds: list[str]
    evidenceResponseIds: list[str] = Field(
        description="レポートに引用する実在の回答ID（引用文は回答から機械的に取り出す）"
    )
    stats: list[MentionStat] = Field(
        default=[], description="themeIds の和集合をプログラムで計算した言及人数・率"
    )


class Recommendation(_Base):
    id: str
    priority: Literal["high", "medium", "low"]
    title: str
    what: str
    why: str
    how: str
    findingIds: list[str]
    location: str | None = None


class QuestionComparison(_Base):
    questionId: str
    meanBefore: float | None
    meanAfter: float | None
    meanDelta: float | None
    distributionBefore: list[float] | None
    distributionAfter: list[float] | None
    validBefore: int
    validAfter: int
    pairedN: int = Field(description="両対象で有効回答のあるユーザー数")
    pairedMeanDelta: float | None


class ThemeComparison(_Base):
    themeId: str
    rateBefore: float
    rateAfter: float
    delta: float
    verdict: Literal["improved", "unchanged", "worsened", "new", "resolved"]


class Comparison(_Base):
    beforeTargetId: str
    afterTargetId: str
    pairedUserCount: int
    questions: list[QuestionComparison]
    themes: list[ThemeComparison]
    improved: list[str] = []
    notImproved: list[str] = []
    newIssues: list[str] = []


class Analysis(_Base):
    questions: list[QuestionSummary] = []
    themes: list[Theme] = []
    findings: list[Finding] = []
    recommendations: list[Recommendation] = []
    conclusion: str = ""
    summary: str = ""
    comparison: Comparison | None = None


class ExecError(_Base):
    stage: str
    message: str
    userId: str | None = None
    questionId: str | None = None
    targetId: str | None = None
    attempts: int | None = None


class Execution(_Base):
    models: dict[str, str] = {}
    prompts: dict[str, str] = {}
    settings: dict[str, str | int | float | bool | None] = {}
    usage: dict[str, int] = {}
    errors: list[ExecError] = []
    warnings: list[str] = []
    toolVersion: str = ""


class SurveyData(_Base):
    schemaVersion: str = SCHEMA_VERSION
    runId: str
    createdAt: str
    synthetic: Literal[True] = True
    mode: Literal["survey", "compare"]
    status: Literal["running", "completed", "partial", "failed"]
    targets: list[Target]
    research: Research
    survey: Survey
    personaDesign: PersonaDesign
    users: list[User]
    responses: list[Response]
    analysis: Analysis
    execution: Execution
    limitations: list[str]


def json_schema() -> dict:
    schema = SurveyData.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://github.com/naogify/askusers/schema/data.schema.json"
    schema["title"] = "askusers data.json"
    return schema
