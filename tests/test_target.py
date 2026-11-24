import httpx
import pytest

from askusers import target as T


def test_file_target_snapshot_and_hash(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("# Hi\nbody", encoding="utf-8")
    t = T.load_file(str(p))
    assert t.type == "file" and t.content == "# Hi\nbody" and t.contentHash == T.sha256("# Hi\nbody")


@pytest.mark.parametrize("name,data", [("missing.md", None), ("empty.md", b"  \n"), ("bin.md", b"\xff\xfe\x00"),
                                       ("x.pdf", b"%PDF")])
def test_file_errors_are_explicit(tmp_path, name, data):
    p = tmp_path / name
    if data is not None:
        p.write_bytes(data)
    with pytest.raises(T.TargetError):
        T.load_file(str(p))


def test_truncation_is_flagged(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("あ" * (T.MAX_CHARS + 10), encoding="utf-8")
    t = T.load_file(str(p))
    assert t.truncated and len(t.content) == T.MAX_CHARS


PUBLIC = lambda host, port, type=None: [(2, 1, 6, "", ("93.184.216.34", port))]  # noqa: E731


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_url_html_snapshot():
    html = "<html><head><title>T</title><style>x{}</style></head><body><script>evil()</script><h1>H</h1><p>para</p></body></html>"
    t = T.load_url("https://example.com/", client=client(lambda r: httpx.Response(200, text=html, headers={"content-type": "text/html"})), resolve=PUBLIC)
    assert "evil" not in t.content and "para" in t.content and t.type == "url" and t.contentHash


def test_url_http_error_is_clear_error():
    with pytest.raises(T.TargetError, match="404"):
        T.load_url("https://example.com/x", client=client(lambda r: httpx.Response(404)), resolve=PUBLIC)


def test_url_network_error_is_clear_error():
    def boom(r):
        raise httpx.ConnectError("x")
    with pytest.raises(T.TargetError):
        T.load_url("https://example.com/", client=client(boom), resolve=PUBLIC)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)"])
def test_dangerous_schemes_rejected(url):
    with pytest.raises(T.TargetError):
        T.check_url_safe(url, PUBLIC)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "172.16.0.1"])
def test_internal_addresses_rejected(ip):
    with pytest.raises(T.TargetError, match="内部"):
        T.check_url_safe("http://internal.example/", lambda h, p, type=None: [(2, 1, 6, "", (ip, p))])


def test_redirect_to_internal_is_blocked():
    def resolve(host, port, type=None):
        return [(2, 1, 6, "", ("127.0.0.1" if host == "evil.internal" else "93.184.216.34", port))]

    def h(r):
        return httpx.Response(302, headers={"location": "http://evil.internal/secret"})
    with pytest.raises(T.TargetError, match="内部"):
        T.load_url("https://example.com/", client=client(h), resolve=resolve)


def test_too_many_redirects_and_bad_content_type():
    with pytest.raises(T.TargetError, match="リダイレクト"):
        T.load_url("https://example.com/", client=client(lambda r: httpx.Response(302, headers={"location": "/a"})), resolve=PUBLIC)
    with pytest.raises(T.TargetError, match="Content-Type"):
        T.load_url("https://example.com/", client=client(lambda r: httpx.Response(200, content=b"x", headers={"content-type": "image/png"})), resolve=PUBLIC)
