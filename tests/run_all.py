"""一次跑完全部测试：python tests/run_all.py

不引入 pytest：这三个测试文件本身就是可直接执行的脚本，
多一个依赖不如少一个——评委 clone 下来 pip install -r requirements.txt 就能跑。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ["test_tools.py", "test_history.py", "test_loop.py"]


def main() -> int:
    failed = []
    for suite in SUITES:
        print(f"\n{'=' * 52}\n  {suite}\n{'=' * 52}")
        result = subprocess.run([sys.executable, str(HERE / suite)])
        if result.returncode != 0:
            failed.append(suite)

    print(f"\n{'=' * 52}")
    if failed:
        print(f"失败的测试文件：{', '.join(failed)}")
        return 1
    print(f"全部通过（{len(SUITES)} 个测试文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
