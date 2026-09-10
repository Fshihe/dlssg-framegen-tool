"""随程序发布的 payload 完整性基线。

这些哈希在构建时对 payload/ 里的文件实算得出。
安装前会逐个核对，任何不匹配都会中止安装 —— 防止损坏或被篡改的
DLL 被写进游戏目录。

version.dll = c844646d… 与上游 README 公布的 0.2.4 已签名 DLL 哈希一致。
"""

from __future__ import annotations

PAYLOAD_VERSION = "0.2.4"

# 文件名 -> (sha256, 字节数)
MANIFEST: dict[str, tuple[str, int]] = {
    "version.dll": ("c844646d835a7b88ed1382eea80403d38b433f8ac09cf92581c73698c44ae7c2", 15667520),
    "winmm.dll": ("1004dd4ee0edbe4e1af4c8c7b30d4786bea0f5e7c0412566996b4c2543ae7e36", 15678272),
    "dinput8.dll": ("ef3c3d49c5b5c8a17289c24da9b22885570793d72f3db628fa500f9efdb20489", 15666496),
    "winhttp.dll": ("1619839e4d1b6145ce9a587ba807f42e64f2b0984af9e81700d42ccf46ff7253", 15674176),
    "dxgi.dll": ("8d29eddbd7f1c3e272d07f94ab8812a80ef5b7aeb73923320bf9a432ddcf74c0", 15668032),
}

__all__ = ["MANIFEST", "PAYLOAD_VERSION"]
