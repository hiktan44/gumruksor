import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/controls/search", params={"q": "oyuncak"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    assert b.get("total", 0) > 0 or len(b.get("results", [])) > 0, b


if __name__ == "__main__":
    test()
    print("PASS: test_controls_search_toy")
