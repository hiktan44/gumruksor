import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/gazette/search", params={"q": "ithalat"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    assert isinstance(r.json(), dict)


if __name__ == "__main__":
    test()
    print("PASS: test_gazette_search")
