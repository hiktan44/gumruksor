import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/foreign/vat", params={"iso2": "DE"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    assert b.get("iso2") == "DE", b.get("iso2")
    assert b.get("standard") == 19.0, b.get("standard")


if __name__ == "__main__":
    test()
    print("PASS: test_foreign_vat_de")
