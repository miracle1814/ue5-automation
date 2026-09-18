# -*- coding: utf-8 -*-
"""
mermaid_render.py - Mermaid diagram renderer for UE5.1 automation skill system.

Renders Mermaid syntax into PNG/SVG/ASCII for auto-generated reports.
Three-tier fallback: L1 mmdc (local) -> L2 mermaid.ink (online) -> L3 ASCII.

Usage:
    from mermaid_render import MermaidRenderer
    r = MermaidRenderer()
    result = r.render("flowchart TD\\n  A-->B", format="png")

Author: Xiaoxi (mWNCXY) | 2026-07-22
"""

import base64, json, os, re, subprocess, shutil, sys, tempfile, time, uuid
from pathlib import Path
from typing import Optional, Dict, List

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

HAS_MMDC = shutil.which("mmdc") is not None
MERMAID_INK_BASE = "https://mermaid.ink"
DEFAULT_TIMEOUT = 30

# ---- defaults --------------------------------------------------
def _default_output_dir():
    return Path(__file__).resolve().parent.parent / "output"

DIAGRAM_TYPES = {
    "flowchart": "Flowchart", "sequenceDiagram": "Sequence",
    "classDiagram": "Class", "stateDiagram": "State",
    "erDiagram": "ER", "gantt": "Gantt", "pie": "Pie",
    "graph": "Graph (legacy)", "mindmap": "Mindmap",
    "timeline": "Timeline", "gitGraph": "Git graph",
}


class MermaidRenderError(Exception):
    """Raised when all rendering backends fail."""
    pass


class MermaidRenderer:
    """Mermaid diagram renderer with automatic backend selection."""

    def __init__(self, output_dir=None, timeout=DEFAULT_TIMEOUT,
                 allow_external=False):
        """Args:
            output_dir: 输出目录，默认 skills/ue5-automation/output/
            timeout: 渲染超时秒数
            allow_external: 是否允许 L2 在线渲染（mermaid.ink）。
                默认 False，防止蓝图代码外泄。需显式开启。
                ⚠️ 隐私警告：L2 会将 Mermaid 代码通过 HTTP GET 发送到
                mermaid.ink 第三方服务，若代码含项目敏感信息（函数名、类名、
                架构细节），存在数据外泄风险。
        """
        self.output_dir = Path(output_dir) if output_dir else _default_output_dir()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.allow_external = allow_external
        self._level_used = None

    # -- public API -------------------------------------------------

    def render(self, mermaid_code, format="png",
               width=None, height=None, theme="default", bg_color=None):
        """Render Mermaid code. Auto-selects L1>L2>L3.

        Returns dict: {path, format, level, level_name, size_bytes,
                       diagram_type, warnings, elapsed_ms}
        """
        t0 = time.time()
        warnings = []
        code = mermaid_code.strip()
        if not code:
            raise MermaidRenderError("Empty code")
        if format not in ("png", "svg", "ascii"):
            raise MermaidRenderError("Unsupported format: " + format)

        # T-20260726-HEALTHFIX #4 W1: render 调 validate() 拦截垃圾输入
        ok, vmsg = self.validate(code)
        if not ok:
            raise MermaidRenderError(vmsg)

        dtype = self._detect_type(code)

        if format == "ascii":
            return self._render_ascii(code, dtype, t0)

        errors = []
        backends = [(1, self._render_mmdc)]
        if self.allow_external:
            backends.append((2, self._render_ink))
        else:
            warnings.append("L2 disabled (set allow_external=True to enable mermaid.ink)")

        for lvl, fn in backends:
            try:
                r = fn(code, format, width, height, theme, bg_color)
                r["level"] = lvl
                r["level_name"] = {1: "mmdc (local)", 2: "mermaid.ink"}[lvl]
                r["diagram_type"] = dtype
                r["warnings"] = warnings
                r["elapsed_ms"] = int((time.time() - t0) * 1000)
                self._level_used = lvl
                return r
            except MermaidRenderError as e:
                errors.append("L%d: %s" % (lvl, e))
                warnings.append("L%d failed" % lvl)

        raise MermaidRenderError(
            "All backends failed for '%s':\n%s" % (dtype, "\n".join(errors)))

    def render_batch(self, diagrams, format="png"):
        """Batch render. diagrams=[{code,name},...] -> [{name,result,ok},...]"""
        out = []
        for i, d in enumerate(diagrams):
            name = d.get("name", "diagram_%03d" % (i + 1))
            try:
                out.append({"name": name, "result": self.render(d["code"], format=format), "ok": True})
            except MermaidRenderError as e:
                out.append({"name": name, "error": str(e), "ok": False})
        return out

    @property
    def available_engines(self):
        return {"L1_mmdc": HAS_MMDC, "L2_ink": HAS_REQUESTS, "L3_ascii": True}

    @property
    def last_level(self):
        return self._level_used

    # -- L1: mmdc ---------------------------------------------------

    def _render_mmdc(self, code, fmt, w, h, theme, bg):
        if not HAS_MMDC:
            raise MermaidRenderError("mmdc not found. npm install -g @mermaid-js/mermaid-cli")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".mmd", delete=False, encoding="utf-8") as f:
            f.write(code)
            mmd = f.name
        try:
            # v2.20 修复：输出文件名只到秒 → 同秒同类图互相覆盖；追加毫秒+短随机后缀
            ts = time.strftime("%Y%m%d_%H%M%S")
            name = "%s_%s_%s.%s" % ((self._detect_type(code) or "diagram").replace(" ", "_"),
                                    ts, uuid.uuid4().hex[:6], fmt)
            out = self.output_dir / name
            # v2.20 修复（P1）：Windows 上 npm 的 mmdc 是 mmdc.cmd 批处理垫片，
            #   subprocess 裸名 "mmdc" 只按 .exe 解析 → FileNotFoundError 且 render()
            #   只捕 MermaidRenderError → 异常穿透、整批渲染崩溃、不降级 L2/L3。
            #   改用 shutil.which 的解析结果（与 HAS_MMDC 探测一致）。
            mmdc_exe = shutil.which("mmdc") or "mmdc"
            cmd = [mmdc_exe, "-i", mmd, "-o", str(out), "-t", theme, "-b", bg or "transparent"]
            if w: cmd += ["-w", str(w)]
            if h: cmd += ["-H", str(h)]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
            except OSError as e:
                # FileNotFoundError / 权限等 → 转 MermaidRenderError，让 L2/L3 降级链生效
                raise MermaidRenderError("mmdc launch failed: %s" % e)
            if proc.returncode != 0:
                raise MermaidRenderError("mmdc rc=%d: %s" % (proc.returncode, proc.stderr.strip()[:200]))
            if not out.exists():
                raise MermaidRenderError("mmdc output missing: " + str(out))
            return {"path": str(out.resolve()), "format": fmt, "size_bytes": out.stat().st_size}
        except subprocess.TimeoutExpired:
            raise MermaidRenderError("mmdc timeout %ds" % self.timeout)
        finally:
            try: os.unlink(mmd)
            except OSError: pass

    # -- L2: mermaid.ink --------------------------------------------

    def _render_ink(self, code, fmt, w, h, theme, bg):
        if not HAS_REQUESTS:
            raise MermaidRenderError("requests not available: pip install requests")
        ext = {"png": "png", "svg": "svg"}[fmt]
        # Use JSON state encoding (more reliable than raw base64)
        state = json.dumps({"code": code, "mermaid": {"theme": theme}}, separators=(",", ":"))
        encoded = base64.urlsafe_b64encode(state.encode()).decode().rstrip("=")
        url = "%s/img/%s?type=%s" % (MERMAID_INK_BASE, encoded, ext)
        if bg and bg != "transparent":
            url += "&bgColor=" + bg.replace("#", "")
        if w: url += "&width=%d" % w
        if h: url += "&height=%d" % h
        try:
            resp = requests.get(url, timeout=self.timeout)
            if resp.status_code != 200:
                raise MermaidRenderError("HTTP %d: %s" % (resp.status_code, resp.text[:200]))
        except requests.Timeout:
            raise MermaidRenderError("mermaid.ink timeout %ds" % self.timeout)
        except requests.ConnectionError:
            raise MermaidRenderError("Cannot reach mermaid.ink - check network")

        ts = time.strftime("%Y%m%d_%H%M%S")
        name = "%s_%s_%s.%s" % ((self._detect_type(code) or "diagram").replace(" ", "_"),
                                ts, uuid.uuid4().hex[:6], fmt)
        out = self.output_dir / name
        with open(out, "wb") as f:
            f.write(resp.content)
        return {"path": str(out.resolve()), "format": fmt, "size_bytes": out.stat().st_size}

    # -- L3: ASCII --------------------------------------------------

    def _render_ascii(self, code, dtype, t0):
        ts = time.strftime("%Y%m%d_%H%M%S")
        name = "%s_%s.txt" % ((dtype or "diagram").replace(" ", "_"), ts)
        out = self.output_dir / name
        lines = code.split("\n")
        w = max(max(len(l) for l in lines), 50)
        border = "+" + "-" * (w + 2) + "+"
        hdr = "Mermaid: %s" % (dtype or "unknown")
        buf = [border, "| %s |" % hdr.ljust(w), border]
        for l in lines:
            buf.append("| %s |" % l.ljust(w))
        buf.append(border)
        buf.append("Engine: L3 ASCII | Install mmdc for graphics")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(buf))
        return {"path": str(out.resolve()), "format": "ascii", "level": 3,
                "level_name": "ASCII", "diagram_type": dtype,
                "size_bytes": out.stat().st_size, "warnings": ["ASCII fallback"],
                "elapsed_ms": int((time.time() - t0) * 1000)}

    # -- helpers ----------------------------------------------------

    @staticmethod
    def _detect_type(code):
        first = code.split("\n")[0].strip()
        for t in ["sequenceDiagram", "classDiagram", "stateDiagram",
                   "erDiagram", "gantt", "pie", "gitGraph",
                   "mindmap", "timeline", "flowchart", "graph"]:
            if first.startswith(t):
                return t
        return None

    @staticmethod
    def validate(code):
        if not code or not code.strip():
            return False, "Empty"
        first = code.strip().split("\n")[0].strip()
        if not first.startswith(tuple(DIAGRAM_TYPES)):
            return False, "First line must be diagram type: %s" % ", ".join(sorted(DIAGRAM_TYPES))
        return True, ""


# ---- module-level utilities ---------------------------------------

def quick_render(code, output_dir=None, format="png"):
    return MermaidRenderer(output_dir).render(code, format=format)["path"]


def render_to_base64(code, format="png", output_dir=None):
    r = MermaidRenderer(output_dir).render(code, format=format)
    with open(r["path"], "rb") as f:
        data = f.read()
    mime = {"png": "image/png", "svg": "image/svg+xml"}[format]
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode())


def render_to_html_img(code, format="png", output_dir=None, alt="Mermaid"):
    uri = render_to_base64(code, format, output_dir)
    return '<img src="%s" alt="%s" style="max-width:100%%" />' % (uri, alt)


def render_mermaid_blocks_in_md(md_path: str, output_dir: str = None,
                                format: str = "png") -> list:
    """扫描 Markdown 文件中的 ```mermaid 代码块，逐一渲染为图片。

    Args:
        md_path: .md 文件路径
        output_dir: 输出目录（默认与 md 同目录）
        format: png/svg/ascii

    Returns:
        list of {"index": int, "code": str, "path": str, "level_name": str}
        失败时部分返回，错误写入 mermaid_render_errors.log
    """
    md_path = os.path.abspath(md_path)
    if output_dir is None:
        output_dir = os.path.dirname(md_path) or "."
    os.makedirs(output_dir, exist_ok=True)

    with open(md_path, "r", encoding="utf-8") as f:
        content = f.read()

    # 提取 ```mermaid ... ``` 块
    pattern = re.compile(r"```(?:mermaid|mmd)\s*\n(.*?)```", re.DOTALL)
    blocks = pattern.findall(content)
    if not blocks:
        return []

    renderer = MermaidRenderer(output_dir)
    results = []
    for idx, code in enumerate(blocks, 1):
        try:
            r = renderer.render(code, format=format)
            results.append({
                "index": idx,
                "code": code,
                "path": r["path"],
                "level_name": r.get("level_name", "?"),
            })
        except MermaidRenderError as e:
            with open(os.path.join(output_dir, "mermaid_render_errors.log"),
                      "a", encoding="utf-8") as logf:
                logf.write("[%s] block#%d failed: %s\n" % (md_path, idx, e))
    return results


def check_environment():
    r = {"python": sys.version.split()[0],
         "L1_mmdc": {"available": HAS_MMDC, "version": None},
         "L2_ink": {"available": HAS_REQUESTS, "detail": "requests OK" if HAS_REQUESTS else "pip install requests"},
         "L3_ascii": {"available": True, "detail": "always"}}
    if HAS_MMDC:
        try:
            p = subprocess.run(["mmdc", "--version"], capture_output=True, text=True, timeout=5)
            r["L1_mmdc"]["version"] = (p.stdout or p.stderr).strip()
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            r["L1_mmdc"]["version"] = f"error: {e}"
        r["recommended"] = "L1 (mmdc)"
    elif HAS_REQUESTS:
        r["recommended"] = "L2 (mermaid.ink)"
    else:
        r["recommended"] = "L3 (ASCII) - install node.js + mermaid-cli for graphics"
    return r


# ---- CLI ----------------------------------------------------------

def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Mermaid renderer for UE5.1 automation")
    ap.add_argument("input", nargs="?", help="Mermaid code or .mmd file path")
    ap.add_argument("-f", "--file", help="Read from .mmd file")
    ap.add_argument("-o", "--output-dir", default=None, help="Output directory")
    ap.add_argument("-F", "--format", default="png", choices=["png", "svg", "ascii"])
    ap.add_argument("-t", "--theme", default="default", choices=["default", "dark", "forest", "neutral"])
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--bg", default=None, help="Background color")
    ap.add_argument("--base64", action="store_true", help="Output base64 data URI")
    ap.add_argument("--html-img", action="store_true", help="Output HTML img tag")
    ap.add_argument("--check", action="store_true", help="Check environment")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--allow-external", action="store_true",
                    help="⚠️ 启用 L2 在线渲染 (mermaid.ink)，将 Mermaid 代码发送到第三方服务，存在数据外泄风险")
    args = ap.parse_args()

    if args.check:
        env = check_environment()
        if args.json:
            print(json.dumps(env, indent=2, ensure_ascii=False))
        else:
            for k, v in env.items():
                if isinstance(v, dict):
                    print("%s: %s" % (k, "OK" if v.get("available") else "MISSING"))
                else:
                    print("%s: %s" % (k, v))
        return 0

    # Read input
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            code = fh.read()
    elif args.input:
        p = Path(args.input)
        if p.exists() and p.suffix in (".mmd", ".mermaid", ".txt"):
            with open(p, "r", encoding="utf-8") as fh:
                code = fh.read()
        else:
            code = args.input
    else:
        code = sys.stdin.read()

    if not code or not code.strip():
        print("Error: no input", file=sys.stderr)
        return 1

    try:
        r = MermaidRenderer(args.output_dir, allow_external=args.allow_external).render(
            code, format=args.format, width=args.width,
            height=args.height, theme=args.theme, bg_color=args.bg)
    except MermaidRenderError as e:
        print("ERROR: " + str(e), file=sys.stderr)
        return 2

    if args.base64:
        print(render_to_base64(code, args.format, args.output_dir))
    elif args.html_img:
        print(render_to_html_img(code, args.format, args.output_dir))
    elif args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False, default=str))
    else:
        print("OK: %s (L%d, %d bytes, %dms)" % (
            r["path"], r["level"], r["size_bytes"], r["elapsed_ms"]))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
