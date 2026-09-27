"""
알림 발송 (폰 앱 ntfy · 메일 · 문자)
- ntfy : 무료. 폰에 ntfy 앱 설치 후 QR 구독 (기본)
- 메일 : 무료. config.json "email" 또는 Secrets NTFY_EMAIL
- 문자 : 유료(건당 약 20원대). 솔라피(solapi.com) 가입 후 Secrets 4개 등록 시 자동 사용
         SOLAPI_KEY, SOLAPI_SECRET, SMS_FROM(등록한 발신번호), SMS_TO(받을 번호, 여러 개면 쉼표)
"""
import datetime, hashlib, hmac, json, os, secrets, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")
SOLAPI_URL = os.environ.get("SOLAPI_URL", "https://api.solapi.com/messages/v4/send")

def log(*a): print(time.strftime("[%H:%M:%S]"), *a, flush=True)

def load_config():
    try:
        with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f: cfg = json.load(f)
    except Exception: cfg = {}
    cfg.setdefault("keywords", []); cfg.setdefault("fast_hours", 72); cfg.setdefault("notify_all_new", False)
    cfg.setdefault("price_drop_pct", 5); cfg.setdefault("daily_report", True)
    import secure
    pw = secure.password()
    # 우선순위: Secrets NTFY_TOPIC > config.json > 비밀번호에서 자동 생성(비번 아는 사람만 구독 가능)
    cfg["ntfy_topic"] = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or (secure.topic_from(pw) if pw else "")
    cfg["email"] = (os.environ.get("NTFY_EMAIL") or cfg.get("email") or "").strip()
    # 키워드를 공개 저장소에 두기 싫으면 Secret WATCH_KEYWORDS (쉼표 구분)
    if os.environ.get("WATCH_KEYWORDS", "").strip():
        cfg["keywords"] = [k.strip() for k in os.environ["WATCH_KEYWORDS"].split(",") if k.strip()]
    return cfg

def pages_url():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo: return None
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"

def _post(url, body, headers):
    data = json.dumps(body).encode("utf-8")
    for i in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                if 200 <= r.status < 300: return True
        except Exception as e:
            log(f"전송 실패 ({i + 1}/3) {url.split('/')[2]}:", e)
        time.sleep(5 * (i + 1))
    return False

def _sms(text):
    key, sec = os.environ.get("SOLAPI_KEY"), os.environ.get("SOLAPI_SECRET")
    frm, to = os.environ.get("SMS_FROM", ""), os.environ.get("SMS_TO", "")
    if not (key and sec and frm and to): return None
    ok = True
    for num in [x.strip().replace("-", "") for x in to.split(",") if x.strip()]:
        date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        salt = secrets.token_hex(16)
        sig = hmac.new(sec.encode(), (date + salt).encode(), hashlib.sha256).hexdigest()
        auth = f"HMAC-SHA256 apiKey={key}, date={date}, salt={salt}, signature={sig}"
        ok &= _post(SOLAPI_URL, {"message": {"to": num, "from": frm.replace("-", ""), "text": text}}, {"Authorization": auth})
    return ok

def send(cfg, title, message, click=None, priority=4, tags=None, sms=False):
    """성공 여부 반환. ntfy(+메일)는 항상, 문자는 sms=True이고 Secrets가 있을 때만."""
    ok = False
    if cfg.get("ntfy_topic"):
        body = {"topic": cfg["ntfy_topic"], "title": title, "message": message, "priority": priority, "tags": tags or []}
        if click: body["click"] = click
        ok = _post(NTFY_URL, body, {})
        # 메일은 따로 보냄: 메일 발송 한도에 걸려도 폰 알림은 이미 도착
        if cfg.get("email"):
            # 구독자 없는 별도 채널로 보내 앱에 같은 알림이 두 번 뜨지 않게 함
            _post(NTFY_URL, {**body, "topic": cfg["ntfy_topic"] + "-mail", "email": cfg["email"]}, {})
    if sms:
        r = _sms(f"{title}\n{message}" + (f"\n{click}" if click else ""))
        if r is not None: ok = ok or r
    return ok
