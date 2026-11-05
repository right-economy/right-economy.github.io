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

# 본문 시각자료가 놓이는 자리. 공통 레이아웃의 로고·마스코트와 가르는 기준이다.
CONTENT_IMG_PREFIX = "/images/posts/"

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
        # 본문 이미지(src 가 /images/posts/ 로 시작)를 **나온 순서대로** 모은다.
        # 개수가 아니라 순서가 필요하다 — 첫 장만 LCP 후보라서 둘째 이후의
        # lazy 는 올바른 값이고, 같은 속성이 자리에 따라 결함과 정답으로 갈린다.
        self.content_imgs = []
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
            src = a.get("src") or ""
            if src.startswith(CONTENT_IMG_PREFIX):
                self.content_imgs.append((src, a.get("loading")))
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


def check_itemlist(m):
    """ItemList 의 내용이 쓸 수 있는 모양인가. (목록·카테고리·태그 허브)

    왜 개수로 안 되나: 빵가루와 같은 층의 문제다. `jsonld-missing` 은 블록이
    있나만 보고, 블록 수는 ItemList 가 망가져도 그대로 1 이다. position 이
    어긋나거나 url 이 상대경로면 검색엔진이 목록을 캐러셀로 못 읽고 **태그는
    전부 있는 채로** 리치결과에서만 조용히 빠진다.

    ⚠️ 「없다」를 이슈로 적지 않는다 — 글 상세에는 ItemList 가 없는 게 맞다.
       있을 때 모양만 본다. 빵가루 검사(check_breadcrumb)와 같은 규약이다.
    ⚠️ numberOfItems 는 항목 수와 어긋날 때만 걸린다. **홈은 최신 20편만 싣고
       그게 의도다**(src/pages/index.astro 주석) — 보이는 링크 수와 다른 것은
       결함이 아니라 설계라서, 페이지의 링크 수와는 대조하지 않는다.
    """
    out = []
    nodes, _broken = _ldjson_nodes(m.ldjson)
    for il in [n for n in nodes if n.get("@type") == "ItemList"]:
        items = il.get("itemListElement")
        if not isinstance(items, list) or not items:
            out.append(("itemlist-empty", "ItemList 에 itemListElement 가 없다 (name %r)" % (il.get("name"),)))
            continue
        pos = [it.get("position") if isinstance(it, dict) else None for it in items]
        if pos != list(range(1, len(items) + 1)):
            out.append(("itemlist-position", "position 이 1..%d 가 아니다: %r" % (len(items), pos)))
        count = il.get("numberOfItems")
        if count is not None and count != len(items):
            out.append(("itemlist-count", "numberOfItems %r 인데 항목은 %d개" % (count, len(items))))
        for it in items:
            if not isinstance(it, dict):
                out.append(("itemlist-item", "itemListElement 항목이 객체가 아니다"))
                continue
            if not (it.get("name") or "").strip():
                out.append(("itemlist-item", "name 이 비어 있다 (position %r)" % (it.get("position"),)))
            target = it.get("url")
            if target is None:
                target = it.get("item")
            url = target.get("@id") if isinstance(target, dict) else target
            if not isinstance(url, str) or not url.startswith(SITE):
                out.append(("itemlist-item", "url 이 %s 로 시작하는 절대 URL 이 아니다: %r" % (SITE, url)))
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
    out += check_itemlist(m)
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
"mainEntityOfPage":{"@type":"WebPage","@id":"https://right-economy.com/posts/x/"}},
{"@type":"ItemList","numberOfItems":2,"itemListElement":[
{"@type":"ListItem","position":1,"name":"글1","url":"https://right-economy.com/posts/x/"},
{"@type":"ListItem","position":2,"name":"글2","url":"https://right-economy.com/posts/y/"}]}]}</script>
</head><body><h1>제목</h1><h2>소제목</h2><h3>더 안</h3><h2>소제목2</h2>
<img src="a.svg" alt="설명"><img src="b.svg" alt=""></body></html>"""

BAD = """<!doctype html><html><head>
<title></title>
<link rel="canonical" href="/posts/x/">
<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":[
{"@type":"ListItem","position":1,"name":"","item":"/posts/x/"},
{"@type":"ListItem","position":5,"name":"글"}]}</script>
<script type="application/ld+json">{"@type":"BreadcrumbList", 이건 JSON 이 아니다}</script>
<script type="application/ld+json">{"@type":"ItemList","numberOfItems":9,"itemListElement":[
{"@type":"ListItem","position":1,"name":"","url":"/posts/x/"},
{"@type":"ListItem","position":7,"name":"글2","url":"https://right-economy.com/posts/y/"}]}</script>
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


# 본문 링크는 **원고(.md)에서 읽는다.** dist 에서는 템플릿이 단 링크(로고·목록으로·
# 관련 글·이전/다음)와 사람이 본문에 단 링크가 같은 `<a href>` 로 섞여서, 본문 링크만
# 골라내려면 마크업 구조에 기대야 한다. 원고에는 본문 링크만 있다.
MD_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((/[^)\s]*)\)")


# 페이지 한 장만 봐도 보이지만 **라우트를 알아야** 판정되는 축.
#   글 본문 첫 시각자료가 lazy 인가 — 그 한 장이 LCP(가장 큰 콘텐츠가 그려지는
#   시각) 후보다. lazy 는 레이아웃이 잡힌 뒤에야 요청을 걸기 때문에, 첫 장에
#   붙으면 그 대기만큼 LCP 가 밀린다. 둘째 이후에는 lazy 가 맞는 값이다.
#
# 🔴 왜 검사가 필요한가 — astro.config.mjs 의 rehypeImgLoading 이 **작성자가 적은
#    값을 일부러 존중한다**(없을 때만 첫 장 eager·나머지 lazy 로 채운다). 그래서
#    원고에 날 HTML 로 <img loading="lazy"> 를 직접 쓰면 플러그인이 안 고치고,
#    빌드도 통과하고, 메타 검사도 통과한다. 막는 자리가 한 군데도 없다.
#    2026-11-02 실측: 190편 중 1편(what-is-benner-cycle)이 그 모양이었다.
#
# ⚠️ 글 라우트만 본다. 허브·태그 페이지가 나중에 썸네일을 깔면 그 첫 장은 lazy 가
#    맞는 값이라, 같이 세면 바로 과민이 된다. (현재 실측으로는 글 밖에서 본문
#    이미지를 쓰는 라우트가 0개다 — 지금 0이라는 것과 앞으로도 0이라는 건 다르다)
def check_lcp(dist):
    """글 본문 첫 이미지의 loading 값을 센다."""
    out = []
    seen_posts = 0
    for root, _dirs, files in os.walk(dist):
        if "index.html" not in files:
            continue
        route = _route_of(dist, root)
        if not route.startswith("/posts/") or route == "/posts/":
            continue
        with open(os.path.join(root, "index.html"), encoding="utf-8", errors="replace") as fh:
            imgs = parse(fh.read()).content_imgs
        seen_posts += 1
        if not imgs:
            continue
        src, loading = imgs[0]
        if (loading or "").strip().lower() == "lazy":
            out.append(("lcp-img-lazy",
                        "본문 첫 이미지가 lazy 다 — LCP 후보가 지연된다: %s (%s)" % (route, src)))
    if seen_posts == 0:
        out.append(("lcp-unmeasured",
                    "글 라우트를 한 개도 못 찾았다 — 첫 이미지 축을 못 쟀다(0건이 아니라 모름)"))
    return out


def check_sources(posts_dir):
    """원고 본문의 내부 링크가 **약속한 자리로 떨어지나.**

    🔴 `](/)` 는 dist 검사로 영영 안 보인다. 홈은 실재하는 라우트라
    `dead-internal-link` 에 안 걸리고, 템플릿의 로고·「목록으로」가 같은 주소를
    쓰니 페이지 검사로도 못 가른다. 유입 그래프에서는 **홈의 inbound 가 늘어나
    오히려 건강해 보인다.** 앵커는 주제를 약속하는데 독자는 홈으로 떨어진다.
    2026-10-28 실측: `what-is-stagflation` 2건(앵커 「기준금리를 올려」·「분산」).
    그 글의 본문 링크는 그 둘이 전부여서 **내보내는 링크가 사실상 0이었다.**

    못 읽으면 0건이 아니라 「모름」으로 올린다.
    """
    out = []
    if not os.path.isdir(posts_dir):
        out.append(("sources-unreadable",
                    "원고 디렉터리를 못 읽었다 — 본문 링크를 못 쟀다(0건이 아니라 모름): %s" % posts_dir))
        return out
    names = [n for n in sorted(os.listdir(posts_dir)) if n.endswith(".md")]
    if not names:
        out.append(("sources-unreadable",
                    "원고를 한 편도 못 찾았다 — 본문 링크를 못 쟀다(0건이 아니라 모름): %s" % posts_dir))
        return out
    slugs = set(n[:-3] for n in names)
    for name in names:
        with open(os.path.join(posts_dir, name), encoding="utf-8") as fh:
            raw = fh.read()
        for m in MD_LINK.finditer(raw):
            text, url = m.group(1), m.group(2)
            if url == "/":
                out.append(("body-link-to-home",
                            "본문 링크가 홈으로 떨어진다 — 앵커가 약속한 자리가 아니다: "
                            "[%s](/) ← %s" % (text, name)))
            elif url.startswith("/posts/"):
                slug = url[len("/posts/"):].strip("/")
                if slug and slug not in slugs:
                    out.append(("body-link-dead-slug",
                                "본문 링크가 없는 글을 가리킨다: [%s](%s) ← %s" % (text, url, name)))
    return out


# ── 본문 링크 그래프의 차수 ──────────────────────────────────────────────
#
# 🔴 **이것은 이슈가 아니라 보고다.** 종료코드를 바꾸지 않는다.
#    한 번에 고칠 수 없는 양(2026-11-05 실측 114편)이라 이슈로 올리면 rc 가
#    영구히 1 이 되고, 그러면 다음 사람이 그 1 을 기준선으로 알고 **진짜 결함을
#    같이 묻는다.** 제목 길이를 「보고만 하고 차단하지 않는다」로 둔 것과 같은 자리다.
#
# 왜 센가 — `check_links` 의 고아 검사는 **dist 의 모든 `<a href>`** 를 센다.
# 그런데 글 한 편은 태그·허브·이전/다음에서 템플릿으로 늘 걸리므로 **유입이
# 0 이 되는 일이 구조적으로 없다.** 2026-11-05 실측에서 `orphan-post` 는 0건인데,
# 사람이 본문에 단 링크만으로 세면 받는 유입이 0 인 글이 183편이었다.
# 템플릿 링크는 모든 글에 똑같이 붙어서 글 사이의 가중치를 만들지 못한다.
#
# 세 축을 가른다.
#   내보내기 0 — 그 글을 읽고 나서 갈 자리를 본문이 제시하지 않는다(막다른 글)
#   받기 0     — 다른 글이 그 글을 근거로 인용하지 않는다
#   둘 다 0    — 본문 링크 그래프에서 완전히 떨어져 있다
# 「둘 다 0」만 세면 한쪽만 끊긴 글이 안 보이고, 합쳐 세면 조치가 다른 둘이 섞인다.
#
# ⚠️ 자기 자신을 가리키는 링크는 안 센다. 세면 자기 링크 한 줄로 두 축이 동시에
#    0 을 벗어나서, 그래프가 좋아진 것처럼 보인다.
def measure_body_graph(posts_dir):
    """원고 본문 링크만으로 글 사이 그래프를 세어 돌려준다.

    못 읽으면 None 이다 — **0건이 아니라 「모름」이다.**
    """
    if not os.path.isdir(posts_dir):
        return None
    names = [n for n in sorted(os.listdir(posts_dir)) if n.endswith(".md")]
    if not names:
        return None
    slugs = set(n[:-3] for n in names)
    outdeg = dict((s, 0) for s in slugs)
    indeg = dict((s, 0) for s in slugs)
    category = {}
    for name in names:
        me = name[:-3]
        with open(os.path.join(posts_dir, name), encoding="utf-8") as fh:
            raw = fh.read()
        hit = re.search(r'^category:\s*"(.*)"\s*$', raw, re.M)
        category[me] = hit.group(1) if hit else "(없음)"
        seen = set()
        for m in MD_LINK.finditer(raw):
            url = m.group(2)
            if not url.startswith("/posts/"):
                continue
            slug = url[len("/posts/"):].strip("/")
            # 없는 글을 가리키는 링크는 check_sources 가 따로 올린다. 여기서는
            # 실재하는 글 사이의 연결만 세서 두 축이 겹쳐 보고되지 않게 한다.
            if not slug or slug == me or slug not in slugs or slug in seen:
                continue
            seen.add(slug)
            outdeg[me] += 1
            indeg[slug] += 1
    zero_out = sorted(s for s in slugs if outdeg[s] == 0)
    zero_in = sorted(s for s in slugs if indeg[s] == 0)
    both = sorted(set(zero_out) & set(zero_in))
    by_cat = defaultdict(int)
    for s in both:
        by_cat[category[s]] += 1
    return {
        "total": len(slugs),
        "zero_out": zero_out,
        "zero_in": zero_in,
        "both": both,
        "by_category": dict(by_cat),
    }


def _body_graph_fixture(root):
    """원고 모양의 최소 표본. a→b 한 줄만 있고 c 는 떨어져 있다."""
    os.makedirs(root, exist_ok=True)

    def post(slug, category, body):
        with open(os.path.join(root, slug + ".md"), "w", encoding="utf-8") as fh:
            fh.write('---\ntitle: "t"\ncategory: "%s"\n---\n\n%s\n' % (category, body))

    post("a", "기초 개념", "[비](/posts/b/) 를 건다. [자기](/posts/a/) 도 건다.")
    post("b", "지표 읽기", "[없는 글](/posts/nope/) 만 건다.")
    post("c", "시황", "링크가 없다.")
    return root


def selftest_body_graph():
    """차수 축 점검. 자기 링크·없는 글 링크가 차수를 올리지 않는지까지 본다."""
    problems = []
    tmp = tempfile.mkdtemp()
    try:
        got = measure_body_graph(_body_graph_fixture(os.path.join(tmp, "posts")))
        if got is None:
            problems.append("표본 원고를 못 읽었다(차수 축이 아예 안 돈다)")
            return problems
        # a 는 b 로 내보낸다. b 는 없는 글만 가리키므로 내보내기 0 이다.
        if got["zero_out"] != ["b", "c"]:
            problems.append("내보내기 0 이 ['b','c'] 가 아니다: %s" % got["zero_out"])
        # b 만 받는다. a 의 자기 링크는 a 의 받기를 올리면 안 된다.
        if got["zero_in"] != ["a", "c"]:
            problems.append("받기 0 이 ['a','c'] 가 아니다: %s" % got["zero_in"])
        if got["both"] != ["c"]:
            problems.append("둘 다 0 이 ['c'] 가 아니다: %s" % got["both"])
        if got["by_category"] != {"시황": 1}:
            problems.append("카테고리 집계가 {'시황': 1} 이 아니다: %s" % got["by_category"])
        if measure_body_graph(os.path.join(tmp, "nope")) is not None:
            problems.append("없는 원고 경로가 「모름」(None) 으로 안 나왔다(0건으로 묻힌다)")
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        if measure_body_graph(empty) is not None:
            problems.append("원고 0편이 「모름」(None) 으로 안 나왔다(0건으로 묻힌다)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


# ── description 의 상대 시점어가 검색결과에서 고정되나 ────────────────────
#
# 제목과 description 은 **본문 없이 둘이서만** 검색결과·공유카드에 나간다.
# 그래서 「오늘 밤 CPI 가 나와요」 같은 문장은 **발행일을 알아야 뜻이 정해진다.**
# 2026-09-13 에 세운 처방은 「제목에 날짜를 박는다」였고, 그러면 같은 카드에
# 날짜가 있으니 description 의 상대어가 덮인다고 봤다. 그 전제에 조건이 하나
# 빠져 있었다 — **제목이 잘리면 날짜가 먼저 사라진다**(날짜는 대개 끝에 있다).
#
#   제목 날짜가 35자 안    → 잘려도 남는다 → description 의 상대어가 덮인다
#   제목 날짜가 35자 밖    → 먼저 사라진다 → 덮을 것이 없어진다   ← 이 검사
#
# 🔴 **이 축은 dist 검사로도, 제목 검사로도, 길이 검사로도 안 보인다.** 태그는
#    전부 있고 길이도 권고 안이며 제목에 날짜도 **있다.** 어긋나는 건 둘의 조합뿐이다.
# 📌 2026-11-04 실측 5건(전부 6·8월 시황). description 에 절대날짜를 넣어 0 이 됐다.
#
# ⚠️ **날짜 의존이 본질인 lane 만 본다**(시황·경제 뉴스). 해설글의 「요즘 서울은」은
#    며칠에 쓰였는지 몰라도 뜻이 안 뒤집혀서 결함이 아니고, 전 글에 걸면 노이즈가
#    실제 결함을 묻는다. 2026-11-04 실측으로 해설·부동산 lane 6건이 이 조건에
#    걸렸는데 전부 그 종류였다(그리고 부동산은 다른 담당 lane 이다).
# ⚠️ 35자는 픽셀 근사치다(구글은 ~600px 로 자른다). 경계가 아니라 위험 구간이다.
DATED_LANES = ("시황", "경제 뉴스")

# 「이 말이 며칠에 쓰였는지 몰라도 뜻이 같은가」에서 갈리는 말들. 목록은 반드시
# 빠지니 **판정은 이 목록이 아니라 그 물음**이다 — 새로 보이면 여기 더한다.
RELATIVE_TIME_WORDS = (
    "오늘", "어제", "그제", "내일", "간밤", "지금", "요즘", "방금", "최근", "현재",
    "이번 주", "이번주", "지난주", "지난 주", "다음 주", "다음주",
    "이번 달", "이달", "지난달", "이번 분기", "올해", "작년", "사흘째", "당분간",
)

# 절대날짜 표기. 「6/20」·「2026년」·「8월 12일」·「8월」 넷을 받는다.
ABS_DATE = re.compile(r"\d{1,2}\s?/\s?\d{1,2}|\d{4}\s?년|\d{1,2}\s?월")

# 제목이 잘리는 위험 구간의 시작. 위 주석의 근사치다.
TITLE_DATE_SAFE_MAX = 35

FM_FIELD = {}
for _k in ("title", "description", "category"):
    FM_FIELD[_k] = re.compile(r'^%s:\s*"(.*)"\s*$' % _k, re.M)


def _frontmatter(raw):
    out = {}
    for key, pat in FM_FIELD.items():
        m = pat.search(raw)
        out[key] = m.group(1) if m else ""
    return out


def check_desc_anchor(posts_dir):
    """description 의 상대 시점어가 **검색결과 안에서** 날짜로 고정되나.

    제목의 날짜가 잘리는 자리(35자 밖)에 있으면 덮개가 사라진다 — 위 주석 참조.
    못 읽으면 0건이 아니라 「모름」으로 올린다.
    """
    out = []
    if not os.path.isdir(posts_dir):
        out.append(("desc-anchor-unreadable",
                    "원고 디렉터리를 못 읽었다 — description 상대어를 못 쟀다"
                    "(0건이 아니라 모름): %s" % posts_dir))
        return out
    names = [n for n in sorted(os.listdir(posts_dir)) if n.endswith(".md")]
    if not names:
        out.append(("desc-anchor-unreadable",
                    "원고를 한 편도 못 찾았다 — description 상대어를 못 쟀다"
                    "(0건이 아니라 모름): %s" % posts_dir))
        return out
    for name in names:
        with open(os.path.join(posts_dir, name), encoding="utf-8") as fh:
            fm = _frontmatter(fh.read())
        if fm["category"] not in DATED_LANES:
            continue
        desc = fm["description"]
        if not desc:
            continue
        words = [w for w in RELATIVE_TIME_WORDS if w in desc]
        if not words or ABS_DATE.search(desc):
            continue
        m = ABS_DATE.search(fm["title"])
        where = m.start() if m else None
        if where is not None and where < TITLE_DATE_SAFE_MAX:
            continue
        out.append(("desc-relative-unanchored",
                    "description 의 상대어(%s)를 고정할 날짜가 카드에 안 남는다 — "
                    "제목 날짜 위치 %s: %s"
                    % ("·".join(words),
                       "없음" if where is None else "%d자" % where, name)))
    return out


# SVG 의 `fill` 은 presentation attribute 라 **그 파일 안의 `<style>` 규칙에 진다.**
# 그래서 `<text … fill="#ffffff">` 라고 적혀 있어도 `text{fill:#2a2521}` 이 있으면
# 어두운 글자로 찍히고, 어두운 상자 위에서는 **대비 1.00 = 안 보인다.**
# 글은 SVG 를 `<img>` 로 부르므로 SVG 한 장이 그 자체로 한 문서다 — 사이트
# 스타일시트는 여기에 끼어들지 않고, 이기는 건 같은 파일의 `<style>` 뿐이다.
#
# ⚠️ **이 검사는 「안 보이나」를 모른다 — 「지정이 무시되나」만 본다.** 실제 대비는
#    띄워서 계산된 색을 받아야 갈리고(2026-10-18·11-03 두 번 다 그렇게 쟀다),
#    그 측정은 헤드리스 브라우저가 필요해서 이 감사에 넣지 않았다. 대신 **기준선을
#    0 으로 만들어 두고 기계가 메커니즘을 지킨다** — 2026-11-03 에 51곳을 전부
#    인라인 `style="fill:…"` 로 옮겨(또는 지정이 기본색보다 나쁜 6곳은 지워서) 0 이 됐다.
#    여기서 1건이라도 올라오면 **그 자리는 렌더해서 대비를 재야 한다.**
# ⚠️ 아는 선택자는 태그 이름과 한 겹 클래스(`text`·`.box`·쉼표 묶음)뿐이다.
#    자손 선택자(`g .box`)를 쓰면 못 본다 — 이 저장소의 도식은 전부 앞의 두 모양이다.
FILL_TAGS = ("text", "tspan")


class SvgFill(HTMLParser):
    """`<style>` 의 fill 규칙과, fill 속성을 든 글자 요소를 같이 모은다."""

    def __init__(self):
        HTMLParser.__init__(self)
        self.css_fill_selectors = set()
        self.nodes = []          # (tag, class 목록, fill 속성, style 속성, 글자)
        self._in_style = False
        self._open = None

    def handle_starttag(self, tag, attrs):
        if tag == "style":
            self._in_style = True
            return
        if tag in FILL_TAGS:
            a = dict(attrs)
            self._open = [tag, (a.get("class") or "").split(), a.get("fill"), a.get("style"), ""]
            self.nodes.append(self._open)

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
        if tag in FILL_TAGS:
            self._open = None

    def handle_data(self, data):
        if self._in_style:
            # 규칙 하나하나가 아니라 「fill 을 정하는 선택자」만 모은다.
            for block in data.split("}"):
                if "{" not in block:
                    continue
                sel, decl = block.split("{", 1)
                if "fill" not in decl:
                    continue
                for part in sel.split(","):
                    part = part.strip()
                    if part:
                        self.css_fill_selectors.add(part)
        elif self._open is not None:
            self._open[4] += data


def check_svg_fill(images_dir):
    """SVG 안에서 fill 속성이 같은 파일의 스타일시트 규칙에 지는 자리를 센다.

    못 읽으면 0건이 아니라 「모름」으로 올린다.
    """
    out = []
    if not os.path.isdir(images_dir):
        out.append(("svgfill-unmeasured",
                    "그림 디렉터리를 못 읽었다 — SVG fill 축을 못 쟀다(0건이 아니라 모름): %s"
                    % images_dir))
        return out
    seen = 0
    for root, _dirs, files in os.walk(images_dir):
        for name in sorted(files):
            if not name.endswith(".svg"):
                continue
            seen += 1
            path = os.path.join(root, name)
            with open(path, encoding="utf-8", errors="replace") as fh:
                p = SvgFill()
                p.feed(fh.read())
            for tag, classes, fill, style, text in p.nodes:
                if not fill or (style and "fill" in style):
                    continue   # 인라인 style 은 규칙을 이긴다 — 그게 처방이다
                names = set([tag]) | set("." + c for c in classes)
                if names & p.css_fill_selectors:
                    out.append(("svg-fill-loses-to-css",
                                "fill=%s 가 같은 파일 <style> 에 진다 — 적힌 색으로 안 찍힌다: "
                                "%s 「%s」 (style=\"fill:…\" 로 적을 것)"
                                % (fill, os.path.relpath(path, images_dir),
                                   (text or "").strip()[:24])))
    if seen == 0:
        out.append(("svgfill-unmeasured",
                    "SVG 를 한 장도 못 찾았다 — fill 축을 못 쟀다(0건이 아니라 모름): %s"
                    % images_dir))
    return out


def _svgfill_fixture(root, losing, inline_style=False, no_rule=False):
    """SVG 한 장. 세 모양을 만든다 —
    ①규칙에 지는 fill 속성 ②인라인 style(처방대로) ③fill 규칙이 없는 파일."""
    os.makedirs(os.path.join(root, "a"), exist_ok=True)
    rule = "" if no_rule else "text{font-size:13px;fill:#2a2521}\n.cap{fill:#7a7066}"
    if inline_style:
        body = '<text class="cap" style="fill:#ffffff">값</text>'
    elif losing:
        body = '<text class="cap" fill="#ffffff">값</text>'
    else:
        body = '<text class="cap">값</text>'
    with open(os.path.join(root, "a", "x.svg"), "w", encoding="utf-8") as fh:
        fh.write('<svg xmlns="http://www.w3.org/2000/svg"><style>%s</style>%s</svg>'
                 % (rule, body))
    return root


def selftest_svg_fill():
    """fill 축 점검. 양성·음성·처방·규칙없음·「모름」 다섯을 같은 실행에서 본다."""
    problems = []
    tmp = tempfile.mkdtemp()
    try:
        good = _svgfill_fixture(os.path.join(tmp, "good"), losing=False)
        if check_svg_fill(good):
            problems.append("SVG fill 양성 표본이 걸렸다(과민): %s"
                            % [i for i, _ in check_svg_fill(good)])
        bad = _svgfill_fixture(os.path.join(tmp, "bad"), losing=True)
        if "svg-fill-loses-to-css" not in set(i for i, _ in check_svg_fill(bad)):
            problems.append("SVG fill 음성 표본에서 svg-fill-loses-to-css 가 안 걸렸다(둔감)")
        # 처방(인라인 style)을 쓴 파일은 걸리면 안 된다. 안 그러면 고친 자리가
        # 영구 경고로 남아서 다음 사람이 처방을 되돌린다.
        fixed = _svgfill_fixture(os.path.join(tmp, "fixed"), losing=True, inline_style=True)
        if check_svg_fill(fixed):
            problems.append("처방(인라인 style) 표본이 걸렸다(과민)")
        # fill 규칙이 없는 파일의 fill 속성은 아무것에도 지지 않는다.
        plain = _svgfill_fixture(os.path.join(tmp, "plain"), losing=True, no_rule=True)
        if check_svg_fill(plain):
            problems.append("fill 규칙이 없는 표본이 걸렸다(과민)")
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        if "svgfill-unmeasured" not in set(i for i, _ in check_svg_fill(empty)):
            problems.append("SVG 0장이 「모름」으로 안 올라왔다(0건으로 묻힌다)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


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


def _source_fixture(root, broken):
    """원고 디렉터리 표본. 입력이 **파일 모양**이라 임시 폴더로 만든다."""
    posts = os.path.join(root, "posts")
    os.makedirs(posts)
    with open(os.path.join(posts, "a.md"), "w", encoding="utf-8") as fh:
        fh.write("본문에서 [금리](/posts/b/)를 건다.\n")
    body = "둘째 글.\n"
    if broken:
        # 두 결함을 한 파일에 같이 둔다 — 서로 다른 id 라 섞여도 안 가려진다.
        body += "[기준금리를 올려](/) 하고 [없는 글](/posts/zzz/)도 건다.\n"
    with open(os.path.join(posts, "b.md"), "w", encoding="utf-8") as fh:
        fh.write(body)
    return posts


def selftest_sources():
    """원고 검사 점검. 양성·음성·「모름」 셋을 같은 실행에서 본다.

    🔴 「모름」 표본이 따로 있는 이유 — 경로가 틀렸을 때 0건이 나오면 그 0 이
    「본문 링크가 멀쩡하다」로 읽힌다. 조회 실패는 값이 아니다.
    """
    problems = []
    tmp = tempfile.mkdtemp()
    try:
        good = _source_fixture(os.path.join(tmp, "good"), broken=False)
        bad = _source_fixture(os.path.join(tmp, "bad"), broken=True)
        good_issues = check_sources(good)
        if good_issues:
            problems.append("원고 양성 표본이 걸렸다(과민): %s" % [i for i, _ in good_issues])
        bad_ids = set(i for i, _ in check_sources(bad))
        for need in ("body-link-to-home", "body-link-dead-slug"):
            if need not in bad_ids:
                problems.append("원고 음성 표본에서 %s 가 안 걸렸다(둔감)" % need)
        unknown = set(i for i, _ in check_sources(os.path.join(tmp, "nope")))
        if "sources-unreadable" not in unknown:
            problems.append("없는 원고 경로가 「모름」으로 안 올라왔다(0건으로 묻힌다)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


def _anchor_fixture(root, unanchored):
    """원고 표본. 네 갈래를 한 폴더에 같이 둔다 — 걸려야 하는 한 편과,
    **걸리면 과민인** 세 편(제목 날짜가 안쪽 · description 에 절대날짜 · 다른 lane).
    과민 표본을 같이 두는 이유: 조건 넷 중 하나만 풀려도 0 이 되어야 한다.
    """
    posts = os.path.join(root, "posts")
    os.makedirs(posts)

    def post(name, title, desc, category):
        body = ('---\ntitle: "%s"\ndescription: "%s"\ncategory: "%s"\n'
                'publishedDate: 2026-08-12\n---\n\n본문.\n'
                % (title, desc, category))
        with open(os.path.join(posts, name), "w", encoding="utf-8") as fh:
            fh.write(body)

    # 과민 표본 셋 — 어느 것도 걸려서는 안 된다.
    post("safe-title-date.md",
         "짧은 제목 — 8월 12일 아침 시황",      # 날짜가 35자 안
         "오늘 밤 지표가 나와요.", "시황")
    post("desc-has-date.md",
         "아주 긴 제목을 일부러 늘려서 날짜를 뒤로 밀어낸 시황 제목입니다 — 8월 12일 아침 시황",
         "8월 12일 밤 지표가 나와요.", "시황")          # description 에 절대날짜
    post("other-lane.md",
         "아주 긴 제목을 일부러 늘려서 날짜를 뒤로 밀어낸 해설 제목입니다 — 8월 12일 정리",
         "요즘 서울은 반대예요.", "기초 개념")          # 날짜 의존 lane 이 아니다

    if unanchored:
        post("bad.md",
             "아주 긴 제목을 일부러 늘려서 날짜를 뒤로 밀어낸 시황 제목입니다 — 8월 12일 아침 시황",
             "오늘 밤 지표가 나오고 어제는 올랐어요.", "시황")
    return posts


def selftest_desc_anchor():
    """description 상대어 검사 점검. 양성·음성·「모름」을 같은 실행에서 본다.

    🔴 양성 표본이 **세 편**인 이유 — 이 검사는 조건 넷의 곱이라, 한 조건을
    잘못 써도 음성 표본은 그대로 걸린다. 과민은 음성만으로는 안 보인다.
    """
    problems = []
    tmp = tempfile.mkdtemp()
    try:
        good = _anchor_fixture(os.path.join(tmp, "good"), unanchored=False)
        bad = _anchor_fixture(os.path.join(tmp, "bad"), unanchored=True)
        good_issues = check_desc_anchor(good)
        if good_issues:
            problems.append("description 상대어 양성 표본이 걸렸다(과민): %s"
                            % [m for _, m in good_issues])
        bad_rows = check_desc_anchor(bad)
        bad_ids = set(i for i, _ in bad_rows)
        if "desc-relative-unanchored" not in bad_ids:
            problems.append("description 상대어 음성 표본에서 desc-relative-unanchored "
                            "가 안 걸렸다(둔감)")
        elif len(bad_rows) != 1:
            # 걸린 편수까지 본다 — 과민 표본이 같이 걸렸다면 위 양성 검사가 잡지만,
            # 음성 표본 쪽에서 편수가 늘면 조건이 느슨해진 것이다.
            problems.append("description 상대어 음성 표본에서 %d편이 걸렸다(1편이어야 한다): %s"
                            % (len(bad_rows), [m for _, m in bad_rows]))
        unknown = set(i for i, _ in check_desc_anchor(os.path.join(tmp, "nope")))
        if "desc-anchor-unreadable" not in unknown:
            problems.append("없는 원고 경로가 「모름」으로 안 올라왔다(0건으로 묻힌다)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


def _lcp_fixture(root, first_lazy, hub_lazy_thumb=False):
    """dist 모양의 최소 표본. 글 두 편 + 허브 한 장."""
    def page(path, body):
        os.makedirs(os.path.join(root, path), exist_ok=True)
        with open(os.path.join(root, path, "index.html"), "w", encoding="utf-8") as fh:
            fh.write("<!doctype html><html lang=\"ko\"><body>%s</body></html>" % body)

    first = "lazy" if first_lazy else "eager"
    page("posts/a", (
        '<img src="/images/posts/a/one.svg" alt="x" loading="%s">'
        '<img src="/images/posts/a/two.svg" alt="y" loading="lazy">' % first))
    # 본문 이미지가 없는 글. 「이미지 0장」과 「첫 장이 lazy」를 가르는 표본이다.
    page("posts/b", '<img src="/mascot/logo.png" alt="">')
    # 허브. 썸네일 첫 장이 lazy 여도 걸리면 안 된다(과민 표본).
    page("category/x", (
        '<img src="/images/posts/a/one.svg" alt="x" loading="lazy">'
        if hub_lazy_thumb else "<p>허브</p>"))
    return root


def selftest_lcp():
    """첫 이미지 축 점검. 양성·음성·과민·「모름」 넷을 같은 실행에서 본다."""
    problems = []
    tmp = tempfile.mkdtemp()
    try:
        good = _lcp_fixture(os.path.join(tmp, "good"), first_lazy=False, hub_lazy_thumb=True)
        if check_lcp(good):
            problems.append("첫이미지 양성 표본이 걸렸다(과민): %s"
                            % [i for i, _ in check_lcp(good)])
        bad = _lcp_fixture(os.path.join(tmp, "bad"), first_lazy=True)
        bad_ids = set(i for i, _ in check_lcp(bad))
        if "lcp-img-lazy" not in bad_ids:
            problems.append("첫이미지 음성 표본에서 lcp-img-lazy 가 안 걸렸다(둔감)")
        # 글이 한 편도 없는 dist 는 0건이 아니라 「모름」이다.
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        if "lcp-unmeasured" not in set(i for i, _ in check_lcp(empty)):
            problems.append("글 0편인 dist 가 「모름」으로 안 올라왔다(0건으로 묻힌다)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


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
        # 2026-10-30 추가. 허브의 ItemList 도 「있나」가 아니라 「해석되는 모양인가」.
        "itemlist-position", "itemlist-item", "itemlist-count",
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
    problems.extend(selftest_sources())
    problems.extend(selftest_desc_anchor())
    problems.extend(selftest_lcp())
    problems.extend(selftest_svg_fill())
    problems.extend(selftest_body_graph())
    return problems


def main():
    problems = selftest()
    if problems:
        print("🔴 자체 점검 실패 — 검사기를 믿을 수 없으므로 실물을 재지 않는다")
        for p in problems:
            print("   " + p)
        return 2
    print("✅ 자체 점검 통과 (페이지·사이트·유입그래프 검사 각각 양성 1본 무이슈 · "
          "음성 1본 전 검사 발화 · 빈 dist·없는 원고는 「모름」 · 원고 본문 링크 축 포함 · "
          "본문 첫 이미지 축 포함(허브 썸네일 과민 표본까지) · "
          "SVG fill 축 포함(처방·규칙없음 과민 표본까지) · "
          "description 상대어 축 포함(과민 표본 3종) · "
          "본문 링크 차수 축 포함(자기 링크·없는 글 링크 과민 표본까지) · "
          "loc 추출기 %s 경로)"
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
    for issue_id, msg in check_lcp(dist):
        by_issue[issue_id].append(("(사이트 전체)", msg))
    # dist 가 아니라 원고를 읽는 축. dist 쪽 검사로는 안 보이는 자리다(함수 주석 참조).
    for issue_id, msg in check_sources(os.path.join("src", "content", "posts")):
        by_issue[issue_id].append(("(원고)", msg))
    # 같은 원고를 읽는 다른 축. 검색결과 안에서 상대어가 날짜로 고정되나를 본다.
    for issue_id, msg in check_desc_anchor(os.path.join("src", "content", "posts")):
        by_issue[issue_id].append(("(원고)", msg))
    # 그림 파일을 읽는 축. dist 에서는 SVG 가 <img src> 한 줄이라 안쪽이 안 보인다.
    for issue_id, msg in check_svg_fill(os.path.join("public", "images", "posts")):
        by_issue[issue_id].append(("(그림)", msg))

    dup_titles = dict((k, v) for k, v in titles.items() if len(v) > 1)
    dup_descs = dict((k, v) for k, v in descs.items() if len(v) > 1)

    print("\n📄 검사한 페이지 %d장 / 제외 %d장 (%s)" % (len(pages) - len(skipped), len(skipped), dist))
    for rel in skipped:
        print("   제외: %s (페이지 아님 — 소유확인 파일)" % rel)

    # 보고 전용 축. 종료코드를 바꾸지 않는다(함수 주석 참조).
    graph = measure_body_graph(os.path.join("src", "content", "posts"))
    if graph is None:
        print("🔗 본문 링크 차수 — 원고를 못 읽어 못 쟀다(0건이 아니라 모름)")
    else:
        print("🔗 본문 링크 차수 — 글 %d편 중 내보내기 0 이 %d편 · 받기 0 이 %d편 · 둘 다 0 이 %d편"
              % (graph["total"], len(graph["zero_out"]), len(graph["zero_in"]), len(graph["both"])))
        if graph["by_category"]:
            print("   둘 다 0 의 카테고리: %s"
                  % " · ".join("%s %d" % (k, v) for k, v in
                               sorted(graph["by_category"].items(), key=lambda kv: -kv[1])))
            print("   (템플릿 링크는 모든 글에 같이 붙어서 이 축에 안 잡힌다 — 사람이 본문에 단 링크만 센다)")

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
