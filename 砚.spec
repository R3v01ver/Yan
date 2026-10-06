# -*- mode: python ; coding: utf-8 -*-
import sys

# 图标只在 Windows 上生效（Linux 版打不了 .ico，硬传会直接编失败）。
# 一个 spec 同时喂给 Windows 和 Linux 两个 runner，靠这里分流。
_ICON = ['aiun/assets/yan.ico'] if sys.platform.startswith('win') else []


a = Analysis(
    ['tools/launcher.py'],
    # 必须把项目根目录放进搜索路径：launcher.py 在 tools/ 下，PyInstaller
    # 默认只把脚本所在目录（tools/）加入 path，aiun 包在上一级 —— 少这一行
    # 就会打成"能启动、一 import aiun.gui 就报 No module named"的空壳 exe。
    pathex=['.'],
    binaries=[],
    datas=[('aiun/assets/yan.ico', 'aiun/assets')],
    # 同上：包是靠根目录被搜到才进来的，逐个列出来双保险，
    # 免得哪天换了打包环境又静默漏掉。
    hiddenimports=['aiun', 'aiun.gui', 'aiun.cli', 'aiun.council',
                   'aiun.delegate', 'aiun.providers', 'aiun.transcript',
                   'aiun.notes', 'aiun.keyring', 'aiun.models'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='砚',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ICON,
)
