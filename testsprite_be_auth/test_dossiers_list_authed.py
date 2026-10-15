import os
import requests

BASE = "https://gumruksor.com"
TIMEOUT = 45

# TestSprite kod başına __AUTH_HEADERS__ enjekte eder (projeye ayarlı Bearer
# credential). Yerelde TESTSPRITE_AUTH_CREDENTIAL env'inden okunur. Anahtar
# asla kaynakta/commit'te yer almaz.
try:
    AUTH = __AUTH_HEADERS__  # type: ignore  # noqa: F821  (TestSprite enjekte eder)
except NameError:
    _k = os.environ.get("TESTSPRITE_AUTH_CREDENTIAL", "")
    AUTH = {"Authorization": f"Bearer {_k}"} if _k else {}


def test():
    r = requests.get(f"{BASE}/api/dossiers", headers=AUTH, timeout=TIMEOUT)
    assert r.status_code == 200, f"beklenen 200 (kimlik doğrulanmış), gelen {r.status_code}"
    assert isinstance(r.json(), (dict, list)), "gövde dict/list değil"


if __name__ == "__main__":
    test()
    print("PASS")
