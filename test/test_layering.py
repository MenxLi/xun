"""Core layering invariant: no module-level import inside `xun.core` may reach
outside it. Python never flags package cycles at runtime, so the rule is
pinned here. Imports inside functions (and TYPE_CHECKING/__main__ blocks)
are nested, hence exempt; the rule is layout-agnostic by design.
"""

import ast
import unittest
from pathlib import Path

# located by path, not `import xun`: a violation can break the package import
CORE_DIR = Path(__file__).resolve().parent.parent / "src" / "xun" / "core"

FORBIDDEN_THIRD_PARTY = frozenset({"prompt_toolkit"})  # core is headless


def module_level_imports(tree: ast.Module):
    return [s for s in tree.body if isinstance(s, (ast.Import, ast.ImportFrom))]


def imported_names(node: ast.AST) -> list[str]:
    """Absolute module names imported by this node (relative imports excluded)."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom) and not node.level:
        return [node.module or ""]
    return []


def outside_core(mod: str) -> bool:
    return mod == "xun" or (mod.startswith("xun.") and not mod.startswith("xun.core"))


def escapes_core(node: ast.Import | ast.ImportFrom, pkg: tuple[str, ...]) -> str | None:
    """Why this import leaves `xun.core`, or None if it stays. `pkg` is the
    module's enclosing package chain, e.g. ("xun", "core", "tools")."""
    if isinstance(node, ast.Import):
        offenders = [n for n in imported_names(node) if outside_core(n)]
        return f"import {', '.join(offenders)}" if offenders else None
    if node.level:
        base = pkg[: len(pkg) - node.level + 1]
        resolved = ".".join(base + ((node.module,) if node.module else ()))
    else:
        resolved = node.module or ""
    return ast.unparse(node) if outside_core(resolved) else None


class CoreLayeringTest(unittest.TestCase):

    def _core_files(self):
        files = sorted(CORE_DIR.rglob("*.py"))
        self.assertTrue(files, f"no core files found under {CORE_DIR}")
        return files

    def test_core_imports_stay_inside_core(self):
        for path in self._core_files():
            pkg = ("xun", "core") + path.relative_to(CORE_DIR).parts[:-1]
            with self.subTest(path=str(path.relative_to(CORE_DIR))):
                for node in module_level_imports(ast.parse(path.read_text(encoding="utf-8"))):
                    self.assertIsNone(escapes_core(node, pkg), "module-level import escapes xun.core")

    def test_core_is_ui_dependency_free(self):
        for path in self._core_files():
            with self.subTest(path=str(path.relative_to(CORE_DIR))):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    for name in imported_names(node):
                        self.assertNotIn(name.split(".")[0], FORBIDDEN_THIRD_PARTY,
                                         f"{name} imported by core at any depth")

    def test_detector_catches_known_violations(self):
        tree = ast.parse(
            "from ..tools import x\n"
            "import xun.displays\n"
            "if TYPE_CHECKING:\n"
            "    from ..displays import y\n"
        )
        nodes = module_level_imports(tree)  # the nested TYPE_CHECKING import is exempt
        self.assertEqual(len(nodes), 2)
        for node in nodes:
            with self.subTest(node=ast.unparse(node)):
                self.assertIsNotNone(escapes_core(node, ("xun", "core")))


if __name__ == "__main__":
    unittest.main()
