"""LaTeX 编译器 - 编译 LaTeX 工程并检查输出"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional



LATEX_INSTALL_GUIDANCE = """未检测到 LaTeX 引擎（pdflatex / xelatex / lualatex），已跳过 PDF 编译。
LaTeX 工程本身**已经生成、可直接使用**，两条出路任选：

  · 方案 A（本地编译）：安装 TeX 发行版后，在工程目录执行 `pdflatex main.tex`（或 `xelatex main.tex`），需重复 2-3 次以解析交叉引用。
      macOS   : brew install --cask mactex-no-gui   （或 mactex，约 5GB）
      Windows : 安装 MiKTeX  https://miktex.org/download  或 TeX Live
      Linux   : sudo apt install texlive-full  /  sudo dnf install texlive-scheme-full
  · 方案 B（免安装）：把整个 LaTeX 工程目录打包，上传到 Overleaf https://www.overleaf.com 直接编译。
"""

class LatexCompiler:
    """编译 LaTeX 工程并分析编译结果"""

    def __init__(self, engine: str = "pdflatex", max_passes: int = 3):
        self.engine = engine
        self.max_passes = max_passes
        self.log_content = ""

    def resolve_engine(self) -> Optional[str]:
        """解析可用的 LaTeX 引擎；首选 self.engine，其次其它常见引擎。

        返回 None 表示本机没有任何 LaTeX 引擎 —— 调用方应「跳过编译」而不是报错
        （LaTeX 工程仍然有效，用户可本地装 TeX 或上传 Overleaf 编译）。
        """
        candidates = [self.engine] + [
            name for name in ("xelatex", "pdflatex", "lualatex") if name != self.engine
        ]
        for name in candidates:
            if shutil.which(name):
                return name
        return None

    def compile(self, project_dir: str, main_file: str = "main.tex") -> Dict:
        """编译 LaTeX 工程

        若本机未安装 LaTeX 引擎，返回 skipped=True 的降级结果（不抛错、不中断流程）：
        工程文件已生成，只需用户在本地装 TeX 或上传 Overleaf 编译。
        """
        resolved = self.resolve_engine()
        if resolved is None:
            return {
                "success": False,
                "skipped": True,
                "reason": "latex_engine_missing",
                "engine": self.engine,
                "errors": ["未检测到 LaTeX 引擎（pdflatex / xelatex / lualatex）"],
                "guidance": LATEX_INSTALL_GUIDANCE,
                "log": "",
            }
        self.engine = resolved

        project_path = Path(project_dir)
        tex_file = project_path / main_file

        if not tex_file.exists():
            return {
                "success": False,
                "error": f"主文件不存在: {tex_file}",
                "log": ""
            }

        errors = []
        warnings = []
        passes = 0

        for i in range(self.max_passes):
            passes += 1
            result = self._run_engine(tex_file, project_path)

            if not result["success"]:
                errors.extend(result["errors"])
                break

            # 解析日志
            log_file = project_path / (tex_file.stem + ".log")
            if log_file.exists():
                self.log_content = log_file.read_text(encoding='utf-8', errors='ignore')
                errors = self._extract_errors(self.log_content)
                warnings = self._extract_warnings(self.log_content)

            if not errors:
                break

        # 运行 BibTeX
        if passes > 0:
            self._run_bibtex(tex_file, project_path)
            # 再编译一次解决引用
            self._run_engine(tex_file, project_path)
            self._run_engine(tex_file, project_path)

        return {
            "success": len(errors) == 0,
            "engine": self.engine,
            "passes": passes,
            "errors": errors,
            "warnings": warnings[:20],
            "overfull_hbox": self._extract_overfull(self.log_content),
            "underfull_hbox": self._extract_underfull(self.log_content),
            "log": self.log_content[:5000] if self.log_content else ""
        }

    def _run_engine(self, tex_file: Path, cwd: Path) -> Dict:
        """运行 LaTeX 引擎"""
        try:
            cmd = [
                self.engine,
                "-interaction=nonstopmode",
                "-halt-on-error",
                str(tex_file.name)
            ]
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=120
            )

            errors = []
            if result.returncode != 0:
                # 从输出中提取错误
                for line in result.stdout.split('\n'):
                    if line.startswith('!'):
                        errors.append(line.strip())

            return {"success": result.returncode == 0, "errors": errors}
        except FileNotFoundError:
            return {
                "success": False,
                "skipped": True,
                "reason": "latex_engine_missing",
                "errors": [f"未找到 {self.engine}"],
                "guidance": LATEX_INSTALL_GUIDANCE,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "errors": ["编译超时"]}

    def _run_bibtex(self, tex_file: Path, cwd: Path):
        """运行 BibTeX"""
        try:
            subprocess.run(
                ["bibtex", tex_file.stem],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=60
            )
        except Exception:
            pass

    def _extract_errors(self, log: str) -> List[str]:
        """从日志提取错误"""
        errors = []
        patterns = [
            r'^!\s+(.+)',
            r'Undefined control sequence',
            r'Undefined reference',
            r'Undefined citation',
            r'Missing .+ inserted',
            r'Emergency stop'
        ]
        for line in log.split('\n'):
            for pattern in patterns:
                if re.search(pattern, line):
                    errors.append(line.strip())
                    break
        return errors

    def _extract_warnings(self, log: str) -> List[str]:
        """从日志提取警告"""
        warnings = []
        for line in log.split('\n'):
            if 'Warning' in line or 'warning' in line:
                warnings.append(line.strip())
        return warnings

    def _extract_overfull(self, log: str) -> List[str]:
        """提取 Overfull hbox 警告"""
        return re.findall(r'Overfull \\hbox.*', log)

    def _extract_underfull(self, log: str) -> List[str]:
        """提取 Underfull hbox 警告"""
        return re.findall(r'Underfull \\hbox.*', log)
