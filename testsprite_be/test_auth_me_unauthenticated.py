import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/auth/me", timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    assert b.get("authenticated") is False, b.get("authenticated")
    assert b.get("google_enabled") is True, b.get("google_enabled")


if __name__ == "__main__":
    test()
    print("PASS: test_auth_me_unauthenticated")
