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
    payload = {"rows": [
        {"gtip": "6911.10.00.00.11", "origin_country": "Çin", "invoice_value": 10000, "currency": "USD"}
    ]}
    r = requests.post(f"{BASE}/api/tariff/bulk", json=payload, headers=AUTH, timeout=TIMEOUT)
    assert r.status_code == 200, f"toplu hesap 200 beklenir, gelen {r.status_code}"
    assert isinstance(r.json(), dict)


if __name__ == "__main__":
    test()
    print("PASS")

