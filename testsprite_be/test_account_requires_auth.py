import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/account", timeout=TIMEOUT)
    assert r.status_code == 401, r.status_code
    assert r.json().get("code") == "authentication_required", r.json()


if __name__ == "__main__":
    test()
    print("PASS: test_account_requires_auth")
