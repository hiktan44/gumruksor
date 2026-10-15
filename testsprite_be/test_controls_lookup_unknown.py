import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/controls/lookup", json={"gtip": "010121000000"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    assert r.json().get("status") in ("matched", "not_matched", "no_match", "unavailable"), r.json().get("status")


if __name__ == "__main__":
    test()
    print("PASS: test_controls_lookup_unknown")
