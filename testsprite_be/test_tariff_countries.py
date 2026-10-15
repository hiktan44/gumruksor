import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/tariff/countries", timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    items = b.get("countries") or b.get("items") or b
    txt = str(items)
    assert "Çin" in txt or "China" in txt or "CN" in txt, "ulke listesi Çin icermiyor"


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_countries")
