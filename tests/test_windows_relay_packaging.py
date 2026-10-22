"""Check relay packaging without launching the tray, relay, or a browser."""
import ast
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


REPO = Path(__file__).resolve().parents[1]


def load_packaging_module(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "packaging" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RelayPackagingTests(unittest.TestCase):
    def test_portable_app_includes_shared_core_and_windows_host(self):
        build = load_packaging_module("build_windows")
        self.assertTrue({"cdp_relay.py", "cdp_multiplex.py", "relay_windows.py"}.issubset(build.APP_FILES))
        self.assertEqual(len(build.APP_FILES), len(set(build.APP_FILES)))
        for name in build.APP_FILES:
            self.assertTrue((REPO / name).is_file(), name)

    def test_runtime_checks_exact_pinned_dependencies(self):
        runtime = load_packaging_module("verify_runtime")
        pins = dict(line.split("==") for line in (REPO / "packaging/windows-requirements.txt").read_text().splitlines()
                    if line and not line.startswith("#"))
        self.assertEqual(runtime.EXPECTED_DEPENDENCIES, pins)
        with patch.object(runtime, "version", side_effect=pins.__getitem__):
            self.assertEqual(runtime.dependency_versions(), pins)

    def test_runtime_rejects_a_different_websockets_version(self):
        runtime = load_packaging_module("verify_runtime")
        versions = dict(runtime.EXPECTED_DEPENDENCIES, websockets="15.0.0")
        with patch.object(runtime, "version", side_effect=versions.__getitem__):
            with self.assertRaisesRegex(RuntimeError, "Expected websockets 15.0.1; found 15.0.0"):
                runtime.dependency_versions()

    def test_runtime_imports_both_relay_modules_and_asyncio_apis(self):
        tree = ast.parse((REPO / "packaging/verify_runtime.py").read_text())
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        from_imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertTrue({"cdp_relay", "cdp_multiplex", "relay_windows"}.issubset(imports))
        self.assertTrue({"websockets.asyncio.client", "websockets.asyncio.server"}.issubset(from_imports))

    def test_source_installs_websockets_on_windows_and_macos(self):
        lines = (REPO / "requirements.txt").read_text().splitlines()
        relay = next(line for line in lines if line.startswith("websockets"))
        requirement, marker = relay.split(";", 1)
        self.assertEqual(requirement.strip(), "websockets>=15.0.1,<16")
        # Evaluate only this fixed comparison grammar, without importing pip.
        tree = ast.parse(marker.strip(), mode="eval")
        allowed = (ast.Expression, ast.BoolOp, ast.Or, ast.Compare, ast.Eq, ast.Name, ast.Load, ast.Constant)
        self.assertTrue(all(isinstance(node, allowed) for node in ast.walk(tree)))
        code = compile(tree, "requirements marker", "eval")
        for platform in ("win32", "darwin"):
            self.assertTrue(eval(code, {"__builtins__": {}}, {"sys_platform": platform}))
        self.assertFalse(eval(code, {"__builtins__": {}}, {"sys_platform": "linux"}))


if __name__ == "__main__":
    unittest.main()
