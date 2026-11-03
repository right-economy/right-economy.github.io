#!/usr/bin/env python3
"""도식 SVG 글자의 **실제로 계산된 색**으로 WCAG 대비를 잰다.

왜 파일을 읽는 것으로는 안 되나 — SVG 의 `fill` 은 presentation attribute 라
같은 파일 `<style>` 규칙에 진다. 파일에는 `fill="#ffffff"` 가 멀쩡히 적혀
있는데 화면에는 `text{fill:#2a2521}` 이 찍히고, 어두운 상자 위에서는 대비
1.00 이 된다. 2026-10-18·11-03 두 번 다 이 방식으로만 갈렸다.

`scripts/seo_audit.py` 의 `svg-fill-loses-to-css` 는 **지정이 무시되나**까지만
본다(순수 파이썬이라 게이트에서 매번 돈다). 이 파일은 **그래서 안 보이나**를
재는 쪽이고, 헤드리스 브라우저가 필요해서 감사에 넣지 않았다 — SEO 감사
슬롯에서 손으로 부른다.

글은 SVG 를 `<img>` 로 부르므로 SVG 한 장이 그 자체로 한 문서다. 그래서
iframe 으로 파일을 그대로 띄우는 것이 실제 렌더 맥락과 같다(사이트
스타일시트는 끼어들지 않는다).

사용법:
    python3 scripts/svg_contrast.py [그림 디렉터리] [--all]
    기본 디렉터리 public/images/posts · 기본 출력은 AA(4.5) 미달만, --all 은 전부
"""

import glob
import html
import json
import os
import subprocess
import sys
import tempfile

AA = 4.5
CHUNK = 30

# playwright 가 받아둔 headless shell 을 쓴다. 버전 폴더는 환경마다 다르므로
# 박지 않고 찾는다 — 박으면 다음 업데이트에서 조용히 「0장」이 된다.
CHROME_GLOBS = [
    os.path.expanduser("~/Library/Caches/ms-playwright/chromium_headless_shell-*/"
                       "chrome-headless-shell-mac-arm64/chrome-headless-shell"),
    os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/"
                       "chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

JS = r"""
function lum(c){
  const m = c.match(/[\d.]+/g); if(!m) return null;
  const f = m.slice(0,3).map(v=>{v=v/255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4);});
  return 0.2126*f[0]+0.7152*f[1]+0.0722*f[2];
}
function ratio(a,b){const L1=Math.max(a,b),L2=Math.min(a,b);return (L1+0.05)/(L2+0.05);}
function measure(doc, name){
  const out=[];
  const shapes=[...doc.querySelectorAll('rect,circle,ellipse,path,polygon')];
  for(const t of doc.querySelectorAll('text')){
    const txt=(t.textContent||'').trim(); if(!txt) continue;
    const r=t.getBoundingClientRect(); if(!r.width||!r.height) continue;
    const cx=r.left+r.width/2, cy=r.top+r.height/2;
    const fg=getComputedStyle(t).fill;
    // 글자 중심을 덮는 도형 중 문서 순서상 가장 나중 = 가장 위에 그려진 것
    let bg=null;
    for(const s of shapes){
      const sr=s.getBoundingClientRect();
      if(cx>=sr.left&&cx<=sr.right&&cy>=sr.top&&cy<=sr.bottom){
        const f=getComputedStyle(s).fill;
        if(f&&f!=='none'&&!/rgba\(0, 0, 0, 0\)/.test(f)) bg=f;
      }
    }
    if(!bg) bg='rgb(255, 255, 255)';   // <img> 배경은 투명 → 글 본문 배경(흰색)
    const lf=lum(fg), lb=lum(bg);
    if(lf===null||lb===null) continue;
    out.push({file:name, text:txt.slice(0,40), fg:fg, bg:bg,
              attr:t.getAttribute('fill'), ratio:+ratio(lf,lb).toFixed(2)});
  }
  return out;
}
const res=[];
for(const fr of document.querySelectorAll('iframe')){
  try{ res.push(...measure(fr.contentDocument, fr.dataset.name)); }
  catch(e){ res.push({file:fr.dataset.name, error:String(e)}); }
}
document.body.textContent='@@'+JSON.stringify(res)+'@@';
"""


def find_chrome():
    for pat in CHROME_GLOBS:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def run(chrome, chunk):
    body = "\n".join(
        '<iframe src="file://%s" data-name="%s" width="900" height="600" style="border:0"></iframe>'
        % (html.escape(os.path.abspath(f)), html.escape(f)) for f in chunk)
    page = ("<!doctype html><meta charset=utf-8><body>" + body +
            "<script>window.addEventListener('load',()=>{setTimeout(()=>{" + JS + "},300)});</script>")
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
        fh.write(page)
        path = fh.name
    try:
        p = subprocess.run([chrome, "--headless", "--disable-gpu", "--no-sandbox",
                            "--allow-file-access-from-files", "--virtual-time-budget=8000",
                            "--dump-dom", "file://" + path],
                           capture_output=True, text=True, timeout=180)
        if p.returncode != 0 or "@@" not in p.stdout:
            return None      # 측정 실패는 0건이 아니라 「모름」이다
        return json.loads(p.stdout.split("@@")[1])
    except subprocess.TimeoutExpired:
        return None
    finally:
        os.unlink(path)


def _hex_rgb(h):
    if not h or not h.startswith("#"):
        return None
    h = h[1:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _rgb_tuple(s):
    nums = [int(float(x)) for x in (s or "").replace(",", " ")
            .replace("(", " ").replace(")", " ").split() if x.replace(".", "", 1).isdigit()]
    return tuple(nums[:3]) if len(nums) >= 3 else None


def _ignored(row):
    """파일에 적힌 색과 실제로 찍힌 색이 다른가 = 스타일시트가 이겼나."""
    want, got = _hex_rgb(row.get("attr")), _rgb_tuple(row.get("fg"))
    return bool(want and got and want != got)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_all = "--all" in sys.argv
    images = args[0] if args else os.path.join("public", "images", "posts")
    chrome = find_chrome()
    if not chrome:
        print("🔴 헤드리스 브라우저를 못 찾았다 — 대비를 못 쟀다(0건이 아니라 모름)")
        return 2
    files = sorted(glob.glob(os.path.join(images, "**", "*.svg"), recursive=True))
    if not files:
        print("🔴 SVG 를 한 장도 못 찾았다: %s (0건이 아니라 모름)" % images)
        return 2

    rows, failed = [], []
    for i in range(0, len(files), CHUNK):
        chunk = files[i:i + CHUNK]
        got = run(chrome, chunk)
        if got is None:
            failed += chunk
        else:
            rows += got
    errs = [r for r in rows if "error" in r]
    rows = [r for r in rows if "ratio" in r]
    print("📐 글자 %d개 / SVG %d장 (%s)" % (len(rows), len(files), images))
    if failed or errs:
        print("🔴 못 잰 파일 %d장 — 아래 숫자는 전수가 아니다" % (len(failed) + len(errs)))
        for f in (failed + [e["file"] for e in errs])[:10]:
            print("   %s" % f)

    bad = sorted([r for r in rows if r["ratio"] < AA], key=lambda r: r["ratio"])
    show = rows if show_all else bad
    print("AA(%.1f) 미달 %d개 · 그중 지정색이 무시된 자리 %d개"
          % (AA, len(bad), len([r for r in bad if _ignored(r)])))
    for r in sorted(show, key=lambda r: r["ratio"]):
        lost = _ignored(r)
        print("   %5.2f  %-22s on %-22s %s| %s 「%s」"
              % (r["ratio"], r["fg"], r["bg"], "지정=%s 무시 " % r["attr"] if lost else "",
                 os.path.relpath(r["file"], images), r["text"][:24]))
    return 1 if (bad or failed or errs) else 0


if __name__ == "__main__":
    sys.exit(main())
