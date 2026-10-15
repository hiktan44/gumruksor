import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/tariff/scenarios", json={"gtip": "610910000000", "origin_country": "Çin"}, timeout=TIMEOUT)
    assert r.status_code == 401, r.status_code


if __name__ == "__main__":
    test()
    print("PASS: test_scenarios_requires_auth")
