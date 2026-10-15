import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/admin/overview", timeout=TIMEOUT)
    assert r.status_code == 403, r.status_code


if __name__ == "__main__":
    test()
    print("PASS: test_admin_requires_auth")
