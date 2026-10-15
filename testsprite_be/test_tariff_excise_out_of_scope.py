import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/tariff/excise", json={"gtip": "610910000000"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    assert b.get("in_scope") is False, b.get("in_scope")


if __name__ == "__main__":
    test()
    print("PASS: test_tariff_excise_out_of_scope")
