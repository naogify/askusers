"""オフライン用のモック LLM / Embedding（決定的）。テストと `--mock` のデモ実行で使う。

実際の LLM ではない。出力内容に意味は無く、パイプラインと出力ファイルの確認だけが目的。
"""

from __future__ import annotations

import hashlib
import re

import numpy as np

from .llm import RawResult

_THEMES = [
    ("価格・費用への懸念", "価格", "費用対効果が見えず、価格が高いと感じる"),
    ("精度・信頼性への不安", "精度", "情報の精度や信頼性が分からず不安だ"),
    ("導入・学習コスト", "手間", "導入や学習の手間がかかりそうだ"),
    ("機能不足", "機能", "必要な機能が足りないと感じる"),
    ("使いやすさの評価", "使いやすい", "画面や説明が使いやすいと思う"),
    ("そもそも必要性を感じない", "必要性", "今のやり方で困っておらず必要性を感じない"),
]
_ANCHORS = [
    ["利用するつもりはまったくない", "利用する可能性は低い", "条件次第では利用を検討する",
     "試験的に利用してみたい", "積極的に利用したい"],
    ["絶対に使わない", "たぶん使わない", "使うかどうか分からない", "たぶん使うと思う", "ぜひ使いたい"],
    ["まったく興味がない", "あまり興味がない", "どちらとも言えない", "やや興味がある", "とても興味がある"],
]
_SSR_ANSWERS = [
    ["今のところ利用するつもりはまったくありません。", "必要性を感じないので利用しないと思います。"],
    ["利用する可能性は低いです。価格や手間が気になります。", "あまり使わないと思います。"],
    ["条件次第では利用を検討します。精度が分かれば考えたいです。", "どちらとも言えません。"],
    ["試験的に利用してみたいです。使いやすそうだと思います。", "まず試してみたいと思います。"],
    ["ぜひ積極的に利用したいです。", "とても役に立つので積極的に使いたいです。"],
]


def _h(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


def _task(system: str) -> str:
    m = re.match(r"\[task:(\w+)\]", system)
    return m.group(1) if m else ""


class MockRawLLM:
    name = "mock"
    model = "mock-model"

    async def call(self, *, system: str, user: str, schema: dict, max_tokens: int,
                   temperature: float | None) -> RawResult:
        task = _task(system)
        data = getattr(self, f"_{task}")(user)
        return RawResult(data, len(user) // 3, 200)

    def _design(self, user: str) -> dict:
        aud = re.search(r"^Audience: (.*)$", user, re.M)
        audience = aud.group(1) if aud and not aud.group(1).startswith("(not") else "一般的な利用者"
        ssr_ok = "SSR scoring available: yes" in user
        qs = [
            {"text": "現在、この分野で困っていることはありますか？", "purpose": "現在の課題の把握",
             "phase": "before_exposure", "responseType": "text", "analysisMethod": "qualitative"},
            {"text": "現在、どのような方法で対応していますか？", "purpose": "既存の代替手段の把握",
             "phase": "before_exposure", "responseType": "single_choice", "analysisMethod": "categorical",
             "options": ["専用ツールを使っている", "手作業で対応している", "特に対応していない", "分からない"]},
            {"text": "これを見て、どのような印象を持ちましたか？", "purpose": "第一印象の把握",
             "phase": "after_exposure", "responseType": "text", "analysisMethod": "qualitative"},
        ]
        if ssr_ok:
            qs.append({"text": "これを利用したいと思いますか？", "purpose": "利用意向の測定",
                       "phase": "after_exposure", "responseType": "text", "analysisMethod": "ssr",
                       "scaleDescription": "1=まったく利用したくない / 5=積極的に利用したい",
                       "anchorSets": _ANCHORS})
        else:
            qs.append({"text": "これを利用したいと思いますか？その理由は？", "purpose": "利用意向と理由の把握",
                       "phase": "after_exposure", "responseType": "text", "analysisMethod": "qualitative"})
        qs += [
            {"text": "導入を見送るとしたら、その理由は何ですか？", "purpose": "導入障壁の把握",
             "phase": "after_exposure", "responseType": "text", "analysisMethod": "qualitative"},
            {"text": "導入を検討するために必要なものを選んでください（複数選択可）", "purpose": "改善条件の把握",
             "phase": "after_exposure", "responseType": "multiple_choice", "analysisMethod": "categorical",
             "options": ["価格の明示", "導入事例", "無料トライアル", "精度の説明", "特になし"]},
        ]
        return {"title": "モック調査", "audience": audience, "questions": qs}

    def _persona_schema(self, user: str) -> dict:
        return {"attributeSchema": [
            {"key": "occupation", "label": "職種", "type": "category", "description": ""},
            {"key": "experienceYears", "label": "経験年数", "type": "number", "description": ""},
            {"key": "orgSize", "label": "組織規模", "type": "category", "description": ""},
            {"key": "currentTool", "label": "現在の手段", "type": "category", "description": ""},
        ], "policy": "多様な職種・規模・満足度を含める。迎合的な人物だけにしない。",
            "segments": ["既存手段に満足している人", "困っているが予算がない人", "新しいものに積極的な人",
                         "懐疑的な人", "必要性を感じない人"]}

    def _personas(self, user: str) -> dict:
        slots = [int(x) for x in re.findall(r"- slot (\d+):", user)]
        occ = ["経営者", "営業責任者", "個人事業主", "総務担当", "エンジニア", "店舗オーナー"]
        size = ["1-9人", "10-49人", "50-299人", "300人以上"]
        tool = ["専用ツール", "表計算ソフト", "紙・手作業", "特になし"]
        users = []
        for s in slots:
            users.append({
                "attributes": {"occupation": occ[_h(f"o{s}") % len(occ)], "experienceYears": 1 + _h(f"e{s}") % 25,
                               "orgSize": size[_h(f"s{s}") % len(size)], "currentTool": tool[_h(f"t{s}") % len(tool)]},
                "background": f"モック人物{s}。{occ[_h(f'o{s}') % len(occ)]}として{1 + _h(f'e{s}') % 25}年働く。背景番号{_h(f'b{s}')}。",
                "goals": [f"目標{s}"], "constraints": ["予算に限りがある"],
                "currentSolutions": [tool[_h(f"t{s}") % len(tool)]], "values": ["堅実さ"]})
        return {"users": users}

    def _answer(self, user: str) -> dict:
        uid = re.search(r"Respondent (user-\d+)", user)
        uid = uid.group(1) if uid else "user-000"
        level = _h(uid) % 5
        if "【改善】" in user:  # 比較デモ: 改善版は少し評価が上がる
            level = min(4, level + 1)
        out = []
        for m in re.finditer(r"^\[(q\d+)\] \((text|single_choice|multiple_choice)\) (.*)$", user, re.M):
            qid, rtype, text = m.groups()
            tail = user[m.end():]
            opts = []
            for line in tail.split("\n")[1:]:
                if line.startswith("    * "):
                    opts.append(line[6:])
                else:
                    break
            if rtype == "single_choice":
                ans = opts[_h(uid + qid) % len(opts)]
            elif rtype == "multiple_choice":
                k = 1 + _h(uid + qid) % 2
                ans = [opts[(_h(uid + qid) + i) % len(opts)] for i in range(k)]
            elif "利用したい" in text and "理由" not in text:
                pool = _SSR_ANSWERS[level]
                ans = pool[_h(uid + qid) % len(pool)]
            else:
                t = _THEMES[(_h(uid + qid) + (1 if level >= 3 else 0)) % len(_THEMES)]
                t2 = _THEMES[(_h(uid + qid + "x")) % len(_THEMES)]
                ans = f"{t[2]}。" + (f"また、{t2[2]}。" if _h(uid + qid + "y") % 3 == 0 else "")
            out.append({"questionId": qid, "answer": ans})
        return {"answers": out}

    def _codebook(self, user: str) -> dict:
        return {"themes": [{"label": lab, "description": f"キーワード: {k}。{d}", "valence":
                            "positive" if "使いやすい" in k else "negative"} for lab, k, d in _THEMES]}

    def _classify(self, user: str) -> dict:
        book = {}
        for m in re.finditer(r"^(c\d+): .*?キーワード: (.*?)。", user, re.M):
            book[m.group(1)] = m.group(2)
        out = []
        for m in re.finditer(r"^\[(response-\d+)\] (.*)$", user, re.M):
            out.append({"responseId": m.group(1),
                        "themeIds": [c for c, kw in book.items() if kw in m.group(2)]})
        return {"assignments": out}

    def _report(self, user: str) -> dict:
        themes = []
        for m in re.finditer(r"^(theme-\d+) \[(q\d+)\] \((\w+)\) (.*?): .*? -> (.*)$", user, re.M):
            counts = [int(x) for x in re.findall(r": (\d+)/", m.group(5))]
            themes.append((max(counts or [0]), m.groups()))
        themes.sort(key=lambda x: -x[0])
        ev: dict[str, list[str]] = {}
        cur = None
        for line in user.splitlines():
            mt = re.match(r"^(theme-\d+) ", line)
            if mt:
                cur = mt.group(1)
            me = re.match(r"^    (response-\d+) \(", line)
            if me and cur:
                ev.setdefault(cur, []).append(me.group(1))
        findings = []
        for _cnt, (tid, qid, _val, label, _s) in themes[:3]:
            findings.append({"title": label, "what": f"{label}に関する言及が見られた（モック生成）。",
                             "why": "モックの理由説明。", "themeIds": [tid], "questionIds": [qid],
                             "evidenceResponseIds": ev.get(tid, [])[:2]})
        if not findings:
            findings = [{"title": "データ不足", "what": "集計できるテーマがありません。", "why": "-",
                         "themeIds": [], "questionIds": ["q1"], "evidenceResponseIds": []}]
        recs = [{"priority": ["high", "medium", "low"][i], "title": f"{f['title']}への対応",
                 "what": "対応を検討する。", "why": "言及があったため。", "how": "具体策を検討する。",
                 "findingIndexes": [i]} for i, f in enumerate(findings)]
        return {"conclusion": "モック: 需要はありそうだが、いくつかの障壁が見られる。",
                "summary": "モック: 需要はありそうだが、障壁がある。", "findings": findings,
                "recommendations": recs}


class MockRawEmbedder:
    model = "mock-embedding"
    dim = 256

    async def call(self, texts: list[str]) -> tuple[list[list[float]], int]:
        return [self.vec(t) for t in texts], sum(len(t) for t in texts)

    def vec(self, text: str) -> list[float]:
        v = np.zeros(self.dim)
        t = re.sub(r"\s+", "", text)
        for i in range(len(t) - 1):
            v[_h(t[i : i + 2]) % self.dim] += 1.0
        v = v / max(1.0, np.linalg.norm(v))
        rng = np.random.default_rng(_h(text))  # 無関係な文でも類似度に差が出るよう微小な決定的ノイズを足す
        v = v + 0.1 * rng.normal(size=self.dim) / np.sqrt(self.dim)
        return (v / np.linalg.norm(v)).tolist()
