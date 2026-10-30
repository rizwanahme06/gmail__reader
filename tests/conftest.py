import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_tmp = Path(tempfile.mkdtemp(prefix="quotepo-test-"))
# must be set BEFORE the app is imported
os.environ.update(
    DB_PATH=str(_tmp / "test.db"), INBOX_DIR=str(_tmp / "inbox"), ATTACH_DIR=str(_tmp / "attach"),
    ANTHROPIC_API_KEY="", GMAIL_TOKEN_FILE=str(_tmp / "no-token.json"),
)
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def samples() -> Path:
    out = _tmp / "samples"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_samples.py"), str(out)], check=True, capture_output=True)
    return out


@pytest.fixture()
def fresh_db():
    from app import config, db

    config.DB_PATH.unlink(missing_ok=True)
    db.init()
    yield db
