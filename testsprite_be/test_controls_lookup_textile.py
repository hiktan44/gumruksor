import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/controls/lookup", json={"gtip": "610442000000"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    assert b.get("status") == "matched", b.get("status")
    codes = [m.get("rule", {}).get("code", "") for m in b.get("matches", [])]
    assert any("2026/18" in c for c in codes), codes


if __name__ == "__main__":
    test()
    print("PASS: test_controls_lookup_textile")
