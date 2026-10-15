import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/tariff/status", timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert isinstance(body, dict), type(body)
    assert "ready" in body and "active_snapshots" in body, list(body.keys())


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_status_ready")
