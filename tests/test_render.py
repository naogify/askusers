"""--render のテスト。実ブラウザは使わず Playwright を差し替える。実ブラウザのテストは browser マーカー。"""

import contextlib
import functools
import http.server
import json
import threading

import pytest

from askusers import cli, render
from askusers import target as T

PUBLIC = lambda host, port, type=None: [(2, 1, 6, "", ("93.184.216.34", port))]  # noqa: E731
LOOP = lambda host, port, type=None: [(2, 1, 6, "", ("127.0.0.1", port))]  # noqa: E731


class FakeRequest:
    def __init__(self, url):
        self.url = url


class FakeRoute:
    def __init__(self, url):
        self.request, self.action = FakeRequest(url), None

    def continue_(self):
        self.action = "continue"

    def abort(self):
        self.action = "abort"


class FakePage:
    def __init__(self, ctx):
        self.ctx, self.url = ctx, None

    def goto(self, url, **kw):
        self.url = url
        self.ctx.visited.append(url)
        for sub in self.ctx.subrequests:
            r = FakeRoute(sub)
            self.ctx.handler(r)
            self.ctx.route_results.append((sub, r.action))

    def wait_for_load_state(self, *a, **kw):
        pass

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js):
        return self.ctx.screens[self.url.split("#")[-1] if "#" in self.url else ""]

    def close(self):
        pass


class FakeCtx:
    def __init__(self, screens, subrequests):
        self.screens, self.subrequests = screens, subrequests
        self.visited, self.route_results, self.handler = [], [], None

    def route(self, pattern, handler):
        self.handler = handler

    def new_page(self):
        return FakePage(self)


class FakeBrowser:
    def __init__(self, ctx):
        self.ctx, self.closed = ctx, False

    def new_context(self, **kw):
        return self.ctx

    def close(self):
        self.closed = True


def fake_playwright(screens, subrequests=(), launch_error=None):
    ctx = FakeCtx(screens, list(subrequests))
    browser = FakeBrowser(ctx)

    class Chromium:
        def launch(self):
            if launch_error:
                raise launch_error
            return browser

    @contextlib.contextmanager
    def factory():
        yield type("PW", (), {"chromium": Chromium()})()
    factory.ctx, factory.browser = ctx, browser
    return factory


def screen(text, controls=(), canvas=0, title="T"):
    return {"text": text, "title": title, "canvas": canvas, "controls": list(controls)}


def test_split_routes_merges_and_dedupes():
    assert render.split_routes(["#a", "#b,#c", " #a , "]) == ["#a", "#b", "#c"]


def test_full_url_hash_and_relative():
    assert render._full_url("http://h/app?x=1#old", "#spec") == "http://h/app?x=1#spec"
    assert render._full_url("http://h/a/b", "/c") == "http://h/c"
    assert render._full_url("http://h/a", "") == "http://h/a"


def test_render_concatenates_screens_with_headings_and_controls():
    f = fake_playwright({
        "land": screen("土地を選ぶ\n  敷地  面積", [
            {"kind": "ボタン", "label": "次へ"},
            {"kind": "選択肢", "label": "工法", "options": ["木造", "鉄骨"]},
            {"kind": "入力欄", "label": "予算", "type": "number"},
            {"kind": "ボタン", "label": "次へ"}], canvas=1),
        "spec": screen("仕様を決める"),
    })
    t = render.render_url("http://127.0.0.1:8852/", "target-001", ["#land", "#spec"],
                          allow_local=True, resolve=LOOP, playwright_factory=f)
    assert t.type == "url" and t.contentHash == T.sha256(t.content)
    assert t.source == "http://127.0.0.1:8852/ (render: #land, #spec)"
    c = t.content
    assert c.index("# 画面 #land") < c.index("# 画面 #spec")
    assert "敷地 面積" in c and "仕様を決める" in c
    assert "- ボタン: 次へ" in c and c.count("- ボタン: 次へ") == 1  # 重複は 1 つに
    assert "- 選択肢: 工法 [木造 / 鉄骨]" in c and "- 入力欄(number): 予算" in c
    assert "canvas" in c
    assert f.ctx.visited == ["http://127.0.0.1:8852/#land", "http://127.0.0.1:8852/#spec"]
    assert f.browser.closed


def test_render_without_routes_opens_top():
    f = fake_playwright({"": screen("トップ")})
    t = render.render_url("https://example.com/", "t", [], resolve=PUBLIC, playwright_factory=f)
    assert "トップ" in t.content and t.source == "https://example.com/"


def test_render_loopback_requires_allow_local():
    f = fake_playwright({"": screen("x")})
    with pytest.raises(T.TargetError, match="--allow-local"):
        render.render_url("http://127.0.0.1:8852/", "t", [], resolve=LOOP, playwright_factory=f)
    assert f.ctx.visited == []


def test_subrequests_to_internal_addresses_are_blocked():
    def resolve(host, port, type=None):
        ip = {"cdn.example": "93.184.216.34", "meta.internal": "169.254.169.254"}.get(host, "93.184.216.34")
        return [(2, 1, 6, "", (ip, port))]
    f = fake_playwright({"": screen("x")}, subrequests=[
        "https://cdn.example/a.js", "http://meta.internal/latest/meta-data/", "data:text/plain,hi"])
    t = render.render_url("https://example.com/", "t", [], resolve=resolve, playwright_factory=f)
    assert dict(f.ctx.route_results) == {
        "https://cdn.example/a.js": "continue",
        "http://meta.internal/latest/meta-data/": "abort",
        "data:text/plain,hi": "continue"}
    assert "遮断した要求 1 件" in t.content


def test_allow_local_does_not_open_private_lan_to_subrequests():
    def resolve(host, port, type=None):
        return [(2, 1, 6, "", ("192.168.0.9" if host == "nas.lan" else "127.0.0.1", port))]
    f = fake_playwright({"": screen("x")}, subrequests=["http://nas.lan/x", "http://127.0.0.1:9/y"])
    render.render_url("http://127.0.0.1:8852/", "t", [], allow_local=True, resolve=resolve, playwright_factory=f)
    assert dict(f.ctx.route_results) == {"http://nas.lan/x": "abort", "http://127.0.0.1:9/y": "continue"}


def test_missing_playwright_gives_install_hint(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake_import(name, *a, **kw):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(T.TargetError, match="uv sync --extra render"):
        render.render_url("https://example.com/", "t", [], resolve=PUBLIC)


def test_chromium_launch_failure_gives_install_hint():
    f = fake_playwright({}, launch_error=RuntimeError("Executable doesn't exist"))
    with pytest.raises(T.TargetError, match="playwright install chromium"):
        render.render_url("https://example.com/", "t", [], resolve=PUBLIC, playwright_factory=f)


def test_each_screen_gets_a_share_of_the_size_budget():
    big = "あ" * (T.MAX_CHARS * 2)
    f = fake_playwright({"a": screen(big), "b": screen("後ろの画面の本文")})
    t = render.render_url("https://example.com/", "t", ["#a", "#b"], resolve=PUBLIC, playwright_factory=f)
    assert "後ろの画面の本文" in t.content


# --- CLI -----------------------------------------------------------------------------------

def test_cli_passes_render_options(tmp_path, capsys, monkeypatch):
    seen = {}

    def fake(src, tid="target-001", **kw):
        from askusers.models import Target
        seen.update(kw)
        return Target(id=tid, type="url", source=src, content="本文", contentHash=T.sha256("本文"))
    monkeypatch.setattr(T, "load_target", fake)
    code = cli.main(["http://127.0.0.1:8852/", "--allow-local", "--render", "--route", "#a",
                     "--routes", "#b,#c", "--mock", "--no-ssr", "--count", "5",
                     "--output", str(tmp_path / "o")])
    assert code == 0
    assert seen == {"render": True, "routes": ["#a", "#b", "#c"], "allow_local": True}


def test_cli_defaults_do_not_allow_local_or_render(tmp_path, monkeypatch):
    seen = {}

    def fake(src, tid="target-001", **kw):
        from askusers.models import Target
        seen.update(kw)
        return Target(id=tid, type="url", source=src, content="本文", contentHash=T.sha256("本文"))
    monkeypatch.setattr(T, "load_target", fake)
    cli.main(["https://example.com/", "--mock", "--no-ssr", "--count", "5", "--output", str(tmp_path / "o")])
    assert seen == {"render": False, "routes": [], "allow_local": False}


def test_cli_routes_without_render_is_usage_error(capsys):
    assert cli.main(["https://example.com/", "--routes", "#a", "--mock"]) == 2
    assert "--render" in capsys.readouterr().err


def test_cli_loopback_without_allow_local_exits_with_target_error(capsys, monkeypatch):
    monkeypatch.setattr(T.socket, "getaddrinfo", LOOP)
    assert cli.main(["http://127.0.0.1:8852/", "--mock", "--no-ssr"]) == 3
    assert "--allow-local" in capsys.readouterr().err


def test_cli_render_on_file_is_target_error(tmp_path, capsys):
    f = tmp_path / "a.md"
    f.write_text("x", encoding="utf-8")
    assert cli.main([str(f), "--render", "--mock"]) == 3


# --- 実ブラウザ（Playwright + Chromium が無ければ skip） -------------------------------------

APP = """<!doctype html><html><head><meta charset=utf-8><title>試作</title></head><body>
<h1 id=h></h1><button id=b>次へ進む</button>
<label for=y>予算（万円）</label><input id=y type=number>
<select aria-label="工法"><option>木造</option><option>鉄骨</option></select>
<script>
const t={'#land':'土地を選ぶ画面','#spec':'仕様を決める画面'};
document.getElementById('h').textContent = t[location.hash] || 'トップ';
</script></body></html>"""


@pytest.fixture
def local_app(tmp_path):
    (tmp_path / "index.html").write_text(APP, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


@pytest.mark.browser
def test_real_browser_renders_js_screens(local_app):
    pytest.importorskip("playwright")
    try:
        t = T.load_target(local_app, render=True, routes=["#land", "#spec"], allow_local=True)
    except T.TargetError as e:
        if "Chromium" in str(e):
            pytest.skip("Chromium 未導入")
        raise
    assert "土地を選ぶ画面" in t.content and "仕様を決める画面" in t.content
    assert "- ボタン: 次へ進む" in t.content and "- 入力欄(number): 予算（万円）" in t.content
    assert "- 選択肢: 工法 [木造 / 鉄骨]" in t.content
    json.dumps(t.model_dump())
