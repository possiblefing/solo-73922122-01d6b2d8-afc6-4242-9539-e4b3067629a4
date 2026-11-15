import os
import sys
import tempfile
from pathlib import Path

# 汇交状态（SQLite）与原件（共享卷）统一放到临时目录，测试互不污染
_TEST_DATA = Path(tempfile.gettempdir()) / "flood_ingest_tests"
_TEST_DATA.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("FLOOD_DATA_DIR", str(_TEST_DATA))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
