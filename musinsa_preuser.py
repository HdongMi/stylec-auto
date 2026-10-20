"""
무신사 체험단 자동 신청 프로그램 (v2)
=====================================

사용법 (cmd 에서 이 파일이 있는 폴더로 이동한 뒤)
------
1) 로그인 (최초 1회, 로그인이 풀렸을 때만)
       python musinsa_preuser.py --login
   -> 브라우저가 뜨면 로그인하세요. 로그인이 확인되면 자동으로 저장되고 닫힙니다.

2) 테스트 (최종 '신청 완료' 버튼은 누르지 않음, 3개만)
       python musinsa_preuser.py --dry-run --limit 3

3) 실제 실행 (하루 한 번)
       python musinsa_preuser.py

결과는 logs/날짜_시간/ 폴더에 log.txt 와 스크린샷으로 남습니다.

4) GitHub 자동 실행용 로그인 상태 내보내기 (로그인 후)
       python musinsa_preuser.py --export-session
   -> musinsa_session_b64.txt 내용을 GitHub Secrets 의 MUSINSA_SESSION 에 넣으세요.

5) 대시보드 기록까지 남기기 (GitHub Actions 가 쓰는 방식)
       python musinsa_preuser.py --headless --storage-state session.json --history docs/data/musinsa.json
"""

import argparse
import base64
import gzip
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ============================================================
# 설정 - 여기만 본인에 맞게 수정하세요
# ============================================================

# 체험단 목록 필터 (gf=M 페이지 = 남성 + 공용)
GENDERS = ["M", "A"]

# 옷 사이즈: 남아있는 사이즈 중 가장 큰 것을 고릅니다 (XL > L > M ..., 110 > 105 > 100 ...)
# 신발 사이즈(mm): 정확히 없으면 가장 가까운 사이즈
SHOE_SIZE = 280
# 반지 호수: 정확히 없으면 가장 가까운 호수
RING_SIZE = 21

# 색상/상품 선호 (여러 색상·상품 중 고를 때). 없으면 첫 번째 것을 고릅니다.
PREFERRED_OPTIONS = ["블랙", "BLACK", "네이비", "NAVY"]

# 이름에 이 단어가 들어간 체험단은 건너뜀 (예: ["키즈", "[샘플]"]). 비워두면 전부 신청
EXCLUDE_KEYWORDS = []

# 체험단 사이 대기 시간(초)
DELAY_RANGE = (2, 4)

# ============================================================

VERSION = "v6"

BASE = Path(__file__).resolve().parent
STATE_FILE = BASE / "musinsa_login.json"
LOG_DIR = BASE / "logs" / datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d_%H%M")

LIST_API = "https://www.musinsa.com/preuser/api/v1/event/events"
LOGIN_API = "https://my.musinsa.com/api/member/v1/login-status"
LOGIN_URL = "https://www.musinsa.com/auth/login"
MAIN_URL = "https://www.musinsa.com/preuser/main?gf=M"
EVENT_URL = "https://www.musinsa.com/preuser/events/{id}"

KST = timezone(timedelta(hours=9))
SESSION_EXPORT = BASE / "musinsa_session_b64.txt"
MAX_RECORDS, MAX_RUNS = 3000, 180

SOLDOUT = re.compile(r"품절|sold\s*out|재입고", re.I)
DONE_MSG = re.compile(r"신청이\s*완료되었습니다")

dialogs = []  # 페이지에서 뜬 alert 메시지 모음


# ------------------------------------------------------------
# 대시보드 기록 (docs/data/musinsa.json)
# ------------------------------------------------------------
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
            print(f"기록 파일을 읽지 못해 새로 만듭니다: {e}")
    return {"runs": [], "records": []}


def save_history(path, h):
    if not path:
        return
    h["records"] = h["records"][-MAX_RECORDS:]
    h["runs"] = h["runs"][-MAX_RUNS:]
    h["updated_at"] = now_kst()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")


def write_live(path, live):
    """실행 중 진행 상황 파일 (대시보드가 실시간으로 읽음). 실패해도 신청에는 영향 없음."""
    if not path:
        return
    try:
        live["updated_at"] = now_kst()
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(live, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        print(f"진행 파일 기록 실패: {e}")


def status_of(result):
    """결과 문구 → 대시보드 상태 (ok / check / fail / skip / dryrun / login)."""
    key = result.split()[0]
    return {"OK": "ok", "UNKNOWN": "check", "FAIL": "fail", "ERROR": "fail",
            "SKIP": "skip", "DRY-RUN": "dryrun", "LOGIN": "login"}.get(key, "fail")


# ------------------------------------------------------------
# 썸네일
# ------------------------------------------------------------
IMG_RE = re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)


def _abs_img(v):
    v = v.strip()
    if v.startswith("//"):
        return "https:" + v
    if v.startswith("/"):
        return "https://image.msscdn.net" + v
    return v


def event_image(ev, depth=0):
    """체험단 목록 데이터에서 대표 이미지 주소를 찾음 (이미지·썸네일 이름의 필드 우선)."""
    best = ""
    if depth > 3:
        return best
    if isinstance(ev, dict):
        items = sorted(ev.items(), key=lambda kv: 0 if re.search(r"thumb|image|img", kv[0], re.I) else 1)
        for k, v in items:
            if isinstance(v, str) and (IMG_RE.search(v) or (re.search(r"thumb|image|img", k, re.I) and v.startswith(("http", "//")))):
                return _abs_img(v)
            if isinstance(v, (dict, list)):
                best = best or event_image(v, depth + 1)
    elif isinstance(ev, list):
        for v in ev:
            best = best or event_image(v, depth + 1)
    return best


PAGE_IMAGE_JS = """() => {
    const bad = s => !s || s.startsWith('data:') || /\\.svg|logo|icon|ico_|banner|profile|avatar|badge/i.test(s);
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


def page_image(page):
    try:
        return page.evaluate(PAGE_IMAGE_JS) or ""
    except Exception:
        return ""


def backfill_images(page, hist, path, limit=80):
    todo = [r for r in hist["records"] if not r.get("img_checked")][:limit]
    if not todo:
        return
    log(f"[썸네일] 기록 {len(todo)}개의 제품 이미지를 가져옵니다")
    found = 0
    for r in todo:
        try:
            page.goto(r["url"], wait_until="domcontentloaded", timeout=20000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PWTimeout:
                pass
            img = page_image(page)
            if img:
                r["image"] = img
                found += 1
        except Exception as e:
            log(f"    이미지 실패: {str(e).splitlines()[0][:60]}")
        r["img_checked"] = True
        nap(0.8, 1.2)
    save_history(path, hist)
    log(f"[썸네일] {found}개 채움")


# ------------------------------------------------------------
# GitHub 용 로그인 상태 내보내기
# ------------------------------------------------------------
def export_session():
    if not STATE_FILE.exists():
        print("로그인 정보가 없습니다. 먼저 'python musinsa_preuser.py --login' 을 실행하세요.")
        return 1
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state = {
        "cookies": [c for c in state.get("cookies", []) if "musinsa" in c.get("domain", "")],
        "origins": [o for o in state.get("origins", []) if "musinsa" in o.get("origin", "")],
    }
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packed = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    SESSION_EXPORT.write_text(packed, encoding="ascii")
    print(f"로그인 상태를 {SESSION_EXPORT.name} 로 내보냈습니다. (쿠키 {len(state['cookies'])}개, {len(packed) / 1024:.1f}KB)")
    if len(packed) > 48 * 1024:
        print("※ 48KB를 넘어 GitHub 시크릿에 들어가지 않습니다. 이 메시지를 그대로 알려주세요.")
    print("이 파일 내용 전체를 GitHub 저장소 Settings > Secrets > Actions 의 MUSINSA_SESSION 에 붙여넣으세요.")
    print("※ 이 파일은 계정 열쇠와 같습니다. 다른 사람에게 보내거나 저장소에 올리지 마세요.")
    return 0


def log(msg: str):
    line = f"[{datetime.now(KST).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "log.txt", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def nap(a=None, b=None):
    a, b = (a, b) if a is not None else DELAY_RANGE
    time.sleep(random.uniform(a, b))


def shot(page, name: str):
    safe = re.sub(r"[^\w가-힣-]+", "_", name)[:60]
    try:
        page.screenshot(path=str(LOG_DIR / f"{safe}.png"), full_page=True)
    except Exception:
        pass


def visible(locator, timeout=1500) -> bool:
    try:
        locator.first.wait_for(state="visible", timeout=timeout)
        return True
    except PWTimeout:
        return False


# ------------------------------------------------------------
# 로그인
# ------------------------------------------------------------
def is_logged_in(ctx) -> bool:
    try:
        r = ctx.request.get(LOGIN_API, headers={"Referer": MAIN_URL})
        return bool(r.json().get("data", {}).get("loggedIn"))
    except Exception:
        return False


def do_login(browser):
    ctx = browser.new_context(locale="ko-KR", viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    page.goto(LOGIN_URL)
    print("브라우저에서 무신사에 로그인하세요. (최대 5분 기다립니다)")
    for _ in range(150):
        time.sleep(2)
        if is_logged_in(ctx):
            ctx.storage_state(path=str(STATE_FILE))
            log("로그인 확인 완료! 로그인 정보를 저장했습니다.")
            ctx.close()
            return True
    log("5분 안에 로그인이 확인되지 않았습니다. 다시 시도해 주세요.")
    ctx.close()
    return False


# ------------------------------------------------------------
# 1. 체험단 목록 (API)
# ------------------------------------------------------------
def fetch_events(ctx):
    events, page_no = [], 1
    while page_no <= 50:
        params = [("page", page_no), ("size", 18)] + [("genders", g) for g in GENDERS] + [("sort", "LATEST_REGISTRATION")]
        qs = "&".join(f"{k}={v}" for k, v in params)
        r = ctx.request.get(f"{LIST_API}?{qs}", headers={"Referer": MAIN_URL})
        data = r.json().get("data", {})
        events += data.get("content", [])
        if not data.get("hasNext"):
            break
        page_no += 1
        nap(0.5, 1)
    # 중복 제거 + 모집 중인 것만
    seen, result = set(), []
    for e in events:
        if e["id"] in seen or e.get("state") != "OPEN":
            continue
        seen.add(e["id"])
        if any(k in e["name"] for k in EXCLUDE_KEYWORDS):
            continue
        result.append(e)
    return result


# ------------------------------------------------------------
# 2. 옵션 고르기
# ------------------------------------------------------------
LETTER_RANK = {"XXS": 0, "XS": 1, "S": 2, "M": 3, "L": 4, "XL": 5, "XXL": 6, "2XL": 6,
               "XXXL": 7, "3XL": 7, "4XL": 8, "5XL": 9}
LETTER_RE = re.compile(r"(?<![A-Z0-9])(XXS|XS|S|M|L|XL|XXL|XXXL|[2-5]XL)(?![A-Z])", re.I)
NUM_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d)?)(?![\d.])")


def _letter(t):
    m = LETTER_RE.search(t.upper())
    return LETTER_RANK.get(m.group(1).upper()) if m else None


def _number(t):
    m = NUM_RE.search(t)
    return float(m.group(1)) if m else None


def _nearest(usable, values, target):
    """target 과 가장 가까운 값 (같은 거리면 큰 쪽)."""
    best = min(range(len(usable)), key=lambda i: (abs(values[i] - target), -values[i]))
    return usable[best]


def pick_option(texts):
    """드롭다운 항목 중 하나를 고름. 반지 → 신발 → 옷 사이즈 → 색상 순으로 판단."""
    usable = [t for t in texts if t.strip() and not SOLDOUT.search(t)]
    if not usable:
        return None
    first = [t.split("\n")[0].strip() for t in usable]

    # 반지 (예: 13호, 21호)
    if sum("호" in t for t in first) >= len(first) / 2:
        vals = [_number(t) for t in first]
        pairs = [(u, v) for u, v in zip(usable, vals) if v is not None]
        if pairs:
            return _nearest([p[0] for p in pairs], [p[1] for p in pairs], RING_SIZE)

    nums = [_number(t) for t in first]
    letters = [_letter(t) for t in first]

    # 신발 (예: 250, 280, 285)
    shoe = [(u, v) for u, v in zip(usable, nums) if v is not None and 200 <= v <= 320]
    if shoe and len(shoe) >= len(first) / 2:
        return _nearest([p[0] for p in shoe], [p[1] for p in shoe], SHOE_SIZE)

    # 옷 - 알파벳 사이즈 (S/M/L/XL...) : 가장 큰 것
    lett = [(u, v) for u, v in zip(usable, letters) if v is not None]
    if lett and len(lett) >= len(first) / 2:
        return max(lett, key=lambda p: p[1])[0]

    # 옷 - 숫자 사이즈 (90/95/100, 28/30/32, 1/2/3 ...) : 가장 큰 것
    short = [(u, v) for u, v, f in zip(usable, nums, first) if v is not None and len(f) <= 12]
    if short and len(short) >= len(first) / 2:
        return max(short, key=lambda p: p[1])[0]

    # 색상/상품 선택
    for pref in PREFERRED_OPTIONS:
        for t in usable:
            if pref.lower() in t.lower():
                return t
    return usable[0]


def choose_in_open_dropdown(page):
    """방금 열린 드롭다운(새로 보이는 li)에서 하나 선택."""
    texts = page.evaluate("""() => [...document.querySelectorAll('li:not([data-pre])')]
        .filter(e => e.offsetParent !== null).map(e => (e.innerText || '').trim())""")
    pick = pick_option(texts)
    if pick is None:
        return None
    idx = texts.index(pick)
    page.evaluate("""(i) => [...document.querySelectorAll('li:not([data-pre])')]
        .filter(e => e.offsetParent !== null)[i].click()""", idx)
    return pick.split("\n")[0].strip()


MARK_OPENERS_JS = r"""
(anyText) => {
  document.querySelectorAll('[data-op]').forEach(e => e.removeAttribute('data-op'));
  const btn = [...document.querySelectorAll('button')]
      .find(b => /^\s*선택\s*완료\s*$/.test(b.innerText || '') && b.offsetParent !== null);
  if (!btn) return [];
  // 드롭다운 안내문구: 사이즈, 옵션 선택, 프레임선택, C, S ...
  const ph = /^\s*(사이즈|옵션|색상|컬러|size|color|c|s|선택해\s*주세요|.{0,12}선택)\s*$/i;
  const own = e => [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('').trim();
  const bt = btn.getBoundingClientRect().top;
  let box = btn.parentElement, depth = 0;
  while (box && box !== document.body && depth++ < 8) {
    const found = [...box.querySelectorAll('*')].filter(e => {
      if (e.offsetParent === null || btn.contains(e) || e.closest('li')) return false;
      const r = e.getBoundingClientRect();          // 드롭다운은 '선택 완료' 버튼 바로 위쪽
      if (!(r.bottom <= bt + 2 && r.top >= bt - 400)) return false;
      if (e.tagName === 'INPUT') return !e.value;   // placeholder 만 보이는 입력칸
      const t = own(e);
      if (!t) return false;
      return anyText ? t.length < 60 : ph.test(t);
    });
    if (found.length) {
      found.sort((x, y) => x.getBoundingClientRect().top - y.getBoundingClientRect().top);
      return found.map((e, i) => { e.setAttribute('data-op', String(i));
                                   return e.tagName === 'INPUT' ? (e.placeholder || '') : own(e); });
    }
    box = box.parentElement;
  }
  return [];
}
"""


def mark_existing_li(page):
    # 지금 화면에 보이는 li 만 표시 (숨겨져 있던 옵션 목록은 열린 뒤 '새 항목'으로 잡힘)
    page.evaluate("""() => document.querySelectorAll('li').forEach(e => {
        e.removeAttribute('data-pre');
        if (e.offsetParent !== null) e.setAttribute('data-pre', '1');
    })""")


# ------------------------------------------------------------
# 3. 체험단 하나 신청
# ------------------------------------------------------------
def apply_one(page, ev, dry_run: bool) -> str:
    dialogs.clear()
    page.goto(EVENT_URL.format(id=ev["id"]), wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass
    nap(1, 1.5)

    if "/auth/login" in page.url:
        return "LOGIN (로그인 풀림 - --login 다시 실행)"

    img = page_image(page)
    if img:
        ev["_image"] = img

    body = page.inner_text("body")
    for pat, msg in [(r"신청\s*완료", "이미 신청함"), (r"Lv\.\d+\s*등급부터", "등급 미달"),
                     (r"모집\s*종료|당첨자\s*선정\s*중", "모집 종료")]:
        if re.search(pat, body) and not visible(page.get_by_role("button", name=re.compile(r"^\s*신청하기")), 800):
            return f"SKIP ({msg})"

    apply_btn = page.get_by_role("button", name=re.compile(r"^\s*신청하기"))
    if not visible(apply_btn, 5000):
        shot(page, f"no_button_{ev['id']}")
        return "FAIL (신청하기 버튼 없음)"
    apply_btn.first.click()
    nap(1, 1.5)

    # 옵션 선택 단계 (상품 -> 옵션1 -> 옵션2 -> 선택 완료 -> 신청하기)
    chosen = []
    tried = set()
    done_clicks = 0
    for _ in range(16):
        if "/preuser/apply/" in page.url:
            break
        if "/preuser/history" in page.url:
            return "FAIL (신청 제한 - 미작성 후기 등 패널티 확인 필요)"

        done = page.get_by_role("button", name=re.compile(r"^\s*선택\s*완료"))
        if visible(done, 800):
            # 옵션을 하나라도 골랐고 '선택 완료'가 활성화되면 바로 누름
            # (고른 뒤에는 드롭다운이 다시 빈칸으로 돌아가므로 먼저 확인)
            if chosen and done.first.is_enabled() and done_clicks < 2:
                done.first.click()
                done_clicks += 1
                nap(1.5, 2)
                continue
            # 위에서부터 차례로 드롭다운을 열어봄 (목록이 안 나오면 다음 것)
            texts = page.evaluate(MARK_OPENERS_JS, False)
            cand = [i for i, t in enumerate(texts) if t not in tried]
            if not cand and not done.first.is_enabled():
                texts = page.evaluate(MARK_OPENERS_JS, True)
                cand = [i for i, t in enumerate(texts) if t not in tried and t not in chosen]
            if cand:
                i = cand[0]
                mark_existing_li(page)
                page.locator(f'[data-op="{i}"]').first.click()
                nap(0.6, 1)
                pick = choose_in_open_dropdown(page)
                if not pick:
                    tried.add(texts[i])          # 이 드롭다운은 아직 못 엶 -> 다음 것 시도
                    page.keyboard.press("Escape")
                    nap(0.4, 0.7)
                    continue
                tried = {texts[i]}               # 방금 고른 드롭다운은 바로 다시 열지 않음
                chosen.append(pick)
                nap(0.6, 1)
                continue
            if done.first.is_enabled():
                done.first.click()
                nap(1, 1.5)
                continue
            shot(page, f"option_stuck_{ev['id']}")
            return "FAIL (옵션을 다 못 고름 - 스크린샷 확인)"

        again = page.get_by_role("button", name=re.compile(r"^\s*신청하기"))
        if visible(again, 500):
            again.last.click()
            nap(1.5, 2)
            continue
        break

    if chosen:
        log(f"    옵션: {' / '.join(chosen)}")
        ev["_options"] = " / ".join(chosen)

    try:
        page.wait_for_url(re.compile(r"/preuser/apply/"), timeout=8000)
    except PWTimeout:
        shot(page, f"no_applypage_{ev['id']}")
        extra = f" alert: {dialogs[-1]}" if dialogs else ""
        return "FAIL (신청서 화면으로 못 넘어감)" + extra

    # 신청서 화면
    nap(1.5, 2)
    if re.search(r"등록한\s*배송지가\s*없습니다", page.inner_text("body")):
        return "FAIL (배송지 없음 - 무신사에 기본 배송지를 먼저 등록하세요)"

    agree_all = page.get_by_text(re.compile(r"전체\s*동의하기"))
    if visible(agree_all, 3000):
        agree_all.first.click()
        nap(0.5, 1)

    submit = page.get_by_role("button", name=re.compile(r"^\s*신청\s*완료\s*$"))
    if not visible(submit, 5000):
        shot(page, f"no_submit_{ev['id']}")
        return "FAIL (신청 완료 버튼 없음)"

    if submit.last.is_disabled():
        # 배송 요청사항이 필수인 경우 첫 항목 선택
        req = page.get_by_placeholder(re.compile("배송 요청사항"))
        if visible(req, 1000):
            mark_existing_li(page)
            req.first.click()
            nap(0.5, 1)
            page.locator("button:visible").filter(has_text=re.compile(r"문 앞|경비실|부재")).first.click()
            nap(0.5, 1)

    if dry_run:
        shot(page, f"dryrun_{ev['id']}")
        return "DRY-RUN (신청 완료 직전까지 진행)"

    submit.last.click()
    try:
        page.get_by_text(DONE_MSG).first.wait_for(state="visible", timeout=8000)
        return "OK (신청 완료)"
    except PWTimeout:
        shot(page, f"result_{ev['id']}")
        extra = f" alert: {dialogs[-1]}" if dialogs else ""
        return "UNKNOWN (완료 메시지 미확인 - 스크린샷 확인)" + extra


# ------------------------------------------------------------
def main():
    global STATE_FILE
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", action="store_true", help="로그인해서 저장")
    ap.add_argument("--export-session", action="store_true", help="GitHub Secrets 용 로그인 상태 내보내기")
    ap.add_argument("--storage-state", default="", help="로그인 상태 파일 경로 (기본: musinsa_login.json)")
    ap.add_argument("--history", default="", help="대시보드 기록 파일 경로 (예: docs/data/musinsa.json)")
    ap.add_argument("--live", default="", help="실행 중 진행 상황 파일 경로 (대시보드 실시간 표시용)")
    ap.add_argument("--dry-run", action="store_true", help="최종 신청 버튼은 누르지 않음")
    ap.add_argument("--limit", type=int, default=0, help="앞에서부터 N개만 처리 (테스트용)")
    ap.add_argument("--headless", action="store_true", help="브라우저 창 숨기기")
    args = ap.parse_args()

    if args.storage_state:
        STATE_FILE = Path(args.storage_state).resolve()
    if args.export_session:
        return export_session()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    hist = load_history(args.history)
    record = bool(args.history) and not args.dry_run
    run = {"started_at": now_kst(), "finished_at": "", "status": "running",
           "listed": 0, "ok": 0, "check": 0, "manual": 0, "fail": 0, "skip": 0}

    live = {"site": "musinsa", "status": "running", "started_at": run["started_at"],
            "total": 0, "done": 0, "current": "목록을 불러오는 중", "run": run, "records": []}
    live_path = "" if args.dry_run else args.live

    def close_run(status):
        run["status"] = status
        run["finished_at"] = now_kst()
        live.update(status=status, current="")
        write_live(live_path, live)
        if record:
            hist["runs"].append(run)
            save_history(args.history, hist)

    if not args.login:
        write_live(live_path, live)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless and not args.login)

        if args.login:
            do_login(browser)
            browser.close()
            return

        if not STATE_FILE.exists():
            log("로그인 정보가 없습니다. 먼저 'python musinsa_preuser.py --login' 을 실행하세요.")
            return 1

        ctx = browser.new_context(storage_state=str(STATE_FILE), locale="ko-KR",
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.set_default_timeout(10000)
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))

        page.goto(MAIN_URL, wait_until="domcontentloaded")
        if not is_logged_in(ctx):
            log("로그인이 풀렸습니다. 'python musinsa_preuser.py --login' 을 다시 실행하세요.")
            close_run("login_expired")
            browser.close()
            return 2
        log(f"[버전 {VERSION}] 로그인 확인 OK")

        try:
            events = fetch_events(ctx)
        except Exception as e:
            log(f"체험단 목록을 가져오지 못했습니다: {e}")
            close_run("no_list")
            browser.close()
            return 1

        # 대시보드 기록상 이미 신청한 체험단은 건너뜀
        done_urls = {r["url"] for r in hist["records"] if r.get("status") in ("ok", "check")}
        before = len(events)
        events = [e for e in events if EVENT_URL.format(id=e["id"]) not in done_urls]
        if args.limit:
            events = events[: args.limit]
        run["listed"] = before
        log(f"신청 대상 체험단 {len(events)}개 (모집 중 {before}개, 이미 신청 {before - len(events)}개 제외)")

        live["total"] = len(events)
        write_live(live_path, live)

        summary, exit_code = {}, 0
        for i, ev in enumerate(events, 1):
            log(f"[{i}/{len(events)}] {ev['name']}  (id={ev['id']})")
            live["current"] = ev.get("name", "")
            write_live(live_path, live)
            try:
                result = apply_one(page, ev, args.dry_run)
            except Exception as e:
                shot(page, f"error_{ev['id']}")
                result = f"ERROR ({type(e).__name__}: {str(e)[:150]})"
            log(f"    -> {result}")
            key = result.split()[0]
            summary[key] = summary.get(key, 0) + 1
            st = status_of(result)
            if st in run:
                run[st] += 1
            if record and st in ("ok", "check", "fail"):
                hist["records"].append({
                    "at": now_kst(), "status": st, "result": result.splitlines()[0][:200],
                    "title": ev.get("name", ""), "url": EVENT_URL.format(id=ev["id"]),
                    "image": ev.get("_image") or event_image(ev), "img_checked": bool(ev.get("_image")),
                    "note": ev.get("_options", ""),
                })
                save_history(args.history, hist)  # 중간에 끊겨도 기록이 남도록 매번 저장
                live["records"].append(hist["records"][-1])
            live["done"] += 1
            write_live(live_path, live)
            if key == "LOGIN":
                exit_code = 2
                break
            nap()

        try:
            ctx.storage_state(path=str(STATE_FILE))  # 로그인 연장
        except Exception:
            pass
        log("요약: " + (", ".join(f"{k} {v}건" for k, v in summary.items()) or "신청할 체험단 없음"))
        log(f"로그 폴더: {LOG_DIR}")
        close_run("login_expired" if exit_code == 2 else "success")
        if record:
            backfill_images(page, hist, args.history)
        browser.close()
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
