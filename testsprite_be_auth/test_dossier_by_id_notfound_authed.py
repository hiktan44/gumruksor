import os
import requests

BASE = "https://gumruksor.com"
TIMEOUT = 90

try:
    AUTH = __AUTH_HEADERS__  # type: ignore  # noqa: F821
except NameError:
    _k = os.environ.get("TESTSPRITE_AUTH_CREDENTIAL", "")
    AUTH = {"Authorization": f"Bearer {_k}"} if _k else {}


def test():
    r = requests.get(f"{BASE}/api/dossiers/nonexistent-xyz-123", headers=AUTH, timeout=TIMEOUT)
    assert r.status_code == 404, f"kimlik doğrulanmış, olmayan dosya 404 beklenir, gelen {r.status_code}"


if __name__ == "__main__":
    test()
    print("PASS")

