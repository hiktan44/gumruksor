import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/health", timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert body.get("status") in ("ok", "healthy", True) or body.get("ok") is True or "status" in body, body


if __name__ == "__main__":
    test()
    print("PASS: test_health")
