"""发票报销核对助手 - 启动入口。

用法:
    python run.py            # 默认 http://127.0.0.1:8000
    python run.py --port 9000
"""
import argparse

import uvicorn

from app.config import settings  # noqa: F401（初始化路径）
from app.main import app


def main() -> None:
    parser = argparse.ArgumentParser(description="发票报销核对助手")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # 直接传入 app 对象：打包为 exe 后不依赖 "模块:属性" 字符串再导入一次
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
