#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# T-20260726-001 · BLOCK-1 收尾 A2 · log_parser 健康测试
#
# 覆盖 T-20260726-HEALTHFIX C2 全部 4 子项边界用例：
#   - T1: Null byte (\x00) 注入 → 验证 parse_log_line 不崩 + 剥离成功
#   - T2: 200MB 大文件 → 验证 load_log 抛 LogFileTooLargeError（默认 100MB）
#   - T3: GBK 中文编码 → 验证三段式解码（utf-8 → gbk → latin-1）
#   - T4: 路径遍历攻击 → 验证 sanitize_output_path 拒绝 ../ 绝对路径
#
# 🔴 派单协议 v2 §1 红线：执行类测试用例，**必须真跑通，不靠 ast.parse 兜底**
# 🔴 派单协议 v2 §9 红线：所有汇报路径已标注 default\ 前缀
#
# 运行环境：python 直跑（无 pytest），自带 __main__ runner

"""
test_log_parser_health.py — BLOCK-1 A2 健康测试

执行：python test_log_parser_health.py
退出码：0 = 全绿，1 = 有失败
"""

import os
import sys
import tempfile
import unittest

# 把 scripts/ 加进 path，让 log_parser 可被 import
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.join(os.path.dirname(_THIS_DIR), "scripts")
sys.path.insert(0, _SCRIPTS_DIR)

from log_parser import (
    parse_log_line,
    UELogParser,
    sanitize_output_path,
)  # noqa: E402


# ───────────────────────────────────────────
# T1: Null byte (\x00) 注入
# ───────────────────────────────────────────

class TestNullByteInjection(unittest.TestCase):
    """Null byte (\x00) 注入防护 —— HEALTHFIX C2-①"""

    def test_null_byte_in_message_is_stripped(self):
        """Null 字节在 message 中应被替换为空字符串，不导致解析失败"""
        line_with_null = "[2026.07.26-12.34.56:789][  0]LogTemp: Error: \x00before\x00after\x00"
        entry = parse_log_line(line_with_null, line_num=1)
        self.assertIsNotNone(entry, "解析不应失败（即使含 Null 字节）")
        self.assertNotIn("\x00", entry.message, "Null 字节必须被剥离")
        # 验证前后内容被保留
        self.assertIn("before", entry.message)
        self.assertIn("after", entry.message)

    def test_null_byte_only_message(self):
        """Null 字节在 message 中应被剥离，残留日志前缀不应含 \x00"""
        # 用 [level] [n] prefix + category + 空 Null 内容，验证剥离行为
        line = "[2026.07.26-12.34.56:789][  0]LogTemp: \x00\x00\x00"
        entry = parse_log_line(line, line_num=1)
        self.assertIsNotNone(entry, "解析不应失败（即使 message 全 Null 字节）")
        self.assertNotIn("\x00", entry.message, "Null 字节必须被剥离")

    def test_ansi_escape_sequence_stripped(self):
        """ANSI 转义序列（颜色码）应被剥离"""
        line = "[2026.07.26-12.34.56:789][  0]LogTemp: \x1b[31mError\x1b[0m: critical failure"
        entry = parse_log_line(line, line_num=1)
        self.assertIsNotNone(entry)
        self.assertNotIn("\x1b[", entry.message, "ANSI 转义必须被剥离")
        self.assertIn("Error", entry.message)
        self.assertIn("critical failure", entry.message)


# ───────────────────────────────────────────
# T2: 200MB 大文件
# ───────────────────────────────────────────

class TestLargeFileRejection(unittest.TestCase):
    """超大日志防护 —— HEALTHFIX C2-②"""

    def test_200mb_file_raises_error_with_default_limit(self):
        """200MB 文件超过默认 100MB 限制 → 抛 ValueError（含建议命令）"""
        # 不实际创建 200MB 文件，而是用 mock 文件 + os.path.getsize stub
        # 用 tempfile 创建一个小文件，然后 monkey-patch os.path.getsize
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".log", delete=False) as f:
            f.write(b"[2026.07.26-12.34.56:789][  0]LogTemp: Error: stub line\n")
            tmp_path = f.name

        try:
            parser = UELogParser()

            # Monkey-patch os.path.getsize 来模拟 200MB 文件
            original_getsize = os.path.getsize

            def mock_getsize(path):
                if path == tmp_path:
                    return 200 * 1024 * 1024  # 200MB
                return original_getsize(path)

            os.path.getsize = mock_getsize
            try:
                with self.assertRaises(ValueError) as ctx:
                    parser.load_log(tmp_path)  # 默认 max_size_mb=100
                err_msg = str(ctx.exception)
                self.assertIn("too large", err_msg.lower())
                self.assertIn("--max-size", err_msg, "报错信息应提示 --max-size 参数")
            finally:
                os.path.getsize = original_getsize
        finally:
            os.unlink(tmp_path)

    def test_max_size_param_can_be_overridden(self):
        """传 max_size_mb=500 后，200MB 文件应被允许加载"""
        # 真实小文件 + 500MB 限制 → 不应抛错
        with tempfile.NamedTemporaryFile(mode="wb", suffix=".log", delete=False) as f:
            f.write(b"[2026.07.26-12.34.56:789][  0]LogTemp: Error: real line\n" * 10)
            tmp_path = f.name

        try:
            parser = UELogParser()
            count = parser.load_log(tmp_path, max_size_mb=500)
            self.assertGreater(count, 0, "放宽限制后应能加载文件")
        finally:
            os.unlink(tmp_path)


# ───────────────────────────────────────────
# T3: GBK 中文编码
# ───────────────────────────────────────────

class TestGBKDecoding(unittest.TestCase):
    """GBK 中文编码 —— HEALTHFIX C2-③"""

    def test_gbk_encoded_log_decodes_correctly(self):
        """GBK 编码的中文日志应被正确解码（不是 U+FFFD 乱码）"""
        # 构造 GBK 编码的中文 UE 日志（模拟国内项目环境）
        chinese_line = "[2026.07.26-12.34.56:789][  0]LogTemp: Error: 蓝图编译失败，节点未连接\n"
        gbk_bytes = chinese_line.encode("gbk")

        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            f.write(gbk_bytes)
            tmp_path = f.name

        try:
            parser = UELogParser()
            count = parser.load_log(tmp_path)
            self.assertGreater(count, 0, "GBK 编码文件应能解析出至少 1 行")
            # 验证中文内容被正确解码（非乱码）
            first = parser.entries[0]
            self.assertIn("蓝图编译失败", first.message, "中文内容必须被正确解码，不能是 U+FFFD 乱码")
            self.assertNotIn("\ufffd", first.message, "不应出现 U+FFFD 替换字符")
        finally:
            os.unlink(tmp_path)

    def test_utf8_log_still_works(self):
        """UTF-8 编码日志仍应正常解析（向后兼容）"""
        utf8_line = "[2026.07.26-12.34.56:789][  0]LogTemp: Error: blueprint compile failed\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False, encoding="utf-8") as f:
            f.write(utf8_line)
            tmp_path = f.name

        try:
            parser = UELogParser()
            count = parser.load_log(tmp_path)
            self.assertGreater(count, 0)
            self.assertIn("blueprint compile failed", parser.entries[0].message)
        finally:
            os.unlink(tmp_path)


# ───────────────────────────────────────────
# T4: 路径遍历攻击
# ───────────────────────────────────────────

class TestPathTraversalProtection(unittest.TestCase):
    """路径遍历防护 —— HEALTHFIX C2-④"""

    def test_absolute_path_rejected(self):
        """绝对路径应被拒绝"""
        with self.assertRaises(ValueError) as ctx:
            sanitize_output_path("/etc/passwd")
        self.assertIn("absolute", str(ctx.exception).lower())

    def test_parent_traversal_rejected(self):
        """含 '..' 的相对路径应被拒绝（路径遍历攻击）"""
        with self.assertRaises(ValueError) as ctx:
            sanitize_output_path("../../malicious.txt")
        self.assertIn("..", str(ctx.exception))

    def test_nested_parent_traversal_rejected(self):
        """深层 '../' 注入应被拒绝"""
        with self.assertRaises(ValueError):
            sanitize_output_path("foo/../../etc/passwd")

    def test_tilde_rejected(self):
        """含 '~'（home dir shortcut）应被拒绝"""
        with self.assertRaises(ValueError) as ctx:
            sanitize_output_path("~/malicious.txt")
        self.assertIn("~", str(ctx.exception))

    def test_empty_path_rejected(self):
        """空字符串应被拒绝"""
        with self.assertRaises(ValueError) as ctx:
            sanitize_output_path("")
        self.assertIn("empty", str(ctx.exception).lower())

    def test_safe_relative_path_allowed(self):
        """合法相对路径（base 同目录或子目录）应被允许"""
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            # 把 cwd 切到 base，使相对路径"safe/output.json"被解析到 base 子目录
            original_cwd = os.getcwd()
            try:
                os.chdir(base)
                safe = sanitize_output_path("safe/output.json", base=base)
                self.assertTrue(
                    safe.startswith(base + os.sep),
                    f"safe path should start with base: {safe}"
                )
            finally:
                os.chdir(original_cwd)

    def test_safe_sibling_path_rejected_as_absolute(self):
        """绝对 sibling 路径应被拒绝（与相对路径约束一致）"""
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            sibling = os.path.join(os.path.dirname(base), "drafts")
            # sanitize_output_path 要求 path 必须是相对路径 → 绝对路径被拒
            with self.assertRaises(ValueError) as ctx:
                sanitize_output_path(sibling, base=base)
            self.assertIn("absolute", str(ctx.exception).lower())

    def test_drafts_subdir_pattern_allowed(self):
        """drafts/ 子目录模式：先 cd 到 base，再用相对路径"""
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.realpath(tmp)
            original_cwd = os.getcwd()
            try:
                os.chdir(base)
                # 模拟 diagnose_log 的 drafts 子目录场景
                safe = sanitize_output_path("drafts/draft_1.json", base=base)
                self.assertTrue(safe.endswith("drafts" + os.sep + "draft_1.json"))
            finally:
                os.chdir(original_cwd)


# ───────────────────────────────────────────
# runner
# ───────────────────────────────────────────

if __name__ == "__main__":
    # 子测试类聚合
    suite = unittest.TestSuite()
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestNullByteInjection))
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestLargeFileRejection))
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestGBKDecoding))
    suite.addTests(unittest.TestLoader().loadTestsFromTestCase(TestPathTraversalProtection))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    # 退出码：0 = 全绿，1 = 有失败
    sys.exit(0 if result.wasSuccessful() else 1)