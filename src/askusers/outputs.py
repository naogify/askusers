"""4 ファイルの書き出し。survey.html / report.md / responses.csv はすべて data.json の内容だけから生成する。"""

from __future__ import annotations

from pathlib import Path

from . import store
from .export_csv import render_csv
from .models import SurveyData
from .render_html import render_html
from .render_md import render_markdown

FILES = ("survey.html", "report.md", "responses.csv", "data.json")


def render_all(d: SurveyData, out: Path) -> None:
    """data.json 以外の 3 ファイルを（再）生成する。LLM は使わない。"""
    out.mkdir(parents=True, exist_ok=True)
    (out / "survey.html").write_text(render_html(d), encoding="utf-8")
    (out / "report.md").write_text(render_markdown(d), encoding="utf-8")
    (out / "responses.csv").write_bytes(render_csv(d))


def write_all(d: SurveyData, out: Path) -> None:
    store.save(d, out / "data.json")
    render_all(d, out)
