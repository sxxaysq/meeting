"""Uvicorn 使用的默认 ASGI 应用。"""

from .main import create_app


app = create_app()

