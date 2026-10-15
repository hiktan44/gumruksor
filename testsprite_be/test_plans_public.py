import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/plans", timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    plans = r.json().get("plans", [])
    assert any(p.get("code") == "starter" for p in plans), [p.get("code") for p in plans]


if __name__ == "__main__":
    test()
    print("PASS: test_plans_public")
