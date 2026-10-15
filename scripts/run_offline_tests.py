"""Run unittest with temporary storage, no credentials and no external sockets.

Usage: .venv/bin/python scripts/run_offline_tests.py discover -s tests -v
"""

from __future__ import annotations

import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    executable_path = os.environ.get("PATH", os.defpath)
    os.environ.clear()
    os.environ.update(PATH=executable_path, TZ="UTC", PYTHON_DOTENV_DISABLED="1")
    if hasattr(time, "tzset"):
        time.tzset()
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("External network disabled in synthetic tests")
        return original_connect(sock, address)

    def no_dns(*args, **kwargs):
        raise RuntimeError("External DNS disabled in synthetic tests")

    with tempfile.TemporaryDirectory(prefix="gumruksor-test-") as data:
        os.environ.update(MEVZUAT_DATA_DIR=data, XDG_CACHE_HOME=data)
        with (
            patch.object(socket.socket, "connect", local_connect),
            patch.object(socket.socket, "connect_ex", local_connect),
            patch.object(socket, "getaddrinfo", no_dns),
        ):
            unittest.main(module=None, argv=["unittest", *sys.argv[1:]])


if __name__ == "__main__":
    main()
