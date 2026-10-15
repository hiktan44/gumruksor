import os
import requests

BASE = "https://gumruksor.com"
TIMEOUT = 90

try:
    AUTH = __AUTH_HEADERS__  # type: ignore  # noqa: F821
except NameError:
    _k = os.environ.get("TESTSPRITE_AUTH_CREDENTIAL", "")
    AUTH = {"Authorization": f"Bearer {_k}"} if _k else {}


def test():
    payload = {"result": {
        "status": "preliminary",
        "summary": "Porselen fincan için ön değerlendirme",
        "candidate_gtips": [{"code": "691110", "explanation": "Porselen sofra eşyası", "confidence": "medium", "citations": ["S1"]}],
        "sources": [{"id": "S1", "title": "İthalat Rejimi Kararı", "authority": "Ticaret Bakanlığı", "url": "https://www.ticaret.gov.tr/ithalat/rejim", "excerpt": "...", "sha256": "a"*64}],
    }}
    r = requests.post(f"{BASE}/api/customs/declaration-draft", json=payload, headers=AUTH, timeout=TIMEOUT)
    assert r.status_code not in (401, 403), f"kimlik doğrulama reddedildi: {r.status_code}"
    assert r.status_code in (200, 402, 422), f"beklenmeyen durum: {r.status_code}"


if __name__ == "__main__":
    test()
    print("PASS")

