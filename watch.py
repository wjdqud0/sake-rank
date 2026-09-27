"""
신규 입고 / 재입고 / 가격 인하 알림 (15분마다 GitHub Actions에서 실행, 폰 ntfy 앱으로 푸시)

감시 페이지(요청 약 8건/회): 메인(신상품·재입고 섹션) + 카테고리 1페이지 + 키워드 검색 1페이지
알림 대상: ① config.json 키워드가 이름에 포함  ② 이력상 빨리 품절되는 상품(평균 fast_hours 이내 or 품절 2회 이상)
          ③ notify_all_new=true 면 모든 신규 상품
알림 종류: 신규 입고(처음 보는 상품, 판매중) / 재입고(품절 → 판매중) / 가격 인하(price_drop_pct% 이상)
첫 실행은 기준만 저장하고 '설정 완료' 1건만 보냄 (알림 폭탄 방지)
"""
import json, os, re, sys, time
from urllib.parse import quote
import crawl, notify

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state", "watch.json")

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def norm(s): return re.sub(r"\s+", "", (s or "")).lower()

def fast_set(cfg):
    try: hist = crawl.load_history().get("products", {})
    except SystemExit as e:
        crawl.log("이력 읽기 실패(빨리 품절 판단 제외):", e); hist = {}
    out = {}
    for no, h in hist.items():
        cnt, avg_h, _ = crawl.sellout_stats(h)
        if cnt >= 2 or (avg_h is not None and avg_h <= cfg["fast_hours"]):
            out[int(no)] = (cnt, avg_h)
    return out

def main():
    cfg = notify.load_config()
    if not cfg["ntfy_topic"]: sys.exit("알림 채널이 없습니다. Secrets에 SITE_PASSWORD를 등록하거나 config.json의 ntfy_topic을 채우세요.")
    if os.environ.get("TEST_ALERT", "").lower() == "true":
        ok = notify.send(cfg, "🔔 사케러버 테스트 알림", "알림이 정상적으로 도착했습니다.", click=notify.pages_url(),
                         priority=4, tags=["white_check_mark"], sms=True)
        crawl.log(f"테스트 알림 {'전송 성공' if ok else '전송 실패'}")
        sys.exit(0 if ok else 1)

    rb = crawl.robots()
    urls = [crawl.BASE + "/"]
    if crawl.allowed(rb, "/product/list.html"):
        urls += [f"{crawl.BASE}/product/list.html?cate_no={c}&page=1" for c in crawl.CATEGORIES]
    if crawl.allowed(rb, "/product/search.html"):
        urls += [f"{crawl.BASE}/product/search.html?keyword={quote(k)}" for k in cfg["keywords"] if k.strip()]
    pages = crawl.fetch_all(urls, max(crawl.PAUSE, rb["delay"]))
    if not pages: sys.exit("감시 페이지를 하나도 가져오지 못했습니다")

    seen = {}
    for body in pages.values():
        for no, p in crawl.parse_list(body).items():
            if no in seen: seen[no]["soldout"] = seen[no]["soldout"] and p["soldout"]
            else: seen[no] = p
    crawl.log(f"감시 페이지 {len(pages)}/{len(urls)}개, 상품 {len(seen)}개 확인")

    state = load_json(STATE_FILE, None)
    first = state is None
    state = state or {"items": {}}
    # Cafe24 상품번호는 등록순으로 증가 → 지금까지 본 최대 번호보다 크면 '신규' (키워드 추가 시 기존 상품 폭탄 방지)
    max_no = state.get("max_no") or max([int(k) for k in state["items"]] or [0])
    fast = fast_set(cfg)
    sent = 0
    for no, p in seen.items():
        old = state["items"].get(str(no))
        event = None
        if not first and not p["soldout"]:
            if old is None and no > max_no: event = "신규 입고"
            elif old is not None and old["soldout"]: event = "재입고"
            elif (old is not None and old.get("price") and p.get("price")
                  and p["price"] <= old["price"] * (1 - cfg["price_drop_pct"] / 100)):
                event = "가격 인하"
        if event:
            hit_kw = next((k for k in cfg["keywords"] if norm(k) and norm(k) in norm(p["name"])), None)
            hit_fast = fast.get(no)
            if hit_kw or hit_fast or (event == "신규 입고" and cfg["notify_all_new"]):
                why = []
                if event == "가격 인하": why.append(f"₩{old['price']:,} → ₩{p['price']:,}")
                elif p.get("price"): why.append(f"₩{p['price']:,}")
                if hit_kw: why.append(f"키워드 '{hit_kw}'")
                if hit_fast:
                    cnt, avg_h = hit_fast
                    why.append(f"품절 {cnt}회" + (f", 평균 {round(avg_h)}시간 만에 품절" if avg_h is not None else ""))
                ok = notify.send(cfg, f"[{event}] {p['name']}", " · ".join(why),
                                 click=f"{crawl.BASE}/product/detail.html?product_no={no}",
                                 tags=["sake", "rotating_light" if hit_fast else "bell"],
                                 priority=5 if hit_fast else 4, sms=True)
                crawl.log(f"알림 {'성공' if ok else '실패'}: [{event}] {crawl.pname(p['name'])}")
                sent += bool(ok)
        state["items"][str(no)] = {"soldout": p["soldout"], "name": p["name"], "price": p.get("price"),
                                   "seen": int(time.time())}

    state["max_no"] = max([max_no] + list(seen))
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, ensure_ascii=False)
    if first:
        notify.send(cfg, "사케러버 알림 설정 완료",
                    f"감시 상품 {len(seen)}개 기준 저장. 키워드: {', '.join(cfg['keywords']) or '없음'} / 빨리 품절 이력 {len(fast)}개",
                    click=notify.pages_url(), tags=["white_check_mark"], priority=3)
        crawl.log("첫 실행: 기준 저장 + 설정 완료 알림")
    crawl.log(f"알림 {sent}건 전송")

if __name__ == "__main__":
    main()
