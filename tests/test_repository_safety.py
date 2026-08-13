import ast
import json
from pathlib import Path
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "zidoutrade"


class RepositorySafetyTests(unittest.TestCase):
    def test_all_python_sources_parse(self):
        for path in sorted((ROOT / "src").rglob("*.py")) + sorted(
            (ROOT / "tests").rglob("*.py")
        ):
            with self.subTest(path=path.relative_to(ROOT)):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_unlock_trade_and_real_environment_are_unreachable(self):
        violations = []
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Attribute, ast.Name)):
                    name = node.attr if isinstance(node, ast.Attribute) else node.id
                    if name == "unlock_trade":
                        violations.append(f"{path.name}:{node.lineno}:unlock_trade")
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "REAL"
                    and (
                        (isinstance(node.value, ast.Name) and node.value.id == "TrdEnv")
                        or (
                            isinstance(node.value, ast.Attribute)
                            and node.value.attr == "TrdEnv"
                        )
                    )
                ):
                    violations.append(f"{path.name}:{node.lineno}:TrdEnv.REAL")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in {"REAL", "unlock_trade"}
                ):
                    violations.append(
                        f"{path.name}:{node.lineno}:dynamic-forbidden-attribute"
                    )
        self.assertEqual(violations, [])

    def test_only_broker_adapter_may_import_moomoo(self):
        violations = []
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                if any(name == "moomoo" or name.startswith("moomoo.") for name in modules):
                    if path.name != "broker.py":
                        violations.append(f"{path.name}:{node.lineno}")
        self.assertEqual(violations, [])

    def test_examples_are_inert_and_loopback_only(self):
        config = json.loads((ROOT / "config/system.example.json").read_text(encoding="utf-8"))
        self.assertEqual(config["mode"], "SHADOW")
        self.assertEqual(config["operating_mode"], "SUPERVISED_ONLY")
        self.assertEqual(config["broker_environment"], "SIMULATE")
        self.assertEqual(config["opend_host"], "127.0.0.1")
        self.assertEqual(config["opend_port"], 11111)
        self.assertEqual(config["session"], "RTH")
        env_lines = [
            line
            for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
            if line
        ]
        self.assertEqual(env_lines[-1], "N1_RSI_PAPER_ACCOUNT_ID=")

    def test_no_local_absolute_paths_or_account_literals(self):
        violations = []
        account_literal = re.compile(
            r"\bacc(?:ount)?[_ -]?id\D{0,8}(\d{6,})\b", re.IGNORECASE
        )
        unlabelled_identifier = re.compile(r"\b(\d{7,12})\b")
        for directory in (ROOT / "src", ROOT / "tests", ROOT / "config"):
            for path in sorted(
                p
                for p in directory.rglob("*")
                if p.is_file()
                and (p.suffix in {".py", ".json", ".toml"} or p.name.endswith(".example"))
            ):
                text = path.read_text(encoding="utf-8")
                if ("/" + "Users/") in text or ("C:\\" + "Users\\") in text:
                    violations.append(f"{path.relative_to(ROOT)}:absolute-path")
                # Long integers in tests must be clearly synthetic repeated digits.
                for match in account_literal.finditer(text):
                    digits = match.group(1)
                    if len(set(digits)) > 1:
                        violations.append(f"{path.relative_to(ROOT)}:numeric-identifier")
                for match in unlabelled_identifier.finditer(text):
                    digits = match.group(1)
                    if len(set(digits)) > 1:
                        violations.append(
                            f"{path.relative_to(ROOT)}:unlabelled-numeric-identifier"
                        )
        self.assertEqual(violations, [])

    def test_no_activation_or_runtime_artifacts_in_tree(self):
        forbidden_names = {
            "activation.secret",
            "activation_marker.json",
            "final_lock.json",
            "GLOBAL_STOP",
            "selection-latest.json",
            "runtime_state.json",
            "system.active.json",
            "writer.lock",
        }
        found = [
            str(path.relative_to(ROOT))
            for path in ROOT.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and (path.name in forbidden_names or path.suffix in {".jsonl", ".log"})
        ]
        self.assertEqual(found, [])

        tracked_output = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
        tracked_forbidden = []
        for raw in tracked_output.split("\0"):
            if not raw:
                continue
            path = Path(raw)
            if (
                path.name in forbidden_names
                or path.name.endswith(".secret")
                or "intents" in path.parts
                or "selection-revisions" in path.parts
            ):
                tracked_forbidden.append(raw)
        self.assertEqual(tracked_forbidden, [])

    def test_sensitive_local_activation_artifacts_are_gitignored(self):
        names = (
            "activation.secret",
            "system.active.json",
            "GLOBAL_STOP",
            "activation_marker.json",
            "final_lock.json",
            "state.json",
        )
        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=ROOT,
            input="".join("private/%s\n" % name for name in names),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            set(result.stdout.splitlines()),
            {"private/%s" % name for name in names},
        )

    def test_market_history_and_backtest_results_are_gitignored(self):
        names = (
            "backtests/run/backtest_report.json",
            "backtest_input_manifest.json",
            "backtest_report_aapl.json",
            "symbol_15m_qfq.json",
            "symbol_15m_raw.json",
            "symbol_daily_qfq.json",
            "symbol_daily_raw.json",
            "spy_daily_qfq.json",
            "us_trading_calendar.json",
        )
        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=ROOT,
            input="".join("%s\n" % name for name in names),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()), set(names))

    def test_public_tree_has_no_high_confidence_credentials_or_private_paths(self):
        patterns = {
            "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
            "github-token": re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
            "aws-key": re.compile(r"AKIA[0-9A-Z]{16}"),
            "local-user-path": re.compile(r"/(?:Users|home)/[^/\s]+/"),
        }
        violations = []
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for label, pattern in patterns.items():
                if pattern.search(text):
                    violations.append(f"{path.relative_to(ROOT)}:{label}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
