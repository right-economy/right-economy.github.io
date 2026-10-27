#!/usr/bin/env python3
"""빌드 산출물(dist)의 SEO 메타를 실측한다.

왜 정규식이 아니라 html.parser 인가 — 2026-10-10 에 정규식 프로브로 같은 점검을
돌렸다가 한 파일에서 버그 셋이 났다. `content=["\']` 패턴이 아포스트로피로 시작하는
본문에서 끊겨 「description 누락 9건」(실제 0)이 나왔고, 글롭 중복으로 「중복 2페이지」
(실제 0)가 나왔다. 멀쩡한 글 8개를 덮어쓰는 PR 을 열 뻔했다. 파서로 읽으면 속성
경계를 파서가 정하므로 그 계열 오탐이 원리적으로 안 생긴다.

실행하면 **자체 점검(양성·음성 표본)을 같은 실행 안에서 먼저 돌린다.** 검사기가
죽으면 실물에서 0건이 깨끗하게 찍히기 때문에, 음성(불량) 표본이 전부 걸리고
양성(정상) 표본이 하나도 안 걸리는 것까지 확인한 뒤에만 실물을 잰다.

사용법:
    python3 scripts/seo_audit.py [dist 경로]
종료코드:
    0 = 실질 이슈 없음 / 1 = 이슈 있음 / 2 = 자체 점검 실패(검사기 고장)
"""

import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

SITE = "https://right-economy.com"

# 길이 권고치. 경계가 아니라 「잘릴 위험 구간」이다 — 넘으면 보고만 하고 차단하지 않는다.
TITLE_SOFT_MAX = 60
DESC_SOFT_MAX = 160

# 페이지가 아닌 html 은 제외한다. 이유로 적는다 — 「개수」로 적으면 다음 사람이
# 숫자를 올려 진짜 결함을 같이 묻는다.
#   google<토큰>.html = 서치콘솔 소유확인 파일. 구글이 받은 그대로여야 하므로
#   title·description·canonical 이 없는 게 정상이고, 고치면 확인이 깨진다.
def is_page(rel):
    base = os.path.basename(rel)
    return not (base.startswith("google") and base.endswith(".html") and "/" not in rel)


class Meta(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.title = None
        self._in_title = False
        self.description = None
        self.canonical = None
        self.og = {}
        self.twitter = {}
        self.robots = None
        self.h1 = 0
        self.headings = []
        self.img_total = 0
        self.img_no_alt = 0
        self.jsonld = 0
        # ld+json 의 **본문**도 모은다. 개수만 세면 「있다」는 보지만 「해석된다」는
        # 못 본다 — 깨진 JSON-LD 는 검색엔진이 블록 전체를 버리는데 개수는 1 이다.
        self.ldjson = []
        self._in_ldjson = False
        self.lang = None
        self.internal_hrefs = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or "").lower()
            prop = (a.get("property") or "").lower()
            content = a.get("content")
            if name == "description":
                self.description = content
            elif name == "robots":
                self.robots = content
            elif prop.startswith("og:"):
                self.og[prop] = content
            elif name.startswith("twitter:"):
                self.twitter[name] = content
        elif tag == "link":
            if (a.get("rel") or "").lower() == "canonical":
                self.canonical = a.get("href")
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self.headings.append(int(tag[1]))
            if tag == "h1":
                self.h1 += 1
        elif tag == "img":
            self.img_total += 1
            # alt 속성 자체가 없으면 누락. alt="" 는 장식 이미지의 올바른 표기라 통과.
            if "alt" not in a:
                self.img_no_alt += 1
        elif tag == "script":
            if (a.get("type") or "").strip().lower() == "application/ld+json":
                self.jsonld += 1
                self._in_ldjson = True
                self.ldjson.append("")
        elif tag == "a":
            href = a.get("href")
            # 사이트 안쪽 링크만 센다. 스킴이 붙은 외부 링크·mailto·앵커만 있는
            # href 는 유입 그래프의 변이 아니다.
            if href and href.startswith("/"):
                self.internal_hrefs.append(href)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ldjson = False

    def handle_data(self, data):
        if self._in_title:
            self.title = ((self.title or "") + data)
        if self._in_ldjson and self.ldjson:
            self.ldjson[-1] += data


def parse(html):
    m = Meta()
    m.feed(html)
    if m.title is not None:
        m.title = m.title.strip()
    return m


def _ldjson_nodes(blocks):
    """ld+json 블록들에서 노드를 평평하게 꺼낸다. (노드목록, 깨진블록수)

    @graph · 최상위 배열 · 단일 객체 세 모양이 다 쓰여서 한 자리에서 펼친다.
    깨진 블록은 **세어서 돌려준다** — 건너뛰고 0건으로 만들면 검사가 조용히 통과한다.
    """
    nodes, broken = [], 0
    for b in blocks:
        try:
            d = json.loads(b)
        except Exception:
            broken += 1
            continue
        for o in (d if isinstance(d, list) else [d]):
            if not isinstance(o, dict):
                continue
            g = o.get("@graph")
            for n in (g if isinstance(g, list) else [o]):
                if isinstance(n, dict):
                    nodes.append(n)
    return nodes, broken


ARTICLE_TYPES = ("Article", "BlogPosting", "NewsArticle")


def check_breadcrumb(m):
    """BreadcrumbList 의 내용이 쓸 수 있는 모양인가.

    왜 개수로 안 되나: `jsonld == 0` 검사는 「블록이 있나」만 본다. 빵가루는
    있는데 position 이 어긋나거나 item 이 상대경로면 검색엔진이 계층을 못 읽고
    리치결과에서 조용히 빠진다. 블록 수는 그대로 1 이라 기존 검사에 안 걸린다.

    ⚠️ 홈(`/`)과 404 에는 빵가루가 없는 게 맞다 — 조상이 없는 자리다.
       그래서 「없다」를 여기서 이슈로 적지 않는다. 있을 때 모양만 본다.
    """
    out = []
    nodes, broken = _ldjson_nodes(m.ldjson)
    if broken:
        out.append(("jsonld-broken", "ld+json 블록 %d개가 파싱되지 않는다 — 검색엔진은 블록 전체를 버린다" % broken))
    for bc in [n for n in nodes if n.get("@type") == "BreadcrumbList"]:
        items = bc.get("itemListElement")
        if not isinstance(items, list) or not items:
            out.append(("breadcrumb-empty", "BreadcrumbList 에 itemListElement 가 없다"))
            continue
        pos = [it.get("position") if isinstance(it, dict) else None for it in items]
        if pos != list(range(1, len(items) + 1)):
            out.append(("breadcrumb-position", "position 이 1..%d 가 아니다: %r" % (len(items), pos)))
        for it in items:
            if not isinstance(it, dict):
                out.append(("breadcrumb-item", "itemListElement 항목이 객체가 아니다"))
                continue
            if not (it.get("name") or "").strip():
                out.append(("breadcrumb-item", "name 이 비어 있다 (position %r)" % (it.get("position"),)))
            target = it.get("item")
            if target is None:
                # 마지막 칸은 item 을 생략해도 된다(구글 권고). 중간 칸이면 링크가 끊긴다.
                if it.get("position") != len(items):
                    out.append(("breadcrumb-item", "중간 칸에 item 이 없다 (position %r)" % (it.get("position"),)))
                continue
            url = target.get("@id") if isinstance(target, dict) else target
            if not isinstance(url, str) or not url.startswith(SITE):
                out.append(("breadcrumb-item", "item 이 %s 로 시작하는 절대 URL 이 아니다: %r" % (SITE, url)))
    return out


def check_page(m):
    """한 페이지의 이슈를 (id, 설명) 목록으로 돌려준다."""
    out = []
    if not (m.title or "").strip():
        out.append(("title-missing", "<title> 이 없거나 비어 있다"))
    elif len(m.title) > TITLE_SOFT_MAX:
        out.append(("title-long", "title %d자 (권고 %d자)" % (len(m.title), TITLE_SOFT_MAX)))

    if not (m.description or "").strip():
        out.append(("desc-missing", "meta description 이 없거나 비어 있다"))
    elif len(m.description) > DESC_SOFT_MAX:
        out.append(("desc-long", "description %d자 (권고 %d자)" % (len(m.description), DESC_SOFT_MAX)))

    if not m.canonical:
        out.append(("canonical-missing", "canonical 이 없다"))
    elif not m.canonical.startswith(SITE):
        out.append(("canonical-not-site", "canonical 이 %s 로 시작하지 않는다: %s" % (SITE, m.canonical)))

    for key in ("og:title", "og:description", "og:url", "og:image"):
        if not (m.og.get(key) or "").strip():
            out.append(("og-missing", "%s 가 없다" % key))

    if not (m.twitter.get("twitter:card") or "").strip():
        out.append(("twitter-missing", "twitter:card 가 없다"))

    if m.h1 == 0:
        out.append(("h1-none", "h1 이 없다"))
    elif m.h1 > 1:
        out.append(("h1-multi", "h1 이 %d개" % m.h1))

    # heading 레벨 건너뛰기(h2→h4 등). h1 유일성과 다른 축이다 — 개수는 맞는데
    # 순서가 어긋나면 스크린리더·크롤러가 보는 문서 구조가 실제와 달라진다.
    for prev, nxt in zip(m.headings, m.headings[1:]):
        if nxt > prev + 1:
            out.append(("heading-skip", "heading 레벨을 건너뛴다: h%d → h%d" % (prev, nxt)))

    if m.img_no_alt:
        out.append(("img-no-alt", "alt 속성 없는 img %d개 (전체 %d)" % (m.img_no_alt, m.img_total)))

    out += check_breadcrumb(m)
    out += check_identity(m)
    return out


def check_identity(m):
    """같은 페이지를 가리키는 주소 셋이 같은 자리를 가리키나.

    canonical(검색결과) · og:url(공유 카드) · Article mainEntityOfPage(리치
    스니펫) 셋이 어긋나면 **태그는 전부 있는 채로** 서로 다른 자리를 가리킨다.
    「있나」만 세는 검사로는 영영 안 보인다. 빵가루 검사가 「블록이 있나」에서
    「해석되는 모양인가」로 간 것과 같은 자리다.

    끝 슬래시는 같은 자리의 두 표기라 벗겨서 비교한다.
    """
    out = []

    og_url = (m.og.get("og:url") or "").strip()
    if m.canonical and og_url and m.canonical.rstrip("/") != og_url.rstrip("/"):
        out.append(("ogurl-ne-canonical",
                    "og:url 과 canonical 이 다른 자리를 가리킨다: og=%s / canonical=%s"
                    % (og_url, m.canonical)))

    # lang 이 없으면 크롤러와 스크린리더가 언어를 추측한다. 한국어 본문에 영어로
    # 추측이 붙으면 검색결과에서 번역본처럼 취급될 수 있다.
    if m.lang is None:
        out.append(("lang-missing", "<html> 에 lang 속성이 없다"))
    elif not m.lang.strip().lower().startswith("ko"):
        out.append(("lang-unexpected", "<html lang> 이 ko 계열이 아니다: %s" % m.lang))

    nodes, _broken = _ldjson_nodes(m.ldjson)
    for node in [n for n in nodes if n.get("@type") in ARTICLE_TYPES]:
        headline = (node.get("headline") or "").strip()
        if headline and (m.title or "") and headline not in m.title:
            out.append(("headline-ne-title",
                        "Article headline 이 <title> 안에 없다: headline=%s / title=%s"
                        % (headline, m.title)))
        moe = node.get("mainEntityOfPage")
        moe_url = moe.get("@id") if isinstance(moe, dict) else moe
        if moe_url and m.canonical and str(moe_url).rstrip("/") != m.canonical.rstrip("/"):
            out.append(("moe-ne-canonical",
                        "mainEntityOfPage 가 canonical 과 다르다: %s / canonical=%s"
                        % (moe_url, m.canonical)))
    return out


GOOD = """<!doctype html><html lang="ko"><head>
<title>좋은 제목</title>
<meta name="description" content="It's a description with an apostrophe and \"quotes\"">
<link rel="canonical" href="https://right-economy.com/posts/x/">
<meta property="og:title" content="t"><meta property="og:description" content="d">
<meta property="og:url" content="https://right-economy.com/posts/x"><meta property="og:image" content="i">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[
{"@type":"BreadcrumbList","itemListElement":[
{"@type":"ListItem","position":1,"name":"홈","item":{"@id":"https://right-economy.com/"}},
{"@type":"ListItem","position":2,"name":"글"}]},
{"@type":"Article","headline":"좋은 제목",
"mainEntityOfPage":{"@type":"WebPage","@id":"https://right-economy.com/posts/x/"}}]}</script>
</head><body><h1>제목</h1><h2>소제목</h2><h3>더 안</h3><h2>소제목2</h2>
<img src="a.svg" alt="설명"><img src="b.svg" alt=""></body></html>"""

BAD = """<!doctype html><html><head>
<title></title>
<link rel="canonical" href="/posts/x/">
<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":[
{"@type":"ListItem","position":1,"name":"","item":"/posts/x/"},
{"@type":"ListItem","position":5,"name":"글"}]}</script>
<script type="application/ld+json">{"@type":"BreadcrumbList", 이건 JSON 이 아니다}</script>
</head><body><h1>a</h1><h1>b</h1><h4>c</h4><img src="a.svg"></body></html>"""

# 세 번째 표본. BAD 와 합칠 수 없다 — BAD 는 title 이 비어야 `title-missing` 이
# 걸리는데 headline 대조는 **title 이 있어야** 할 수 있는 검사다. 한 장에 몰면
# 둘 중 하나가 반드시 못 걸리고, 그 자리는 조용히 0건으로 남는다.
# JSON-LD 모양도 GOOD(@graph) 과 갈라 최상위 배열로 둬서 둘 다 읽히는지 본다.
BAD_IDENTITY = """<!doctype html><html lang="en"><head>
<title>진짜 제목</title>
<meta name="description" content="설명">
<link rel="canonical" href="https://right-economy.com/posts/a/">
<meta property="og:title" content="t"><meta property="og:description" content="d">
<meta property="og:url" content="https://right-economy.com/posts/b/">
<meta property="og:image" content="i">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">[{"@type":"Article","headline":"다른 헤드라인",
"mainEntityOfPage":{"@type":"WebPage","@id":"https://right-economy.com/posts/c/"}}]</script>
</head><body><h1>제목</h1><h2>소제목</h2><img src="a.svg" alt=""></body></html>"""


SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# ET 는 expat(C 확장) 위에서만 돈다. 이 맥의 homebrew python 3.14.7 은 pyexpat 가
# 시스템 libexpat 심볼을 못 찾아 `ET.parse` 가 ImportError 로 죽는다(2026-10-14 실측:
# "No module named expat"). 그러면 sitemap 축만 못 재는 게 아니라 **자체 점검이
# 터져서 감사 전체가 종료코드 1 로 멈춘다** — 실제로 이 슬롯에서 그렇게 멈췄다.
# 그래서 `<loc>` 추출은 순수 파이썬 파서로도 되게 둔다. html.parser 는 표준
# 라이브러리에 순수 파이썬으로 들어있어 같은 방식으로 깨지지 않고, 속성·텍스트
# 경계를 파서가 정하는 점은 ET 와 같다(= 이 파일이 정규식을 안 쓰는 이유 그대로).
# ⚠️ 대신 fallback 은 XML 적격성(well-formedness)을 검사하지 않는다. 망가진
# sitemap 이면 loc 집합이 모자라게 나오고, 그건 sitemap-vs-noindex 로 시끄럽게
# 드러난다(조용히 0건이 되지는 않는다).
def _expat_ok():
    try:
        ET.XMLParser()
        return True
    except ImportError:
        return False


class _Locs(HTMLParser):
    """sitemap 의 `<loc>` 텍스트만 모은다. 네임스페이스 접두사가 붙어도
    (`<s:loc>`) 같이 받는다."""

    def __init__(self):
        HTMLParser.__init__(self)
        self.locs = []
        self._buf = None

    def handle_starttag(self, tag, attrs):
        if tag == "loc" or tag.endswith(":loc"):
            self._buf = []

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if (tag == "loc" or tag.endswith(":loc")) and self._buf is not None:
            self.locs.append("".join(self._buf).strip())
            self._buf = None


def _locs_et(text):
    root = ET.fromstring(text)
    return [(e.text or "").strip() for e in root.findall("s:url/s:loc", SITEMAP_NS)]


def _locs_parser(text):
    p = _Locs()
    p.feed(text)
    p.close()
    return p.locs


def _locs_in(text):
    """sitemap XML 한 장에서 loc 문자열 목록. expat 이 있으면 ET 로 읽고,
    없으면 순수 파이썬 파서로 읽는다. 두 경로가 같은 답을 내는지는
    `selftest_locs` 가 매 실행에서 대조한다."""
    return _locs_et(text) if _expat_ok() else _locs_parser(text)


# 단일-글 태그 페이지는 thin/중복이라 ①sitemap 에서 빼고 ②noindex 를 박는다.
# 결정이 두 자리에 나뉘어 있어서 **한쪽만 바뀌면 조용히 어긋난다** —
#   sitemap 만 빼고 noindex 를 안 박으면 얇은 페이지가 계속 색인되고,
#   noindex 만 박고 sitemap 에 남기면 색인 거부 URL 을 제출하게 된다.
# 둘 다 에러를 안 내므로 사람이 볼 일이 없다. 그래서 「둘이 같은 집합인가」를 센다.
# 기준은 astro.config.mjs 의 sitemap filter(글 2개 이상)와 같아야 한다.
TAG_MIN_POSTS = 2


def tag_post_counts(posts_dir):
    """태그별 글 수. 원고를 못 읽으면 0 이 아니라 None(=모름)을 돌려준다."""
    if not os.path.isdir(posts_dir):
        return None
    counts = {}
    for name in sorted(os.listdir(posts_dir)):
        if not name.endswith(".md"):
            continue
        with open(os.path.join(posts_dir, name), encoding="utf-8") as fh:
            raw = fh.read()
        m = re.search(r"^tags:\s*\[([^\]]*)\]", raw, re.M)
        if not m:
            continue
        for t in m.group(1).split(","):
            t = t.strip().strip("\"'")
            if t:
                counts[t] = counts.get(t, 0) + 1
    return counts or None


def sitemap_paths(dist):
    """sitemap 의 URL 경로 집합. 한국어 경로가 퍼센트 인코딩돼 들어오므로
    **반드시 디코드해서 맞춘다** — 안 맞추면 같은 URL 이 양쪽에 다르게 앉아
    「447장 빠졌다」 같은 허수가 나온다(2026-10-13 내가 밟음). 없으면 None."""
    files = [
        os.path.join(dist, f)
        for f in sorted(os.listdir(dist))
        if f.startswith("sitemap") and f.endswith(".xml") and "index" not in f
    ] if os.path.isdir(dist) else []
    if not files:
        return None
    paths = set()
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for loc in _locs_in(fh.read()):
                paths.add(unquote(urlparse(loc).path))
    return paths or None


def check_site(dist, posts_dir):
    """페이지 한 장으로는 안 보이는 축 — sitemap 과 noindex 가 같은 집합인가."""
    out = []
    counts = tag_post_counts(posts_dir)
    sm = sitemap_paths(dist)
    if counts is None:
        out.append(("tagcount-unknown", "원고에서 태그 수를 못 읽었다 — 정합성을 못 쟀다(0건이 아니라 모름)"))
    if sm is None:
        out.append(("sitemap-unreadable", "sitemap 을 못 읽었다 — 정합성을 못 쟀다(0건이 아니라 모름)"))
    if counts is None or sm is None:
        return out

    # index.html 만 본다. 404.html 처럼 index.html 이 아닌 산출물은 애초에
    # sitemap 대상이 아니라서, 같이 세면 「제외됐는데 색인 허용」 허수가 된다.
    noindex, indexable = set(), set()
    for root, _dirs, files in os.walk(dist):
        if "index.html" not in files:
            continue
        rel = os.path.relpath(root, dist).replace(os.sep, "/")
        path = "/" if rel == "." else "/%s/" % rel
        with open(os.path.join(root, "index.html"), encoding="utf-8", errors="replace") as fh:
            robots = (parse(fh.read()).robots or "")
        blocked = "noindex" in robots.lower()
        (noindex if blocked else indexable).add(path)

        if path.startswith("/tags/"):
            tag = unquote(path[len("/tags/"):].rstrip("/"))
            thin = counts.get(tag, 0) < TAG_MIN_POSTS
            if thin and not blocked:
                out.append(("thin-tag-indexable",
                            "글 %d개뿐인 태그인데 noindex 가 없다: %s" % (counts.get(tag, 0), path)))
            elif not thin and blocked:
                out.append(("noindex-on-listed-tag",
                            "글 %d개 묶인 태그인데 noindex 다: %s" % (counts.get(tag, 0), path)))
        elif blocked:
            out.append(("noindex-on-real-page", "글·허브인데 noindex 다: %s" % path))

    excluded = (noindex | indexable) - sm
    for path in sorted(excluded - noindex)[:20]:
        out.append(("sitemap-vs-noindex", "sitemap 에서 빠졌는데 색인은 허용한다: %s" % path))
    for path in sorted(noindex - excluded)[:20]:
        out.append(("sitemap-vs-noindex", "noindex 인데 sitemap 에 제출한다: %s" % path))
    return out


def _route_of(dist, root):
    """dist 안의 디렉터리를 사이트 경로로 바꾼다."""
    rel = os.path.relpath(root, dist).replace(os.sep, "/")
    return "/" if rel == "." else "/%s/" % rel


def _normalize_href(href):
    """내부 href 를 라우트 모양으로 맞춘다. 맞춰두지 않으면 같은 글이
    `/posts/a`·`/posts/a/`·`/posts/a/#요약` 세 모양으로 갈려 **유입이 있는 글이
    고아로 잡힌다**(허수 쪽으로 틀리는 방향이라 더 위험하다)."""
    href = href.split("#")[0].split("?")[0]
    if not href:
        return None
    if not href.endswith("/"):
        href += "/"
    return unquote(href)


# 페이지 한 장만 보면 안 보이는 축 둘.
#   ① 고아 글 — 어느 페이지에서도 <a> 로 안 걸리는 글. sitemap 에 있으니 색인은
#      되지만 크롤러가 따라올 길이 없고 내부 링크 가중치도 못 받는다. 페이지
#      자체는 메타가 완벽해도 걸리지 않으므로 check_page 로는 영영 안 보인다.
#   ② JSON-LD 누락 — 글 페이지에 Article 구조화 데이터가 없으면 검색결과 리치
#      스니펫 대상에서 빠진다.
#   ③ 깨진 내부 링크 — 가리키는 자리가 dist 에 없는 `<a href="/…">`. ①이 「받는
#      쪽이 0인가」를 세는 반면 이건 **보내는 쪽이 허공을 가리키나**다. 같은
#      href 목록을 읽고도 서로 안 보이는 축이라, 유입 그래프가 멀쩡해도 오타
#      하나가 조용히 404 로 남는다. 본문 내부링크는 글 발행 크론이 매일 손으로
#      적는 값이라 오타가 들어올 자리가 상시 열려 있다.
#
# ⚠️ **index.html 만 본다.** 404.html 은 .html 이지만 라우트가 아니고, 아무도
#    링크하지 않는 게 정상이며 JSON-LD 가 없는 게 맞다. 같이 세면 「고아 1건·
#    JSON-LD 누락 1건」이 매번 찍혀서, 다음 사람이 그 2건을 기준선으로 알고
#    진짜 결함을 같이 묻는다. (is_page 가 google 소유확인 파일을 뺀 것과 같은 이유)
def _asset_target(url):
    """공유 카드 이미지 주소를 dist 안의 자리로 바꾼다.

    사이트 밖이거나 파일로 떨어지지 않는 모양(`data:`·프로토콜 상대 `//host/…`)
    이면 None 을 돌려준다. **「없는 파일」과 「밖을 가리킨다」를 섞지 않는다** —
    앞은 오타고 뒤는 의도일 수 있어서 조치가 다르다.
    """
    url = url.split("#")[0].split("?")[0]
    pr = urlparse(url)
    if pr.scheme in ("http", "https"):
        if not url.startswith(SITE):
            return None
        path = pr.path
    elif not pr.scheme and not pr.netloc and url.startswith("/"):
        path = url
    else:
        return None
    return unquote(path) or None


def check_links(dist):
    """유입 그래프와 글 페이지의 구조화 데이터. 못 쟀으면 「모름」으로 올린다."""
    out = []
    routes = {}
    # 링크가 가리킬 수 있는 자리는 둘이다 — 라우트(디렉터리의 index.html)와
    # dist 안의 실제 파일(`/og-default.png`·`/rss.xml`·`/_astro/*.css`). 파일을
    # 빼고 세면 멀쩡한 자산 링크가 전부 깨진 링크로 잡힌다.
    assets = set()
    for root, _dirs, files in os.walk(dist):
        base = _route_of(dist, root)
        for f in files:
            assets.add(base + f)
        if "index.html" not in files:
            continue
        with open(os.path.join(root, "index.html"), encoding="utf-8", errors="replace") as fh:
            routes[base] = parse(fh.read())
    if not routes:
        out.append(("linkgraph-unknown",
                    "index.html 을 한 장도 못 찾았다 — 유입을 못 쟀다(0건이 아니라 모름)"))
        return out

    inbound = dict((route, 0) for route in routes)
    dead = {}
    for route, m in routes.items():
        # 한 페이지가 같은 글을 여러 번 걸어도 유입 1 로 센다. 안 그러면
        # 본문에 두 번 걸린 글이 「인기 글」처럼 보인다.
        for href in set(m.internal_hrefs):
            target = _normalize_href(href)
            if target is None:
                continue
            if target in inbound:
                if target != route:
                    inbound[target] += 1
                continue
            # 라우트가 아니면 파일일 수 있다. `_normalize_href` 가 끝 슬래시를
            # 붙여놨으니 파일 쪽은 벗겨서 본다.
            if target.rstrip("/") in assets:
                continue
            dead.setdefault(target, set()).add(route)

    for target in sorted(dead)[:20]:
        srcs = sorted(dead[target])
        out.append(("dead-internal-link",
                    "가리키는 자리가 dist 에 없다: %s ← %d곳 (예: %s)"
                    % (target, len(srcs), srcs[0])))

    # 공유 카드 이미지. **태그가 있는지는 페이지 검사가 보고, 여기서는 그 주소가
    # dist 안의 실제 파일로 떨어지는지를 본다.** 경로 오타 하나로 카드가 빈 채
    # 나가는데 태그는 그대로 남아서 페이지 검사로는 영영 안 걸린다. 깨진 내부
    # 링크 검사와 같은 축인데 `<a href>` 가 아니라 meta 라서 그쪽에도 안 걸린다.
    for route in sorted(routes):
        m = routes[route]
        for key in ("og:image", "twitter:image"):
            raw = (m.og.get(key) or m.twitter.get(key) or "").strip()
            if not raw:
                continue
            target = _asset_target(raw)
            if target is None:
                out.append(("social-image-offsite",
                            "%s 가 %s 안의 파일을 가리키지 않는다: %s ← %s"
                            % (key, SITE, raw, route)))
            elif target not in assets:
                out.append(("social-image-missing",
                            "%s 가 가리키는 파일이 dist 에 없다: %s ← %s"
                            % (key, raw, route)))

    for route in sorted(routes):
        if not route.startswith("/posts/"):
            continue
        if inbound[route] == 0:
            out.append(("orphan-post", "어느 페이지에서도 링크로 안 걸린다: %s" % route))
        if routes[route].jsonld == 0:
            out.append(("jsonld-missing", "글인데 JSON-LD 가 없다: %s" % route))
    return out


# 사이트 레벨 검사의 양성·음성 표본. 페이지 한 장이 아니라 **디렉터리 모양**이
# 입력이라 임시 폴더로 만든다.
def _fixture(root, thin_noindex, thin_in_sitemap, post_b_noindex):
    posts = os.path.join(root, "posts")
    dist = os.path.join(root, "dist")
    os.makedirs(posts)
    # 많=2건(목록 태그) · 하나=1건(얇은 태그)
    open(os.path.join(posts, "a.md"), "w", encoding="utf-8").write('tags: ["많", "하나"]\n')
    open(os.path.join(posts, "b.md"), "w", encoding="utf-8").write('tags: ["많"]\n')

    def page(relpath, robots):
        d = os.path.join(dist, relpath) if relpath else dist
        os.makedirs(d, exist_ok=True)
        meta = '<meta name="robots" content="%s">' % robots if robots else ""
        open(os.path.join(d, "index.html"), "w", encoding="utf-8").write(
            "<!doctype html><html><head>%s</head><body></body></html>" % meta)

    page("", "index, follow")
    page("posts/a", "index, follow")
    page("posts/b", "noindex, follow" if post_b_noindex else "index, follow")
    page("tags/많", "index, follow")
    page("tags/하나", "noindex, follow" if thin_noindex else "index, follow")

    locs = ["/", "/posts/a/", "/posts/b/", "/tags/%EB%A7%8E/"]
    if thin_in_sitemap:
        locs.append("/tags/%ED%95%98%EB%82%98/")
    body = "".join("<url><loc>https://x%s</loc></url>" % u for u in locs)
    open(os.path.join(dist, "sitemap-0.xml"), "w", encoding="utf-8").write(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>' % body)
    return dist, posts


def _link_fixture(root, b_linked, b_jsonld, dead_link=False, bad_social=False):
    """유입 그래프 표본. 양성 쪽은 href 모양을 일부러 어긋나게 적는다 —
    `/posts/a`(끝 슬래시 없음)·`/posts/b/#요약`(앵커 붙음)으로 걸어서,
    정규화가 깨지면 **양성 표본이 고아로 잡히며** 바로 드러나게 둔다.

    양성에는 **실제로 있는 파일 링크**(`/og-default.png`)도 같이 넣는다. 깨진
    링크 검사가 라우트만 타깃으로 보면 멀쩡한 자산 링크가 전부 걸리는데,
    그 과민이 여기서 드러난다."""
    dist = os.path.join(root, "dist")
    # 양성 쪽 og:image 는 **실제로 만드는 파일**을 절대 URL 로 가리킨다. 공유 카드
    # 검사가 SITE 접두어를 못 벗기면 멀쩡한 1,045장이 전부 걸리는데, 그 과민이
    # 여기서 드러난다.
    social = '<meta property="og:image" content="%s/%s">' % (
        SITE, "ghost.png" if bad_social else "og-default.png")

    def page(relpath, body, jsonld):
        d = os.path.join(dist, relpath) if relpath else dist
        os.makedirs(d, exist_ok=True)
        ld = '<script type="application/ld+json">{}</script>' if jsonld else ""
        open(os.path.join(d, "index.html"), "w", encoding="utf-8").write(
            "<!doctype html><html><head>%s%s</head><body>%s</body></html>"
            % (social, ld, body))

    links = '<a href="/posts/a">가</a><a href="/og-default.png">그림</a>'
    if b_linked:
        links += '<a href="/posts/b/#%EC%9A%94%EC%95%BD">나</a>'
    if dead_link:
        links += '<a href="/posts/ghost/">없는 글</a>'
    page("", links, False)                 # 허브는 글이 아니라 JSON-LD 를 안 본다
    page("posts/a", "", True)
    page("posts/b", "", b_jsonld)
    open(os.path.join(dist, "og-default.png"), "wb").write(b"\x89PNG")

    # 404.html 은 라우트가 아니다. 섞어 넣어서 **같이 세지 않는지**까지 본다.
    os.makedirs(dist, exist_ok=True)
    open(os.path.join(dist, "404.html"), "w", encoding="utf-8").write(
        "<!doctype html><html><head></head><body></body></html>")
    return dist


def selftest_links():
    """양성 1본 + 음성 1본. 양성에는 404.html 과 어긋난 href 모양을 같이 넣는다."""
    problems = []
    root = tempfile.mkdtemp(prefix="seoaudit-link-good-")
    try:
        issues = check_links(_link_fixture(root, b_linked=True, b_jsonld=True))
        if issues:
            problems.append("양성 표본이 걸렸다(과민): %s" % [i for i, _ in issues])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    root = tempfile.mkdtemp(prefix="seoaudit-link-bad-")
    try:
        fired = set(i for i, _ in check_links(
            _link_fixture(root, b_linked=False, b_jsonld=False, dead_link=True,
                          bad_social=True)))
        missing = {"orphan-post", "jsonld-missing", "dead-internal-link",
                   "social-image-missing"} - fired
        if missing:
            problems.append("음성 표본에서 안 걸린 검사(둔감): %s" % sorted(missing))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # dist 가 비면 「고아 0건」이 아니라 「못 쟀다」로 올라와야 한다.
    root = tempfile.mkdtemp(prefix="seoaudit-link-empty-")
    try:
        fired = set(i for i, _ in check_links(root))
        if "linkgraph-unknown" not in fired:
            problems.append("빈 dist 에서 「모름」이 안 올라왔다 — 0건으로 읽는다: %s" % sorted(fired))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return problems


def selftest_site():
    """양성 1본 + 음성 1본을 같은 실행에서 돌린다."""
    problems = []
    root = tempfile.mkdtemp(prefix="seoaudit-good-")
    try:
        dist, posts = _fixture(root, thin_noindex=True, thin_in_sitemap=False, post_b_noindex=False)
        issues = check_site(dist, posts)
        if issues:
            problems.append("양성 표본이 걸렸다(과민): %s" % [i for i, _ in issues])
    finally:
        shutil.rmtree(root, ignore_errors=True)

    root = tempfile.mkdtemp(prefix="seoaudit-bad-")
    try:
        dist, posts = _fixture(root, thin_noindex=False, thin_in_sitemap=False, post_b_noindex=True)
        fired = set(i for i, _ in check_site(dist, posts))
        missing = {"thin-tag-indexable", "noindex-on-real-page", "sitemap-vs-noindex"} - fired
        if missing:
            problems.append("음성 표본에서 안 걸린 검사(둔감): %s" % sorted(missing))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return problems


SITEMAP_SAMPLE = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    '  <url><loc>https://right-economy.com/</loc><lastmod>2026-10-14</lastmod></url>\n'
    '  <url>\n    <loc>\n      https://right-economy.com/posts/what-is-cpi/\n    </loc>\n  </url>\n'
    '  <url><loc>https://right-economy.com/tags/%EA%B8%88%EB%A6%AC/</loc></url>\n'
    '</urlset>\n'
)
SITEMAP_SAMPLE_EXPECTED = [
    "https://right-economy.com/",
    "https://right-economy.com/posts/what-is-cpi/",
    "https://right-economy.com/tags/%EA%B8%88%EB%A6%AC/",
]
SITEMAP_EMPTY = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
)


def selftest_locs():
    """loc 추출기 점검. **실제로 쓰이는 경로를 잰다** — 이 맥에서는 expat 이
    없어서 fallback 이 본선이다. expat 이 있는 환경에서는 두 경로가 같은 답을
    내는지까지 대조해서, fallback 이 조용히 어긋나는 걸 막는다."""
    problems = []
    if _locs_parser(SITEMAP_SAMPLE) != SITEMAP_SAMPLE_EXPECTED:
        problems.append("loc 파서가 표본을 못 읽었다: %s" % _locs_parser(SITEMAP_SAMPLE))
    if _locs_parser(SITEMAP_EMPTY) != []:
        problems.append("url 0건 표본에서 loc 이 나왔다(허수): %s" % _locs_parser(SITEMAP_EMPTY))
    if _expat_ok():
        if _locs_et(SITEMAP_SAMPLE) != _locs_parser(SITEMAP_SAMPLE):
            problems.append("ET 와 fallback 의 답이 다르다: %s vs %s"
                            % (_locs_et(SITEMAP_SAMPLE), _locs_parser(SITEMAP_SAMPLE)))
    return problems


def selftest():
    """양성(정상)·음성(불량) 표본을 같은 실행에서 돌린다.

    음성만 돌리면 검사기가 죽어도 전부 통과로 보이고, 양성만 돌리면 과민을
    못 걸러낸다. 그래서 판정식은 「둘을 같은 실행에」다.
    """
    good_issues = check_page(parse(GOOD))
    bad_ids = set(i for i, _ in check_page(parse(BAD)))
    bad_ids |= set(i for i, _ in check_page(parse(BAD_IDENTITY)))
    must_fire = {
        "title-missing", "desc-missing", "canonical-not-site",
        "og-missing", "twitter-missing", "h1-multi", "img-no-alt",
        "heading-skip",
        # 2026-10-24 추가. 빵가루는 「블록이 있나」가 아니라 「해석되는 모양인가」를 본다.
        "jsonld-broken", "breadcrumb-position", "breadcrumb-item",
        # 2026-10-27 추가. 같은 자리를 가리켜야 하는 주소 셋과 lang.
        # 음성 표본은 BAD_IDENTITY 쪽이다.
        "ogurl-ne-canonical", "lang-unexpected",
        "headline-ne-title", "moe-ne-canonical",
    }
    problems = []
    if good_issues:
        problems.append("양성 표본이 걸렸다(과민): %s" % [i for i, _ in good_issues])
    missing = must_fire - bad_ids
    if missing:
        problems.append("음성 표본에서 안 걸린 검사(둔감): %s" % sorted(missing))
    problems.extend(selftest_locs())
    problems.extend(selftest_site())
    problems.extend(selftest_links())
    return problems


def main():
    problems = selftest()
    if problems:
        print("🔴 자체 점검 실패 — 검사기를 믿을 수 없으므로 실물을 재지 않는다")
        for p in problems:
            print("   " + p)
        return 2
    print("✅ 자체 점검 통과 (페이지·사이트·유입그래프 검사 각각 양성 1본 무이슈 · "
          "음성 1본 전 검사 발화 · 빈 dist 는 「모름」 · loc 추출기 %s 경로)"
          % ("ET" if _expat_ok() else "순수 파이썬 fallback"))

    dist = sys.argv[1] if len(sys.argv) > 1 else "dist"
    if not os.path.isdir(dist):
        print("🔴 dist 가 없다: %s" % dist)
        return 2

    pages = []
    for root, _dirs, files in os.walk(dist):
        for f in files:
            if f.endswith(".html"):
                pages.append(os.path.join(root, f))
    pages.sort()
    if not pages:
        print("🔴 html 을 한 장도 못 찾았다 — 경로를 확인하라: %s" % dist)
        return 2

    skipped = []
    by_issue = defaultdict(list)
    titles = defaultdict(list)
    descs = defaultdict(list)

    for path in pages:
        with open(path, encoding="utf-8") as fh:
            m = parse(fh.read())
        rel = os.path.relpath(path, dist)
        if not is_page(rel):
            skipped.append(rel)
            continue
        for issue_id, msg in check_page(m):
            by_issue[issue_id].append((rel, msg))
        if (m.title or "").strip():
            titles[m.title.strip()].append(rel)
        if (m.description or "").strip():
            descs[m.description.strip()].append(rel)

    for issue_id, msg in check_site(dist, os.path.join("src", "content", "posts")):
        by_issue[issue_id].append(("(사이트 전체)", msg))
    for issue_id, msg in check_links(dist):
        by_issue[issue_id].append(("(사이트 전체)", msg))

    dup_titles = dict((k, v) for k, v in titles.items() if len(v) > 1)
    dup_descs = dict((k, v) for k, v in descs.items() if len(v) > 1)

    print("\n📄 검사한 페이지 %d장 / 제외 %d장 (%s)" % (len(pages) - len(skipped), len(skipped), dist))
    for rel in skipped:
        print("   제외: %s (페이지 아님 — 소유확인 파일)" % rel)
    if not by_issue and not dup_titles and not dup_descs:
        print("실질 이슈 없음")
        return 0

    for issue_id in sorted(by_issue):
        rows = by_issue[issue_id]
        print("\n[%s] %d건" % (issue_id, len(rows)))
        for rel, msg in rows[:10]:
            print("   %s — %s" % (rel, msg))
        if len(rows) > 10:
            print("   … 외 %d건" % (len(rows) - 10))

    if dup_titles:
        print("\n[title-duplicate] %d종" % len(dup_titles))
        for t, v in sorted(dup_titles.items(), key=lambda kv: -len(kv[1]))[:10]:
            print("   %d장: %r" % (len(v), t[:60]))
    if dup_descs:
        print("\n[desc-duplicate] %d종" % len(dup_descs))
        for d, v in sorted(dup_descs.items(), key=lambda kv: -len(kv[1]))[:10]:
            print("   %d장: %r" % (len(v), d[:60]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
