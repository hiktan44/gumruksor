import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/customs/declaration/status", timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert isinstance(body, dict), type(body)
    assert len(body) >= 1


if __name__ == "__main__":
    test()
    print("PASS: test_declaration_status")
