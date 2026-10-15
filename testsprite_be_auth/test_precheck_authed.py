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
        "question": "Porselen tabak ithalatında vergi ve belgeler nelerdir?",
        "product_description": "Porselen yemek tabağı, 27 cm, ev kullanımı",
        "candidate_gtip": "691110000011",
        "origin_country": "Çin",
        "tariff_selection_confirmed": True,
    }
    r = requests.post(f"{BASE}/api/customs/precheck", json=payload, headers=AUTH, timeout=TIMEOUT)
    # Kimlik doğrulama kabul edilmeli (401/403 DEĞİL). 200 başarı, 402 kota sınırı.
    assert r.status_code not in (401, 403), f"kimlik doğrulama reddedildi: {r.status_code}"
    assert r.status_code in (200, 402), f"beklenmeyen durum: {r.status_code}"


if __name__ == "__main__":
    test()
    print("PASS")

