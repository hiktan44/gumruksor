import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    for path in ("/api/foreign/eu-a2m/status", "/api/foreign/ebti/status", "/api/foreign/tariff/status"):
        r = requests.get(f"{BASE}{path}", timeout=TIMEOUT)
        assert r.status_code == 200, (path, r.status_code)
        assert isinstance(r.json(), dict), path


if __name__ == "__main__":
    test()
    print("PASS: test_foreign_statuses")
