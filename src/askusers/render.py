"""JavaScript を実行して描画後の画面を読む（Playwright / Chromium。optional extra `render`）。

複数の画面（ハッシュや相対パス）を順に開き、描画後のテキストと操作できる要素を
見出し付きで連結して 1 つの Target にする。スクリーンショットは撮らない（画像入力は対象外）。
ブラウザが発行するサブリソース要求も、取得時と同じ安全判定（check_url_safe）を通す。
"""

from __future__ import annotations

import socket
from urllib.parse import urldefrag, urljoin, urlparse

from .models import Target
from .target import MAX_CHARS, TargetError, _finish, check_url_safe

NAV_TIMEOUT_MS = 30_000
IDLE_TIMEOUT_MS = 5_000
SETTLE_MS = 800
MAX_CONTROLS = 80

INSTALL_HINT = (
    "--render には Playwright と Chromium が必要です。次で導入してください:\n"
    "  uv sync --extra render   （pip なら pip install 'askusers[render]'）\n"
    "  uv run playwright install chromium"
)

# 画面に見えている操作要素を集める（ブラウザ内で実行）。
_COLLECT_JS = """
() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect(), st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
  const labelOf = (el) => {
    let t = clean(el.getAttribute('aria-label'));
    if (!t && el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t = clean(l.innerText); }
    if (!t) { const l = el.closest('label'); if (l) t = clean(l.innerText); }
    return t || clean(el.getAttribute('placeholder')) || clean(el.getAttribute('title')) || clean(el.name);
  };
  const out = [];
  for (const el of document.querySelectorAll(
      'button, a[href], input, select, textarea, [role=button], [role=tab], [role=link], [role=switch]')) {
    if (!vis(el)) continue;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'hidden') continue;
      if (type === 'button' || type === 'submit') out.push({kind: 'ボタン', label: clean(el.value) || labelOf(el)});
      else out.push({kind: type === 'range' ? 'スライダー' : type === 'checkbox' ? 'チェックボックス'
        : type === 'radio' ? 'ラジオボタン' : '入力欄', label: labelOf(el), type});
    } else if (tag === 'select') {
      out.push({kind: '選択肢', label: labelOf(el),
                options: [...el.options].map((o) => clean(o.textContent)).filter(Boolean)});
    } else if (tag === 'textarea') {
      out.push({kind: '入力欄', label: labelOf(el), type: 'textarea'});
    } else if (tag === 'a') {
      out.push({kind: 'リンク', label: clean(el.innerText) || labelOf(el), href: el.getAttribute('href')});
    } else {
      out.push({kind: 'ボタン', label: clean(el.innerText) || labelOf(el)});
    }
  }
  return {text: document.body ? document.body.innerText : '', title: document.title,
          canvas: document.querySelectorAll('canvas').length, controls: out};
}
"""


def _full_url(base: str, route: str) -> str:
    """route が `#frag` ならベース URL のハッシュ違い、それ以外は相対 URL として解決する。"""
    route = route.strip()
    if not route:
        return base
    if route.startswith("#"):
        return urldefrag(base)[0] + route
    return urljoin(base, route)


def split_routes(values: list[str]) -> list[str]:
    """`--route` の繰り返しと `--routes "a,b"` を 1 つの順序付きリストにする（重複は除く）。"""
    seen: list[str] = []
    for v in values:
        for r in v.split(","):
            r = r.strip()
            if r and r not in seen:
                seen.append(r)
    return seen


def _fmt_control(c: dict) -> str:
    label = c.get("label") or "(ラベルなし)"
    if c["kind"] == "選択肢":
        return f"- 選択肢: {label} [{' / '.join(c.get('options', [])[:12])}]"
    if c["kind"] == "リンク":
        return f"- リンク: {label} → {c.get('href', '')}"
    if c["kind"] == "入力欄" and c.get("type") not in (None, "text"):
        return f"- 入力欄({c['type']}): {label}"
    return f"- {c['kind']}: {label}"


def format_screen(route: str, url: str, data: dict, limit: int) -> str:
    controls, seen = [], set()
    for c in data["controls"]:
        line = _fmt_control(c)
        if line not in seen:
            seen.add(line)
            controls.append(line)
    parts = [f"# 画面 {route or '(トップ)'}", f"URL: {url}"]
    if data.get("title"):
        parts.append(f"タイトル: {data['title']}")
    body = "\n".join(ln for ln in (" ".join(x.split()) for x in data["text"].splitlines()) if ln)
    parts += ["", "## 表示されているテキスト", body or "(テキストなし)"]
    if data.get("canvas"):
        parts += ["", f"(この画面には canvas の描画が {data['canvas']} 個あるが、画像なのでテキスト化できない)"]
    parts += ["", "## 操作できる要素"]
    if controls:
        parts += controls[:MAX_CONTROLS]
        if len(controls) > MAX_CONTROLS:
            parts.append(f"- …ほか {len(controls) - MAX_CONTROLS} 件")
    else:
        parts.append("(なし)")
    return "\n".join(parts)[:limit]


def render_url(url: str, tid: str, routes: list[str], *, allow_local: bool = False,
               resolve=socket.getaddrinfo, playwright_factory=None) -> Target:
    """playwright_factory はテスト用の差し替え口（既定は playwright.sync_api.sync_playwright）。"""
    if playwright_factory is None:
        try:
            from playwright.sync_api import sync_playwright as playwright_factory
        except ImportError as e:
            raise TargetError(INSTALL_HINT) from e
    check_url_safe(url, resolve, allow_local)
    screens = routes or [""]
    per_screen = max(MAX_CHARS // len(screens) - 200, 2000)
    verdicts: dict[str, bool] = {}
    blocked: list[str] = []

    def allowed(req_url: str) -> bool:
        if urlparse(req_url).scheme in ("data", "blob", "about"):
            return True
        host_key = urlparse(req_url).netloc
        if host_key not in verdicts:
            try:
                check_url_safe(req_url, resolve, allow_local)
                verdicts[host_key] = True
            except TargetError:
                verdicts[host_key] = False
        return verdicts[host_key]

    def handler(route):
        if allowed(route.request.url):
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    sections: list[str] = []
    try:
        with playwright_factory() as pw:
            try:
                browser = pw.chromium.launch()
            except Exception as e:  # noqa: BLE001  Chromium 未導入など
                raise TargetError(f"Chromium を起動できません。\n{INSTALL_HINT}\n({e})") from e
            try:
                ctx = browser.new_context(accept_downloads=False, service_workers="block")
                ctx.route("**/*", handler)
                for r in screens:
                    full = _full_url(url, r)
                    check_url_safe(full, resolve, allow_local)
                    page = ctx.new_page()
                    try:
                        page.goto(full, wait_until="load", timeout=NAV_TIMEOUT_MS)
                        try:
                            page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
                        except Exception:  # noqa: BLE001  常時通信するページは待ちきらない
                            pass
                        page.wait_for_timeout(SETTLE_MS)
                        data = page.evaluate(_COLLECT_JS)
                    except TargetError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        raise TargetError(f"画面を描画できません: {full} ({type(e).__name__}: {e})") from e
                    finally:
                        page.close()
                    sections.append(format_screen(r, full, data, per_screen))
            finally:
                browser.close()
    except TargetError:
        raise
    except Exception as e:  # noqa: BLE001
        raise TargetError(f"ブラウザでの取得に失敗しました: {url} ({type(e).__name__}: {e})") from e
    header = f"[JavaScript 描画後の内容。画面 {len(sections)} 件: {', '.join(screens) if routes else 'トップ'}]"
    if blocked:
        header += f"\n[安全判定で遮断した要求 {len(blocked)} 件（内部アドレス宛て）]"
    src = url if not routes else f"{url} (render: {', '.join(routes)})"
    return _finish(tid, "url", src, header + "\n\n" + "\n\n---\n\n".join(sections))
