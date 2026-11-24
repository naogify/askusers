"""評価対象の取得（Target Loader）。内容は「データ」であり、指示としては扱わない。"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .models import Target

MAX_CHARS = 100_000
MAX_BYTES = 2_000_000
MAX_REDIRECTS = 5
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text", ""}


class TargetError(Exception):
    """評価対象を取得できない。推測して続行せず、明確なエラーにする。"""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _finish(tid: str, ttype: str, source: str, text: str) -> Target:
    text = text.strip()
    if not text:
        raise TargetError(f"評価対象が空です: {source}")
    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS]
    return Target(id=tid, type=ttype, source=source, content=text,
                  contentHash=sha256(text), truncated=truncated)


def load_file(path: str, tid: str = "target-001") -> Target:
    p = Path(path)
    if p.suffix.lower() not in TEXT_SUFFIXES:
        raise TargetError(f"未対応の形式です（MVP は Markdown / テキスト / URL）: {path}")
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise TargetError(f"ファイルを読めません: {path} ({e.strerror})") from e
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TargetError(f"UTF-8 として読めません: {path}") from e
    return _finish(tid, "file", str(path), text)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section",
             "article", "header", "footer", "ul", "ol", "table", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "h1" or tag == "h2" or tag == "h3":
            self.parts.append("# ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    lines = [" ".join(line.split()) for line in "".join(p.parts).splitlines()]
    body = "\n".join(line for line in lines if line)
    title = " ".join(p.title.split())
    return f"{title}\n\n{body}" if title else body


def _is_public_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return a.is_global and not (a.is_multicast or a.is_loopback or a.is_link_local)


def check_url_safe(url: str, resolve=socket.getaddrinfo) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise TargetError(f"http/https 以外のスキームは取得しません: {u.scheme or '(なし)'}")
    if not u.hostname:
        raise TargetError(f"URL が不正です: {url}")
    try:
        infos = resolve(u.hostname, u.port or (443 if u.scheme == "https" else 80),
                        type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise TargetError(f"ホスト名を解決できません: {u.hostname}") from e
    for info in infos:
        if not _is_public_ip(info[4][0]):
            raise TargetError(f"内部ネットワーク/予約アドレスへのアクセスは拒否します: {u.hostname}")


def load_url(url: str, tid: str = "target-001", client: httpx.Client | None = None,
             resolve=socket.getaddrinfo) -> Target:
    own = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=False,
                                    headers={"User-Agent": "askusers/0.1 (+synthetic-user research)"})
    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_url_safe(current, resolve)
            try:
                with client.stream("GET", current) as r:
                    if r.is_redirect:
                        loc = r.headers.get("location")
                        if not loc:
                            raise TargetError(f"リダイレクト先が不正です: {current}")
                        current = urljoin(current, loc)
                        continue
                    if r.status_code >= 400:
                        raise TargetError(f"URL を取得できません (HTTP {r.status_code}): {url}")
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not (ctype.startswith("text/") or ctype in
                                      ("application/xhtml+xml", "application/xml")):
                        raise TargetError(f"未対応の Content-Type です: {ctype}")
                    buf = bytearray()
                    for chunk in r.iter_bytes():
                        buf += chunk
                        if len(buf) > MAX_BYTES:
                            break
                    enc = r.encoding or "utf-8"
            except httpx.HTTPError as e:
                raise TargetError(f"URL を取得できません: {url} ({type(e).__name__})") from e
            text = bytes(buf).decode(enc, "replace")
            if ctype in ("text/html", "application/xhtml+xml", "") or "<html" in text[:2000].lower():
                text = html_to_text(text)
            return _finish(tid, "url", url, text)
        raise TargetError(f"リダイレクトが多すぎます: {url}")
    finally:
        if own:
            client.close()


def load_target(source: str, tid: str = "target-001", **kw) -> Target:
    if source.startswith(("http://", "https://")) or "://" in source:
        return load_url(source, tid, **kw)
    return load_file(source, tid)
