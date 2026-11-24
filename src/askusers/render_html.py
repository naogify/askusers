"""HTML Renderer: data.json から単一ファイルの集計画面 survey.html を作る。

- 内容はすべてサーバー側で html.escape して静的に出力する（JS は表示切替・絞り込みだけ）。
- CSS / JS / データは HTML に内包。外部ネットワーク不要。JS 無効でも全内容が読める。
"""

from __future__ import annotations

import json
from html import escape as _esc
from statistics import median

from .models import Question, Response, SurveyData, User
from .names import FICTIONAL_NOTICE, short_profile

STATUS_JA = {"completed": "回答済み", "unanswered": "未回答", "failed": "回答取得失敗"}
VALENCE_JA = {"positive": "肯定", "negative": "課題", "neutral": "中立", "request": "要望"}
PHASE_JA = {"before_exposure": "提示前", "after_exposure": "提示後"}
METHOD_JA = {"ssr": "SSR", "categorical": "選択肢集計", "qualitative": "定性分析"}


def e(x) -> str:
    return _esc(str(x), quote=True)


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def json_for_script(obj) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return (s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def bar_row(label: str, value_txt: str, frac: float, cls: str = "") -> str:
    w = max(0.0, min(1.0, frac)) * 100
    return (f'<div class="bar-row"><span class="bar-label">{e(label)}</span>'
            f'<span class="bar-track"><span class="bar-fill {cls}" style="width:{w:.1f}%"></span></span>'
            f'<span class="bar-val">{e(value_txt)}</span></div>')


def ssr_dist(dist: list[float], cls: str = "") -> str:
    return "".join(bar_row(f"{i}", pct(p), p, cls) for i, p in enumerate(dist, 1))


class Ctx:
    def __init__(self, d: SurveyData):
        self.d = d
        self.q = {q.id: q for q in d.survey.questions}
        self.u = {u.id: u for u in d.users}
        self.r: dict[tuple[str, str | None, str], Response] = {
            (r.userId, r.targetId, r.questionId): r for r in d.responses}
        self.themes = {t.id: t for t in d.analysis.themes}
        self.by_q: dict[str, list[Response]] = {}
        for r in d.responses:
            self.by_q.setdefault(r.questionId, []).append(r)
        self.compare = d.mode == "compare"
        self.tlabel = {t.id: ({"before": "Before", "after": "After"}.get(t.role or "", t.id))
                       for t in d.targets}


def tlabel(c: Ctx, tid: str | None) -> str:
    return "" if tid is None else c.tlabel[tid]


def answer_html(r: Response | None) -> str:
    if r is None:
        return '<span class="muted">（記録なし）</span>'
    if r.status == "failed":
        return f'<span class="status failed">回答取得失敗</span> <span class="muted">{e(r.error or "")}</span>'
    if r.status == "unanswered":
        return '<span class="status unanswered">未回答</span>'
    if isinstance(r.answer, list):
        return "<ul>" + "".join(f"<li>{e(a)}</li>" for a in r.answer) + "</ul>"
    return f'<p class="answer">{e(r.answer)}</p>'


def themes_chips(c: Ctx, r: Response) -> str:
    if not r.themeIds:
        return ""
    return " ".join(f'<span class="chip">{e(c.themes[t].label)}</span>' for t in r.themeIds)


def uname(u: User) -> str:
    """表示名。氏名が無い（古い data.json）ときは ID。"""
    return u.displayName or u.id


def ulabel(u: User) -> str:
    return f"{u.displayName}（{u.id}）" if u.displayName else u.id


def has_names(d: SurveyData) -> bool:
    return any(u.displayName for u in d.users)


def notice(d: SurveyData) -> str:
    return f'<p class="note">{e(FICTIONAL_NOTICE)}</p>' if has_names(d) else ""


def answer_item(c: Ctx, r: Response, show_target: bool) -> str:
    u = c.u[r.userId]
    score = f' <span class="score">SSR {r.ssr.expectedScore:.2f}</span>' if r.ssr else ""
    tgt = f' <span class="tag">{e(tlabel(c, r.targetId))}</span>' if show_target and r.targetId else ""
    idtag = f' <span class="muted">{e(u.id)}</span>' if u.displayName else ""
    return (f'<li class="ans" data-themes="{e(" ".join(r.themeIds))}" data-status="{e(r.status)}">'
            f'<a class="uid" href="#resp-{e(u.id)}" data-goto="{e(u.id)}">{e(uname(u))}</a>{tgt}'
            f'{score}{idtag}'
            f' <span class="muted">{e(r.id)}</span>{answer_html(r)}{themes_chips(c, r)}</li>')


def question_card(c: Ctx, q: Question) -> str:
    d = c.d
    summ = next(s for s in d.analysis.questions if s.questionId == q.id)
    head = (f'<header><h3><span class="qid">{e(q.id.upper())}</span> {e(q.text)}</h3>'
            f'<p class="muted">目的: {e(q.purpose)} ／ {e(PHASE_JA[q.phase])} ／ '
            f'{e(METHOD_JA[q.analysisMethod])}（{e(q.responseType)}）</p></header>')
    body = []
    cols = []
    for p in summ.perTarget:
        lab = tlabel(c, p.targetId) if c.compare and p.targetId else ""
        inner = [f'<p class="counts">有効回答 <b>{p.validN}</b>'
                 f'{f" ／ 回答取得失敗 <b>{p.failedN}</b>" if p.failedN else ""}'
                 f'{f" ／ 未回答 <b>{p.unansweredN}</b>" if p.unansweredN else ""}</p>']
        if q.analysisMethod == "ssr":
            if p.ssr and p.ssr.meanScore is not None and p.ssr.distribution:
                inner.append(f'<p class="big">平均スコア <b>{p.ssr.meanScore:.2f}</b> / 5 '
                             f'<span class="muted">（SSR 計算済み {p.ssr.validN} 件）</span></p>')
                inner.append(f'<div class="bars">{ssr_dist(p.ssr.distribution, "after" if lab == "After" else "")}</div>')
                inner.append('<p class="note">SSR による推定分布です。実際に回答者が選んだ割合・購入率ではありません。</p>')
            else:
                inner.append('<p class="muted">SSR を計算できた回答がありません。</p>')
            for bd in p.breakdowns:
                lbl = next((a.label for a in d.personaDesign.attributeSchema if a.key == bd.attribute), bd.attribute)
                inner.append(f'<details><summary>属性別の平均スコア: {e(lbl)}</summary><table><tr><th>{e(lbl)}</th>'
                             '<th>人数</th><th>平均</th></tr>' + "".join(
                                 f"<tr><td>{e(g.value)}</td><td>{g.n}</td><td>{g.meanScore:.2f}</td></tr>"
                                 for g in bd.groups) + "</table></details>")
        elif q.analysisMethod == "categorical" and p.choices is not None:
            inner.append('<div class="bars">' + "".join(
                bar_row(ch.option, f"{ch.count}人 ({pct(ch.rate)})", ch.rate, "after" if lab == "After" else "")
                for ch in p.choices) + "</div>")
        cols.append(f'<div class="col">{f"<h4>{e(lab)}</h4>" if lab else ""}{"".join(inner)}</div>')
    body.append(f'<div class="cols {"two" if len(cols) > 1 else ""}">{"".join(cols)}</div>')

    qthemes = [t for t in d.analysis.themes if t.questionId == q.id]
    if qthemes:
        rows = []
        for t in qthemes:
            cells = "".join(
                f'<td>{s.userCount}人 / {s.denominator}人 ({pct(s.rate)})</td>' for s in t.stats)
            rows.append(f'<tr><td><button type="button" class="theme-btn" data-q="{e(q.id)}" '
                        f'data-theme="{e(t.id)}">{e(t.label)}</button> <span class="tag">{e(VALENCE_JA[t.valence])}</span>'
                        f'<br><span class="muted">{e(t.description)}</span></td>{cells}</tr>')
        th = "".join(f"<th>{e(tlabel(c, s.targetId) or '言及')}</th>" for s in qthemes[0].stats)
        body.append('<h4>テーマ別集計</h4><p class="note">言及人数 / 有効回答ユーザー数（分母）。'
                    '1 人が複数テーマに該当しうるため合計は 100% になりません。テーマ名を押すと該当回答に絞り込みます。</p>'
                    f'<table class="themes"><tr><th>テーマ</th>{th}</tr>{"".join(rows)}</table>'
                    f'<p><button type="button" class="theme-btn" data-q="{e(q.id)}" data-theme="">絞り込みを解除</button></p>')
        for p in summ.perTarget:
            if p.unclassifiedResponseIds:
                body.append(f'<p class="muted">どのテーマにも分類されなかった回答: {len(p.unclassifiedResponseIds)} 件'
                            f'{(" (" + e(tlabel(c, p.targetId)) + ")") if c.compare else ""}'
                            f'（<button type="button" class="theme-btn" data-q="{e(q.id)}" data-theme="__none__">表示</button>）</p>')
    allr = [r for r in c.by_q.get(q.id, [])]
    items = "".join(answer_item(c, r, c.compare) for r in allr)
    body.append(f'<details class="all-answers" open><summary>すべての回答（{len(allr)}件）</summary>'
                f'<ul class="ans-list" data-q="{e(q.id)}">{items}</ul></details>')
    return f'<section class="card" id="q-{e(q.id)}">{head}{"".join(body)}</section>'


def summary_tab(c: Ctx) -> str:
    d = c.d
    tinfo = "".join(
        f'<li>{e(tlabel(c, t.id) + "：" if c.compare else "")}{e(t.source)} '
        f'<span class="muted">({e(t.type)}, sha256 {e(t.contentHash[:12])}…'
        f'{"、先頭" + str(len(t.content)) + "文字に切り詰め" if t.truncated else ""})</span></li>' for t in d.targets)
    warn = ""
    if d.status != "completed":
        warn = (f'<p class="warn">この調査は不完全です（status: {e(d.status)}）。失敗した回答が含まれます。'
                '失敗は各回答に「回答取得失敗」と表示され、集計から除外されています。</p>')
    nfail = sum(r.status == "failed" for r in d.responses)
    info = (f'<section class="card"><h3>調査概要</h3>{warn}<dl>'
            f'<dt>調査対象</dt><dd><ul>{tinfo}</ul></dd>'
            f'<dt>調査目的</dt><dd>{e(d.research.objective)}'
            f'{" <span class=tag>既定値</span>" if d.research.objectiveInferred else ""}</dd>'
            f'<dt>対象ユーザー</dt><dd>{e(d.research.audience)}'
            f'{" <span class=tag>AIによる推定</span>" if d.research.audienceInferred else ""}</dd>'
            f'<dt>回答者数</dt><dd>{d.research.completedUserCount} / {d.research.requestedUserCount} 人（合成ユーザー）'
            f'{f"、回答取得失敗 {nfail} 件" if nfail else ""}'
            f'{" ／ " + e(FICTIONAL_NOTICE) if has_names(d) else ""}</dd>'
            f'<dt>質問数</dt><dd>{len(d.survey.questions)} 問</dd>'
            f'<dt>生成</dt><dd class="muted">{e(d.execution.models.get("generation", ""))} '
            f'({e(d.execution.models.get("generationBackend", ""))})'
            f'{", Embedding " + e(d.execution.models["embedding"]) if "embedding" in d.execution.models else ""}</dd>'
            f'</dl></section>')
    cards = "".join(question_card(c, q) for q in d.survey.questions)
    return info + cards


def user_attr_distributions(c: Ctx) -> str:
    d = c.d
    out = []
    for a in d.personaDesign.attributeSchema:
        vals = [u.attributes.get(a.key) for u in d.users if u.attributes.get(a.key) is not None]
        if not vals:
            continue
        if a.type == "number" and all(isinstance(v, int | float) for v in vals):
            out.append(f'<div class="card small"><h4>{e(a.label)}</h4><p>最小 {min(vals)} ／ 中央値 {median(vals)} ／ '
                       f'最大 {max(vals)}（{len(vals)}人）</p></div>')
        elif a.type == "category":
            counts: dict[str, int] = {}
            for v in vals:
                counts[str(v)] = counts.get(str(v), 0) + 1
            n = len(vals)
            out.append(f'<div class="card small"><h4>{e(a.label)}</h4><div class="bars">' + "".join(
                bar_row(k, f"{v}人 ({pct(v / n)})", v / n) for k, v in
                sorted(counts.items(), key=lambda x: -x[1])) + "</div></div>")
    return '<div class="grid">' + "".join(out) + "</div>"


def user_row(c: Ctx, u: User) -> str:
    d = c.d
    attrs = "".join(f'<tr><th>{e(a.label)}</th><td>{e(u.attributes.get(a.key, ""))}</td></tr>'
                    for a in d.personaDesign.attributeSchema)
    lists = "".join(f"<p><b>{t}</b>：{e('、'.join(items))}</p>" for t, items in
                    (("目的・課題", u.goals), ("制約", u.constraints), ("現在の手段", u.currentSolutions),
                     ("価値観", u.values)) if items)
    hay = " ".join([u.id, u.displayName or "", u.background, *map(str, u.attributes.values()), *u.goals, *u.constraints,
                    *u.currentSolutions, *u.values]).lower()
    key = " / ".join(str(u.attributes.get(a.key, "")) for a in d.personaDesign.attributeSchema[:3])
    if u.displayName:
        prof = short_profile(u, d.personaDesign.attributeSchema)
        key = f"{u.id}" + (f" ・ {prof}" if prof else "")
    return (f'<article class="card user" data-search="{e(hay)}"><h4><a href="#resp-{e(u.id)}" data-goto="{e(u.id)}">'
            f'{e(uname(u))}</a> <span class="muted">{e(key)}</span></h4>'
            f'<details><summary>属性・背景</summary><table class="attrs">{attrs}</table>'
            f'<p>{e(u.background)}</p>{lists}</details></article>')


def users_tab(c: Ctx) -> str:
    d = c.d
    pol = (f'<section class="card"><h3>調査対象者の設計</h3><p>{e(d.personaDesign.policy)}</p>'
           f'<p class="muted">生成したユーザーは実在する人物ではなく、統計的な代表性は保証されません。'
           f'属性分布は実際の市場の分布ではありません。</p>{notice(d)}</section>')
    return (pol + "<h3>属性の分布</h3>" + user_attr_distributions(c) +
            f'<h3>ユーザー一覧（{len(d.users)}人）</h3>'
            '<p><input type="search" id="user-search" placeholder="氏名・ユーザーID・属性・背景で絞り込み" '
            'aria-label="ユーザーを絞り込み"> <span id="user-count" class="muted"></span></p>'
            f'<div id="user-list">{"".join(user_row(c, u) for u in d.users)}</div>')


def response_section(c: Ctx, u: User) -> str:
    d = c.d
    blocks = []
    for q in d.survey.questions:
        tids = [None] if q.phase == "before_exposure" else [t.id for t in d.targets]
        parts = []
        for tid in tids:
            r = c.r.get((u.id, tid, q.id))
            lab = f'<span class="tag">{e(tlabel(c, tid))}</span> ' if (c.compare and tid) else ""
            extra = ""
            if r and r.ssr:
                extra = (f'<div class="bars mini">{ssr_dist(r.ssr.distribution)}</div>'
                         f'<p class="score">SSR 期待スコア {r.ssr.expectedScore:.2f}（推定分布。選択した値ではありません）</p>')
            parts.append(f'<div class="resp">{lab}{answer_html(r)}{extra}'
                         f'{themes_chips(c, r) if r else ""}</div>')
        blocks.append(f'<div class="qa"><h5><span class="qid">{e(q.id.upper())}</span> {e(q.text)}</h5>'
                      f'<div class="cols {"two" if len(parts) > 1 else ""}">{"".join(parts)}</div></div>')
    attrs = "".join(f'<tr><th>{e(a.label)}</th><td>{e(u.attributes.get(a.key, ""))}</td></tr>'
                    for a in d.personaDesign.attributeSchema)
    idtag = f' <span class="muted">{e(u.id)}</span>' if u.displayName else ""
    return (f'<section class="card resp-user" id="resp-{e(u.id)}" data-uid="{e(u.id)}">'
            f'<h3>{e(uname(u))}{idtag}</h3><details open><summary>ペルソナ</summary><table class="attrs">{attrs}</table>'
            f'<p>{e(u.background)}</p></details>{"".join(blocks)}</section>')


def responses_tab(c: Ctx) -> str:
    d = c.d
    opts = "".join(f'<option value="{e(u.id)}">{e(ulabel(u))}</option>' for u in d.users)
    nav = ('<div class="nav"><button type="button" id="prev-user">← 前</button> '
           f'<select id="user-select" aria-label="ユーザーを選択">{opts}</select> '
           '<button type="button" id="next-user">次 →</button> '
           '<input type="search" id="resp-search" placeholder="氏名・ユーザーIDで検索（例: user-007）" '
           'aria-label="氏名・ユーザーIDで検索"> <span id="resp-msg" class="muted"></span></div>')
    return notice(d) + nav + "".join(response_section(c, u) for u in d.users)


CSS = """
:root{--bg:#fafafa;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e2e2e6;--accent:#2a5bd7;--bar:#2a5bd7;--bar2:#b85c00;--warn:#fff4d6;--warnfg:#5c4300}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececf0;--muted:#a0a0a8;--card:#1f1f23;--line:#34343a;--accent:#8aa9ff;--bar:#8aa9ff;--bar2:#ffb066;--warn:#3a3016;--warnfg:#f3dca0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 system-ui,-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
main{max-width:980px;margin:0 auto;padding:16px}h1{font-size:1.4rem;margin:.2rem 0}h2{font-size:1.1rem}h3{font-size:1.05rem;margin:.2rem 0}h4{margin:.6rem 0 .3rem}h5{margin:.8rem 0 .3rem;font-size:.95rem}
.muted{color:var(--muted);font-size:.9em}.note{color:var(--muted);font-size:.85em;margin:.3rem 0}
.banner{background:var(--warn);color:var(--warnfg);padding:8px 12px;border-radius:6px;margin:8px 0;font-size:.9em}
.warn{background:var(--warn);color:var(--warnfg);padding:8px 12px;border-radius:6px;border:1px solid var(--warnfg)}
.links a{margin-right:12px}a{color:var(--accent)}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:12px 0;position:sticky;top:0;background:var(--bg);z-index:2}
.tabs button{border:0;background:none;color:var(--fg);padding:10px 16px;font:inherit;cursor:pointer;border-bottom:3px solid transparent}
.tabs button[aria-selected=true]{border-bottom-color:var(--accent);font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;margin:12px 0}.card.small{margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.qid{display:inline-block;background:var(--line);border-radius:4px;padding:0 6px;font-size:.8em;margin-right:4px}
.tag{display:inline-block;border:1px solid var(--line);border-radius:4px;padding:0 6px;font-size:.78em;color:var(--muted)}
.chip{display:inline-block;background:var(--line);border-radius:10px;padding:0 8px;font-size:.78em;margin:2px 2px 0 0}
.bar-row{display:grid;grid-template-columns:minmax(80px,30%) 1fr auto;gap:8px;align-items:center;margin:3px 0;font-size:.9em}
.bar-track{background:var(--line);border-radius:3px;height:14px;display:block}.bar-fill{display:block;height:14px;background:var(--bar);border-radius:3px}
.bar-fill.after{background:var(--bar2)}.bar-val{font-variant-numeric:tabular-nums;min-width:90px;text-align:right}
.bars.mini .bar-row{font-size:.78em}.bars.mini .bar-track,.bars.mini .bar-fill{height:8px}
.cols.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}.big{font-size:1.05rem}
table{border-collapse:collapse;width:100%;font-size:.9em}th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
.ans-list{list-style:none;padding:0;margin:.4rem 0}.ans{border-top:1px solid var(--line);padding:8px 0}
.ans p,.resp p{margin:.2rem 0;white-space:pre-wrap;overflow-wrap:anywhere}.ans.dim{display:none}
.uid{font-weight:700}.score{color:var(--accent);font-size:.85em}
.status{font-weight:700;border:1px solid currentColor;border-radius:4px;padding:0 6px;font-size:.8em}.status.failed{color:#c0261d}.status.unanswered{color:var(--muted)}
button,input,select{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:6px;padding:4px 10px}
.theme-btn{cursor:pointer;color:var(--accent);padding:0 6px}.nav{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.attrs th{width:30%;color:var(--muted);font-weight:400}dl dt{color:var(--muted);margin-top:6px}dd{margin:0}
html.js .sr{display:none}footer{margin:32px 0;color:var(--muted);font-size:.85em}
@media (max-width:640px){.cols.two{grid-template-columns:1fr}.bar-row{grid-template-columns:1fr 1fr auto}main{padding:12px}}
@media print{.tabs{display:none}[hidden]{display:block!important}}
"""

JS = """
(function(){
var tabs=[].slice.call(document.querySelectorAll('[role=tab]')),panels=[].slice.call(document.querySelectorAll('[role=tabpanel]'));
function show(name){tabs.forEach(function(t){t.setAttribute('aria-selected',String(t.dataset.tab===name))});
 panels.forEach(function(p){p.hidden=p.dataset.panel!==name});}
tabs.forEach(function(t){t.addEventListener('click',function(){show(t.dataset.tab)})});
document.documentElement.classList.add('js');
var users=[].slice.call(document.querySelectorAll('.resp-user')),ids=users.map(function(s){return s.dataset.uid});
var sel=document.getElementById('user-select'),labels=sel?[].slice.call(sel.options).map(function(o){return o.textContent.toLowerCase()}):[],msg=document.getElementById('resp-msg'),cur=0;
function showUser(i){if(i<0||i>=ids.length)return;cur=i;users.forEach(function(s,j){s.hidden=j!==i});if(sel)sel.value=ids[i];}
function goto(uid){var i=ids.indexOf(uid);if(i>=0){show('responses');showUser(i);window.scrollTo(0,0);}}
document.addEventListener('click',function(ev){var a=ev.target.closest&&ev.target.closest('[data-goto]');
 if(a){ev.preventDefault();goto(a.dataset.goto);}});
if(sel){sel.addEventListener('change',function(){showUser(ids.indexOf(sel.value))});
 document.getElementById('prev-user').addEventListener('click',function(){showUser(cur-1)});
 document.getElementById('next-user').addEventListener('click',function(){showUser(cur+1)});
 document.getElementById('resp-search').addEventListener('input',function(ev){var v=ev.target.value.trim().toLowerCase();
  if(!v){msg.textContent='';return;}var i=ids.findIndex(function(x){return x.toLowerCase()===v})
  if(i<0)i=labels.findIndex(function(x){return x.indexOf(v)>=0});
  if(i>=0){showUser(i);msg.textContent='';}else{msg.textContent='該当するユーザーがいません';}});}
var us=document.getElementById('user-search'),cards=[].slice.call(document.querySelectorAll('#user-list .user')),uc=document.getElementById('user-count');
if(us){us.addEventListener('input',function(){var v=us.value.trim().toLowerCase(),n=0;cards.forEach(function(c){
  var ok=!v||c.dataset.search.indexOf(v)>=0;c.hidden=!ok;if(ok)n++});uc.textContent=v?n+'人が該当':'';});}
document.addEventListener('click',function(ev){var b=ev.target.closest&&ev.target.closest('.theme-btn');if(!b)return;
 var list=document.querySelector('.ans-list[data-q="'+b.dataset.q+'"]'),t=b.dataset.theme;if(!list)return;
 [].slice.call(list.children).forEach(function(li){var th=(li.dataset.themes||'').split(' ').filter(Boolean);
  var ok=!t||(t==='__none__'?(th.length===0&&li.dataset.status==='completed'):th.indexOf(t)>=0);li.classList.toggle('dim',!ok);});});
show('summary');showUser(0);
})();
"""


def render_html(d: SurveyData) -> str:
    c = Ctx(d)
    title = d.survey.title
    sub = f"合成ユーザー{d.research.completedUserCount}人・質問{len(d.survey.questions)}問"
    if c.compare:
        sub += "・改善前後の比較"
    tabs = [("summary", "概要", summary_tab(c)), ("users", "ユーザー", users_tab(c)),
            ("responses", "個別回答", responses_tab(c))]
    nav = "".join(f'<button type="button" role="tab" data-tab="{k}" aria-selected="{str(i == 0).lower()}">{e(n)}</button>'
                  for i, (k, n, _) in enumerate(tabs))
    panels = "".join(f'<div role="tabpanel" data-panel="{k}"><h2 class="sr">{e(n)}</h2>{html}</div>'
                     for k, n, html in tabs)
    lim = "".join(f"<li>{e(x)}</li>" for x in d.limitations)
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} — askusers</title><style>{CSS}</style></head>
<body><main>
<p class="muted">askusers ／ Ask users. Build better. ／ {e(d.runId)}</p>
<h1>{e(title)}</h1><p>{e(sub)}</p>
<p class="banner">この結果は AI による<b>合成ユーザー</b>の調査です。実在の人物ではなく、実際の市場需要や顧客の反応を示すものではありません。
{"改善前後の差は合成ユーザーによる評価の差であり、実際の改善効果を保証しません。" if c.compare else ""}</p>
<p class="links"><a href="report.md">report.md</a><a href="responses.csv">responses.csv</a><a href="data.json">data.json</a></p>
<nav class="tabs" role="tablist">{nav}</nav>
{panels}
<footer><h4>この調査の限界</h4><ul>{lim}</ul></footer>
</main>
<script type="application/json" id="askusers-data">{json_for_script(d.model_dump(mode="json"))}</script>
<script>{JS}</script></body></html>
"""
