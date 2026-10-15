import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/tariff/gts", params={"gtip": "610910000000"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    assert isinstance(r.json(), dict)


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_gts")
