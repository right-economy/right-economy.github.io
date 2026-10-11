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
import sys
from collections import defaultdict
from html.parser import HTMLParser

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
        elif tag == "h1":
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
</head><body><h1>제목</h1><img src="a.svg" alt="설명"><img src="b.svg" alt=""></body></html>"""

BAD = """<!doctype html><html><head>
<title></title>
<link rel="canonical" href="/posts/x/">
</head><body><h1>a</h1><h1>b</h1><img src="a.svg"></body></html>"""


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
    }
    problems = []
    if good_issues:
        problems.append("양성 표본이 걸렸다(과민): %s" % [i for i, _ in good_issues])
    missing = must_fire - bad_ids
    if missing:
        problems.append("음성 표본에서 안 걸린 검사(둔감): %s" % sorted(missing))
    return problems


def main():
    problems = selftest()
    if problems:
        print("🔴 자체 점검 실패 — 검사기를 믿을 수 없으므로 실물을 재지 않는다")
        for p in problems:
            print("   " + p)
        return 2
    print("✅ 자체 점검 통과 (양성 1본 무이슈 · 음성 1본 전 검사 발화)")

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
