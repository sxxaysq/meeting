# -*- coding: utf-8 -*-
"""顿悟模型服务禁思考兜底代理（监听 8002 → 转发主 vLLM 192.168.30.215:8000）。

复用 ~/qwen35_9b_api/no_think_proxy.py 的既有实现（注入禁思考指令 + 剥离推理块），
不复制逻辑；只改监听端口与默认后端。

启动::

    bash start_no_think_proxy_8002.sh start
    # 等价于：NO_THINK_BACKEND_URL=http://192.168.30.215:8000 \
    #   uvicorn no_think_proxy:app --port 8002

顿悟侧模型服务 API Base 填 http://192.168.30.214:8002/v1（见 dunwu_config_checklist.md）。
"""

import os
import sys

os.environ.setdefault("NO_THINK_BACKEND_URL", "http://192.168.30.215:8000")
sys.path.insert(0, "/home/yty/qwen35_9b_api")

from no_think_proxy import app  # noqa: E402,F401


def main() -> int:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("NO_THINK_PROXY_PORT", "8002")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
