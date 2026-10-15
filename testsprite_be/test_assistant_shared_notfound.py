import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/customs/assistant/shared/does-not-exist-token", timeout=TIMEOUT)
    assert r.status_code in (404, 410), r.status_code


if __name__ == "__main__":
    test()
    print("PASS: test_assistant_shared_notfound")
