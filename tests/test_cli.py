import json

import pytest

from askusers import cli, target

BASE = ["--mock", "--count", "4"]


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_parse_defaults():
    a = cli.build_parser().parse_args(["idea.md"])
    assert (a.who, a.ask, a.count, a.output, a.format, a.open) == (None, None, 100, None, "text", False)
    assert a.backend == "auto" and a.max_requests == 3000 and a.no_ssr is False


def test_parse_all_options():
    a = cli.build_parser().parse_args(["idea.md", "--who", "経営者", "--ask", "買う？", "--count", "30",
                                       "--output", "o", "--format", "json", "--open"])
    assert (a.who, a.ask, a.count, a.output, a.format, a.open) == ("経営者", "買う？", 30, "o", "json", True)


def test_file_input_text_output_and_four_files(tmp_path, idea, capsys):
    out_dir = tmp_path / "res"
    code, out, err = run([str(idea), "--who", "中小企業の経営者", "--output", str(out_dir), *BASE], capsys)
    assert code == 0
    assert {p.name for p in out_dir.iterdir()} == {"survey.html", "report.md", "responses.csv", "data.json"}
    assert "Summary:" in out and "Results:" in out and str(out_dir / "data.json") in out
    assert "✓ Questionnaire created" in err and "回答取得" in err  # 進捗は stderr
    assert "外部の LLM" in err and "確定料金ではありません" in err  # 送信の明示・見積もり


def test_default_output_dir_is_auto_generated(tmp_path, idea, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out, _ = run([str(idea), *BASE], capsys)
    dirs = list((tmp_path / "askusers-results").iterdir())
    assert code == 0 and len(dirs) == 1 and dirs[0].name.startswith("run-")


def test_minimal_input_infers_audience_and_objective(tmp_path, idea, capsys):
    code, _, _ = run([str(idea), "--output", str(tmp_path / "o"), *BASE], capsys)
    d = json.loads((tmp_path / "o" / "data.json").read_text())
    assert code == 0 and d["research"]["audienceInferred"] and d["research"]["objectiveInferred"]
    assert d["research"]["audience"] and d["research"]["objective"] == "総合的な評価と改善点"
    assert "AIによる推定" in (tmp_path / "o" / "report.md").read_text()


def test_format_json_stdout_is_pure_json(tmp_path, idea, capsys):
    code, out, err = run([str(idea), "--format", "json", "--output", str(tmp_path / "o"), *BASE], capsys)
    obj = json.loads(out)  # 標準出力は JSON のみ
    assert code == 0 and obj["status"] == "completed" and obj["synthetic"] is True
    assert set(obj["results"]) == {"survey.html", "report.md", "responses.csv", "data.json"}
    assert obj["scores"] and "Summary" not in out and "✓" not in out
    assert "✓" in err  # 進捗は標準エラー


def test_format_flag_does_not_change_saved_files(tmp_path, idea, capsys):
    run([str(idea), "--format", "json", "--output", str(tmp_path / "j"), *BASE], capsys)
    run([str(idea), "--format", "text", "--output", str(tmp_path / "t"), *BASE], capsys)
    assert {p.name for p in (tmp_path / "j").iterdir()} == {p.name for p in (tmp_path / "t").iterdir()}


def test_url_input(tmp_path, capsys, monkeypatch):
    def fake(src, tid="target-001", **kw):
        from askusers.models import Target
        return Target(id=tid, type="url", source=src, content="ページ本文", contentHash=target.sha256("ページ本文"))
    monkeypatch.setattr(target, "load_target", fake)
    code, _, _ = run(["https://example.com/", "--output", str(tmp_path / "o"), *BASE], capsys)
    d = json.loads((tmp_path / "o" / "data.json").read_text())
    assert code == 0 and d["targets"][0]["type"] == "url" and d["targets"][0]["content"] == "ページ本文"


def test_exit_codes(tmp_path, idea, capsys):
    assert run([], capsys)[0] == 2
    assert run([str(idea), str(idea)], capsys)[0] == 2
    assert run(["compare", str(idea)], capsys)[0] == 2
    assert run([str(idea), "--count", "0", "--mock"], capsys)[0] == 2
    assert run(["--bogus"], capsys)[0] == 2
    code, _, err = run([str(tmp_path / "nope.md"), *BASE], capsys)
    assert code == 3 and "評価対象を取得できません" in err  # 推測して続行しない
    assert run(["render"], capsys)[0] == 2


def test_missing_embedding_key_stops_before_any_call(tmp_path, idea, capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "select_llm_backend", lambda *a, **k: pytest.fail("LLM は呼ばれない"))
    # LLM 選択より前にキー判定する必要はないので、選択だけ通す
    from askusers.mock import MockRawLLM
    monkeypatch.setattr(cli, "select_llm_backend", lambda *a, **k: MockRawLLM())
    code, _, err = run([str(idea), "--count", "2", "--output", str(tmp_path / "o")], capsys)
    assert code == 5 and "OPENAI_API_KEY" in err and "--no-ssr" in err
    assert not (tmp_path / "o").exists()


def test_no_llm_available_is_config_error(idea, capsys, monkeypatch):
    from askusers.llm import BackendUnavailable

    def boom(*a, **k):
        raise BackendUnavailable("使えません")
    monkeypatch.setattr(cli, "select_llm_backend", boom)
    code, _, err = run([str(idea), "--count", "2"], capsys)
    assert code == 5 and "使えません" in err


def test_no_ssr_flag_runs_without_embedding_key(tmp_path, idea, capsys, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code, out, _ = run([str(idea), "--no-ssr", "--format", "json", "--output", str(tmp_path / "o"), *BASE], capsys)
    assert code == 0 and json.loads(out)["scores"] == []


def test_max_requests_guard(tmp_path, idea, capsys):
    code, _, err = run([str(idea), "--count", "50", "--max-requests", "10", "--mock", "--output", str(tmp_path / "o")], capsys)
    assert code == 5 and "--max-requests" in err


def test_incomplete_run_exit_code_and_warning(tmp_path, idea, capsys, monkeypatch):
    from conftest import RecordingLLM

    from askusers import llm as L

    class Failing(RecordingLLM):
        async def call(self, **kw):
            from askusers.mock import _task
            if _task(kw["system"]) == "answer" and "user-001" in kw["user"]:
                raise L.LLMError("transient", "down")
            return await super().call(**kw)
    monkeypatch.setattr(cli, "MockRawLLM", Failing)
    code, out, _ = run([str(idea), "--output", str(tmp_path / "o"), *BASE], capsys)
    d = json.loads((tmp_path / "o" / "data.json").read_text())
    assert code == 4 and d["status"] == "partial" and "不完全" in out
    assert "不完全" in (tmp_path / "o" / "report.md").read_text() and "不完全" in (tmp_path / "o" / "survey.html").read_text()


def test_render_command_regenerates_without_llm(tmp_path, idea, capsys):
    o = tmp_path / "o"
    run([str(idea), "--output", str(o), *BASE], capsys)
    for f in ("survey.html", "report.md", "responses.csv"):
        (o / f).unlink()
    code, out, _ = run(["render", str(o / "data.json")], capsys)
    assert code == 0 and all((o / f).exists() for f in ("survey.html", "report.md", "responses.csv"))
    code, _, err = run(["render", str(tmp_path / "missing.json")], capsys)
    assert code == 1


def test_compare_cli(tmp_path, two_docs, capsys):
    a, b = tmp_path / "before-v1.md", tmp_path / "after-v2.md"
    out = tmp_path / "c"
    code, _, _ = run(["compare", str(a), str(b), "--output", str(out), *BASE], capsys)
    d = json.loads((out / "data.json").read_text())
    assert code == 0 and d["mode"] == "compare" and [t["role"] for t in d["targets"]] == ["before", "after"]
    assert {p.name for p in out.iterdir()} == {"survey.html", "report.md", "responses.csv", "data.json"}
