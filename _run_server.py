"""智析 后台常驻启动器（pythonw 无窗口运行，日志写 logs/server.log）"""
import os
import sys
import logging

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

LOG_DIR = os.path.join(BASE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    handlers=[logging.FileHandler(os.path.join(LOG_DIR, "server.log"), encoding="utf-8")],
)

import uvicorn  # noqa: E402
from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8010, log_config=None, access_log=False)
