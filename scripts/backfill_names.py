"""名前なしの data.json（古い examples など）に、架空の氏名を後付けして再描画する。

LLM は使わない。性別・年代の属性があればそれに合う名前を、無ければ中立な名前を、決定的に付ける。
使い方: uv run python scripts/backfill_names.py examples/real-survey [examples/real-compare ...]
既に displayName のあるユーザーは変更しない。data.json 以外（survey.html / report.md /
responses.csv）は outputs.render_all で作り直す。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from askusers import outputs, store
from askusers.models import SurveyData
from askusers.names import find_name_problems

SURNAMES = ["青木", "安藤", "池田", "石井", "上田", "内田", "遠藤", "大野", "岡田", "小川", "加藤", "川口",
            "菊地", "木村", "久保", "黒田", "小林", "近藤", "斎藤", "坂本", "島田", "杉山", "関口", "高木",
            "武田", "千葉", "土屋", "辻本", "中島", "西村", "野口", "長谷川", "原田", "平井", "福田", "藤田",
            "堀", "松井", "三浦", "宮崎", "村上", "森田", "矢野", "山口", "横山", "吉田", "和田", "渡辺",
            "佐藤", "鈴木", "高橋", "田中"]
FEMALE = {
    "young": ["美咲", "さくら", "陽菜", "結衣", "彩", "莉子", "葵", "里奈"],
    "mid": ["由美", "麻衣", "彩香", "真由", "千尋", "菜々子", "友香", "恵美"],
    "senior": ["恵子", "洋子", "直美", "裕子", "明美", "久美子", "幸子", "智子"],
}
MALE = {
    "young": ["翔", "拓海", "蓮", "悠真", "駿", "大和", "優斗", "湊"],
    "mid": ["健太", "大輔", "亮", "誠", "慎吾", "和也", "直樹", "貴史"],
    "senior": ["隆", "浩二", "剛", "徹", "孝", "正彦", "茂", "豊"],
}


def _generation(attrs: dict) -> str:
    age = attrs.get("age")
    if isinstance(age, int | float):
        n = int(age)
    else:
        group = str(attrs.get("ageGroup", ""))
        m = re.search(r"(\d+)代", group)
        n = int(m.group(1)) + (7 if "後半" in group else 3) if m else 40
    return "young" if n < 30 else "mid" if n < 50 else "senior"


def backfill(d: SurveyData) -> int:
    used = {u.displayName for u in d.users if u.displayName}
    n = 0
    for i, u in enumerate(d.users):
        if u.displayName:
            continue
        gender = str(u.attributes.get("gender", ""))
        pool_key = _generation(u.attributes)
        if gender in ("女性", "female"):
            pools = [FEMALE[pool_key]]
        elif gender in ("男性", "male"):
            pools = [MALE[pool_key]]
        else:
            pools = [FEMALE[pool_key], MALE[pool_key]]
        for k in range(len(SURNAMES) * 8):
            given_pool = pools[(i + k) % len(pools)]
            name = f"{SURNAMES[(i * 7 + k) % len(SURNAMES)]} {given_pool[(i * 3 + k) % len(given_pool)]}"
            if name not in used:
                break
        used.add(name)
        u.displayName = name
        n += 1
    return n


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        folder = Path(arg)
        d = store.load(folder / "data.json")
        added = backfill(d)
        assert not find_name_problems(d.users), f"{folder}: 氏名に重複/偏りがあります"
        store.save(d, folder / "data.json")
        outputs.render_all(d, folder)
        print(f"{folder}: 氏名を {added} 人に付与し、出力を再生成しました")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
