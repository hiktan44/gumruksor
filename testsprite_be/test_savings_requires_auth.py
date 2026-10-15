import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/tariff/savings", json={"gtip": "610910000000", "origin_country": "Çin", "customs_value": 1000}, timeout=TIMEOUT)
    assert r.status_code == 401, r.status_code


if __name__ == "__main__":
    test()
    print("PASS: test_savings_requires_auth")
