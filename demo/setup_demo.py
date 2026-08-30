"""生成（或重置）演示用的靶子项目。

录视频时最实际的问题是重拍：agent 一旦把 bug 修好，下一条就没得演了。
这个脚本把靶子还原成"测试挂着"的初始状态，重拍前跑一次即可。

用法：
    python demo/setup_demo.py                  # 默认建到 ../agent_demo
    python demo/setup_demo.py D:/some/where    # 指定目录

靶子里埋的是一个真实的边界条件 bug：median 对偶数长度的列表返回了
上中位数，而不是中间两个数的平均。四个测试里恰好挂一个，报错
（3 != 2.5）足够清晰，适合在镜头里被读出来。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

STATS_PY = '''"""一组基础统计函数。"""


def mean(numbers):
    """算术平均数。"""
    if not numbers:
        raise ValueError("空列表没有平均数")
    return sum(numbers) / len(numbers)


def median(numbers):
    """中位数。"""
    if not numbers:
        raise ValueError("空列表没有中位数")
    ordered = sorted(numbers)
    mid = len(ordered) // 2
    return ordered[mid]


def value_range(numbers):
    """极差：最大值减最小值。"""
    if not numbers:
        raise ValueError("空列表没有极差")
    return max(numbers) - min(numbers)
'''

TEST_PY = '''"""stats 模块的单元测试。"""
import unittest

from stats import mean, median, value_range


class TestStats(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([1, 2, 3, 4]), 2.5)

    def test_median_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_median_even(self):
        # 偶数个元素时，中位数应当是中间两个数的平均
        self.assertEqual(median([1, 2, 3, 4]), 2.5)

    def test_value_range(self):
        self.assertEqual(value_range([4, 1, 9]), 8)


if __name__ == "__main__":
    unittest.main()
'''


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("..") / "agent_demo"
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)

    (target / "stats.py").write_text(STATS_PY, encoding="utf-8")
    (target / "test_stats.py").write_text(TEST_PY, encoding="utf-8")

    # 清掉上一轮可能留下的缓存和会话，保证每次录制起点一致
    for junk in target.glob("__pycache__"):
        for f in junk.iterdir():
            f.unlink()
        junk.rmdir()

    print(f"靶子已就绪：{target}")
    print("  stats.py       —— median 对偶数长度列表有 bug")
    print("  test_stats.py  —— 4 个测试，其中 test_median_even 会失败")
    print()
    print("确认初始状态（应当看到 FAILED）：")
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "test_stats"],
        cwd=target, capture_output=True, text=True,
    )
    tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
    for line in tail:
        print("   ", line)

    if result.returncode == 0:
        print("\n警告：测试竟然通过了，靶子没生效，请检查。")
        return 1
    print(f"\n开始演示：python -m web -C {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
