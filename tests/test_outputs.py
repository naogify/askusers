"""JSON / HTML / Markdown / CSV の出力。固定のサンプル JSON（examples/）から生成して検証する。"""
import copy
import csv
import io
import json
import re
from html.parser import HTMLParser

import pytest
from conftest import EXAMPLES

from askusers import store
from askusers.export_csv import build_rows, render_csv, safe_cell
from askusers.integrity import check_integrity
from askusers.models import Response, SurveyData
from askusers.render_html import json_for_script, render_html
from askusers.render_md import md_escape, render_markdown

XSS = '<script>alert(1)</script><img src=x onerror=alert(2)>"&'


# ------------------------------------------------------------ JSON

def test_sample_validates_against_schema_and_integrity(sample, sample_compare):
    for d in (sample, sample_compare):
        store.validate_json_schema(d.model_dump(mode="json"))
        assert check_integrity(d) == []
        assert d.synthetic is True and d.schemaVersion == "1.0.0"


def test_committed_schema_is_up_to_date():
    from importlib import resources
    assert resources.files("askusers").joinpath("data.schema.json").read_text("utf-8") == store.schema_text()


def test_schema_rejects_bad_data(sample):
    obj = sample.model_dump(mode="json")
    obj["responses"][0]["status"] = "weird"
    with pytest.raises(store.DataError):
        store.validate_json_schema(obj)
    obj = sample.model_dump(mode="json")
    obj["extra"] = 1
    with pytest.raises(store.DataError):
        store.validate_json_schema(obj)
    obj = sample.model_dump(mode="json")
    obj["synthetic"] = False
    with pytest.raises(store.DataError):
        store.validate_json_schema(obj)


def test_integrity_detects_broken_references(sample):
    d = sample.model_copy(deep=True)
    d.responses[0].userId = "user-999"
    d.analysis.findings[0].evidenceResponseIds = ["response-9999"]
    d.analysis.themes[0].stats[0].userCount = 99
    probs = " ".join(check_integrity(d))
    assert "user-999" in probs and "evidenceResponseId" in probs and "userCount" in probs


def test_snapshot_hash_and_everything_saved(survey_run):
    d = survey_run
    import hashlib
    for t in d.targets:
        assert t.contentHash == hashlib.sha256(t.content.encode()).hexdigest() and "請求書" in t.content
    assert len(d.users) == 6
    expected = sum(1 if q.phase == "before_exposure" else len(d.targets) for q in d.survey.questions) * 6
    assert len(d.responses) == expected
    assert d.analysis.themes and d.analysis.findings and d.analysis.recommendations and d.analysis.summary
    assert d.execution.models["generation"] and d.execution.models["embedding"]
    assert "answer" in d.execution.prompts and d.execution.usage["requests"] > 0
    assert d.execution.settings["ssrEpsilon"] == 0.0 and d.limitations


def test_errors_are_saved_in_json(tmp_path, idea):
    from conftest import RecordingLLM, make_llm, run_pipeline

    from askusers.llm import LLMError
    from askusers.target import load_file
    llm = make_llm(RecordingLLM())
    llm.raw.fail = lambda t, u: LLMError("transient", "x") if t == "answer" and "user-001" in u else None
    d = run_pipeline(tmp_path, [load_file(str(idea))], llm=llm, count=3)
    saved = json.loads(store.dumps(d))
    assert saved["execution"]["errors"] and saved["status"] == "partial"


def test_no_secrets_in_saved_data(survey_run):
    text = store.dumps(survey_run)
    assert store.find_secrets(text) == []
    assert store.find_secrets("key sk-ant-api03-abcdefghijklmnop") != []
    for f in ("data.json", "report.md", "responses.csv", "survey.html"):
        assert store.find_secrets((EXAMPLES / "mock-survey" / f).read_text("utf-8")) == []


# ------------------------------------------------------------ HTML

class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.attrs = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attrs.append((tag, dict(attrs)))


def parse(html):
    p = Tags()
    p.feed(html)
    return p


def test_html_is_single_offline_file(sample):
    h = render_html(sample)
    p = parse(h)
    assert not [a for t, a in p.attrs if t == "script" and "src" in a]
    assert not [a for t, a in p.attrs if t == "link"]
    assert not [a for t, a in p.attrs if t == "img"]
    assert not re.search(r"(?:src|href)=\"https?://", h) and "@import" not in h and "url(http" not in h
    assert 'name="viewport"' in h and "合成ユーザー" in h and "prefers-color-scheme" in h


def test_html_tabs_summary_users_responses(sample):
    h = render_html(sample)
    for tab in ("summary", "users", "responses"):
        assert f'data-tab="{tab}"' in h and f'data-panel="{tab}"' in h
    for q in sample.survey.questions:
        assert f'id="q-{q.id}"' in h and q.text in h
    for u in sample.users:
        assert f'id="resp-{u.id}"' in h and f'data-uid="{u.id}"' in h
    assert 'id="user-search"' in h and 'id="resp-search"' in h and 'id="prev-user"' in h and 'id="next-user"' in h
    assert "report.md" in h and "responses.csv" in h and "data.json" in h


def test_html_aggregates_show_numbers_not_just_charts(sample):
    h = render_html(sample)
    ssr_q = next(q for q in sample.survey.questions if q.analysisMethod == "ssr")
    summ = next(s for s in sample.analysis.questions if s.questionId == ssr_q.id).perTarget[0]
    assert f"{summ.ssr.meanScore:.2f}" in h and f"{summ.ssr.distribution[0] * 100:.1f}%" in h
    assert "実際に回答者が選んだ割合・購入率ではありません" in h  # SSR を選択件数として表示しない
    cat_q = next(q for q in sample.survey.questions if q.analysisMethod == "categorical")
    ch = next(s for s in sample.analysis.questions if s.questionId == cat_q.id).perTarget[0].choices[0]
    assert f"{ch.count}人" in h
    theme = sample.analysis.themes[0]
    assert theme.label in h and f"{theme.stats[0].userCount}人 / {theme.stats[0].denominator}人" in h


def test_html_lists_every_answer_not_just_representative(sample):
    h = render_html(sample)
    for r in sample.responses:
        if r.status == "completed" and isinstance(r.answer, str):
            assert f">{r.id}<" in h
    n = len(sample.users)
    assert h.count(f"すべての回答（{n}件）") == len(sample.survey.questions)


def test_html_filter_hooks_for_themes(sample):
    h = render_html(sample)
    theme = sample.analysis.themes[0]
    assert f'data-theme="{theme.id}"' in h
    assert any(f'data-themes="{theme.id}' in h or f' {theme.id}' in h for _ in [0])


def test_html_escapes_everything(sample):
    d = sample.model_copy(deep=True)
    d.survey.title = XSS
    d.survey.questions[0].text = XSS
    d.users[0].background = XSS
    d.users[0].attributes[next(iter(d.users[0].attributes))] = XSS
    d.targets[0].source = XSS
    d.analysis.themes[0].label = XSS
    d.responses[0].answer = XSS
    d.responses[0].status = "completed"
    h = render_html(d)
    assert "<script>alert(1)</script>" not in h and "<img src=x" not in h
    assert h.count("<script") == 2  # JSON データと JS の 2 つだけ
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in h
    blob = re.search(r'id="askusers-data">(.*?)</script>', h, re.S).group(1)
    assert "<" not in blob and json.loads(blob)["survey"]["title"] == XSS  # 埋め込み JSON も壊れない


def test_json_for_script_neutralizes_closing_tag():
    s = json_for_script({"a": "</script><!--", "b": " "})
    assert "</script>" not in s and "<!--" not in s and json.loads(s)["a"] == "</script><!--"


def test_html_failed_and_unanswered_states_visible(sample):
    d = sample.model_copy(deep=True)
    d.responses[0].status, d.responses[0].answer, d.responses[0].error, d.responses[0].ssr = "failed", None, "boom", None
    d.responses[1].status, d.responses[1].answer, d.responses[1].ssr = "unanswered", None, None
    d.status = "partial"
    h = render_html(d)
    assert "回答取得失敗" in h and "未回答" in h and "不完全" in h


def test_html_compare_side_by_side(sample_compare):
    h = render_html(sample_compare)
    assert "Before" in h and "After" in h and "改善前後の比較" in h and "実際の改善効果を保証しません" in h
    assert 'class="cols two"' in h
    user = sample_compare.users[0]
    assert h.count(f'id="resp-{user.id}"') == 1  # 同一ユーザーの両対象の回答が 1 画面に並ぶ


# ------------------------------------------------------------ Markdown

def test_markdown_structure_and_evidence(sample):
    md = render_markdown(sample)
    assert md.index("## 1. 結論") < md.index("## 2. 発見事項") < md.index("## 3. 改善提案")
    assert "survey.html" in md and "合成ユーザー" in md and "実際の市場需要を示すものではありません" in md
    rmap = {r.id: r for r in sample.responses}
    for f in sample.analysis.findings:
        assert f.title in md
        for rid in f.evidenceResponseIds:
            assert rmap[rid].answer.strip() in md and rid in md
        for qid in f.questionIds:
            assert qid.upper() in md
        if f.stats:
            assert f"{f.stats[0].userCount}人 / {f.stats[0].denominator}人" in md
    for r in sample.analysis.recommendations:
        assert r.title in md and r.findingIds[0] in md


def test_markdown_scores_only_for_ssr_questions(sample):
    md = render_markdown(sample)
    ssr_q = next(q for q in sample.survey.questions if q.analysisMethod == "ssr")
    assert f"{ssr_q.id.upper()}）：**" in md
    for q in sample.survey.questions:
        if q.analysisMethod != "ssr":
            assert f"（{q.id.upper()}）：**" not in md  # SSR 対象外にスコアを出さない
    assert "/ 5**" in md


def test_markdown_quotes_come_only_from_real_responses(survey_run):
    md = render_markdown(survey_run)
    rids = {r.id for r in survey_run.responses}
    for rid in re.findall(r"response-\d{4}", md):
        assert rid in rids
    quotes = re.findall(r"^> (.+)$", md, re.M)
    answers = " ".join(md_escape(str(r.answer)) for r in survey_run.responses)
    assert quotes and all(q in answers for q in quotes)


def test_report_drops_nonexistent_evidence(tmp_path, idea):
    from conftest import RecordingLLM, make_llm, run_pipeline

    from askusers.target import load_file

    class Liar(RecordingLLM):
        def _report(self, user):
            out = super()._report(user)
            out["findings"][0]["evidenceResponseIds"] = ["response-9999", *out["findings"][0]["evidenceResponseIds"]]
            out["findings"].append({"title": "根拠なし", "what": "x", "why": "y", "themeIds": ["theme-999"],
                                    "questionIds": ["q99"], "evidenceResponseIds": []})
            return out
    d = run_pipeline(tmp_path, [load_file(str(idea))], llm=make_llm(Liar()))
    assert all("response-9999" not in f.evidenceResponseIds for f in d.analysis.findings)
    assert all(f.title != "根拠なし" for f in d.analysis.findings)
    assert any("存在しない" in w or "除外" in w for w in d.execution.warnings)
    assert check_integrity(d) == []


def test_markdown_neutralizes_dangerous_content(sample):
    d = sample.model_copy(deep=True)
    rid = d.analysis.findings[0].evidenceResponseIds[0]
    next(r for r in d.responses if r.id == rid).answer = "<script>x</script>\n# 見出し\n[click](javascript:alert(1))"
    d.analysis.findings[0].title = "<img src=x onerror=1> タイトル"
    md = render_markdown(d)
    assert "<script>" not in md and "<img" not in md
    assert not re.search(r"(?<!\\)\]\(", md)  # エスケープされていないリンク記法が無い
    assert "&lt;script&gt;" in md and "\n# 見出し" not in md


# ------------------------------------------------------------ CSV

def read_csv(d: SurveyData):
    raw = render_csv(d)
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))


def test_csv_one_row_per_user_and_user_id_first(sample):
    rows = read_csv(sample)
    assert rows[0][0] == "user_id" and len(rows) == 1 + len(sample.users)
    assert [r[0] for r in rows[1:]] == [u.id for u in sample.users]


def test_csv_columns_match_questions_and_attributes(sample):
    head = read_csv(sample)[0]
    for a in sample.personaDesign.attributeSchema:
        assert a.key in head
    for q in sample.survey.questions:
        assert f"{q.id.upper()}.{q.text}" in head
        has_score = f"{q.id.upper()}.expected_score" in head
        assert has_score == (q.analysisMethod == "ssr")  # SSR 対象外に不要なスコア列を作らない
        assert (f"{q.id.upper()}.p1" in head) == has_score
    i = head.index("Q4.expected_score")
    assert head[i - 1].startswith("Q4.") and head[i + 1 : i + 6] == [f"Q4.p{k}" for k in range(1, 6)]  # 隣接配置


def test_csv_values_consistent_with_json(sample):
    head, *rows = read_csv(sample)
    byuser = {r[0]: dict(zip(head, r, strict=True)) for r in rows}
    ssr_q = next(q for q in sample.survey.questions if q.analysisMethod == "ssr")
    for r in sample.responses:
        if r.questionId == ssr_q.id and r.ssr:
            row = byuser[r.userId]
            assert float(row[f"{ssr_q.id.upper()}.expected_score"]) == pytest.approx(r.ssr.expectedScore, abs=1e-5)
            assert [float(row[f"{ssr_q.id.upper()}.p{i}"]) for i in range(1, 6)] == pytest.approx(r.ssr.distribution, abs=1e-5)
            assert row[f"{ssr_q.id.upper()}.{ssr_q.text}"] == r.answer
    for u in sample.users:
        for a in sample.personaDesign.attributeSchema:
            assert byuser[u.id][a.key] == str(u.attributes[a.key])


def test_csv_multiple_choice_is_json_array(sample):
    head, *rows = read_csv(sample)
    q = next(q for q in sample.survey.questions if q.responseType == "multiple_choice")
    col = head.index(f"{q.id.upper()}.{q.text}")
    for r in rows:
        v = json.loads(r[col])
        assert isinstance(v, list) and set(v) <= set(q.options)


def test_csv_escaping_roundtrip(sample):
    d = sample.model_copy(deep=True)
    tricky = 'a,b\n"quoted" 日本語\nline3'
    r = next(r for r in d.responses if r.status == "completed" and isinstance(r.answer, str))
    r.answer = tricky
    head, *rows = read_csv(d)
    col = head.index(f"{next(q for q in d.survey.questions if q.id == r.questionId).id.upper()}."
                     f"{next(q for q in d.survey.questions if q.id == r.questionId).text}")
    row = next(x for x in rows if x[0] == r.userId)
    assert row[col] == tricky  # 改行・カンマ・ダブルクォート・日本語が往復する


@pytest.mark.parametrize("v", ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx", "\rx"])
def test_csv_injection_guard(v):
    assert safe_cell(v).startswith("'")
    assert safe_cell("通常") == "通常" and safe_cell(-3) == -3 and safe_cell(3.5) == 3.5


def test_csv_injection_applied_but_json_untouched(sample):
    d = sample.model_copy(deep=True)
    r = next(r for r in d.responses if r.status == "completed" and isinstance(r.answer, str))
    r.answer = "=HYPERLINK(\"http://evil\")"
    d.users[0].background = "@evil"
    raw = render_csv(d).decode("utf-8-sig")
    assert "'=HYPERLINK" in raw and "'@evil" in raw
    assert r.answer.startswith("=") and json.loads(store.dumps(d))["responses"][d.responses.index(r)]["answer"].startswith("=")


def test_csv_numbers_stay_numeric(sample):
    head, *rows = read_csv(sample)
    i = head.index("Q4.expected_score")
    for r in rows:
        if r[i]:
            float(r[i])
    j = head.index("experienceYears")
    assert all(re.fullmatch(r"\d+", r[j]) for r in rows)


def test_csv_missing_failed_and_unanswered_distinguished(sample):
    d = sample.model_copy(deep=True)
    q3 = next(q for q in d.survey.questions if q.id == "q3")
    ra = next(r for r in d.responses if r.userId == d.users[0].id and r.questionId == "q3")
    rb = next(r for r in d.responses if r.userId == d.users[1].id and r.questionId == "q3")
    ra.status, ra.answer, ra.error, ra.themeIds = "failed", None, "boom", []
    rb.status, rb.answer, rb.themeIds = "unanswered", None, []
    head, *rows = read_csv(d)
    c = {r[0]: dict(zip(head, r, strict=True)) for r in rows}
    assert c[d.users[0].id][f"Q3.{q3.text}"] == "" and c[d.users[1].id][f"Q3.{q3.text}"] == ""
    assert json.loads(c[d.users[0].id]["failed_questions"]) == ["q3"] and c[d.users[0].id]["status"] == "partial"
    assert json.loads(c[d.users[1].id]["unanswered_questions"]) == ["q3"] and c[d.users[1].id]["failed_questions"] == "[]"


def test_csv_compare_rows_and_user_id_pairing(sample_compare):
    head, rows = build_rows(sample_compare)
    n = len(sample_compare.users)
    assert len(rows) == 2 * n  # 1 行 = 1 ユーザー × 1 評価対象
    assert head[:4] == ["user_id", "display_name", "target_id", "variant"]
    ids = [(r[0], r[2]) for r in rows]
    assert len(set(ids)) == 2 * n
    for u in sample_compare.users:
        assert {r[3] for r in rows if r[0] == u.id} == {"before", "after"}
    # 提示前の回答は両行で共通
    q1col = head.index(f"Q1.{sample_compare.survey.questions[0].text}")
    for u in sample_compare.users:
        assert len({r[q1col] for r in rows if r[0] == u.id}) == 1


def test_examples_are_regenerable_from_json(tmp_path, sample):
    from askusers import outputs
    outputs.render_all(sample, tmp_path)
    ex = EXAMPLES / "mock-survey"
    assert (tmp_path / "report.md").read_text() == (ex / "report.md").read_text()
    assert (tmp_path / "responses.csv").read_bytes() == (ex / "responses.csv").read_bytes()
    assert (tmp_path / "survey.html").read_text() == (ex / "survey.html").read_text()
    _ = copy, Response
