"""数据目录。

运行数据（订阅规则、放送表缓存、剧集表缓存、封面缓存、登录无关的设置）全部落在
**插件目录下的 data/**。

★ 为什么不放项目根：AstrBot 从插件市场更新插件时，会把压缩包里出现的每个目录先
  `shutil.rmtree` 再覆盖，所以运行数据必须待在**压缩包之外** —— data/ 在 .gitignore
  里、不进仓库、不进压缩包，更新时才不会被清掉。
"""
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = _PLUGIN_ROOT / "data"


def data_dir() -> Path:
    """确保数据目录存在并返回。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR
