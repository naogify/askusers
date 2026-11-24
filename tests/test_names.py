"""合成ユーザーの架空の氏名: 生成・検査・表示・後方互換。LLM はモック。"""

from __future__ import annotations

import asyncio
import json

from conftest import RecordingLLM, make_llm, run_pipeline

from askusers import outputs, personas, store
from askusers.export_csv import build_rows
from askusers.models import AttributeDef, PersonaDesign, User
from askusers.names import FICTIONAL_NOTICE, find_name_problems, short_profile, surname_key
from askusers.render_html import render_html
from askusers.render_md import render_markdown
from askusers.target import load_file


def _u(i, name, **attrs):
    return User(id=f"user-{i:03d}", displayName=name, attributes=attrs, background=f"背景{i}")


def test_mock_run_assigns_unique_names_and_keeps_ids(survey_run):
    names = [u.displayName for u in survey_run.users]
    assert all(names) and len(set(names)) == len(names)
    assert [u.id for u in survey_run.users] == [f"user-{i:03d}" for i in range(1, 7)]
    assert not find_name_problems(survey_run.users)


def test_problems_detect_missing_duplicate_and_surname_skew():
    users = [_u(1, "佐藤 美咲"), _u(2, "佐藤　美咲"), _u(3, None), _u(4, "佐藤 健太"),
             _u(5, "佐藤 翔"), _u(6, "高橋 彩")]
    # 重複(2)・欠落(3)・名字の偏り(5: 佐藤が上限 2 を超える)
    assert find_name_problems(users) == ["user-002", "user-003", "user-005"]
    assert find_name_problems(users, check_surname=False) == ["user-002", "user-003"]


def test_surname_key_handles_japanese_and_english_order():
    assert surname_key("佐藤 美咲") == "佐藤"
    assert surname_key("Emily Carter") == "carter"


def test_names_are_requested_in_prompt_with_rules(tmp_path, idea):
    llm = make_llm()
    run_pipeline(tmp_path, [load_file(str(idea))], llm=llm)
    sys_p = next(s for t, s, _ in llm.raw.calls if t == "personas")
    assert "fictional" in sys_p and "celebrity" in sys_p and "name" in sys_p


def test_duplicate_or_missing_names_are_reassigned_by_llm():
    raw = RecordingLLM()
    llm = make_llm(raw)
    design = PersonaDesign(attributeSchema=[AttributeDef(key="a", label="属性", type="category")],
                           policy="p", segments=[])
    users = [_u(1, "佐藤 美咲", a="x"), _u(2, "佐藤 美咲", a="y"), _u(3, None, a="z")]
    warnings: list[str] = []
    asyncio.run(personas.ensure_names(llm, design, "一般", users, warnings))
    assert not find_name_problems(users, check_surname=False)
    assert users[0].displayName == "佐藤 美咲"  # 先に出た人は変えない
    assert [t for t, _, _ in raw.calls] == ["names"] and not warnings


def test_unfixable_names_fall_back_to_id_display_with_warning():
    from askusers.llm import LLMError

    raw = RecordingLLM()
    raw.fail = lambda task, user: LLMError("fatal", "boom") if task == "names" else None
    design = PersonaDesign(attributeSchema=[AttributeDef(key="a", label="属性", type="category")],
                           policy="p", segments=[])
    users = [_u(1, "佐藤 美咲", a="x"), _u(2, "佐藤 美咲", a="y"), _u(3, None, a="z")]
    warnings: list[str] = []
    asyncio.run(personas.ensure_names(make_llm(raw), design, "一般", users, warnings))
    assert [u.displayName for u in users] == ["佐藤 美咲", None, None]
    assert any("user-002" in w for w in warnings) and any("user-003" in w for w in warnings)


def test_short_profile_prefers_age_and_occupation():
    schema = [AttributeDef(key="gender", label="性別", type="category"),
              AttributeDef(key="age", label="年齢", type="number"),
              AttributeDef(key="occupation", label="職業", type="category")]
    u = _u(1, "佐藤 美咲", gender="女性", age=34, occupation="会社員")
    assert short_profile(u, schema) == "34歳・会社員"
    assert short_profile(_u(2, "x", gender="男性"), schema) == "男性"


def test_markdown_quote_shows_name_first_then_id(survey_run):
    md = render_markdown(survey_run)
    u = next(x for x in survey_run.users if x.displayName in md)
    assert f"— {u.displayName}さん" in md and f"［{u.id}・response-" in md
    assert FICTIONAL_NOTICE in md


def test_html_lists_names_ids_and_notice_and_searchable(survey_run):
    html = render_html(survey_run)
    for u in survey_run.users:
        assert u.displayName in html and u.id in html
    assert FICTIONAL_NOTICE in html
    # 氏名はユーザー一覧の検索対象・ユーザー選択のラベルに入る
    u = survey_run.users[0]
    assert f'{u.displayName}（{u.id}）</option>' in html
    assert u.displayName.lower() in html.lower().split('data-search="')[1].split('"')[0]


def test_csv_has_display_name_column(survey_run):
    head, rows = build_rows(survey_run)
    assert head[:2] == ["user_id", "display_name"]
    assert [r[1] for r in rows] == [u.displayName for u in survey_run.users]


def test_old_data_json_without_names_still_loads_and_renders(tmp_path, survey_run):
    obj = json.loads(store.dumps(survey_run))
    for u in obj["users"]:
        del u["displayName"]
    p = tmp_path / "data.json"
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    d = store.load(p)
    assert all(u.displayName is None for u in d.users)
    outputs.render_all(d, tmp_path)
    md = (tmp_path / "report.md").read_text("utf-8")
    assert FICTIONAL_NOTICE not in md and "— user-" in md and "さん（" not in md
    assert FICTIONAL_NOTICE not in (tmp_path / "survey.html").read_text("utf-8")
    assert (tmp_path / "responses.csv").read_text("utf-8-sig").splitlines()[0].startswith("user_id,display_name")


def test_name_is_escaped_in_html_and_markdown(survey_run):
    d = survey_run.model_copy(deep=True)
    d.users[0].displayName = '<script>alert(1)</script> [x](http://evil)'
    html, md = render_html(d), render_markdown(d)
    assert "<script>alert(1)</script> [x]" not in html
    assert "<script>alert(1)" not in md


def test_pipeline_fixes_duplicate_names_from_persona_generation(tmp_path, idea):
    class SameName(RecordingLLM):
        def _personas(self, user):
            data = super()._personas(user)
            for u in data["users"]:
                u["name"] = "佐藤 美咲"
            return data

    raw = SameName()
    d = run_pipeline(tmp_path, [load_file(str(idea))], llm=make_llm(raw))
    names = [u.displayName for u in d.users]
    assert all(names) and len(set(names)) == len(names)
    assert "names" in [t for t, _, _ in raw.calls]
