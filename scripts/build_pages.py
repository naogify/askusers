"""GitHub Pages 用の静的サイトを _site/ に生成する（標準ライブラリのみ）。

examples/ 配下のサンプル出力（survey.html / report.md / responses.csv / data.json）を
そのまま配置し、report.md は読みやすい HTML（report.html）にも変換する。
"""

from __future__ import annotations

import html
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_site"

SAMPLES = [
    {
        "dir": "real-survey",
        "title": "アイデアの調査（survey）",
        "badge": "実 LLM で生成",
        "main": True,
        "desc": "架空の家計簿アプリ「ふたり家計」の企画を、共働き世帯の夫婦（合成ユーザー15人）に聞いた調査。"
        "月額480円で使いたいか・どこが不安か、をテーマ別に集計し、改善提案まで出ます。",
        "cmd": 'askusers kakeibo-idea.md --who "小学生以下の子どもがいる共働き世帯（30〜40代）の夫婦のどちらか" '
        '--ask "…月額480円で利用したいと思うか？どこが不安か？" --count 15 --no-ssr',
    },
    {
        "dir": "real-compare",
        "title": "LP 文案の比較（compare）",
        "badge": "実 LLM で生成",
        "main": True,
        "desc": "同じアプリの LP 文案の改善前/改善後を、同じ合成ユーザー（12人）に読ませて比較。"
        "どの不安・評価がどう変わったかを出します。",
        "cmd": 'askusers compare lp-before.md lp-after.md --who "…共働き世帯の夫婦…" '
        '--ask "…内容が分かりやすく、試してみたいと思うか？" --count 12 --no-ssr',
    },
    {
        "dir": "mock-survey",
        "title": "（参考）モックの survey",
        "badge": "偽のモック LLM",
        "main": False,
        "desc": "オフラインのデモ（--mock）。中身に意味は無く、出力ファイル形式の確認用です。",
        "cmd": "askusers target.md --mock --count 10",
    },
    {
        "dir": "mock-compare",
        "title": "（参考）モックの compare",
        "badge": "偽のモック LLM",
        "main": False,
        "desc": "オフラインのデモ。SSR（数値化）を含む比較レポートの見た目の確認用です。",
        "cmd": "askusers compare before.md after.md --mock --count 10",
    },
]

CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--acc:#1a5fb4;--soft:#f5f7fa}
@media (prefers-color-scheme:dark){:root{--bg:#15171a;--fg:#e8e8e8;--mut:#9aa0a6;--line:#33373d;--acc:#7ab0ff;--soft:#1e2126}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.75 system-ui,"Hiragino Sans","Noto Sans JP",sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}
a{color:var(--acc)} h1{font-size:1.6rem;line-height:1.4} h2{margin-top:2.2em;border-bottom:1px solid var(--line);padding-bottom:.2em}
table{border-collapse:collapse;display:block;overflow-x:auto;margin:1em 0} th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
th{background:var(--soft)} code{background:var(--soft);padding:1px 5px;border-radius:4px;font-size:.9em}
.card{border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:16px 0;background:var(--soft)}
.card h3{margin:.1em 0 .3em}.badge{display:inline-block;font-size:.75rem;border:1px solid var(--line);border-radius:99px;padding:0 9px;color:var(--mut);margin-left:6px;vertical-align:middle}
.links a{margin-right:14px}.warn{border-left:4px solid #c64600;padding:8px 14px;background:var(--soft)}
pre.cmd{overflow-x:auto;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px 10px;font-size:.8rem}
.mut{color:var(--mut);font-size:.9rem}
"""


def inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    return s.replace("  ", "&nbsp; ") if s.endswith("  ") else s


def md_to_html(md: str) -> str:
    """report.md が使う範囲（見出し・表・箇条書き・引用・段落）だけを変換する簡易変換。"""
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    list_open = False

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            out.append("</ul>")
            list_open = False

    while i < len(lines):
        line = lines[i]
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            close_list()
            head = [c.strip() for c in line.strip("|").split("|")]
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>")
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</tbody></table>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            close_list()
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
        elif re.match(r"^\s*[-*]\s+", line):
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append("<li>" + inline(re.sub(r"^\s*[-*]\s+", "", line)) + "</li>")
        elif line.startswith(">"):
            close_list()
            out.append("<blockquote>" + inline(line.lstrip("> ")) + "</blockquote>")
        elif line.strip() == "":
            close_list()
        else:
            close_list()
            out.append("<p>" + inline(line) + "</p>")
        i += 1
    close_list()
    return "\n".join(out)


def page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>"
    )


def build() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir()
    cards: list[str] = []
    for s in SAMPLES:
        src = ROOT / "examples" / s["dir"]
        if not src.is_dir():
            print(f"skip (not found): {src}", file=sys.stderr)
            continue
        dst = OUT / s["dir"]
        dst.mkdir()
        for f in ("survey.html", "report.md", "responses.csv", "data.json"):
            shutil.copy2(src / f, dst / f)
        md = (src / "report.md").read_text(encoding="utf-8")
        back = '<p class="mut"><a href="../">← サンプル一覧へ</a> ・ <a href="survey.html">集計画面（survey.html）</a></p>'
        (dst / "report.html").write_text(page(s["title"] + " レポート", back + md_to_html(md)), encoding="utf-8")
        card = (
            f'<section class="card"><h3>{html.escape(s["title"])}<span class="badge">{html.escape(s["badge"])}</span></h3>'
            f'<p>{html.escape(s["desc"])}</p>'
            f'<p class="links"><a href="{s["dir"]}/survey.html">集計画面 survey.html</a>'
            f'<a href="{s["dir"]}/report.html">レポート report</a>'
            f'<a href="{s["dir"]}/report.md">report.md</a>'
            f'<a href="{s["dir"]}/responses.csv">responses.csv</a>'
            f'<a href="{s["dir"]}/data.json">data.json</a></p>'
            f'<pre class="cmd">{html.escape(s["cmd"])}</pre></section>'
        )
        cards.append((s["main"], card))
    main_cards = "".join(c for is_main, c in cards if is_main)
    ref_cards = "".join(c for is_main, c in cards if not is_main)
    body = (
        "<h1>askusers サンプルレポート</h1>"
        "<p>askusers は、合成ユーザー（AI が演じる架空の回答者）にアンケート調査を行い、"
        "アイデアや LP などの改善仮説を素早く得る CLI です。1 回の実行で次の 4 ファイルが出ます。</p>"
        "<ul><li><code>survey.html</code> 集計画面（単一ファイル）</li><li><code>report.md</code> 結論・発見事項・改善提案</li>"
        "<li><code>responses.csv</code> 1 行 = 1 ユーザー</li><li><code>data.json</code> 全データの正本</li></ul>"
        '<p class="warn">これらは<strong>架空の題材</strong>を<strong>合成ユーザー</strong>に聞いたサンプルです。'
        "実在の人物・顧客の声ではなく、統計的な代表性も実際の市場需要を示すものでもありません。"
        "SSR（数値化）には OpenAI の Embedding キーが必要なため、実 LLM のサンプルは <code>--no-ssr</code> で生成しています。</p>"
        f"<h2>実 LLM で生成したサンプル</h2>{main_cards}"
        f"<h2>参考: オフラインのモック</h2>{ref_cards}"
        '<p class="mut"><a href="https://github.com/naogify/askusers">naogify/askusers</a></p>'
    )
    (OUT / "index.html").write_text(page("askusers サンプルレポート", body), encoding="utf-8")
    (OUT / ".nojekyll").write_text("")
    print(f"built: {OUT}")


if __name__ == "__main__":
    build()
