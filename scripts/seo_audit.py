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

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
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

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title = ((self.title or "") + data)


def parse(html):
    m = Meta()
    m.feed(html)
    if m.title is not None:
        m.title = m.title.strip()
    return m


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

    return out


GOOD = """<!doctype html><html><head>
<title>좋은 제목</title>
<meta name="description" content="It's a description with an apostrophe and \"quotes\"">
<link rel="canonical" href="https://right-economy.com/posts/x/">
<meta property="og:title" content="t"><meta property="og:description" content="d">
<meta property="og:url" content="u"><meta property="og:image" content="i">
<meta name="twitter:card" content="summary_large_image">
</head><body><h1>제목</h1><h2>소제목</h2><h3>더 안</h3><h2>소제목2</h2>
<img src="a.svg" alt="설명"><img src="b.svg" alt=""></body></html>"""

BAD = """<!doctype html><html><head>
<title></title>
<link rel="canonical" href="/posts/x/">
</head><body><h1>a</h1><h1>b</h1><h4>c</h4><img src="a.svg"></body></html>"""


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
    must_fire = {
        "title-missing", "desc-missing", "canonical-not-site",
        "og-missing", "twitter-missing", "h1-multi", "img-no-alt",
        "heading-skip",
    }
    problems = []
    if good_issues:
        problems.append("양성 표본이 걸렸다(과민): %s" % [i for i, _ in good_issues])
    missing = must_fire - bad_ids
    if missing:
        problems.append("음성 표본에서 안 걸린 검사(둔감): %s" % sorted(missing))
    problems.extend(selftest_locs())
    problems.extend(selftest_site())
    return problems


def main():
    problems = selftest()
    if problems:
        print("🔴 자체 점검 실패 — 검사기를 믿을 수 없으므로 실물을 재지 않는다")
        for p in problems:
            print("   " + p)
        return 2
    print("✅ 자체 점검 통과 (페이지·사이트 검사 각각 양성 1본 무이슈 · 음성 1본 전 검사 발화 · "
          "loc 추출기 %s 경로)" % ("ET" if _expat_ok() else "순수 파이썬 fallback"))

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
