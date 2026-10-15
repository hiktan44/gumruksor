import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/tariff/lookup", json={"gtip": "abc"}, timeout=TIMEOUT)
    assert r.status_code in (200, 422), r.status_code
    if r.status_code == 200:
        assert r.json().get("status") != "matched", "gecersiz gtip matched dondu"


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_lookup_invalid_gtip")
