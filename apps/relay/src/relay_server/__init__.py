"""轻量同步中转服务（路线图阶段 4 / §4）。

边界（与路线图逐条对应，勿越界）：
- 默认同步 Today 摘要、通知、研究摘要、观察事项和计划状态 → 以上全部作为客户端加密信封传输,
  服务器只存密文与元数据,绝无明文字段;
- 完整证据、原始资料、投资日志和研究上下文默认仅本机保存 → 服务器不提供任何「全量拉取」接口;
- 中转完成后删除服务器副本,只保留必要元数据 → 下载成功即删文件与令牌;
- 不部署大型行情库/ES/训练服务/多套队列 → 单进程 FastAPI + SQLite WAL,无外部依赖服务;
- 根据真实并发在 PostgreSQL 与 SQLite WAL 中选择 → 初期低并发,SQLite WAL,如实标注升级触发条件。
"""

from ._version import __version__
from .app import RelaySettings, create_app
from .settings import load_settings

__all__ = ["RelaySettings", "create_app", "load_settings", "__version__"]
