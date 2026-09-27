"""Web 层：服务端（server.WebService）+ 前端页面（templates/static）。"""

from .server import VIEW_NAME, WebService, WebServiceError

__all__ = ["WebService", "WebServiceError", "VIEW_NAME"]
