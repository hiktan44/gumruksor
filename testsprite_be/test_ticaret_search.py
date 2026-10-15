import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/ticaret/search", json={"query": "ithalat"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    assert len(r.json().get("documents", [])) > 0, "ticaret arama bos"


if __name__ == "__main__":
    test()
    print("PASS: test_ticaret_search")
