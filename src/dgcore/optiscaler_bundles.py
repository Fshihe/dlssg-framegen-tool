"""OptiScaler bundle 完整性基线 —— 自动生成，不要手改。

由 tools/fetch_optiscaler.py 生成于 2026-09-16 00:09:53。

这里记录的是"装进游戏目录的每一个文件"的 SHA256 与体积。
安装器按它逐个校验、逐个备份、逐个删除 —— 所以这份清单同时也是
卸载白名单：不在清单里的文件，卸载时一个都不动。

来源压缩包（仅为可追溯，仓库不含这些二进制）：
  optiscaler-dlss5: N卡DLSS5+XESS12倍(游戏适配比6倍原版差)(作者：Coldwood+Juij+ Dagherbou).rar
  optiscaler-xess: N卡版本2(6倍插帧,泛用性更强)Coldwood版.rar
"""

from __future__ import annotations

GENERATED_AT = "2026-09-16 00:09:53"

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
        "total_bytes": 231703699,
        "files": [
            ('D3D12_Optiscaler/D3D12Core.dll', '07d286c306f8117321422affd9e6388c12d0fb4be1c7fc689d9e899324feeb24', 3351064),
            ('Licenses/DirectX_LICENSE.txt', '5239850894610071566f7ecee0b751fde43c862032d92b99d7d0f596b3433ebd', 13147),
            ('Licenses/FidelityFX_v1_LICENSE.md', 'a335c2bb2bd28184b9a30b6e35b0c7e4aa7826abd1c388fc2aa02405e9d04f03', 1142),
            ('Licenses/FidelityFX_v2_LICENSE.md', '232fa4ef1e2d5c6c9a7a9a7775b3ce3c99e71fe3241e9da033318a3ef0d888a0', 62206),
            ('Licenses/XeSS_LICENSE.txt', '784be42c39a4a4d03cf85d88e15e59d75828fca773ce45b8dbfffbd7eaf208df', 4194),
            ('OptiScaler.ini.template', 'b5e1462755aeb2388caa7cdc3612656effcc6bbfa137a762425c9240e14c4e44', 56408),
            ('Optiscaler/dlssg_to_fsr3_amd_is_better.dll', 'ba0abd2c9db46c3934f778b4446c4d92bc49c91db7fe3db95630b3efaf989199', 3045328),
            ('Optiscaler/fakenvapi.dll', '3297642c543b7782c2b76c092eab7782c4a23febaf7081f2c1bb9a41968f5eb2', 422352),
            ('Optiscaler/fakenvapi.ini', 'a4bfa3ec7b4badd93ae5e1ad74a582238082d871f54f38f68e7d9fb6384ce772', 466),
            ('Optiscaler/libxell.dll', 'd2030dcd694fda8f2ec7e044b13e6db8f0b56d4ba9113a5efad334e3f3ded8c7', 415368),
            ('Optiscaler/libxess_fg.dll', 'ec5e0c65e075570c6ede72618bb666d0be0c2e10b2ea9762c0fe8cb8e375ab27', 22957432),
            ('Optiscaler/streamline/NvLowLatencyVk.dll', '2a77dc3e1c724b7eea5755be0ae7423752e79a2459fae72181a9f00e3507e5d6', 57840),
            ('Optiscaler/streamline/nvngx_deepdvc.dll', 'dc9562c67594fa06b763fe5d78f4aa53452c5344f8b43e6e2b81264320588500', 3104880),
            ('Optiscaler/streamline/sl.common.dll', '82924a8954dd671e09351c5de0eb87ad0eb25b944cc9f9ab955ca1d9950de15d', 843392),
            ('Optiscaler/streamline/sl.deepdvc.dll', 'e4b7e83c9e67eab7023222af13de239241e8a09bdb63faf26f803e9fd0f3e9c9', 376448),
            ('Optiscaler/streamline/sl.directsr.dll', '4b2c0c90195c3a9991b4e0b59004113511a90802c85d81fc7faa7c7c75f53927', 369792),
            ('Optiscaler/streamline/sl.dlss.dll', '73bf52c0cfaa5900a8f3f4a91306e4625e7cca696dfb305aae44c9f97b582e1f', 422016),
            ('Optiscaler/streamline/sl.dlss_d.dll', '026b9f4f9ea848f2f5aa0e2024fc6b1c885f57eea5d41078124dba14f873b86b', 437376),
            ('Optiscaler/streamline/sl.dlss_g.dll', 'f4a6b2b14dcc0b1485989e430d3b4e3a44ac1800b92ba1ad74f476e64fb2b09c', 636032),
            ('Optiscaler/streamline/sl.interposer.dll', '8c87c9499461da561edd529aa9bf7831d67d7b94ebb1c1a5ed54ef4934e1ea4c', 652928),
            ('Optiscaler/streamline/sl.nis.dll', '556870e456d268601188919927b3b122f76b25f9b221cc4b15d3ef649f0ad27b', 1155712),
            ('Optiscaler/streamline/sl.nvperf.dll', 'b4ca8f8eeba653446f27d20e8b394cdaae5d518c1292b440a4b97652e0f0376b', 761472),
            ('Optiscaler/streamline/sl.pcl.dll', 'f13d51cfa05f4cd514df2026049e2db8adf359221713170ad386fd499915b582', 360064),
            ('Optiscaler/streamline/sl.reflex.dll', '0ce9725e3e03ea9e7f81d008b57f33ee365973d2e349131c8b1c3e3378fe2db0', 388736),
            ('Optiscaler/streamline/v2.14.1.txt', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', 0),
            ('dxgi.dll', 'b6166883acc838aba7cafd9246a8570d37de215418ee0db901eb843affee6e39', 25855952),
            ('nvngx.dll_dlssnr.dll', '9cb8c3dd4489eec9ceefd25328ca7e06bbb032010acc00468a1031aab72ffe61', 121808),
            ('nvngx_dlssnr.dll', '6eb209e764f39872625debd6abaf45e2bb6322f6f270f781f70c059ae30b3927', 165830144),
        ],
    },
}

__all__ = ["BUNDLES", "GENERATED_AT"]
