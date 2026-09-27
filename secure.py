"""
비밀번호 보호 (GitHub Secrets의 SITE_PASSWORD)
- 공개 페이지에는 암호화된 데이터만 올라감 → 비밀번호를 아는 사람만 브라우저에서 풀어서 봄
- 저장소에 쌓이는 이력도 암호화 → 저장소를 봐도 내용 확인 불가
- 알림 채널 이름도 비밀번호에서 만들어짐 → 비밀번호 아는 사람만 구독 가능
방식: PBKDF2-SHA256(20만 회) → AES-256-GCM (브라우저 WebCrypto와 동일 규격)
"""
import base64, hashlib, json, os, zlib

ITER = 200_000

def password():
    return os.environ.get("SITE_PASSWORD", "").strip()

def topic_from(pw):
    return "sakelover-" + hashlib.sha256(("ntfy:" + pw).encode()).hexdigest()[:12]

def _key(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITER, 32)

def encrypt(obj, pw, compress=False):
    """compress=True 는 서버 전용 기록용(브라우저용 데이터는 압축 안 함)"""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    if compress: raw = zlib.compress(raw, 9)
    ct = AESGCM(_key(pw, salt)).encrypt(iv, raw, None)
    b = lambda x: base64.b64encode(x).decode()
    out = {"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}
    if compress: out["z"] = 1
    return out

def decrypt(blob, pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = lambda k: base64.b64decode(blob[k])
    key = hashlib.pbkdf2_hmac("sha256", pw.encode(), d("salt"), blob.get("iter", ITER), 32)
    raw = AESGCM(key).decrypt(d("iv"), d("ct"), None)
    return json.loads(zlib.decompress(raw) if blob.get("z") else raw)
