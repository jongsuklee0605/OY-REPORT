def main():
    if not GOODS:
        sys.exit("상품번호가 없습니다.")
    rank_pages = parse_rank_urls()
    prev = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    log(f"시작: 상품 {len(GOODS)}개, 랭킹 {len(rank_pages)}개")

    rankings, stock = {}, {}
    rank_errors = []        # 랭킹 실패, 중단 사유 (매번 표시)
    goods_errors = {}       # 상품별 재고 실패 {상품번호: 메시지} (처음 한 번만 표시)
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
                    rank_errors.append(f"랭킹 '{name}' 실패: {str(e)[:100]}")
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
                    stock[g] = get_stock(page, g)
                    consec_fail = 0
                    s = stock[g]
                    if s["options"]:
                        desc = ", ".join(f"{n}({'품절' if v else '판매중'})" for n, v in s["options"].items())
                        log(f"[{i}/{len(GOODS)}] {s['name']} ({g}) 옵션 {len(s['options'])}개: {desc}")
                    else:
                        log(f"[{i}/{len(GOODS)}] {s['name']} ({g}) {'품절' if s['soldout'] else '판매중'} (옵션 없음)")
                except NoStockInfo as e:
                    consec_fail = 0   # 페이지는 정상적으로 열렸으므로 차단 아님
                    goods_errors[g] = str(e)[:100]
                    log(f"[{i}/{len(GOODS)}] {g} 판단 불가: {str(e)[:100]}")
                except Exception as e:
                    consec_fail += 1
                    goods_errors[g] = str(e)[:100]
                    log(f"[{i}/{len(GOODS)}] {g} 실패: {str(e)[:100]}")
                time.sleep(random.uniform(0.5, 1.2))
        finally:
            browser.close()

    if stopped:
        rank_errors.append(f"중단: {stopped}")
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
        if g in stock:
            s = stock[g]
            soldout, options, pname = s["soldout"], s["options"], s["name"] or old.get("name", "")
            err = None                      # 정상 확인 → 오류 기록 삭제
        else:
            soldout, options, pname = old.get("soldout"), old.get("options", {}), old.get("name", "")
            err = goods_errors.get(g, old.get("error"))
        now[g] = {"name": pname, "ranks": ranks, "soldout": soldout, "options": options, "error": err}

    STATE_FILE.write_text(json.dumps(now, ensure_ascii=False))

    # 상품 오류는 '이번에 새로 생긴 것'만 표시 (지난번과 같은 오류면 생략)
    new_goods_errors = []
    for g, msg in goods_errors.items():
        if prev.get(g, {}).get("error") == msg:
            continue
        label = now[g].get("name") or g
        new_goods_errors.append(f"{label} ({g}) 재고 실패: {msg}")
    errors = rank_errors + new_goods_errors

    ts = datetime.now().strftime("%m/%d %H:%M")
    blocks = []
    for g, cur in now.items():
        changed = core(prev.get(g)) != core(cur)
        if not (changed or ALWAYS):
            continue
        tag = "⚠️ 변화 " if changed and g in prev else ""
        title = f"{cur['name']} ({g})" if cur.get("name") else g
        lines = [f"{tag}{title}"]
        for name, r in cur["ranks"].items():
            lines.append(f"  · {name} 랭킹: {f'{r}위' if r else '권 밖'}")
        opts = cur.get("options") or {}
        if opts:
            sold = sum(1 for v in opts.values() if v)
            lines.append(f"  · 재고: 옵션 {len(opts)}개 중 {sold}개 품절 ({stock_label(cur['soldout'])})")
            old_opts = prev.get(g, {}).get("options") or {}
            for oname, v in opts.items():
                mark = ""
                if oname in old_opts and old_opts[oname] != v:
                    mark = f" ← 변화 (이전: {'품절' if old_opts[oname] else '판매중'})"
                lines.append(f"      - {oname}: {stock_label(v)}{mark}")
        else:
            lines.append(f"  · 재고: {stock_label(cur['soldout'])}")
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
