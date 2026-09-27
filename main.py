"""本地贷款服务入口：``uv run python main.py`` 后访问 http://127.0.0.1:8000/docs。"""

import uvicorn

from loan.api import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
