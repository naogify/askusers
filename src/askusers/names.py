"""合成ユーザーの架空の氏名: 検査（欠落・重複・名字の偏り）と、表示用の短い属性ラベル。

氏名の生成は LLM（personas.py）が行う。ここは「生成された氏名が使えるか」を機械的に検査する。
氏名は表示のための飾りであり、参照は常に `User.id` で行う。
"""

from __future__ import annotations

import math
import re

from .models import AttributeDef, User

# 名字が同じ人の最大数: 調査人数の 15%（最低 2 人）。よくある名字ばかりになるのを防ぐ。
SURNAME_SHARE = 0.15

FICTIONAL_NOTICE = "氏名は架空です（合成ユーザー）。実在の人物ではありません。"


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", name).casefold()


def surname_key(name: str) -> str:
    """名字の判定キー。空白区切りで CJK を含めば先頭（姓 名）、それ以外は末尾（Given Family）。"""
    parts = name.split()
    if len(parts) < 2:
        return _norm(name)
    if any(ord(ch) >= 0x2E80 for ch in name):
        return _norm(parts[0])
    return _norm(parts[-1])


def clean_name(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    s = re.sub(r"\s+", " ", raw.replace("　", " ")).strip()
    return s or None


def find_name_problems(users: list[User], check_surname: bool = True) -> list[str]:
    """付け直しが必要なユーザーID（後の方）。欠落・氏名の重複・名字の偏りを検出する。"""
    redo: list[str] = []
    seen: set[str] = set()
    surnames: dict[str, int] = {}
    cap = max(2, math.ceil(len(users) * SURNAME_SHARE))
    for u in users:
        if not u.displayName:
            redo.append(u.id)
            continue
        full, sn = _norm(u.displayName), surname_key(u.displayName)
        if full in seen or (check_surname and surnames.get(sn, 0) >= cap):
            redo.append(u.id)
            continue
        seen.add(full)
        surnames[sn] = surnames.get(sn, 0) + 1
    return redo


def _is_attr(a: AttributeDef, keys: tuple[str, ...], labels: tuple[str, ...]) -> bool:
    return a.key.lower() in keys or any(x in a.label for x in labels)


def short_profile(u: User, schema: list[AttributeDef]) -> str:
    """「34歳・会社員」のような短い属性。年齢 → 職業 → 最初のカテゴリ属性の順に最大 2 つ。"""
    parts: list[str] = []
    used: set[str] = set()
    for is_age, keys, labels in ((True, ("age", "agegroup"), ("年齢", "年代")),
                                 (False, ("occupation", "job", "profession"),
                                  ("職業", "職種", "仕事"))):
        for a in schema:
            v = u.attributes.get(a.key)
            if a.key in used or v in (None, "") or not _is_attr(a, keys, labels):
                continue
            numeric = isinstance(v, int | float) and not isinstance(v, bool)
            parts.append(f"{v}歳" if is_age and numeric else str(v))
            used.add(a.key)
            break
    if len(parts) < 2:
        for a in schema:
            v = u.attributes.get(a.key)
            if a.key not in used and a.type == "category" and v not in (None, ""):
                parts.append(str(v))
                break
    return "・".join(parts[:2])
