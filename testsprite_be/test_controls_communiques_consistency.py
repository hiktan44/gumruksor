import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

def test():
    r = requests.get(f"{BASE}/api/controls/communiques", timeout=TIMEOUT)
    assert r.status_code == 200, r.status_code
    b = r.json()
    comm = b.get("communiques", [])
    assert b.get("count") == len(comm), (b.get("count"), len(comm))
    imports = [c for c in comm if str(c.get("code","")).startswith("2026/")]
    assert len(imports) >= 24, len(imports)


if __name__ == "__main__":
    test()
    print("PASS: test_controls_communiques_consistency")
