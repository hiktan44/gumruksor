import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/tariff/excise", json={"gtip": "330499000000"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    assert b.get("in_scope") is True, b.get("in_scope")
    assert any("IV" in (m.get("list") or "") for m in b.get("matches", [])), b.get("matches")


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_excise_cosmetics")
