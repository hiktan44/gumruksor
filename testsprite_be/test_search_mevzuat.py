import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.post(f"{BASE}/api/search", json={"query": "ithalat"}, timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    docs = r.json().get("documents", [])
    assert len(docs) > 0, "arama sonucu bos"
    assert docs[0].get("title"), "ilk sonucun basligi yok"


if __name__ == "__main__":
    test()
    print("PASS: test_search_mevzuat")
