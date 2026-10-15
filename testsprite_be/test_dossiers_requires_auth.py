import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/dossiers", timeout=TIMEOUT)
    assert r.status_code == 401, r.status_code


if __name__ == "__main__":
    test()
    print("PASS: test_dossiers_requires_auth")
