import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/tariff/autocomplete", params={"q": "telefon"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    items = b.get("items", [])
    assert len(items) > 0, "autocomplete bos"
    assert any("8517" in (it.get("gtip") or "") for it in items), "8517 yok"


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_autocomplete")
