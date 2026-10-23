"""Disposable local API for the browser tests: the real app and the built dashboard, no sandbox, no secrets."""
import os
import subprocess
import sys

env = os.environ.copy()
env.update(OPERATOR_TOKEN='browser-test-disposable-token-0000000000', LOCAL_API_PORT='8200')
command = [sys.executable, '-m', 'uvicorn', 'app.main:create_app', '--factory', '--host', '127.0.0.1', '--port', '8200', '--no-access-log', '--log-level', 'critical']
process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    raise SystemExit(process.wait())
finally:
    process.terminate()
    try: process.wait(timeout=3)
    except subprocess.TimeoutExpired: process.kill()
