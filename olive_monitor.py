"""
올리브영 여러 상품 x 여러 랭킹 페이지 모니터 → 슬랙 알림

환경변수
  SLACK_WEBHOOK_URL  (필수) 슬랙 Incoming Webhook 주소 (코드에 직접 쓰지 마세요)
  GOODS_NO           (선택) 상품번호, 쉼표로 여러 개. 안 넣으면 아래 DEFAULT_GOODS 사용
  RANK_URLS          (선택) 랭킹 페이지, "이름|URL" 을 쉼표로 여러 개
                     안 넣으면 전체 랭킹만 확인
  ALWAYS_NOTIFY      (선택) 1이면 변화 없어도 매번 알림

설치
  pip install playwright
  playwright install chromium
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
# 공백 제거 + 중복 제거(순서 유지)
GOODS = list(dict.fromkeys(g.strip() for g in RAW_GOODS.split(",") if g.strip()))
ALWAYS = os.environ.get("ALWAYS_NOTIFY") == "1"
DEFAULT_RANK = "전체|https://www.oliveyoung.co.kr/store/main/getBestList.do,스킨케어|https://www.oliveyoung.co.kr/store/main/getBestList.do?dispCatNo=900000100100001&fltDispCatNo=10000010001&pageIdx=1&rowsPerPage=100,클렌징|https://www.oliveyoung.co.kr/store/main/getBestList.do?dispCatNo=900000100100001&fltDispCatNo=10000010010&pageIdx=1&rowsPerPage=100,메이크업|https://www.oliveyoung.co.kr/store/main/getBestList.do?dispCatNo=900000100100001&fltDispCatNo=10000010002&pageIdx=1&rowsPerPage=100,푸드|https://www.oliveyoung.co.kr/store/main/getBestList.do?dispCatNo=900000100100001&fltDispCatNo=10000020002&pageIdx=1&rowsPerPage=100,맨즈|https://www.oliveyoung.co.kr/store/main/getBestList.do?dispCatNo=900000100100001&fltDispCatNo=10000060002&pageIdx=1&rowsPerPage=100"
SLACK_CHUNK = 3000  # 슬랙 메시지 한 번에 보낼 대략적 글자 수

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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


def with_retry(fn, tries=2):
    """한 번 실패하면 잠깐 쉬고 다시 시도. 끝까지 실패하면 예외를 그대로 던진다."""
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3)


def fetch_ranking(page, url):
    page.goto(url, wait_until="networkidle", timeout=60000)
    for _ in range(5):
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(500)
    hrefs = page.eval_on_selector_all("a[href*='goodsNo=']", "els => els.map(e => e.href)")
    seen = []
    for h in hrefs:
        m = re.search(r"goodsNo=([A-Za-z0-9]+)", h)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    if not seen:
        raise RuntimeError("랭킹 상품을 하나도 못 찾음 (차단되었거나 구조 변경)")
    return seen


def is_soldout(page, goods_no):
    page.goto(detail_url(goods_no), wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(1000)
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
    """블록들을 SLACK_CHUNK 글자 단위로 나눠서 여러 메시지로 보낸다."""
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

    rankings = {}   # 랭킹이름 -> 상품번호 리스트 (실패하면 키 없음)
    soldout = {}    # 상품번호 -> bool (실패하면 키 없음)
    errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_context(user_agent=UA, locale="ko-KR").new_page()
        try:
            for name, url in rank_pages:
                try:
                    rankings[name] = with_retry(lambda: fetch_ranking(page, url))
                except Exception as e:
                    errors.append(f"랭킹 '{name}' 확인 실패: {str(e)[:80]}")
                time.sleep(random.uniform(1, 2))

            for g in GOODS:
                try:
                    soldout[g] = with_retry(lambda: is_soldout(page, g))
                except Exception as e:
                    errors.append(f"{g} 재고 확인 실패: {str(e)[:80]}")
                time.sleep(random.uniform(1, 2))
        finally:
            browser.close()

    # 현재 상태 만들기. 확인에 실패한 항목은 이전 값을 그대로 유지(가짜 변화 알림 방지)
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
        blocks.append("❗ 확인 실패 항목 (이전 값 유지)\n" + "\n".join(shown) + extra)

    if blocks:
        send_slack(f"[올리브영 {ts}] 상품 {len(GOODS)}개 확인", blocks)
    else:
        print("변화 없음")


if __name__ == "__main__":
    main()
