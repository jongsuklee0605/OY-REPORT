"""
올리브영 여러 상품 x 여러 랭킹 페이지 모니터 → 슬랙 알림 (빠른 실패 + 진행 로그 버전)

환경변수
  SLACK_WEBHOOK_URL  (필수) 슬랙 Incoming Webhook 주소
  GOODS_NO           (선택) 상품번호, 쉼표로 여러 개. 안 넣으면 아래 DEFAULT_GOODS 사용
  RANK_URLS          (선택) 랭킹 페이지, "이름|URL" 을 쉼표로 여러 개. 안 넣으면 전체 랭킹만
  ALWAYS_NOTIFY      (선택) 1이면 변화 없어도 매번 알림
"""
import json
import os
import random
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_GOODS = (
    "A000000227445,A000000227451,A000000227439,A000000227443,A000000227446,"
    "A000000227449,A000000208718,A000000208721,A000000208732,A000000208733,"
    "A000000208731,A000000208730,A000000208726,A000000208723,A000000240361,"
    "A000000240368,A000000240371,A000000232342,A000000253343,A000000232341,"
    "A000000232362,A000000232364,A000001005542,A000001005349,A000000232358,"
    "A000000232361,A000000231899,A000000261287,A000000261290,A000000261292,"
    "A000000261090,A000000245944,A000000262006,A000000215317,A000000265442,"
    "A000000267402,A000000267403,A000000268404"
)

WEBHOOK = os.environ.get("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/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")
RAW_GOODS = os.environ.get("GOODS_NO") or DEFAULT_GOODS
GOODS = list(dict.fromkeys(g.strip() for g in RAW_GOODS.split(",") if g.strip()))
ALWAYS = os.environ.get("ALWAYS_NOTIFY") == "1"
DEFAULT_RANK = "전체|https://www.oliveyoung.co.kr/store/main/getBestList.do"
STATE_FILE = Path(__file__).with_name("olive_state.json")
SLACK_CHUNK = 3000

NAV_TIMEOUT = 25000      # 페이지 하나를 여는 데 기다리는 최대 시간(ms)
TIME_LIMIT = 9 * 60      # 전체 실행 최대 시간(초). 넘으면 중간 결과만 처리
MAX_CONSEC_FAIL = 4      # 연속 실패가 이만큼이면 접속 차단으로 보고 중단

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def parse_rank_urls():
    raw = os.environ.get("RANK_URLS") or DEFAULT_RANK
    pages = []
    for item in raw.split(","):
        if "|" in item:
            name, url = item.split("|", 1)
        else:
            name, url = item, item
        pages.append((name.strip(), url.strip()))
    return pages


def detail_url(goods_no):
    return f"https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={goods_no}"


def open_page(page, url):
    """페이지를 열고 HTTP 상태코드를 돌려준다. 400 이상이면 예외."""
    resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
    status = resp.status if resp else None
    if status and status >= 400:
        raise RuntimeError(f"HTTP {status}")
    page.wait_for_timeout(2000)  # 동적 내용이 채워질 시간
    return status


def fetch_ranking(page, url):
    status = open_page(page, url)
    for _ in range(4):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(400)
    hrefs = page.eval_on_selector_all("a[href*='goodsNo=']", "els => els.map(e => e.href)")
    seen = []
    for h in hrefs:
        m = re.search(r"goodsNo=([A-Za-z0-9]+)", h)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    if not seen:
        raise RuntimeError(f"상품 못 찾음 (HTTP {status}, 제목 '{page.title()[:30]}')")
    return seen


def is_soldout(page, goods_no):
    open_page(page, detail_url(goods_no))
    # 사이트 구조가 바뀌면 이 셀렉터를 수정
    area = page.query_selector(".prd_btn_area") or page.query_selector("body")
    return "품절" in area.inner_text()


def post_slack(text):
    if not WEBHOOK:
        print(text)
        return
    req = urllib.request.Request(
        WEBHOOK,
        data=json.dumps({"text": text}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=15)


def send_slack(header, blocks):
    chunks, cur = [], header
    for b in blocks:
        if len(cur) + len(b) > SLACK_CHUNK and cur != header:
            chunks.append(cur)
            cur = header
        cur += "\n\n" + b
    chunks.append(cur)
    for c in chunks:
        post_slack(c)
        time.sleep(1)


def main():
    if not GOODS:
        sys.exit("상품번호가 없습니다.")
    rank_pages = parse_rank_urls()
    prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    log(f"시작: 상품 {len(GOODS)}개, 랭킹 {len(rank_pages)}개")

    rankings, soldout, errors = {}, {}, []
    consec_fail = 0
    stopped = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=UA, locale="ko-KR").new_page()
        try:
            for name, url in rank_pages:
                if time.time() - T0 > TIME_LIMIT:
                    stopped = "시간 제한"
                    break
                try:
                    rankings[name] = fetch_ranking(page, url)
                    consec_fail = 0
                    log(f"랭킹 '{name}' 완료 ({len(rankings[name])}개 상품 보임)")
                except Exception as e:
                    consec_fail += 1
                    errors.append(f"랭킹 '{name}' 실패: {str(e)[:100]}")
                    log(f"랭킹 '{name}' 실패: {str(e)[:100]}")
                time.sleep(random.uniform(0.5, 1.2))

            for i, g in enumerate(GOODS, 1):
                if time.time() - T0 > TIME_LIMIT:
                    stopped = f"시간 제한 ({i - 1}/{len(GOODS)}개 확인 후 중단)"
                    break
                if consec_fail >= MAX_CONSEC_FAIL:
                    stopped = f"연속 {consec_fail}회 실패 → 접속 차단 의심 ({i - 1}/{len(GOODS)}개 확인 후 중단)"
                    break
                try:
                    soldout[g] = is_soldout(page, g)
                    consec_fail = 0
                    log(f"[{i}/{len(GOODS)}] {g} {'품절' if soldout[g] else '판매중'}")
                except Exception as e:
                    consec_fail += 1
                    errors.append(f"{g} 재고 실패: {str(e)[:100]}")
                    log(f"[{i}/{len(GOODS)}] {g} 실패: {str(e)[:100]}")
                time.sleep(random.uniform(0.5, 1.2))
        finally:
            browser.close()

    if stopped:
        errors.append(f"중단: {stopped}")
        log(f"중단: {stopped}")

    # 확인에 실패한 항목은 이전 값 유지(가짜 변화 알림 방지)
    now = {}
    for g in GOODS:
        old = prev.get(g, {})
        ranks = {}
        for name, _ in rank_pages:
            if name in rankings:
                lst = rankings[name]
                ranks[name] = lst.index(g) + 1 if g in lst else None
            else:
                ranks[name] = old.get("ranks", {}).get(name)
        now[g] = {"ranks": ranks, "soldout": soldout.get(g, old.get("soldout"))}

    STATE_FILE.write_text(json.dumps(now, ensure_ascii=False))

    ts = datetime.now().strftime("%m/%d %H:%M")
    blocks = []
    for g, cur in now.items():
        changed = prev.get(g) != cur
        if not (changed or ALWAYS):
            continue
        tag = "⚠️ 변화 " if changed and g in prev else ""
        lines = [f"{tag}{g}"]
        for name, r in cur["ranks"].items():
            lines.append(f"  · {name} 랭킹: {f'{r}위' if r else '권 밖'}")
        so = cur["soldout"]
        lines.append(f"  · 재고: {'확인 못함' if so is None else ('🔴 품절' if so else '🟢 판매중')}")
        lines.append(f"  {detail_url(g)}")
        blocks.append("\n".join(lines))

    if errors:
        shown = errors[:10]
        extra = f"\n...외 {len(errors) - 10}건" if len(errors) > 10 else ""
        blocks.append("❗ 확인 실패/중단 (이전 값 유지)\n" + "\n".join(shown) + extra)

    if blocks:
        send_slack(f"[올리브영 {ts}] 상품 {len(GOODS)}개 확인", blocks)
    else:
        log("변화 없음")
    log("끝")


if __name__ == "__main__":
    main()
