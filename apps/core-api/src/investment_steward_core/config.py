import os
from dataclasses import dataclass, field
from pathlib import Path

from investment_steward_core.storage.paths import StorageLayout


def _default_plugin_public_key_file() -> Path:
    """发布者公钥路径：env 优先（打包/冻结场景指向 resources 下的副本），否则按包结构解析。

    注意：`__main__` 直接构造 CoreSettings（不经 from_environment），所以默认值本身
    也要读 env，否则打包版拿不到公钥 → 插件签名校验直接失败。
    """
    env = os.environ.get("STEWARD_PLUGIN_PUBLIC_KEY_FILE")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "keys" / "steward-plugin-publishing.pub.pem"


@dataclass(frozen=True)
class CoreSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    session_token: str = ""
    data_dir: Path = Path(".data")
    # 宿主兜底数据目录（桌面壳传 Electron userData/data）：仅当 env 与位置指针都 absent 时
    # 作为 layout 兜底；CLI --data-dir 显式指定时它不参与。供 reset-default 端点取「默认位置」。
    default_data_dir: Path | None = None
    # 独立应用（mount=own_page）在 app.library 槽的容量上限；进配置受 D-6 约束，默认 4。
    app_library_cap: int = 4
    # 龙虎榜席位来源（S6）的回看天数：0 = 关闭该来源，1–5 = 近 N 个自然日。
    # 默认 1（当日）。S6 只在「确有上榜」时才出现，未上榜不生成、也不算缺口。
    youzi_lookback_days: int = 1
    # 插件制品校验的发布者公钥（Ed25519 PEM）。路径相对于 core-api 包解析，不依赖 CWD。
    plugin_public_key_file: Path = field(default_factory=_default_plugin_public_key_file)
    # 模型出网（model_access）开关：关则研究链路强制回退本地确定性引擎，不做任何外呼。
    model_access_enabled: bool = True
    # Jev 决策模型（System One）的内置默认值 / env 兜底。
    # 与 ModelProfile 分工：Jev 是**另一套协议**（POST {base_url}/systemone），
    # 「同一时刻恰好一个使用中方案」的语义不许被它污染，故不进 chat 方案表。
    # 生效优先级：设置页保存值（jev_config 表）> STEWARD_JEV_* 环境变量 > 此处默认。
    # enabled 默认 False——state 会出网到第三方（美国托管、默认非零留存），必须显式开启。
    jev_enabled: bool = False
    jev_base_url: str = "https://api.typesafe.ai/v1"
    jev_model: str = "jev-latest"
    jev_credential_ref: str = ""
    jev_timeout_secs: float = 60.0
    # 统一目录布局（路线图 3.1）：模块一律通过它取路径，不得自行拼接。
    layout: StorageLayout = field(default_factory=StorageLayout.from_environment)

    def __post_init__(self) -> None:
        # 显式传入 data_dir 时以它为准重派生，保证 layout.user_data 与 data_dir 一致。
        if Path(self.layout.user_data) != Path(self.data_dir):
            object.__setattr__(
                self,
                "layout",
                StorageLayout.from_user_data(self.data_dir, install=self.layout.install),
            )

    @property
    def database_url(self) -> str:
        self.layout.user_data.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.layout.database_file.as_posix()}"

    @classmethod
    def from_environment(cls) -> "CoreSettings":
        layout = StorageLayout.from_environment()
        return cls(
            host=os.environ.get("STEWARD_CORE_HOST", "127.0.0.1"),
            port=int(os.environ.get("STEWARD_CORE_PORT", "8765")),
            session_token=os.environ.get("STEWARD_SESSION_TOKEN", ""),
            data_dir=layout.user_data,
            plugin_public_key_file=Path(
                os.environ.get(
                    "STEWARD_PLUGIN_PUBLIC_KEY_FILE",
                    str(cls.plugin_public_key_file),
                )
            ),
            model_access_enabled=os.environ.get("STEWARD_MODEL_ACCESS", "1") != "0",
            jev_enabled=os.environ.get("STEWARD_JEV_ENABLED", "0") == "1",
            jev_base_url=os.environ.get("STEWARD_JEV_BASE_URL", cls.jev_base_url),
            jev_model=os.environ.get("STEWARD_JEV_MODEL", cls.jev_model),
            jev_credential_ref=os.environ.get("STEWARD_JEV_CREDENTIAL_REF", ""),
            jev_timeout_secs=float(os.environ.get("STEWARD_JEV_TIMEOUT_SECS", "60")),
            layout=layout,
        )
