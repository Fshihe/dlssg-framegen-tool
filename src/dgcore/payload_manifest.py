"""payload 完整性基线 —— 由 profiles 模块统一提供。

历史上这里是硬编码的版本字典，现在改成薄封装：
真正的版本定义在 dgcore/profiles.py，那里同时承载 INI 结构等版本差异。

保留这个模块是为了不破坏既有调用方（installer / selftest / build）。
"""

from __future__ import annotations

from .profiles import (
    DEFAULT_VERSION,
    FALLBACK_VERSION,
    PROFILE_024,
    PROFILES,
    get,
)

# 当前默认 profile（新用户走这个）
PAYLOAD_VERSION = DEFAULT_VERSION

# 兼容旧接口：默认 profile 的清单
MANIFEST: dict[str, tuple[str, int]] = dict(PROFILES[DEFAULT_VERSION].manifest)

# 0.2.4 的清单（20 系用户会用到，也是历史回归测试的基准）
MANIFEST_024: dict[str, tuple[str, int]] = dict(PROFILE_024.manifest)

# 全部版本的 哈希 -> 文件名，用于「这个文件是不是我们的」这类按哈希识别的场景
ALL_HASHES: dict[str, str] = {}
for _p in PROFILES.values():
    for _name, (_h, _size) in _p.manifest.items():
        ALL_HASHES.setdefault(_h, _name)

__all__ = [
    "MANIFEST",
    "MANIFEST_024",
    "ALL_HASHES",
    "PAYLOAD_VERSION",
    "DEFAULT_VERSION",
    "FALLBACK_VERSION",
    "PROFILES",
    "get",
]
