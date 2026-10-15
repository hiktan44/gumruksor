import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/tariff/vat", params={"gtip": "6104620000"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    assert b.get("gtip") == "6104620000", b.get("gtip")
    assert b.get("source"), "source bos"


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_vat_valid")
