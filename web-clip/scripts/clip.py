#!/usr/bin/env python3
"""Web page -> self-contained markdown clip package. Page text never reaches the model.

A clip package is a directory holding everything a reader (or another tool) needs:

    <clip>/
      content.md   cleaned markdown; images point at ./assets
      meta.json    manifest: format, title, source, dates, type, hashes, assets
      assets/      downloaded images and PDFs
      raw.html     original html (optional)

meta.json declares `format: "web-clip/clip-package"` and `format_version:
"<major>.<minor>"`. Every path inside it is relative to the package directory,
so a package can be moved or copied. Consumers should accept any package whose
major version they know and ignore unfamiliar fields.

Subcommands:
  fetch    fetch a url, clean it, download its images, write a clip package; print a card
  ingest   build the same package from HTML/markdown already on disk (anti-bot fallback)
  list     list clip packages in the workspace
  verify   check a package against its own manifest
  release  delete a clip package from the workspace
  gc       drop clip packages older than N days

Packages live in $WEB_CLIP_HOME, defaulting to $XDG_DATA_HOME/web-clip or
~/.local/share/web-clip — never inside the skill directory, so updating or
reinstalling the skill cannot delete pending work.
"""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit

try:
    import requests
except ImportError:
    sys.exit("missing dependency: requests")
try:
    from bs4 import BeautifulSoup, Comment, NavigableString, Tag
except ImportError:
    sys.exit("missing dependency: beautifulsoup4")


SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_state_home():
    """Clips live outside the skill directory, so reinstalling or updating the
    skill cannot delete pending work. A `state/` left by an older install is
    migrated once, then never looked at again."""
    explicit = os.environ.get("WEB_CLIP_HOME")
    if explicit:
        home = os.path.abspath(os.path.expanduser(explicit))
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
        home = os.path.join(base, "web-clip")
    legacy = os.path.join(SKILL_ROOT, "state")
    if not os.path.exists(home) and os.path.isdir(legacy):
        try:
            os.makedirs(os.path.dirname(home), exist_ok=True)
            shutil.move(legacy, home)
        except OSError:
            return legacy
    return home


STATE_HOME = resolve_state_home()
CLIPS_DIR = os.path.join(STATE_HOME, "clips")

# Clip packages are the contract between this skill and whatever files them.
# Consumers must accept any package whose major version they know and ignore
# fields added in later minor versions.
PACKAGE_FORMAT = "web-clip/clip-package"
PACKAGE_VERSION = "1.0"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
MAX_ASSET_BYTES = 12 * 1024 * 1024
MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 5
DEFAULT_GC_DAYS = 14
# Allowlist, not a denylist. SVG is deliberately absent: it is a script-bearing
# XML document, and a vault full of them is a stored-XSS surface for any viewer.
IMAGE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "image/heic": ".heic",
}

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "center", "dd", "div", "dl",
    "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
    "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "ul",
}
DROP_TAGS = {
    "script", "style", "noscript", "template", "iframe", "object", "embed",
    "form", "button", "input", "select", "textarea", "svg", "canvas", "audio",
    "video", "source", "track", "map", "area", "link", "meta",
}
# Matched against class/id. Kept deliberately conservative so article bodies survive.
NOISE_RE = re.compile(
    r"(^|[-_\s])("
    r"ad|ads|adsbygoogle|advert(is(ing|ement))?|sponsor(ed)?|promo(tion)?|"
    r"recommend(ed|ation)?|related|read-?more|more-?link|popular|trending|"
    r"share|social|follow|subscribe|newsletter|signup|paywall|"
    r"comment(s)?|disqus|respond|reply-?form|"
    r"sidebar|side-?bar|widget|breadcrumb|pagination|pager|"
    r"popup|modal|overlay|lightbox|cookie|consent|gdpr|banner|toast|"
    r"qrcode|qr-?code|reward|tip-?jar|donate|"
    r"nav|navbar|navigation|menu|toolbar|topbar|masthead|"
    r"footer|copyright|legal|disclaimer|"
    r"skip-?link|screen-?reader|sr-only|visually-?hidden|hidden"
    r")([-_\s]|$)",
    re.I,
)
NOISE_TEXT_RE = re.compile(
    r"^("
    r"预览时标签不可点|微信扫一扫|扫一扫|长按识别|点击上方|点击关注|关注我们|"
    r"一起.?点赞.?三连|点赞.?在看|分享.?点赞.?在看|喜欢此内容的人还喜欢|"
    r"继续滑动看下一个|轻触阅读原文|向上滑动看下一个|"
    r"scan to follow|scan with weixin|got it|"
    r"在小说阅读器读本章|去阅读|在公众号小说中沉浸阅读|"
    r"advertisement|sponsored content|cookie policy"
    r")",
    re.I,
)
# Site-specific main-content selectors, tried in order.
SITE_SELECTORS = [
    ("wechat", r"(^|\.)mp\.weixin\.qq\.com$", ["#js_content", "div.rich_media_content"]),
    ("zhihu", r"(^|\.)zhihu\.com$", ["div.Post-RichTextContainer", "div.RichContent-inner", "div.QuestionAnswer-content"]),
    ("github", r"(^|\.)github\.com$", ["article.markdown-body", "div#readme"]),
    ("juejin", r"(^|\.)juejin\.cn$", ["div.markdown-body", "article"]),
    ("csdn", r"(^|\.)csdn\.net$", ["div#content_views", "article"]),
    ("jianshu", r"(^|\.)jianshu\.com$", ["article", "div.show-content"]),
    ("sspai", r"(^|\.)sspai\.com$", ["div.article-body", "article"]),
    ("medium", r"(^|\.)medium\.com$", ["article"]),
    ("substack", r"(^|\.)substack\.com$", ["div.available-content", "article"]),
    ("arxiv", r"(^|\.)arxiv\.org$", ["blockquote.abstract", "div#abs"]),
    ("xiaohongshu", r"(^|\.)(xiaohongshu\.com|xhslink\.cn)$",
     ["div#detail-desc", "div.note-content", "div.desc"]),
    ("twitter", r"(^|\.)(x\.com|twitter\.com)$",
     ["article", '[data-testid="tweetText"]']),
]
GENERIC_SELECTORS = [
    "article", "main", '[role="main"]', "div.post-content", "div.entry-content",
    "div.article-content", "div.article-body", "div.post-body", "div.content",
    "div#content", "div.markdown-body",
]


# ------------------------------------------------------------------------ util

def gc_clips(max_age_days=DEFAULT_GC_DAYS):
    """Drop abandoned clip packages so raw pages don't pile up forever.

    Runs automatically before every fetch, and on demand via `gc`.
    """
    now = time.time()
    removed = []
    try:
        entries = os.listdir(CLIPS_DIR)
    except OSError:
        return removed
    for name in sorted(entries):
        path = os.path.join(CLIPS_DIR, name)
        try:
            if os.path.isdir(path) and now - os.path.getmtime(path) > max_age_days * 86400:
                shutil.rmtree(path, ignore_errors=True)
                removed.append(path)
        except OSError:
            continue
    return removed


def gc_days():
    try:
        return max(0, int(os.environ.get("WEB_CLIP_GC_DAYS", "")))
    except ValueError:
        return DEFAULT_GC_DAYS


def atomic_write_json(path, obj):
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def atomic_write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def emit(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def fail(msg, **extra):
    payload = {"ok": False, "error": msg}
    payload.update(extra)
    emit(payload)
    sys.exit(1)


def canonical_url(url):
    """Normalize for identity: drop tracking params and fragment."""
    parts = urlsplit(url)
    drop = re.compile(r"^(utm_|ref$|ref_|from$|source$|spm|share|scene$|chksm$|"
                      r"srcid$|sharer_|fbclid$|gclid$|mid$|idx$|sn$|_r$)", re.I)
    keep = []
    for kv in parts.query.split("&"):
        if not kv:
            continue
        name = kv.split("=", 1)[0]
        if not drop.match(name):
            keep.append(kv)
    host = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, "&".join(sorted(keep)), ""))


def slugify(title, limit=60):
    text = unicodedata.normalize("NFKC", title or "").strip()
    text = re.sub(r'[\\/:*?"<>|\n\r\t]+', "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:limit] or "untitled").strip()


def asset_slug(title, limit=40):
    """Asset names must survive both markdown links and Obsidian wiki links,
    so no spaces or link-punctuation ever enter them."""
    text = slugify(title, limit * 2)
    text = re.sub(r"[\s\[\]()<>#?%&|:;,'\"!]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-._")
    return text[:limit].strip("-._") or "clip"


def visible_len(text):
    return len(re.sub(r"\s+", "", text))


# -------------------------------------------------------------------- fetching

BLOCK_RE = re.compile(
    r"(secitptpage/verify|TCaptcha\.js|环境异常|完成验证后即可继续访问|"
    r"captcha-delivery|cf-browser-verification|Just a moment\.\.\.|"
    r"Attention Required!\s*\|\s*Cloudflare|Enable JavaScript and cookies to continue)",
    re.I,
)


class FetchRefused(Exception):
    """A url (or one of its redirect hops) is not safe or not allowed to fetch."""


LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa")


def address_rejection(host):
    """Why `host` must not be fetched, or "" when it resolves to public addresses only.

    Every resolved address is checked, not just the first: a hostname with one
    public and one loopback record is still an SSRF vector.
    """
    host = (host or "").strip().strip("[]").lower().rstrip(".")
    if not host:
        return "missing host"
    if host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return "local hostname {!r}".format(host)
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return "dns lookup failed for {!r}: {}".format(host, exc)
    if not infos:
        return "dns lookup returned no address for {!r}".format(host)
    for info in infos:
        raw = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return "unparsable address {!r}".format(raw)
        if not ip.is_global or ip.is_multicast:
            return "non-public address {}".format(ip)
        # IPv6 transition addresses can smuggle a private IPv4 inside a
        # perfectly global-looking v6 address.
        embedded = [getattr(ip, "ipv4_mapped", None), getattr(ip, "sixtofour", None)]
        embedded.extend(getattr(ip, "teredo", None) or ())
        for inner in embedded:
            if inner is not None and not inner.is_global:
                return "tunnelled non-public address {}".format(inner)
    return ""


def url_rejection(url):
    """Why `url` must not be fetched, or "" when it is safe."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return "unsupported scheme {!r}; only http/https allowed".format(parts.scheme)
    try:
        host = parts.hostname
    except ValueError as exc:
        return "unparsable url: {}".format(exc)
    return address_rejection(host)


def request_headers(referer=None, accept=None):
    headers = {
        "User-Agent": UA,
        "Accept": accept or ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                             "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    return headers


def attach_body(resp, data):
    """Hand streamed bytes back to the response object.

    We always stream so the size cap is enforced before anything lands in
    memory; requests only exposes `.text`/`.content` (and its encoding
    detection) for non-streamed responses, so the bytes are re-attached here
    and the rest of the pipeline stays unchanged.
    """
    resp._content = data
    resp._content_consumed = True
    return resp


def read_capped(resp, max_bytes, what="response"):
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        resp.close()
        raise FetchRefused("{} declares {} bytes, over the {} MB cap".format(
            what, declared, max_bytes // (1024 * 1024)))
    data = b""
    for chunk in resp.iter_content(65536):
        data += chunk
        if len(data) > max_bytes:
            resp.close()
            raise FetchRefused("{} exceeds the {} MB cap".format(
                what, max_bytes // (1024 * 1024)))
    return data


def safe_get(url, referer=None, timeout=30, max_bytes=MAX_HTML_BYTES, accept=None):
    """GET a url with every redirect hop re-validated.

    requests' own redirect following would happily walk a public url into
    127.0.0.1 or a cloud metadata endpoint, so redirects are followed by hand
    and each destination goes through `url_rejection` first. The response body
    is read under `max_bytes`.

    Residual risk: DNS is resolved once for the check and again by the socket
    layer, so a rebinding attacker with control of a domain can still slip
    through. Treat clip packages as untrusted input, never as authority.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        why = url_rejection(current)
        if why:
            raise FetchRefused(why)
        resp = requests.get(current, headers=request_headers(referer, accept),
                            timeout=timeout, stream=True, allow_redirects=False)
        if resp.is_redirect or resp.is_permanent_redirect:
            location = (resp.headers.get("location") or "").strip()
            resp.close()
            if not location:
                raise FetchRefused("redirect with no location header from {}".format(current))
            current = urljoin(current, location)
            continue
        if 300 <= resp.status_code < 400:
            resp.close()
            raise FetchRefused("http {} with no usable location header".format(resp.status_code))
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        cap = MAX_PDF_BYTES if ctype == "application/pdf" else max_bytes
        attach_body(resp, read_capped(resp, cap, what=ctype or "response"))
        return resp, current
    raise FetchRefused("more than {} redirects starting at {}".format(MAX_REDIRECTS, url))


def fetch_html(url, attempts=3):
    """Fetch HTML, retrying past transient anti-bot interstitials."""
    last = None
    for attempt in range(attempts):
        resp, final_url = safe_get(url)
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and not ctype.startswith(("text/", "application/xhtml")):
            return resp, final_url, ctype, False
        if resp.encoding is None or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        blocked = bool(BLOCK_RE.search(resp.text[:20000])) or resp.status_code in (403, 429)
        last = (resp, final_url, ctype, blocked)
        if not blocked:
            return last
        time.sleep(1.2 * (attempt + 1))
    return last


# ------------------------------------------------------------- content cleaning

def is_live(tag):
    """Tags removed by an earlier decompose() keep showing up in stale lists."""
    return isinstance(tag, Tag) and getattr(tag, "attrs", None) is not None


def attr_signature(tag):
    if not is_live(tag):
        return ""
    bits = [tag.get("id") or ""]
    cls = tag.get("class")
    if cls:
        bits.extend(cls if isinstance(cls, list) else [cls])
    bits.append(tag.get("role") or "")
    return " ".join(b for b in bits if b)


def strip_noise(root, aggressive=True):
    for tag in list(root.find_all(list(DROP_TAGS))):
        if is_live(tag):
            tag.decompose()
    for tag in list(root.find_all(attrs={"aria-hidden": "true"})):
        if is_live(tag):
            tag.decompose()
    if not aggressive:
        return
    budget = 0.35 * max(1, visible_len(root.get_text(" ")))
    for tag in list(root.find_all(True)):
        if not is_live(tag) or tag.name in ("html", "body"):
            continue
        sig = attr_signature(tag)
        if sig and NOISE_RE.search(sig):
            # Never drop a node that carries the bulk of the text.
            if visible_len(tag.get_text(" ")) < budget:
                tag.decompose()


def score_node(tag):
    text = tag.get_text(" ", strip=True)
    length = visible_len(text)
    if length < 120:
        return -1
    paras = len(tag.find_all(["p", "section", "li", "h2", "h3"]))
    links = len(tag.find_all("a"))
    link_text = sum(visible_len(a.get_text(" ")) for a in tag.find_all("a"))
    density = link_text / max(1, length)
    score = length + paras * 40 - links * 12
    if density > 0.45:
        score *= 0.25
    sig = attr_signature(tag)
    if sig and NOISE_RE.search(sig):
        score *= 0.3
    if re.search(r"(article|content|post|body|markdown|rich_media)", sig, re.I):
        score *= 1.35
    return score


def pick_main(soup, host):
    for name, pattern, selectors in SITE_SELECTORS:
        if re.search(pattern, host or ""):
            for sel in selectors:
                node = soup.select_one(sel)
                if node and visible_len(node.get_text(" ")) > 80:
                    return node, name, []
    for sel in GENERIC_SELECTORS:
        node = soup.select_one(sel)
        if node and visible_len(node.get_text(" ")) > 400:
            return node, "generic:" + sel, []
    best, best_score = None, 0
    for tag in list(soup.find_all(["div", "section", "article", "main", "td"])):
        if not is_live(tag):
            continue
        s = score_node(tag)
        if s > best_score:
            best, best_score = tag, s
    if best is not None:
        return best, "heuristic", []
    body = soup.body or soup
    return body, "fallback:body", ["fell back to whole document; check for boilerplate"]


# ---------------------------------------------------------- markdown conversion

class Converter:
    def __init__(self, base_url):
        self.base_url = base_url
        self.images = []
        self.links = 0
        self.code_blocks = 0
        self.tables = 0
        self.headings = []
        self._seen_img = {}
        self._strong = 0
        self._em = 0

    def absolute(self, href):
        if not href:
            return ""
        href = href.strip()
        if href.startswith(("data:", "javascript:", "about:", "#")):
            return ""
        return urljoin(self.base_url, href)

    def image_url(self, tag):
        for attr in ("data-src", "data-original", "data-croporisrc", "data-actualsrc",
                     "data-lazy-src", "data-original-src", "src"):
            val = tag.get(attr)
            if val and not val.strip().startswith("data:"):
                return self.absolute(val)
        srcset = tag.get("srcset") or tag.get("data-srcset")
        if srcset:
            return self.absolute(srcset.split(",")[0].strip().split(" ")[0])
        return ""

    def register_image(self, url, alt):
        if not url:
            return None
        if url not in self._seen_img:
            self._seen_img[url] = len(self.images) + 1
            self.images.append({"index": len(self.images) + 1, "url": url, "alt": alt or ""})
        return self._seen_img[url]

    # ---- inline

    def inline(self, node):
        if isinstance(node, Comment):
            # Vue/React SSR emit fragment markers like <!--[--> whose payload
            # would otherwise leak into the text as stray brackets.
            return ""
        if isinstance(node, NavigableString):
            text = str(node)
            if not text.strip():
                return " " if text and re.search(r"[ \t\n]", text) else ""
            return re.sub(r"\s+", " ", text)
        if not isinstance(node, Tag):
            return ""
        name = node.name
        if name == "br":
            return "\n"
        if name == "img":
            url = self.image_url(node)
            alt = (node.get("alt") or "").strip()
            idx = self.register_image(url, alt)
            return "![{}]({})".format(alt, url) if idx else ""
        # Nested <strong>/<em> must not emit nested markers, or the run of
        # asterisks stops parsing as emphasis at all.
        if name in ("strong", "b"):
            self._strong += 1
            inner = "".join(self.inline(c) for c in node.children)
            self._strong -= 1
            if not inner.strip():
                return ""
            return inner if self._strong else "**{}**".format(inner.strip())
        if name in ("em", "i", "cite"):
            self._em += 1
            inner = "".join(self.inline(c) for c in node.children)
            self._em -= 1
            if not inner.strip():
                return ""
            return inner if self._em else "*{}*".format(inner.strip())

        inner = "".join(self.inline(c) for c in node.children)
        if name in ("del", "s", "strike"):
            return "~~{}~~".format(inner.strip()) if inner.strip() else ""
        if name == "code":
            text = node.get_text()
            return "`{}`".format(text.strip()) if text.strip() else ""
        if name in ("kbd", "samp", "var"):
            return "`{}`".format(node.get_text().strip())
        if name == "a":
            href = self.absolute(node.get("href"))
            label = inner.strip()
            if not label:
                return ""
            # An <a> wrapping a whole passage or containing nested links is a
            # layout artifact (common on 小红书 etc.), not a reference.
            if not href or visible_len(label) > 120 or "](" in label:
                return label
            self.links += 1
            return "[{}]({})".format(label, href)
        if name == "sup":
            return "^{}".format(inner.strip())
        if name == "sub":
            return "_{}".format(inner.strip())
        return inner

    def inline_text(self, node):
        text = "".join(self.inline(c) for c in node.children)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        return text.strip()

    # ---- blocks

    @staticmethod
    def has_block_child(node):
        return any(isinstance(c, Tag) and c.name in BLOCK_TAGS for c in node.children)

    def blocks(self, node):
        out = []
        for child in node.children:
            out.extend(self.block(child))
        return [b for b in out if b and b.strip()]

    def block(self, node):
        if isinstance(node, Comment):
            return []
        if isinstance(node, NavigableString):
            text = re.sub(r"\s+", " ", str(node)).strip()
            return [text] if text else []
        if not isinstance(node, Tag):
            return []
        name = node.name

        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self.inline_text(node).replace("\n", " ").strip()
            if not text:
                return []
            level = int(name[1])
            self.headings.append({"level": level, "text": text})
            return ["{} {}".format("#" * level, text)]

        if name == "p":
            text = self.inline_text(node)
            return [text] if text else []

        if name == "pre":
            code = node.get_text("\n")
            code = code.replace("\r\n", "\n").strip("\n")
            if not code.strip():
                return []
            lang = ""
            for source in (node, node.find("code")):
                if not isinstance(source, Tag):
                    continue
                cls = source.get("class") or []
                for c in cls if isinstance(cls, list) else [cls]:
                    m = re.match(r"(?:language|lang|highlight)[-_]([a-z0-9+#]+)", str(c), re.I)
                    if m:
                        lang = m.group(1).lower()
                        break
                if lang:
                    break
            self.code_blocks += 1
            fence = "```"
            while fence in code:
                fence += "`"
            return ["{}{}\n{}\n{}".format(fence, lang, code, fence)]

        if name == "blockquote":
            inner = self.blocks(node) or ([self.inline_text(node)] if self.inline_text(node) else [])
            if not inner:
                return []
            quoted = "\n\n".join(inner)
            return ["\n".join("> " + line if line else ">" for line in quoted.split("\n"))]

        if name in ("ul", "ol"):
            return self.render_list(node)

        if name == "table":
            return self.render_table(node)

        if name == "hr":
            return ["---"]

        if name == "figure":
            parts = []
            for img in node.find_all("img"):
                url = self.image_url(img)
                alt = (img.get("alt") or "").strip()
                if self.register_image(url, alt):
                    parts.append("![{}]({})".format(alt, url))
            cap = node.find("figcaption")
            if cap is not None:
                caption = self.inline_text(cap)
                if caption:
                    parts.append("*{}*".format(caption))
            return parts

        if name == "img":
            url = self.image_url(node)
            alt = (node.get("alt") or "").strip()
            return ["![{}]({})".format(alt, url)] if self.register_image(url, alt) else []

        if name in ("dl",):
            parts = []
            for child in node.find_all(["dt", "dd"], recursive=False):
                text = self.inline_text(child)
                if not text:
                    continue
                parts.append("**{}**".format(text) if child.name == "dt" else text)
            return parts

        if name in ("td", "th", "tr", "tbody", "thead", "tfoot", "caption"):
            return self.blocks(node)

        # generic container
        if self.has_block_child(node):
            return self.blocks(node)
        text = self.inline_text(node)
        return [text] if text else []

    def render_list(self, node, depth=0):
        ordered = node.name == "ol"
        try:
            counter = int(node.get("start", 1))
        except (TypeError, ValueError):
            counter = 1
        lines = []
        for li in node.find_all("li", recursive=False):
            nested = []
            for sub in li.find_all(["ul", "ol"], recursive=False):
                nested.extend(self.render_list(sub, depth + 1))
                sub.extract()
            body_blocks = self.blocks(li) if self.has_block_child(li) else []
            text = "\n\n".join(body_blocks) if body_blocks else self.inline_text(li)
            marker = "{}.".format(counter) if ordered else "-"
            indent = "  " * depth
            if text:
                first, *rest = text.split("\n")
                lines.append("{}{} {}".format(indent, marker, first))
                for extra in rest:
                    lines.append("{}  {}".format(indent, extra) if extra else "")
                counter += 1
            lines.extend(nested)
        return ["\n".join(l for l in lines if l is not None)] if depth == 0 and lines else lines

    def render_table(self, node):
        rows = []
        for tr in node.find_all("tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if not cells:
                continue
            rows.append([self.inline_text(c).replace("\n", " ").replace("|", "\\|") for c in cells])
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            return []
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        self.tables += 1
        head, body = rows[0], rows[1:]
        out = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
        out.extend("| " + " | ".join(r) + " |" for r in body)
        return ["\n".join(out)]


IMAGE_ONLY_RE = re.compile(r"^(!\[[^\]]*\]\([^)]*\)\s*)+$")
# WeChat renders account banners and follow prompts with this image format.
DECOR_IMAGE_RE = re.compile(r"wx_fmt=other|wx_lazy=1&wx_co=1|/0\?wx_fmt=|qrcode", re.I)


def plain(text):
    """Strip markdown emphasis so noise phrases can be matched reliably."""
    out = re.sub(r"[*_`>#]+", "", text)
    return re.sub(r"\s+", " ", out).strip()


def postprocess(blocks):
    cleaned = []
    for block in blocks:
        text = block.strip()
        if not text:
            continue
        flat = plain(text)
        if flat and NOISE_TEXT_RE.match(flat) and len(flat) < 60:
            continue
        if cleaned and cleaned[-1] == text:
            continue
        cleaned.append(text)

    # Trailing promo tail: follow prompts and banner images after the article body.
    while cleaned:
        last = cleaned[-1]
        flat = plain(last)
        is_noise = bool(flat and NOISE_TEXT_RE.match(flat) and len(flat) < 60)
        is_decor_image = bool(IMAGE_ONLY_RE.match(last) and DECOR_IMAGE_RE.search(last))
        if is_noise or is_decor_image or not flat and IMAGE_ONLY_RE.match(last) is None:
            cleaned.pop()
            continue
        break

    body = "\n\n".join(cleaned)
    body = re.sub(r"\*\*\s*\*\*", " ", body)
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip() + "\n"


# -------------------------------------------------------------------- metadata

def meta_content(soup, names=(), props=()):
    for prop in props:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()
    for name in names:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()
    return ""


def json_ld(soup):
    """Merge schema.org blocks; most CMS platforms expose dates only here."""
    merged = {}
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            for sub in ([item] + (item.get("@graph") if isinstance(item.get("@graph"), list) else [])):
                if isinstance(sub, dict):
                    for key, value in sub.items():
                        merged.setdefault(key, value)
    return merged


def flatten_name(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("name") or value.get("@id") or "").strip()
    if isinstance(value, list):
        parts = [flatten_name(v) for v in value]
        return ", ".join(p for p in parts if p)
    return ""


def extract_meta(soup, url, host):
    title = (
        meta_content(soup, ("twitter:title",), ("og:title",))
        or (soup.title.get_text(strip=True) if soup.title else "")
    )
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
    title = re.sub(r"\s+", " ", title).strip()

    site = meta_content(soup, (), ("og:site_name",))
    author = meta_content(soup, ("author", "twitter:creator"), ("article:author",))
    published = meta_content(
        soup, ("pubdate", "publishdate", "date", "citation_publication_date"),
        ("article:published_time", "og:published_time", "og:article:published_time"),
    )
    ld = json_ld(soup)
    if not published:
        published = flatten_name(ld.get("datePublished") or ld.get("dateCreated") or "")
    if not author:
        author = flatten_name(ld.get("author") or ld.get("creator") or "")
    if not site:
        publisher = flatten_name(ld.get("publisher") or "")
        # Legal entity names read worse than the domain ("Wikimedia Foundation, Inc.").
        if publisher and not re.search(r"\b(inc|llc|ltd|gmbh|foundation|corp)\b\.?", publisher, re.I):
            site = publisher
    if not published:
        for sel in ("time[datetime]", "time", "[itemprop=datePublished]",
                    ".post-date", ".entry-date", ".published", ".article-date", ".date"):
            node = soup.select_one(sel)
            if node is None:
                continue
            value = (node.get("datetime") or node.get("content") or node.get_text(" ", strip=True) or "").strip()
            if re.search(r"\d{4}", value):
                published = value
                break
    if not published:
        m = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", url)
        if m:
            published = "-".join(m.groups())

    if re.search(r"mp\.weixin\.qq\.com", host or ""):
        site = site or "微信公众号"
        for sel in ("#js_name", ".rich_media_meta_nickname", "#profileBt a", "a#js_name"):
            node = soup.select_one(sel)
            if node and node.get_text(strip=True):
                account = node.get_text(strip=True)
                site = "{} · 微信公众号".format(account)
                break
        for sel in ("#meta_content .rich_media_meta_text", ".rich_media_meta_text"):
            for node in soup.select(sel):
                text = node.get_text(strip=True)
                if text and not re.search(r"(年|月|日|\d{4}-\d{2})", text) and len(text) < 30:
                    author = author or text
                    break
        for sel in ("#publish_time", "em#publish_time", ".rich_media_meta_text#publish_time"):
            node = soup.select_one(sel)
            if node and node.get_text(strip=True):
                published = published or node.get_text(strip=True)
        if not published:
            m = re.search(r'var\s+(?:ct|createTime)\s*=\s*"?(\d{9,13})"?', str(soup))
            if m:
                ts = int(m.group(1))
                ts = ts / 1000 if ts > 10 ** 11 else ts
                published = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    site = site or (host or "")
    published = normalize_date(published)
    lang = ""
    html_tag = soup.find("html")
    if html_tag:
        lang = (html_tag.get("lang") or "").strip()
    return {
        "title": title,
        "site": site,
        "author": author,
        "published": published,
        "lang": lang,
        "description": meta_content(soup, ("description",), ("og:description",))[:300],
    }


def normalize_date(value):
    if not value:
        return ""
    value = value.strip()
    m = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", value)
    if m:
        return "{}-{:02d}-{:02d}".format(m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{4})[-/年.](\d{1,2})", value)
    if m:
        return "{}-{:02d}".format(m.group(1), int(m.group(2)))
    return value[:40]


def detect_type(url, meta, headings, code_blocks):
    host = (urlparse(url).hostname or "").lower()
    if "arxiv.org" in host or re.search(r"\.pdf($|\?)", url, re.I):
        return "论文"
    if "github.com" in host:
        return "文档"
    if re.search(r"(youtube\.com|youtu\.be|bilibili\.com)", host):
        return "视频"
    if re.search(r"(zhihu\.com/question|stackoverflow\.com|reddit\.com|v2ex\.com)", url):
        return "讨论"
    title = meta.get("title", "")
    if re.search(r"(教程|入门|指南|实战|how ?to|tutorial|guide|快速开始)", title, re.I):
        return "教程"
    if code_blocks >= 3 and re.search(r"(docs?|api|reference|manual)", url, re.I):
        return "文档"
    if re.search(r"(发布|上线|宣布|announc|releas)", title, re.I) and len(headings) < 4:
        return "新闻"
    return "文章"


AD_SRC_RE = re.compile(
    r"(doubleclick|googlesyndication|adservice|adsystem|amazon-adsystem|criteo|"
    r"taboola|outbrain|adnxs|googletagmanager)", re.I)

EMBED_LABEL = {"iframe": "嵌入页面", "video": "视频", "audio": "音频", "embed": "嵌入内容"}


def preserve_embeds(soup, base_url):
    """Keep non-ad embeds (video/iframe/audio) as source links instead of dropping them."""
    for tag in list(soup.find_all(list(EMBED_LABEL))):
        if not is_live(tag):
            continue
        src = tag.get("src") or ""
        if not src:
            child = tag.find("source")
            src = (child.get("src") or "") if child else ""
        src = urljoin(base_url, src) if src else ""
        if not src.startswith(("http://", "https://")) or AD_SRC_RE.search(src):
            continue
        repl = soup.new_tag("p")
        link = soup.new_tag("a", href=src)
        host = urlparse(src).hostname or src[:60]
        link.string = "{}：{}".format(EMBED_LABEL[tag.name], host)
        repl.append(link)
        tag.replace_with(repl)


# --------------------------------------------------------------- clip packages

ASSETS_SUBDIR = "assets"


def clip_dir_for(url, out=None):
    if out:
        return os.path.realpath(os.path.expanduser(out))
    return os.path.join(CLIPS_DIR, hashlib.sha1(canonical_url(url).encode()).hexdigest()[:12])


MARKUP_PREFIXES = (b"<", b"%PDF-", b"\xef\xbb\xbf<")


def download_assets(images, clip_dir, base_name, referer):
    """Pull every image into the package so the clip survives link rot on its own.

    Image urls come from the page and are attacker-controlled, so they go
    through the same redirect-checked fetcher as the page itself, and the bytes
    must be a real raster image — an allowed content-type is not enough.
    """
    if not images:
        return [], []
    asset_dir = os.path.join(clip_dir, ASSETS_SUBDIR)
    os.makedirs(asset_dir, exist_ok=True)
    saved, failed = [], []
    for item in images:
        url = item["url"]
        tmp = None
        try:
            resp, _ = safe_get(url, referer=referer, timeout=25,
                               max_bytes=MAX_ASSET_BYTES, accept="image/*,*/*;q=0.8")
            if resp.status_code >= 400:
                raise RuntimeError("http {}".format(resp.status_code))
            ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype not in IMAGE_EXT:
                raise RuntimeError("content-type not an allowed image: {}".format(ctype or "unknown"))
            data = resp.content
            if not data:
                raise RuntimeError("empty response")
            if data.lstrip()[:8].startswith(MARKUP_PREFIXES):
                raise RuntimeError("payload is markup, not an image")
            ext = IMAGE_EXT[ctype]
            # URL hash keeps names unique once several notes share one attachment dir.
            name = "{}-{}-{:02d}{}".format(
                base_name, hashlib.sha1(url.encode()).hexdigest()[:6], item["index"], ext)
            target = os.path.join(asset_dir, name)
            fd, tmp = tempfile.mkstemp(dir=asset_dir, suffix=".part")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
            tmp = None
            saved.append({"file": name, "url": url, "alt": item.get("alt", ""),
                          "media_type": ctype, "bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
        except Exception as exc:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            failed.append({"url": url[:120], "reason": str(exc)[:120]})
    return saved, failed


def localize_images(body, saved):
    """Rewrite remote image urls to package-relative paths."""
    for item in saved:
        body = body.replace("]({})".format(item["url"]),
                            "]({}/{})".format(ASSETS_SUBDIR, item["file"]))
    return body


def write_package(clip_dir, body, record, raw_html=None):
    """Write the package body-first, manifest last.

    meta.json is the commit marker: a reader that finds it can trust that
    content.md and every listed asset are already on disk.
    """
    os.makedirs(clip_dir, exist_ok=True)
    atomic_write_text(os.path.join(clip_dir, "content.md"), body)
    if raw_html is not None:
        atomic_write_text(os.path.join(clip_dir, "raw.html"), raw_html)
    record["files"] = {
        "content": "content.md",
        "assets_dir": ASSETS_SUBDIR,
        "raw_html": "raw.html" if raw_html is not None else None,
    }
    record["body_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    atomic_write_json(os.path.join(clip_dir, "meta.json"), record)


def build_card(record, body, outline_entries, args):
    outline = [("#" * h["level"] + " " + h["text"])[:60] for h in outline_entries][: args.outline]
    excerpt = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    excerpt = re.sub(r"[#>*`|\[\]()]", "", excerpt)
    excerpt = re.sub(r"\s+", " ", excerpt).strip()[: args.excerpt]
    return {
        "ok": True,
        "clip": record["clip_dir"],
        "content": os.path.join(record["clip_dir"], "content.md"),
        "title": record["title"],
        "url": record["url"],
        "hash": record["content_sha256"],
        "site": record["site"],
        "author": record["author"],
        "published": record["published"],
        "type": record["type"],
        "lang": record["lang"],
        "chars": record["chars"],
        "counts": record["counts"],
        "extractor": record["extractor"],
        "assets": {"saved": len(record["assets"]), "failed": len(record["asset_failures"])},
        "outline": outline,
        "excerpt": excerpt,
        "warnings": record["warnings"],
    }


def base_record(url, final_url, title, clip_dir):
    return {
        "format": PACKAGE_FORMAT,
        "format_version": PACKAGE_VERSION,
        "clip_id": os.path.basename(clip_dir),
        "clip_dir": clip_dir,
        "url": url,
        "final_url": final_url,
        "canonical_url": canonical_url(final_url),
        "title": title,
    }


def stage_pdf(data, final_url, source_url, args, warnings, started):
    """Package a PDF with the same schema as HTML so downstream tools are unaware."""
    clip_dir = clip_dir_for(final_url, args.out)
    asset_dir = os.path.join(clip_dir, ASSETS_SUBDIR)
    os.makedirs(asset_dir, exist_ok=True)
    name = os.path.basename(urlparse(final_url).path)
    title = re.sub(r"\.pdf$", "", name, flags=re.I) or "PDF 文档"
    pdf_name = asset_slug(title) + ".pdf"
    with open(os.path.join(asset_dir, pdf_name), "wb") as fh:
        fh.write(data)
    rel_pdf = "{}/{}".format(ASSETS_SUBDIR, pdf_name)
    body = "[PDF 原文件]({})\n\n来源：<{}>\n".format(rel_pdf, source_url)

    record = base_record(source_url, final_url, title, clip_dir)
    record.update({
        "site": (urlparse(final_url).hostname or "").lower(),
        "author": "",
        "published": "",
        "lang": "",
        "type": detect_type(final_url, {"title": title}, [], 0),
        "extractor": "pdf",
        "kind": "pdf",
        "chars": 0,
        "assets": [{"file": pdf_name, "url": final_url, "alt": "",
                    "media_type": "application/pdf", "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}],
        "asset_failures": [],
        "pdf": rel_pdf,
        "pdf_bytes": len(data),
        "counts": {"images": 0, "links": 1, "code_blocks": 0, "tables": 0, "headings": 0},
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "elapsed_ms": int((time.time() - started) * 1000),
        "warnings": warnings + ["pdf packaged; content.md is a placeholder linking the file"],
    })
    write_package(clip_dir, body, record)
    card = build_card(record, body, [], args)
    card["kind"] = "pdf"
    card["pdf"] = os.path.join(clip_dir, rel_pdf)
    card["hint"] = "正文是占位链接；需要写摘要时用 Read 读上面的 pdf"
    emit(card)


def process_html(html, final_url, source_url, args, warnings, started):
    host = (urlparse(final_url).hostname or "").lower()
    soup = BeautifulSoup(html, "html.parser")
    meta = extract_meta(soup, final_url, host)

    # og:image must be read before strip_noise removes every <meta> tag.
    og_images = []
    for tag in soup.find_all("meta", property="og:image"):
        u = (tag.get("content") or "").strip()
        if u.startswith("//"):
            u = "http:" + u
        if u.startswith("http") and u not in og_images:
            og_images.append(u)

    preserve_embeds(soup, final_url)
    strip_noise(soup)
    main, extractor, extra = pick_main(soup, host)
    warnings.extend(extra)
    strip_noise(main, aggressive=True)
    if extractor == "xiaohongshu":
        # 小红书把整段正文放在一个带字面换行的文本节点里；换行即分行。
        for tnode in list(main.find_all(string=True)):
            if isinstance(tnode, Comment) or "\n" not in tnode:
                continue
            wrapper = soup.new_tag("span")
            for j, piece in enumerate(str(tnode).split("\n")):
                if j:
                    wrapper.append(soup.new_tag("br"))
                if piece:
                    wrapper.append(NavigableString(piece))
            tnode.replace_with(wrapper)

    conv = Converter(final_url)
    body = postprocess(conv.blocks(main))
    if not conv.images:
        # 小红书 / X 的配图常不在正文 DOM，而在 og:image（CDN 链接会过期）。
        # 站点 logo、头像不在这些域名模式里，会被自然排除。
        og_imgs = [u for u in og_images
                   if "xhscdn.com" in u or "pbs.twimg.com/media" in u]
        if og_imgs:
            gallery = "\n\n".join("![]({})".format(u) for u in og_imgs)
            body = gallery + "\n\n" + body if body else gallery
            for u in og_imgs:
                conv.register_image(u, "")
    # Images dropped by noise cleanup must not be downloaded as orphans.
    images = [img for img in conv.images if img["url"] in body]
    for position, img in enumerate(images, 1):
        img["index"] = position
    if visible_len(body) < 200:
        warnings.append("body under 200 chars; page is likely JS-rendered — use browser tools then `ingest`")

    clip_dir = clip_dir_for(final_url, args.out)
    # Identity is the extracted text, fixed before any asset rewriting: whether
    # an image download happened to succeed must not change the clip's hash,
    # or re-clipping the same page would look like new content.
    content_sha256 = hashlib.sha256(body.encode()).hexdigest()
    saved, failed = ([], [])
    if args.assets != "none":
        saved, failed = download_assets(images, clip_dir, asset_slug(meta["title"]), final_url)
        body = localize_images(body, saved)
    if failed:
        body = body.rstrip() + "\n\n> 说明：{} 张图片未能保存（防盗链或已失效），可访问原文查看。\n".format(len(failed))

    record = base_record(source_url, final_url, meta["title"], clip_dir)
    record.update({
        "site": meta["site"],
        "author": meta["author"],
        "published": meta["published"],
        "lang": meta["lang"],
        "type": detect_type(final_url, meta, conv.headings, conv.code_blocks),
        "extractor": extractor,
        "kind": "page",
        "chars": visible_len(body),
        "assets": saved,
        "asset_failures": failed,
        "counts": {
            "images": len(images),
            "links": conv.links,
            "code_blocks": conv.code_blocks,
            "tables": conv.tables,
            "headings": len(conv.headings),
        },
        "content_sha256": content_sha256,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "elapsed_ms": int((time.time() - started) * 1000),
        "warnings": warnings,
    })
    write_package(clip_dir, body, record, raw_html=html if args.keep_raw else None)
    emit(build_card(record, body, conv.headings, args))


# -------------------------------------------------------------------- commands

def cmd_fetch(args):
    os.makedirs(CLIPS_DIR, exist_ok=True)
    gc_clips(gc_days())
    started = time.time()
    warnings = []
    try:
        resp, final_url, ctype, blocked = fetch_html(args.url)
    except FetchRefused as exc:
        # A refusal is a policy decision, not a transient failure; the browser
        # fallback would bypass exactly the check that just fired.
        fail("refused to fetch: {}".format(exc), url=args.url,
             hint="只抓公网 http(s) 地址；本地/内网地址、非图片附件和超大响应会被拒绝")
    except Exception as exc:
        fail("fetch failed: {}".format(exc), url=args.url,
             hint="use WebFetch or the browser tools, then `clip.py ingest --file <path> --url <url>`")

    if resp.status_code in (404, 410):
        # A dead link is not an anti-bot wall; sending the agent down the
        # browser fallback would just waste a round trip.
        fail("page not found (http {})".format(resp.status_code), url=args.url,
             hint="链接可能已失效或拼错，请与用户确认，不要改走 ingest")
    if resp.status_code >= 400 or blocked:
        fail("blocked by anti-bot or login wall (http {})".format(resp.status_code),
             url=args.url,
             hint="fall back to WebFetch (it writes to a file — do not read it), then "
                  "`clip.py ingest --file <that path> --url <url>`")

    # Magic-number check is authoritative: .pdf urls serving HTML fall through
    # to the HTML pipeline, and pdf content on any url is packaged as pdf.
    if resp.content[:5] == b"%PDF-":
        if len(resp.content) > MAX_PDF_BYTES:
            fail("pdf exceeds {} MB cap".format(MAX_PDF_BYTES // (1024 * 1024)), url=args.url)
        stage_pdf(resp.content, final_url, args.url, args, warnings, started)
        return

    process_html(resp.text, final_url, args.url, args, warnings, started)


def cmd_ingest(args):
    """Consume HTML or markdown already on disk, so the model never reads it."""
    os.makedirs(CLIPS_DIR, exist_ok=True)
    gc_clips(gc_days())
    started = time.time()
    path = os.path.expanduser(args.file)
    if not os.path.isfile(path):
        fail("no such file: {}".format(path))
    with open(path, encoding="utf-8", errors="ignore") as fh:
        text = fh.read()
    warnings = ["ingested from {}".format(os.path.basename(path))]
    looks_html = bool(re.search(r"<(html|body|div|section|article|p)\b", text[:4000], re.I))
    if looks_html:
        process_html(text, args.url, args.url, args, warnings, started)
        return

    # Pre-converted markdown (e.g. a WebFetch dump): strip noise lines only.
    lines, kept = text.splitlines(), []
    for line in lines:
        flat = re.sub(r"\s+", " ", line).strip()
        if flat and NOISE_TEXT_RE.match(flat) and len(flat) < 60:
            continue
        kept.append(line)
    body = postprocess([b for b in re.split(r"\n{2,}", "\n".join(kept))])
    headings = [{"level": len(m.group(1)), "text": m.group(2).strip()}
                for m in re.finditer(r"^(#{1,6})\s+(.+)$", body, re.M)]
    images = [{"index": i + 1, "url": m.group(2), "alt": m.group(1)}
              for i, m in enumerate(re.finditer(r"!\[([^\]]*)\]\((https?://[^)]+)\)", body))]
    title = args.title or (headings[0]["text"] if headings else "")
    host = (urlparse(args.url).hostname or "").lower()

    clip_dir = clip_dir_for(args.url, args.out)
    content_sha256 = hashlib.sha256(body.encode()).hexdigest()
    saved, failed = ([], [])
    if args.assets != "none":
        saved, failed = download_assets(images, clip_dir, asset_slug(title), args.url)
        body = localize_images(body, saved)

    record = base_record(args.url, args.url, title, clip_dir)
    record.update({
        "site": args.site or host,
        "author": args.author or "",
        "published": normalize_date(args.published or ""),
        "lang": "",
        "type": detect_type(args.url, {"title": title}, headings, 0),
        "extractor": "ingest:markdown",
        "kind": "page",
        "chars": visible_len(body),
        "assets": saved,
        "asset_failures": failed,
        "counts": {
            "images": len(images),
            "links": len(re.findall(r"(?<!!)\[[^\]]+\]\(", body)),
            "code_blocks": body.count("```") // 2,
            "tables": len(re.findall(r"^\|.+\|$", body, re.M)) and 1 or 0,
            "headings": len(headings),
        },
        "content_sha256": content_sha256,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "elapsed_ms": int((time.time() - started) * 1000),
        "warnings": warnings,
    })
    write_package(clip_dir, body, record)
    emit(build_card(record, body, headings, args))


def cmd_list(args):
    items = []
    for name in sorted(os.listdir(CLIPS_DIR)) if os.path.isdir(CLIPS_DIR) else []:
        meta_path = os.path.join(CLIPS_DIR, name, "meta.json")
        if not os.path.isfile(meta_path):
            continue
        try:
            with open(meta_path, encoding="utf-8") as fh:
                record = json.load(fh)
        except Exception:
            continue
        items.append({"clip": os.path.join(CLIPS_DIR, name), "title": record.get("title", ""),
                      "url": record.get("url", ""), "fetched_at": record.get("fetched_at", ""),
                      "chars": record.get("chars", 0)})
    emit({"ok": True, "workspace": CLIPS_DIR, "clips": items})


def cmd_verify(args):
    """Check a package against its own manifest before anything files it."""
    clip = os.path.realpath(os.path.expanduser(args.clip))
    meta_path = os.path.join(clip, "meta.json")
    if not os.path.isfile(meta_path):
        fail("no meta.json at {}".format(clip))
    try:
        with open(meta_path, encoding="utf-8") as fh:
            record = json.load(fh)
    except ValueError as exc:
        fail("unreadable manifest: {}".format(exc), clip=clip)

    problems = []
    if record.get("format") != PACKAGE_FORMAT:
        problems.append("format is {!r}, expected {!r}".format(record.get("format"), PACKAGE_FORMAT))
    files = record.get("files") or {}
    body_path = os.path.join(clip, files.get("content") or "content.md")
    if not os.path.isfile(body_path):
        problems.append("missing body at {}".format(files.get("content") or "content.md"))
    else:
        with open(body_path, encoding="utf-8") as fh:
            body = fh.read()
        if record.get("body_sha256") and \
                hashlib.sha256(body.encode()).hexdigest() != record["body_sha256"]:
            problems.append("content.md does not match body_sha256")
    checked = 0
    for asset in record.get("assets") or []:
        path = os.path.join(clip, files.get("assets_dir") or ASSETS_SUBDIR, asset["file"])
        if not os.path.isfile(path):
            problems.append("missing asset {}".format(asset["file"]))
            continue
        if asset.get("sha256"):
            with open(path, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() != asset["sha256"]:
                    problems.append("asset {} does not match its hash".format(asset["file"]))
        checked += 1
    emit({"ok": not problems, "clip": clip, "format_version": record.get("format_version"),
          "assets_checked": checked, "problems": problems})
    if problems:
        sys.exit(1)


def cmd_gc(args):
    days = 0 if args.all else (args.days if args.days is not None else gc_days())
    emit({"ok": True, "workspace": CLIPS_DIR, "older_than_days": days,
          "released": gc_clips(days)})


def cmd_release(args):
    target = os.path.realpath(os.path.expanduser(args.clip))
    workspace = os.path.realpath(CLIPS_DIR)
    if os.path.commonpath([target, workspace]) != workspace or target == workspace:
        fail("release only removes clips inside the workspace", clip=target, workspace=workspace)
    if not os.path.isdir(target):
        fail("no such clip: {}".format(target))
    shutil.rmtree(target, ignore_errors=True)
    emit({"ok": True, "released": target})


def main():
    parser = argparse.ArgumentParser(prog="clip.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="fetch a url into a clip package and print a card")
    f.add_argument("url")
    f.add_argument("--out", help="write the package here instead of the workspace")
    f.add_argument("--assets", choices=["download", "none"], default="download")
    f.add_argument("--outline", type=int, default=12, help="max outline entries in the card")
    f.add_argument("--excerpt", type=int, default=160, help="excerpt length in chars")
    f.add_argument("--keep-raw", action="store_true", default=True)
    f.add_argument("--no-keep-raw", dest="keep_raw", action="store_false")
    f.set_defaults(func=cmd_fetch)

    g = sub.add_parser("ingest", help="package HTML/markdown already on disk")
    g.add_argument("--file", required=True)
    g.add_argument("--url", required=True)
    g.add_argument("--title")
    g.add_argument("--site")
    g.add_argument("--author")
    g.add_argument("--published")
    g.add_argument("--out")
    g.add_argument("--assets", choices=["download", "none"], default="download")
    g.add_argument("--outline", type=int, default=14)
    g.add_argument("--excerpt", type=int, default=180)
    g.add_argument("--keep-raw", action="store_true", default=False)
    g.set_defaults(func=cmd_ingest)

    l = sub.add_parser("list", help="list clip packages in the workspace")
    l.set_defaults(func=cmd_list)

    v = sub.add_parser("verify", help="check a package against its own manifest")
    v.add_argument("clip", help="clip package path")
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("release", help="delete a clip package from the workspace")
    r.add_argument("clip", help="clip package path")
    r.set_defaults(func=cmd_release)

    c = sub.add_parser("gc", help="drop clip packages older than N days")
    c.add_argument("--days", type=int, help="age threshold, default {}".format(DEFAULT_GC_DAYS))
    c.add_argument("--all", action="store_true", help="empty the workspace")
    c.set_defaults(func=cmd_gc)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
