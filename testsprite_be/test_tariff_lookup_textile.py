import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/tariff/lookup", json={"gtip": "610910000000", "origin_country": "Çin"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    assert b["status"] == "matched", b.get("status")
    assert b["gtip"] == "610910000000", b.get("gtip")
    assert b.get("goods_description"), "goods_description bos"


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_lookup_textile")
