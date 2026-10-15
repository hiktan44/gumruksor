import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/tariff/vat", params={"gtip": "abc"}, timeout=TIMEOUT)
    assert r.status_code == 422, r.status_code


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_vat_invalid")
