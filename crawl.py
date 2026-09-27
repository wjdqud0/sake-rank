"""
SAKELOVER 좋아요 랭킹 — 클라우드판 (GitHub Actions가 하루 1회 실행, 컴퓨터 꺼져 있어도 동작)

1) robots.txt 확인 → 목록(일본사케/일본소주/기타주류) 전 페이지 → 상세페이지 좋아요
2) data/history.json 에 매일 스냅샷 누적 (좋아요, 품절 여부)
3) 좋아요 증가량(1일/7일), 품절 횟수, 입고→품절까지 걸린 시간 계산
4) site/data.json + site/index.html 생성 → GitHub Pages로 공개 링크 배포
파이썬 기본 라이브러리만 사용.
"""
import html, json, os, re, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get("SAKE_BASE", "https://sakelover.jp")
CATEGORIES = [95, 96, 97]
CAT_NAME = {95: "사케", 96: "소주", 97: "기타"}
CONCURRENCY, PAUSE = 3, 1.0
UA = "Mozilla/5.0 (compatible; SakeLikeRank/1.0; public product pages only)"
HERE = os.path.dirname(os.path.abspath(__file__))
HIST_FILE = os.path.join(HERE, "data", "history.json")          # 비밀번호 없을 때
HIST_ENC = os.path.join(HERE, "data", "history.enc.json")       # 비밀번호 있을 때 (암호화)
SITE = os.path.join(HERE, "site")
KEEP_SNAPSHOTS = 180

def log(*a): print(time.strftime("[%H:%M:%S]"), *a, flush=True)

def private():
    """비밀번호 모드면 공개 로그(GitHub Actions 로그는 누구나 볼 수 있음)에 상품명을 남기지 않음"""
    return bool(os.environ.get("SITE_PASSWORD", "").strip())

def pname(name):
    return "(비공개)" if private() else name

def get(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None

POOL = ThreadPoolExecutor(CONCURRENCY)
def fetch_all(urls, pause):
    """동시 3개씩, 배치 사이 대기. 실패분은 최대 3라운드 재시도(점점 길게 쉼)."""
    out, todo = {}, list(urls)
    for rnd in range(4):
        failed = []
        for i in range(0, len(todo), CONCURRENCY):
            batch = todo[i:i + CONCURRENCY]
            for u, (code, body) in zip(batch, POOL.map(get, batch)):
                if body is not None: out[u] = body
                elif code == 0 or code == 429 or code >= 500: failed.append(u)   # 일시 오류만 재시도
                else: log(f"HTTP {code} (재시도 안 함): {u}")
            time.sleep(pause)
        if not failed: break
        log(f"실패 {len(failed)}건 재시도 대기 ({rnd + 1}라운드)")
        time.sleep(30 * (rnd + 1))
        todo = failed
    return out

# ───── robots.txt ─────
def robots():
    code, body = get(BASE + "/robots.txt")
    rules = {"allow": [], "disallow": [], "delay": 0.0}
    if body:
        apply = seen = False
        for line in body.splitlines():
            line = re.sub(r"#.*", "", line).strip()
            m = re.match(r"^([A-Za-z-]+)\s*:\s*(.*)$", line)
            if not m: continue
            k, v = m.group(1).lower(), m.group(2).strip()
            if k == "user-agent":
                if seen: apply = seen = False
                if v == "*" or v.lower() in UA.lower(): apply = True
                continue
            seen = True
            if not apply: continue
            if k == "disallow" and v: rules["disallow"].append(v)
            elif k == "allow" and v: rules["allow"].append(v)
            elif k == "crawl-delay":
                try: rules["delay"] = float(v)
                except ValueError: pass
    return rules

def allowed(rules, path):
    best, ok = "", True
    for typ, al in (("disallow", False), ("allow", True)):
        for p in rules[typ]:
            rx = "^" + re.escape(p).replace(r"\*", ".*").replace(r"\$", "$")
            if re.match(rx, path) and len(p) >= len(best): best, ok = p, al
    return ok

# ───── 파서 ─────
def dec(s): return re.sub(r"\s+", " ", html.unescape(html.unescape(s))).strip()
def abs_url(u):
    if not u: return None
    if u.startswith("//"): return "https:" + u
    if u.startswith("/"): return BASE + u
    return u
def meta(h, prop):
    p = re.escape(prop)
    m = (re.search(r'<meta[^>]+(?:property|name)="' + p + r'"[^>]*content="([^"]*)"', h, re.I)
         or re.search(r'<meta[^>]+content="([^"]*)"[^>]*(?:property|name)="' + p + r'"', h, re.I))
    return dec(m.group(1)) if m else None
def last_page(h): return max([1] + [int(x) for x in re.findall(r"[?&;]page=(\d+)", h)])

def parse_list(h):
    out = {}
    for p in re.split(r'(?=<li[^>]*\bid="anchorBoxId_\d+")', h):
        m = re.search(r'id="anchorBoxId_(\d+)"', p)
        if not m: continue
        no, p = int(m.group(1)), p[:8000]
        img = name = None
        im = re.search(r'<img[^>]+src="([^"]*/web/product/[^"]+)"[^>]*>', p, re.I)
        if im:
            img = abs_url(im.group(1))
            a = re.search(r'alt="([^"]*)"', im.group(0)); name = dec(a.group(1)) if a else None
        text = dec(re.sub(r"<[^>]+>", " ", p))
        pm = re.search(r"판매가\s*:?\s*[￦₩]?\s*([\d,]+)", text)
        out[no] = {"name": name, "img": img, "price": int(pm.group(1).replace(",", "")) if pm else None,
                   "soldout": bool(re.search(r'product_soldout|alt="품절"', p))}
    return out

# 상세 구조: [대표이미지 /web/product/big/] → 0(자리표시) → [썸네일] → 하트숫자 → <h2>상품명</h2>
NUM_NODE = re.compile(r'>\s*(\d{1,3}(?:,\d{3})+|\d+)\s*<')
def parse_like(h):
    big = re.search(r'<img[^>]+src="[^"]*/web/product/big/[^"]*"[^>]*>', h, re.I)
    if not big: return None
    rest = h[big.end():]
    h2 = re.search(r'<h2\b', rest, re.I)
    region = rest[:h2.start()] if h2 and h2.start() < 30000 else rest[:8000]
    region = re.sub(r'<(script|style)\b.*?</\1>', " ", region, flags=re.S | re.I)
    nums = NUM_NODE.findall(region)
    return int(nums[-1].replace(",", "")) if nums else None

def parse_detail(h):
    name = meta(h, "og:title")
    if name: name = re.sub(r"\s*-\s*사케러버\s*$", "", name)
    price = meta(h, "product:sale_price:amount") or meta(h, "product:price:amount")
    return {"name": name, "img": abs_url(meta(h, "og:image")),
            "price": int(float(price)) if price else None, "likes": parse_like(h)}

# ───── 이력/지표 ─────
def gain(h, hours):
    """약 hours 전 스냅샷 대비 증가량. 그 시점 근처 기록이 없으면 None (다른 기간 값으로 대체하지 않음)."""
    t, l = h["t"], h["l"]
    if l[-1] is None: return None
    target = t[-1] - hours * 3600
    lo, hi = target - max(6 * 3600, hours * 3600 * 0.15), target + 3 * 3600
    idx = [i for i in range(len(t) - 1) if lo <= t[i] <= hi and l[i] is not None]
    return l[-1] - l[min(idx, key=lambda i: abs(t[i] - target))] if idx else None

def sellout_stats(h):
    """입고(재고있음) 시작 → 품절 전환까지 시간. 스냅샷 간격(하루) 단위 정밀도."""
    t, s = h["t"], h["s"]
    durs, start, events, last_so = [], None, 0, None
    for i in range(len(t)):
        if not s[i] and start is None: start = t[i]
        if i and s[i] and not s[i - 1]:
            events += 1; last_so = t[i]
            if start is not None: durs.append((t[i] - start) / 3600)
            start = None
        if s[i]: start = None
    return events, (round(sum(durs) / len(durs), 1) if durs else None), last_so

def is_new(h, snaps):
    """추적 시작 이후에 처음 등장했고 3일 이내면 신상품"""
    return len(snaps) > 1 and h["t"][0] > snaps[0] and h["t"][-1] - h["t"][0] <= 3 * 86400

def prev_price(h):
    """최근 14일 안에 가격이 바뀌었으면 이전 가격"""
    t, pr = h["t"], h.get("p", [])
    if not pr or pr[-1] is None: return None
    for i in range(len(pr) - 2, -1, -1):
        if t[-1] - t[i] > 14 * 86400: break
        if pr[i] is not None and pr[i] != pr[-1]: return pr[i]
    return None

def spark(h, n=14):
    """최근 n회 좋아요 추이 (그래프용)"""
    return [x for x in h["l"][-n:] if x is not None] if len(h["l"]) > 1 else []

def daily_report(prods, snaps, items):
    """하루 1회 요약 알림: 신규 / 품절 / 재입고 / 급상승"""
    import notify
    cfg = notify.load_config()
    if not cfg["ntfy_topic"] or not cfg["daily_report"] or len(snaps) < 2: return
    name = lambda no: (prods[no].get("info") or {}).get("name") or no
    new, sold, back = [], [], []
    for no, h in prods.items():
        if h["t"][-1] != snaps[-1]: continue
        if h["t"][0] == snaps[-1]: new.append(name(no))
        elif len(h["s"]) > 1 and h["s"][-1] and not h["s"][-2]: sold.append(name(no))
        elif len(h["s"]) > 1 and not h["s"][-1] and h["s"][-2]: back.append(name(no))
    hot = sorted([x for x in items if (x["g1"] or 0) > 0], key=lambda x: -x["g1"])[:3]
    short = lambda xs: ", ".join(xs[:3]) + (f" 외 {len(xs) - 3}개" if len(xs) > 3 else "")
    lines = []
    if new: lines.append(f"🆕 신규 {len(new)}: {short(new)}")
    if back: lines.append(f"🔁 재입고 {len(back)}: {short(back)}")
    if sold: lines.append(f"⛔ 품절 {len(sold)}: {short(sold)}")
    if hot: lines.append("🔥 급상승: " + ", ".join(f"{x['name']} +{x['g1']:,}" for x in hot))
    if not lines: return
    ok = notify.send(cfg, "오늘의 사케러버 리포트", "\n".join(lines), click=notify.pages_url(), priority=3, tags=["sake"])
    log(f"일일 리포트 {'전송' if ok else '실패'}")

def load_history():
    """이력 읽기. 비밀번호가 있으면 암호화 파일, 예전 평문 파일이 있으면 가져와서 이어감."""
    import secure
    pw = secure.password()
    if pw and os.path.exists(HIST_ENC):
        with open(HIST_ENC, encoding="utf-8") as f: blob = json.load(f)
        try: return secure.decrypt(blob, pw)
        except Exception:
            sys.exit("SITE_PASSWORD가 이전과 달라 기존 기록을 열 수 없습니다. 이전 비밀번호로 되돌리거나, "
                     "기록을 버리려면 저장소의 data 브랜치를 삭제하세요.")
    if os.path.exists(HIST_FILE):
        with open(HIST_FILE, encoding="utf-8") as f: return json.load(f)
    return {}

def save_history(hist):
    import secure
    pw = secure.password()
    os.makedirs(os.path.dirname(HIST_FILE), exist_ok=True)
    if pw:
        with open(HIST_ENC, "w", encoding="utf-8") as f: json.dump(secure.encrypt(hist, pw, compress=True), f)
        if os.path.exists(HIST_FILE): os.remove(HIST_FILE)     # 평문 기록은 제거
    else:
        with open(HIST_FILE, "w", encoding="utf-8") as f: json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))

# ───── 메인 ─────
def main():
    t0 = time.time()
    rb = robots()
    for path in ("/product/list.html", "/product/detail.html"):
        if not allowed(rb, path): sys.exit(f"robots.txt가 {path} 수집을 금지하여 중단합니다.")
    pause = max(PAUSE, rb["delay"])

    found = {}
    for c in CATEGORIES:
        code, first = get(f"{BASE}/product/list.html?cate_no={c}&page=1")
        if not first: sys.exit(f"목록 요청 실패 (카테고리 {c}, HTTP {code}) — sakelover가 이 서버 접속을 막았을 수 있습니다.")
        for k, v in parse_list(first).items(): found.setdefault(k, {**v, "cat": CAT_NAME[c]})
        lp = last_page(first)
        pages = fetch_all([f"{BASE}/product/list.html?cate_no={c}&page={i}" for i in range(2, lp + 1)], pause)
        for body in pages.values():
            for k, v in parse_list(body).items(): found.setdefault(k, {**v, "cat": CAT_NAME[c]})
        log(f"카테고리 {c}: {lp}페이지, 누적 {len(found)}개")

    urls = {f"{BASE}/product/detail.html?product_no={no}": no for no in found}
    details = fetch_all(list(urls), pause)
    now = int(os.environ.get("SAKE_NOW") or time.time())
    for u, body in details.items():
        d = parse_detail(body); p = found[urls[u]]
        for k in ("name", "img", "price"):
            if d[k] is not None: p[k] = d[k]
        p["likes"] = d["likes"]
    got = sum(1 for p in found.values() if p.get("likes") is not None)
    log(f"상세 {len(details)}/{len(found)} 성공, 좋아요 추출 {got}개")
    if len(found) >= 20 and got == 0: sys.exit("좋아요 숫자를 하나도 찾지 못했습니다(구조 변경 가능). 이력은 저장하지 않습니다.")

    hist = load_history()
    snaps = hist.setdefault("_snapshots", [])
    snaps.append(now); del snaps[:-KEEP_SNAPSHOTS]
    prods = hist.setdefault("products", {})
    for no, p in found.items():
        h = prods.setdefault(str(no), {"t": [], "l": [], "s": []})
        h.setdefault("p", [None] * len(h["t"]))
        h["t"].append(now); h["l"].append(p.get("likes")); h["s"].append(p["soldout"]); h["p"].append(p.get("price"))
        for k in "tlsp": del h[k][:-KEEP_SNAPSHOTS]
        h["info"] = {"name": p["name"], "img": p["img"], "price": p["price"]}
    save_history(hist)

    items = []
    for no, p in found.items():
        if p.get("likes") is None: continue
        h = prods[str(no)]
        cnt, avg_h, last_so = sellout_stats(h)
        items.append({"no": no, "name": p["name"], "img": p["img"], "price": p["price"], "likes": p["likes"],
                      "soldout": p["soldout"], "url": f"{BASE}/product/detail.html?product_no={no}", "cat": p["cat"],
                      "g1": gain(h, 24), "g7": gain(h, 24 * 7), "so_cnt": cnt, "so_avg_h": avg_h, "last_so": last_so,
                      "new": is_new(h, snaps), "price_prev": prev_price(h), "spark": spark(h)})
    items.sort(key=lambda x: (-x["likes"], x["no"]))
    os.makedirs(SITE, exist_ok=True)
    import notify
    cfg = notify.load_config()
    data = {"updated_at": now, "total": len(found), "with_likes": len(items), "snapshots": len(snaps),
            "first_snapshot": snaps[0], "ntfy_topic": cfg["ntfy_topic"],
            "keywords": cfg.get("keywords", []), "items": items}
    import secure
    pw = secure.password()
    for old in ("data.json", "data.enc.json"):
        if os.path.exists(os.path.join(SITE, old)): os.remove(os.path.join(SITE, old))
    if pw:
        with open(os.path.join(SITE, "data.enc.json"), "w", encoding="utf-8") as f: json.dump(secure.encrypt(data, pw), f)
        log("비밀번호 보호 켜짐: 암호화된 데이터만 공개됩니다")
    else:
        with open(os.path.join(SITE, "data.json"), "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)
        log("주의: SITE_PASSWORD가 없어 누구나 볼 수 있는 공개 상태입니다")
    import shutil
    for static in ("index.html", "manifest.webmanifest", "icon-192.png", "icon-512.png"):
        if os.path.exists(os.path.join(HERE, static)): shutil.copy(os.path.join(HERE, static), os.path.join(SITE, static))
    log(f"완료: 상품 {len(found)} / 좋아요 {len(items)} / 누적 스냅샷 {len(snaps)}회 / {round(time.time() - t0)}초")
    try: daily_report(prods, snaps, items)
    except Exception as e: log("일일 리포트 오류(수집 결과에는 영향 없음):", e)

if __name__ == "__main__":
    main()
