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
    payload = {
        "title": "TestSprite otomatik dosya",
        "product_name": "Porselen tabak",
        "gtip": "691110000011",
        "origin_country": "Çin",
        "result": {"status": "preliminary", "summary": "otomatik test kaydı"},
    }
    r = requests.post(f"{BASE}/api/dossiers", json=payload, headers=AUTH, timeout=TIMEOUT)
    assert r.status_code in (200, 201), f"oluşturma 200/201 beklenir, gelen {r.status_code}"
    body = r.json()
    assert body.get("id"), "oluşturulan dosyada id yok"


if __name__ == "__main__":
    test()
    print("PASS")

