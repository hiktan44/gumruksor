import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/tariff/communiques", timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    assert b.get("count", 0) > 0, b.get("count")


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_communiques")
