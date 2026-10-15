import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/tariff/lookup", json={"gtip": "851713000011", "origin_country": "Çin"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    assert b.get("status") in ("matched", "ambiguous"), b.get("status")
    assert b.get("gtip", "").startswith("8517"), b.get("gtip")


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_lookup_electronics")
