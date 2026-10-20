"""
스타일씨(stylec.co.kr) 체험단 신청 자동화
------------------------------------------
- 목록 페이지에서 체험단 링크를 모은 뒤, 각 상세 페이지에서 '신청하기'를 눌러 신청서를 채워 제출합니다.
- 로그인은 처음 한 번만 직접 하면 브라우저 프로필(./stylec_profile)에 저장되어 계속 재사용됩니다.
- 기본은 '미리보기(dry-run)' 모드라 실제 제출하지 않습니다. 실제 신청은 --apply 를 붙이세요.

사용 예:
  python stylec_auto.py --login                       # 최초 1회: 브라우저에서 직접 로그인
  python stylec_auto.py                               # 미리보기 (어떤 체험단을 신청할지 확인만)
  python stylec_auto.py --apply --max 10              # 실제로 최대 10개 신청
  python stylec_auto.py --apply --include 화장품,헤어 --exclude 방문형
  python stylec_auto.py --export-session              # GitHub Actions 용 로그인 상태 내보내기
  python stylec_auto.py --apply --history docs/data/history.json   # 대시보드 기록 남기기
"""

import argparse
import base64
import gzip
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE = "https://www.stylec.co.kr"
# 기본: 제공형 필터, 1페이지부터 50개씩
DEFAULT_LIST_URL = (
    BASE + "/index.php?sortOption=popular&pageNumber=1&count=50"
    "&campaignType=%EC%A0%9C%EA%B3%B5%ED%98%95"
    "&category=&region=&sns=&include_finish=false"
)
DEFAULT_TYPES = "제공형"
LIST_MARKER = "다른 체험단도 둘러볼까요"
PROFILE_DIR = Path("stylec_profile")
LOG_FILE = Path("applied_log.json")
SHOT_DIR = Path("screenshots")
KST = timezone(timedelta(hours=9))
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

# 상세 페이지 링크로 판단할 패턴 (사이트 구조가 바뀌면 여기만 고치면 됩니다)
DETAIL_PATTERNS = [
    re.compile(r"/trials/\d+"),
    re.compile(r"/trials/view/\d+"),
    re.compile(r"[?&]wr_id=\d+"),
]

APPLY_BTN_TEXTS = ["체험단 신청하기", "신청하기", "캠페인 신청", "지원하기"]
SUBMIT_BTN_TEXTS = ["신청 완료", "신청완료하기", "신청하기", "제출하기", "확인"]
DONE_TEXTS = ["신청완료", "신청 완료", "이미 신청", "마감", "모집종료", "모집 종료", "종료된"]


# ---------------------------------------------------------------- 로그 관리
def load_log():
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_log(log):
    LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def human_pause(base, jitter=0.5):
    time.sleep(max(0.3, base + random.uniform(-jitter, jitter)))


# ---------------------------------------------------------------- 대시보드 기록 (docs/data/history.json)
MAX_RECORDS = 3000
MAX_RUNS = 180


def now_kst():
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def load_history(path):
    if path and Path(path).exists():
        try:
            h = json.loads(Path(path).read_text(encoding="utf-8"))
            h.setdefault("runs", [])
            h.setdefault("records", [])
            return h
        except Exception as e:
            print(f"  ※ 기록 파일을 읽지 못해 새로 만듭니다: {e}")
    return {"runs": [], "records": []}


def save_history(path, h):
    if not path:
        return
    h["records"] = h["records"][-MAX_RECORDS:]
    h["runs"] = h["runs"][-MAX_RUNS:]
    h["updated_at"] = now_kst()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")


def status_of(result):
    kind = result.split(":")[0]
    return {"ok": "ok", "ok?": "check", "manual": "manual", "fail": "fail"}.get(kind, "skip")


def history_applied_urls(h):
    return {r["url"] for r in h["records"] if r.get("status") in ("ok", "check")}


# ---------------------------------------------------------------- 로그인 확인
def is_logged_in(page):
    page.goto(BASE + "/", wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    return page.locator("a[href*='auth/logout']").count() > 0


# ---------------------------------------------------------------- 목록 수집
def is_detail_link(href):
    return any(p.search(href) for p in DETAIL_PATTERNS)


ID_KEYS = ["wr_id", "trial_id", "trialId", "id", "idx", "seq", "no"]
TITLE_KEYS = ["wr_subject", "subject", "title", "trial_title", "trial_name", "name", "product_name"]


def find_item_list(obj):
    """JSON 안에서 가장 큰 '객체 목록'을 찾아 반환 (체험단 목록으로 간주)."""
    best = []
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            best = obj
        for x in obj:
            sub = find_item_list(x)
            if len(sub) > len(best):
                best = sub
    elif isinstance(obj, dict):
        for v in obj.values():
            sub = find_item_list(v)
            if len(sub) > len(best):
                best = sub
    return best


IMG_RE = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)


def pick_image(item):
    """목록 데이터에서 대표 이미지 주소를 찾음 (없으면 빈 문자열)."""
    cands = []
    for k, v in item.items():
        if isinstance(v, str) and IMG_RE.search(v):
            score = 0 if re.search(r"img|image|thumb|photo|main", k, re.I) else 1
            cands.append((score, v))
    if not cands:
        return ""
    v = sorted(cands, key=lambda x: x[0])[0][1].strip()
    if v.startswith("//"):
        return "https:" + v
    if v.startswith("http"):
        return v
    if v.startswith("/"):
        return BASE + v
    return "https://cdn.stylec.co.kr/" + v


def pick(item, keys):
    for k in keys:
        if item.get(k) not in (None, ""):
            return item[k], k
    return None, None


def detect_detail_template(page):
    """페이지에 있는 아무 체험단 상세 링크를 보고 주소 형식을 알아냄 (예: /trials/{id})."""
    hrefs = page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
    for h in hrefs:
        if not h.startswith(BASE):
            continue
        for p in DETAIL_PATTERNS:
            m = p.search(h)
            if m:
                num = re.search(r"\d+", m.group(0)).group(0)
                return h.split("#")[0].replace(num, "{id}", 1), h
    return BASE + "/trials/{id}", None


def collect_campaigns(page, list_url, debug=False, max_pages=20):
    from urllib.parse import parse_qs, urlparse

    lq = parse_qs(urlparse(list_url).query)
    want_page = (lq.get("pageNumber") or ["1"])[0]
    want_count = (lq.get("count") or [""])[0]

    captured, api_hits = [], []

    def on_response(resp):
        if "json" in resp.headers.get("content-type", ""):
            api_hits.append(resp.url)
        if "/v1/trial?" in resp.url:
            captured.append(resp)

    page.on("response", on_response)
    page.goto(list_url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    human_pause(1.0)
    page.remove_listener("response", on_response)

    # 우리가 연 목록(같은 page/count)에 해당하는 응답만 사용 — 배달형·방문형 추천 목록은 제외
    target = None
    for r in captured:
        q = parse_qs(urlparse(r.url).query)
        if (q.get("page") or [""])[0] == want_page and (not want_count or (q.get("count") or [""])[0] == want_count):
            target = r
            break

    template, sample_link = detect_detail_template(page)

    items = []
    if target:
        try:
            items = find_item_list(target.json())
        except Exception as e:
            print(f"  ※ 목록 데이터를 읽지 못함: {e}")

    # 다음 페이지들은 같은 목록 데이터 주소에 page 번호만 바꿔 직접 요청 (끝까지)
    if target and items:
        headers = {k: v for k, v in target.request.all_headers().items()
                   if not k.startswith(":") and k.lower() not in ("content-length", "host")}
        per_page = len(items)
        pno = int(want_page)
        print(f"  {pno}페이지: {len(items)}개")
        while pno - int(want_page) + 1 < max_pages:
            pno += 1
            next_url = re.sub(r"([?&]page=)\d+", lambda m: m.group(1) + str(pno), target.url)
            try:
                resp = page.request.get(next_url, headers=headers)
                if not resp.ok:
                    print(f"  {pno}페이지 요청 실패 (HTTP {resp.status})")
                    break
                more = find_item_list(resp.json())
            except Exception as e:
                print(f"  {pno}페이지 읽기 실패: {e}")
                break
            if not more:
                break
            items += more
            print(f"  {pno}페이지: {len(more)}개")
            if len(more) < per_page:
                break
            human_pause(0.8, 0.3)

    campaigns, seen, id_key = [], set(), None
    for it in items:
        cid, id_key = pick(it, ID_KEYS)
        if cid is None:
            continue
        title, _ = pick(it, TITLE_KEYS)
        url = template.replace("{id}", str(cid))
        if url in seen:
            continue
        seen.add(url)
        campaigns.append({"url": url, "title": str(title or "")[:120], "image": pick_image(it)})

    if debug:
        print("\n[debug] JSON 응답 URL:")
        for u in dict.fromkeys(api_hits):
            print("   ", u)
        print(f"\n[debug] 사용한 목록 데이터: {target.url if target else '못 찾음'}")
        print(f"[debug] 항목 수: {len(items)}  /  ID 필드: {id_key}")
        print(f"[debug] 상세 주소 형식: {template}  (참고한 링크: {sample_link})")
        if items:
            sample = {k: (str(v)[:40]) for k, v in list(items[0].items())[:25]}
            print("[debug] 첫 항목 필드:", json.dumps(sample, ensure_ascii=False))
        for c in campaigns[:3]:
            print("[debug] 예시:", c["url"], "|", c["title"][:40])
    return campaigns


def keyword_ok(text, include, exclude):
    if include and not any(k in text for k in include):
        return False
    if exclude and any(k in text for k in exclude):
        return False
    return True


# ---------------------------------------------------------------- 신청 처리
def find_visible_button(page, texts):
    for t in texts:
        loc = page.locator(
            f"button:has-text('{t}'), a:has-text('{t}'), input[type=submit][value*='{t}'], "
            f"[role=button]:has-text('{t}')"
        )
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible() and el.is_enabled():
                    return el
            except Exception:
                continue
    return None


def safe_click(el):
    """일반 클릭이 팝업 등에 가려 실패하면 강제 클릭으로 재시도."""
    try:
        el.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        el.click(timeout=8000)
    except Exception:
        el.click(force=True, timeout=8000)


def fill_form(page, message):
    """보이는 신청서 입력칸을 채웁니다. 채우지 못한 필수칸이 있으면 False."""
    # 동의 체크박스 전부 체크
    boxes = page.locator("input[type=checkbox]")
    for i in range(boxes.count()):
        cb = boxes.nth(i)
        try:
            if cb.is_visible() and not cb.is_checked():
                cb.check(force=True)
        except Exception:
            # 커스텀 체크박스면 라벨 클릭
            try:
                cid = cb.get_attribute("id")
                if cid:
                    page.locator(f"label[for='{cid}']").first.click()
            except Exception:
                pass

    # 비어 있는 textarea 에 신청 메시지 입력
    if message:
        tas = page.locator("textarea")
        for i in range(tas.count()):
            ta = tas.nth(i)
            try:
                if ta.is_visible() and not ta.input_value().strip():
                    ta.fill(message)
            except Exception:
                pass

    # 라디오 그룹은 첫 번째 옵션 선택 (SNS 채널 선택 등) — 이미 선택된 그룹은 건드리지 않음
    radios = page.locator("input[type=radio]")
    groups = {}
    for i in range(radios.count()):
        r = radios.nth(i)
        try:
            if r.is_visible():
                groups.setdefault(r.get_attribute("name") or f"_{i}", []).append(r)
        except Exception:
            pass
    for rs in groups.values():
        if not any(r.is_checked() for r in rs):
            try:
                rs[0].check(force=True)
            except Exception:
                pass

    # select 박스는 첫 번째 유효 옵션 선택
    sels = page.locator("select")
    for i in range(sels.count()):
        s = sels.nth(i)
        try:
            if s.is_visible() and not s.input_value():
                opts = s.locator("option").all()
                vals = [o.get_attribute("value") for o in opts if o.get_attribute("value")]
                if vals:
                    s.select_option(vals[0])
        except Exception:
            pass

    # 비어 있는 필수 입력칸 확인 (주소·연락처 등은 계정 정보로 자동 채워지는 경우가 많음)
    empty_required = page.evaluate(
        """() => Array.from(document.querySelectorAll('input[required], textarea[required], select[required]'))
              .filter(e => e.offsetParent !== null && !e.value && e.type !== 'checkbox' && e.type !== 'radio')
              .map(e => e.name || e.id || e.placeholder || e.tagName)"""
    )
    return empty_required


def page_product_image(page):
    """상세 페이지의 대표 제품 이미지 주소. og:image 를 우선 쓰고, 없거나 사이트 공통 이미지면 본문에서 가장 큰 사진."""
    try:
        return page.evaluate(
            """() => {
                const bad = s => !s || s.startsWith('data:') || /\\.svg|\\/ui\\/|banner|logo|icon|ico_|profile|avatar/i.test(s);
                const og = document.querySelector('meta[property="og:image"]')?.content;
                if (og && !bad(og)) return new URL(og, location.href).href;
                let best = '', area = 0;
                for (const img of document.images) {
                    const src = img.currentSrc || img.src;
                    if (bad(src)) continue;
                    const r = img.getBoundingClientRect();
                    if (r.width < 150 || r.height < 150) continue;
                    const a = (img.naturalWidth || r.width) * (img.naturalHeight || r.height);
                    if (a > area) { area = a; best = src; }
                }
                return best;
            }"""
        ) or ""
    except Exception:
        return ""


def backfill_images(page, hist, path, limit=80):
    """이미지가 확인되지 않은 기록의 상세 페이지를 열어 제품 이미지를 채움 (한 번 확인한 기록은 다시 안 봄)."""
    todo = [r for r in hist["records"] if not r.get("img_checked") and r.get("url", "").startswith(BASE)][:limit]
    if not todo:
        return
    print(f"\n[썸네일] 기록 {len(todo)}개의 제품 이미지를 가져옵니다")
    found = 0
    for r in todo:
        try:
            page.goto(r["url"], wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeout:
                pass
            img = page_product_image(page)
            if img:
                r["image"] = img
                found += 1
        except Exception as e:
            print(f"  ※ {r['url']} 이미지 실패: {str(e).splitlines()[0][:60]}")
        r["img_checked"] = True
        human_pause(1.0, 0.3)
    save_history(path, hist)
    print(f"[썸네일] {found}개 채움")


def apply_one(page, camp, args):
    page.goto(camp["url"], wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass
    human_pause(1.0)

    body = page.inner_text("body")
    img = page_product_image(page)
    if img:
        camp["image"] = img
    camp["img_checked"] = True
    if not camp["title"]:
        camp["title"] = (page.title() or "")[:120]

    if not keyword_ok(camp["title"] + " " + body[:3000], args.include, args.exclude):
        return "skip:키워드 필터"

    # 체험단 유형 확인 (상단 메뉴·하단 정보 글자는 빼고 본문에서만 확인)
    if args.types:
        content = page.evaluate(
            """() => {
                const c = document.body.cloneNode(true);
                c.querySelectorAll('header, nav, footer, [class*=header], [class*=gnb], [class*=footer], [class*=menu]')
                 .forEach(e => e.remove());
                return c.textContent.replace(/\\s+/g, ' ');
            }"""
        )
        if not any(t in content for t in args.types):
            other = [t for t in ["배달형", "방문형", "기자단", "배송형"] if t in content]
            return f"skip:유형 다름{('(' + ','.join(other) + ')') if other else ''}"

    btn = find_visible_button(page, APPLY_BTN_TEXTS)
    if not btn:
        if any(t in body for t in DONE_TEXTS):
            return "skip:마감/이미 신청"
        return "skip:신청 버튼 없음"

    if not args.apply:
        return "dry-run:신청 가능"

    safe_click(btn)
    human_pause(1.5)

    if "auth/login" in page.url:
        return "fail:로그인 필요 (--login 먼저 실행)"

    empty = fill_form(page, args.message)
    if empty:
        SHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOT_DIR / f"manual_{int(time.time())}.png"), full_page=True)
        return f"manual:필수칸 비어 있음 {empty}"

    submit = find_visible_button(page, SUBMIT_BTN_TEXTS)
    if not submit:
        SHOT_DIR.mkdir(exist_ok=True)
        page.screenshot(path=str(SHOT_DIR / f"nosubmit_{int(time.time())}.png"), full_page=True)
        return "manual:제출 버튼을 못 찾음"

    safe_click(submit)
    human_pause(2.0)

    after = page.inner_text("body")
    if any(k in after for k in ["신청이 완료", "신청완료", "신청 완료", "접수되었"]):
        return "ok:신청 완료"
    return "ok?:제출함 (결과 문구 미확인 — MY체험단에서 확인)"


# ---------------------------------------------------------------- 메인
def main():
    ap = argparse.ArgumentParser(description="스타일씨 체험단 신청 자동화")
    ap.add_argument("--url", default=DEFAULT_LIST_URL, help="체험단 목록 페이지 URL")
    ap.add_argument("--types", default=DEFAULT_TYPES,
                    help="신청할 체험단 유형(쉼표 구분). 상세 페이지에서 확인 후 다르면 건너뜀. 빈 값이면 확인 안 함")
    ap.add_argument("--pages", type=int, default=30, help="목록을 최대 몇 페이지까지 읽을지 (기본 30, 보통 끝까지 다 읽음)")
    ap.add_argument("--login", action="store_true", help="브라우저를 열어 직접 로그인 후 세션 저장")
    ap.add_argument("--export-session", action="store_true",
                    help="저장된 로그인 상태를 GitHub Secrets 용 텍스트(session_b64.txt)로 내보내기")
    ap.add_argument("--storage-state", default="", help="로그인 상태 파일(session.json)로 실행 (GitHub Actions 용)")
    ap.add_argument("--history", default="", help="대시보드 기록 파일 경로 (예: docs/data/history.json)")
    ap.add_argument("--apply", action="store_true", help="실제로 신청 (없으면 미리보기)")
    ap.add_argument("--max", type=int, default=20, help="이번 실행에서 최대 신청 개수")
    ap.add_argument("--delay", type=float, default=6.0, help="체험단 사이 대기 시간(초)")
    ap.add_argument("--include", default="", help="포함 키워드(쉼표 구분) — 하나라도 있어야 신청")
    ap.add_argument("--exclude", default="", help="제외 키워드(쉼표 구분)")
    ap.add_argument("--message", default="", help="신청서의 빈 텍스트칸에 넣을 문구")
    ap.add_argument("--headless", action="store_true", help="브라우저 창 숨기기")
    ap.add_argument("--debug", action="store_true", help="링크/API 탐색 정보 출력")
    args = ap.parse_args()

    args.include = [k.strip() for k in args.include.split(",") if k.strip()]
    args.exclude = [k.strip() for k in args.exclude.split(",") if k.strip()]
    args.types = [k.strip() for k in args.types.split(",") if k.strip()]

    hist = load_history(args.history)
    run = {"started_at": now_kst(), "finished_at": "", "status": "running",
           "listed": 0, "ok": 0, "check": 0, "manual": 0, "fail": 0, "skip": 0}

    with sync_playwright() as p:
        browser = None
        if args.storage_state:
            browser = p.chromium.launch(headless=args.headless)
            ctx = browser.new_context(storage_state=args.storage_state, locale="ko-KR",
                                      viewport={"width": 1280, "height": 900}, user_agent=USER_AGENT)
        else:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=args.headless and not args.login and not args.export_session,
                locale="ko-KR",
                viewport={"width": 1280, "height": 900},
                user_agent=USER_AGENT,
            )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("dialog", lambda d: d.accept())  # alert/confirm 자동 확인

        def finish(code=0):
            ctx.close()
            if browser:
                browser.close()
            if code:
                sys.exit(code)

        if args.login:
            page.goto(BASE + "/auth/login")
            input("브라우저에서 로그인을 마친 뒤 여기서 Enter 를 누르세요...")
            print("로그인 세션을 저장했습니다.")
            return finish()

        if args.export_session:
            if not is_logged_in(page):
                print("로그인되어 있지 않습니다. 먼저 python stylec_auto.py --login 으로 로그인하세요.")
                return finish(1)
            state = ctx.storage_state()
            # 스타일씨 로그인에 필요한 것만 남김 (카카오·광고 등 다른 사이트 쿠키 제외) → 시크릿 크기 제한(48KB) 대응
            state = {
                "cookies": [c for c in state.get("cookies", []) if "stylec" in c.get("domain", "")],
                "origins": [o for o in state.get("origins", []) if "stylec" in o.get("origin", "")],
            }
            raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            packed = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
            Path("session_b64.txt").write_text(packed, encoding="ascii")
            print(f"로그인 상태를 session_b64.txt 로 내보냈습니다. (쿠키 {len(state['cookies'])}개, {len(packed) / 1024:.1f}KB)")
            if len(packed) > 48 * 1024:
                print("※ 48KB를 넘어 GitHub 시크릿에 들어가지 않습니다. 이 메시지를 그대로 알려주세요.")
            print("이 파일 내용 전체를 GitHub 저장소 Settings > Secrets > Actions 의 STYLEC_SESSION 에 붙여넣으세요.")
            print("※ 이 파일은 계정 열쇠와 같습니다. 다른 사람에게 보내거나 저장소에 올리지 마세요.")
            return finish()

        # 로그인 확인 (만료 시 시트에 기록하고 실패로 종료 → GitHub 가 실패 알림 메일 발송)
        def close_run(status):
            run["status"] = status
            run["finished_at"] = now_kst()
            if args.apply and args.history:
                hist["runs"].append(run)
                save_history(args.history, hist)

        if args.apply and not is_logged_in(page):
            print("fail:로그인 만료 — session_b64.txt 다시 내보내서 STYLEC_SESSION 갱신 필요")
            close_run("login_expired")
            return finish(2)

        # 목록 수집 (사이트 목록 데이터를 페이지 끝까지 읽음)
        campaigns = collect_campaigns(page, args.url, debug=args.debug, max_pages=args.pages)
        print(f"[목록] 총 {len(campaigns)}개 수집")

        if not campaigns:
            print("체험단 목록을 찾지 못했습니다. --debug 로 실행해 확인하세요.")
            close_run("no_list")
            return finish(1)

        log = load_log()
        already = {u for u, v in log.items() if v.get("result", "").startswith("ok")}
        already |= history_applied_urls(hist)
        run["listed"] = len(campaigns)

        mode = "실제 신청" if args.apply else "미리보기"
        print(f"\n총 {len(campaigns)}개 · 이미 신청 {len(already)}개 · 모드: {mode} · 최대 {args.max}개\n")

        done = 0
        for i, camp in enumerate(campaigns, 1):
            if done >= args.max:
                break
            if camp["url"] in already:
                continue
            try:
                result = apply_one(page, camp, args)
            except Exception as e:
                result = f"fail:{type(e).__name__}: {str(e).splitlines()[0][:80]}"

            print(f"[{i:>3}] {result:<32} {camp['title'][:50]}  {camp['url']}")
            st = status_of(result)
            if result.startswith("dry-run"):
                st = "dryrun"
            if st in run:
                run[st] += 1
            if args.apply:
                log[camp["url"]] = {"title": camp["title"], "result": result,
                                    "at": datetime.now(KST).isoformat(timespec="seconds")}
                save_log(log)
                if args.history and st != "skip":  # 건너뛴 건(마감·유형 다름 등)은 기록하지 않음
                    hist["records"].append({"at": now_kst(), "status": st, "result": result,
                                            "title": camp["title"], "url": camp["url"],
                                            "image": camp.get("image", ""),
                                            "img_checked": camp.get("img_checked", False)})
                    save_history(args.history, hist)  # 중간에 끊겨도 기록이 남도록 매번 저장
            if result.startswith(("ok", "dry-run")):
                done += 1
            human_pause(args.delay, args.delay * 0.3)

        print(f"\n요약: 신청 {run['ok']} · 확인 필요 {run['check']} · 수동 필요 {run['manual']}"
              f" · 실패 {run['fail']} · 건너뜀 {run['skip']} (목록 {len(campaigns)}개)")
        close_run("success")
        if args.apply and args.history:
            backfill_images(page, hist, args.history)
        finish()


if __name__ == "__main__":
    main()
