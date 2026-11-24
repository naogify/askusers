"""data.json の保存・読み込み・検証（Data Store）。"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path

import jsonschema

from .models import SurveyData, json_schema

SCHEMA_FILE = "data.schema.json"

# 秘密情報らしき文字列が data.json に紛れ込んでいないかの簡易検査（テストと保存時に使う）
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)x-api-key"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]{16,}"),
]


class DataError(ValueError):
    pass


@lru_cache(maxsize=1)
def packaged_schema() -> dict:
    return json.loads(resources.files("askusers").joinpath(SCHEMA_FILE).read_text("utf-8"))


def schema_text() -> str:
    return json.dumps(json_schema(), ensure_ascii=False, indent=2) + "\n"


def validate_json_schema(obj: dict) -> None:
    validator = jsonschema.Draft202012Validator(packaged_schema())
    errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        path = "/".join(str(p) for p in e.path)
        raise DataError(f"data.json が JSON Schema に違反しています: {path}: {e.message}")


def find_secrets(text: str) -> list[str]:
    return [p.pattern for p in SECRET_PATTERNS if p.search(text)]


def dumps(data: SurveyData) -> str:
    obj = data.model_dump(mode="json")
    validate_json_schema(obj)
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    return text


def save(data: SurveyData, path: Path) -> None:
    """検証してから原子的に書き込む（取得済みの回答を壊さない）。"""
    from .integrity import check_integrity

    problems = check_integrity(data)
    if problems:
        raise DataError("参照整合性エラー: " + "; ".join(problems[:5]))
    text = dumps(data)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_checkpoint(data: SurveyData, path: Path) -> None:
    """途中経過の保存。整合性が未確定な段階があるのでスキーマ検証のみ行う。"""
    text = dumps(data)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load(path: Path) -> SurveyData:
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise DataError(f"data.json を読めません: {e}") from e
    validate_json_schema(obj)
    return SurveyData.model_validate(obj)
