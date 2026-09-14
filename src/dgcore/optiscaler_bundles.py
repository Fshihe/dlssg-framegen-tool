"""OptiScaler bundle 完整性基线 —— 自动生成，不要手改。

由 tools/fetch_optiscaler.py 生成于 2026-09-15 00:44:20。

这里记录的是"装进游戏目录的每一个文件"的 SHA256 与体积。
安装器按它逐个校验、逐个备份、逐个删除 —— 所以这份清单同时也是
卸载白名单：不在清单里的文件，卸载时一个都不动。

来源压缩包（仅为可追溯，仓库不含这些二进制）：
  optiscaler-dlss5: N卡DLSS5+XESS12倍(游戏适配比6倍原版差)(作者：Coldwood+Juij+ Dagherbou).rar
  optiscaler-xess: N卡版本2(6倍插帧,泛用性更强)Coldwood版.rar
"""

from __future__ import annotations

GENERATED_AT = "2026-09-15 00:44:20"

# bundle key -> 规格
BUNDLES: dict[str, dict] = {
    "optiscaler-xess": {
        "short": 'XeSS 多帧生成',
        "display_name": 'XeSS 多帧生成（OptiScaler · Coldwood 构建）',
        "proxy": 'dxgi.dll',
        "ini_rel": 'OptiScaler.ini',
        "template_rel": 'OptiScaler.ini.template',
        "source_archive": 'N卡版本2(6倍插帧,泛用性更强)Coldwood版.rar',
        "total_bytes": 52900306,
        "files": [
            ('D3D12_Optiscaler/D3D12Core.dll', '07d286c306f8117321422affd9e6388c12d0fb4be1c7fc689d9e899324feeb24', 3351064),
            ('Licenses/DirectX_LICENSE.txt', '5239850894610071566f7ecee0b751fde43c862032d92b99d7d0f596b3433ebd', 13147),
            ('Licenses/FidelityFX_v1_LICENSE.md', 'a335c2bb2bd28184b9a30b6e35b0c7e4aa7826abd1c388fc2aa02405e9d04f03', 1142),
            ('Licenses/FidelityFX_v2_LICENSE.md', 'f0da09d71ad5c82759a179e774535d4a829e5c96c49294167c1152402b2cb400', 61784),
            ('Licenses/XeSS_LICENSE.txt', '784be42c39a4a4d03cf85d88e15e59d75828fca773ce45b8dbfffbd7eaf208df', 4194),
            ('OptiScaler.ini.template', 'be9f98938dcb53a39fd744249b6e031fffb1ffa65ffa5289ace048a6c0634a4d', 47477),
            ('dxgi.dll', 'b055215d458ae36115284d6c4ef1f41d23b3ec9a0f61fcff322dde1bef4d21c6', 25636864),
            ('fakenvapi.dll', '373e73dc06e2d66e90083fde3bc99ef5c1ed0bef98d1f7eda56c8b073269c3f1', 415232),
            ('fakenvapi.ini', 'a4bfa3ec7b4badd93ae5e1ad74a582238082d871f54f38f68e7d9fb6384ce772', 466),
            ('libxell.dll', '61aeeab7fdcfb6d1742f86c14c68c663d759607d01a6132645c857291eaebb86', 411504),
            ('libxess_fg.dll', 'ec5e0c65e075570c6ede72618bb666d0be0c2e10b2ea9762c0fe8cb8e375ab27', 22957432),
        ],
    },
    "optiscaler-dlss5": {
        "short": 'DLSS 5 神经网络渲染',
        "display_name": 'DLSS 5 神经网络渲染 + XeSS 多帧生成（OptiScaler）',
        "proxy": 'dxgi.dll',
        "ini_rel": 'OptiScaler.ini',
        "template_rel": 'OptiScaler.ini.template',
        "source_archive": 'N卡DLSS5+XESS12倍(游戏适配比6倍原版差)(作者：Coldwood+Juij+ Dagherbou).rar',
        "total_bytes": 195296065,
        "files": [
            ('D3D12_Optiscaler/D3D12Core.dll', '07d286c306f8117321422affd9e6388c12d0fb4be1c7fc689d9e899324feeb24', 3351064),
            ('Licenses/DirectX_LICENSE.txt', '5239850894610071566f7ecee0b751fde43c862032d92b99d7d0f596b3433ebd', 13147),
            ('Licenses/FidelityFX_v1_LICENSE.md', 'a335c2bb2bd28184b9a30b6e35b0c7e4aa7826abd1c388fc2aa02405e9d04f03', 1142),
            ('Licenses/FidelityFX_v2_LICENSE.md', '232fa4ef1e2d5c6c9a7a9a7775b3ce3c99e71fe3241e9da033318a3ef0d888a0', 62206),
            ('Licenses/XeSS_LICENSE.txt', '784be42c39a4a4d03cf85d88e15e59d75828fca773ce45b8dbfffbd7eaf208df', 4194),
            ('OptiScaler.ini.template', 'b5e1462755aeb2388caa7cdc3612656effcc6bbfa137a762425c9240e14c4e44', 56408),
            ('dxgi.dll', 'b6166883acc838aba7cafd9246a8570d37de215418ee0db901eb843affee6e39', 25855952),
            ('nvngx.dll_dlssnr.dll', '9cb8c3dd4489eec9ceefd25328ca7e06bbb032010acc00468a1031aab72ffe61', 121808),
            ('nvngx_dlssnr.dll', '6eb209e764f39872625debd6abaf45e2bb6322f6f270f781f70c059ae30b3927', 165830144),
        ],
    },
}

__all__ = ["BUNDLES", "GENERATED_AT"]
