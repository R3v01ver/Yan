"""砚（Yan）图形界面启动入口（供打包用）。

源码运行：python -m aiun.gui
打包 exe：pyinstaller --onefile --windowed --name 砚 tools/launcher.py
"""

import os
import sys

# 保证能找到 aiun 包：源码运行时把仓库根目录塞进 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiun.gui import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
