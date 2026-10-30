"""Installer and uninstaller. No root, no apt, no real qdisc."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
UNINSTALL = ROOT / "uninstall.sh"


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=False, text=True, capture_output=True, **kwargs)


def _quiet_ps(directory: Path) -> dict[str, str]:
    """PATH where `ps` reports nobody, so a real bandctl on this machine is ignored."""
    ps = directory / "ps"
    ps.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    ps.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{directory}:/usr/bin:/bin"
    return env


class ScriptTests(unittest.TestCase):
    def test_syntax(self):
        for script in (INSTALL, UNINSTALL):
            result = _run(["bash", "-n", str(script)])
            self.assertEqual(result.returncode, 0, result.stderr)
            mode = script.stat().st_mode
            self.assertTrue(mode & stat.S_IXUSR, script.name)

    def test_help(self):
        for script in (INSTALL, UNINSTALL):
            result = _run(["bash", str(script), "--help"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Usage", result.stdout)

    def test_default_install_keeps_arguments_when_it_asks_for_root(self):
        if os.geteuid() == 0:
            self.skipTest("already root, so install would not re-exec sudo")
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            sudo = bindir / "sudo"
            sudo.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$BANDCTL_SUDO_LOG\"\n", encoding="utf-8")
            sudo.chmod(0o755)
            log = Path(tmp) / "sudo.log"
            env = os.environ.copy()
            env["PATH"] = f"{bindir}:/usr/bin:/bin"
            env["BANDCTL_SUDO_LOG"] = str(log)
            result = _run(["bash", str(INSTALL), "--skip-deps"], env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = log.read_text(encoding="utf-8")
            self.assertIn(str(INSTALL), text)
            self.assertIn("--skip-deps", text)

    def test_uninstall_removes_only_its_own_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "opt" / "bandctl"
            bindir = root / "bin"
            venv_bin = prefix / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            bindir.mkdir()
            log = root / "python.log"
            python = venv_bin / "python"
            python.write_text(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    printf '%s\\n' "$*" >> {log}
                    exit 0
                    """
                ),
                encoding="utf-8",
            )
            python.chmod(0o755)
            (venv_bin / "bandctl").symlink_to(python)
            (prefix / "install-stamp").write_text(
                f"PREFIX={prefix}\nBINDIR={bindir}\n",
                encoding="utf-8",
            )
            (prefix / "uninstall.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (bindir / "bandctl").symlink_to(venv_bin / "bandctl")
            (bindir / "bandctl-uninstall").write_text(
                f"#!/bin/sh\nexec {prefix}/uninstall.sh \"$@\"\n",
                encoding="utf-8",
            )
            foreign = bindir / "other"
            foreign.write_text("keep\n", encoding="utf-8")
            tools = root / "tools"
            tools.mkdir()
            result = _run(
                ["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)],
                env=_quiet_ps(tools),
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("recover", log.read_text(encoding="utf-8"))
            self.assertFalse(prefix.exists())
            self.assertFalse((bindir / "bandctl").exists())
            self.assertFalse((bindir / "bandctl-uninstall").exists())
            self.assertEqual(foreign.read_text(encoding="utf-8"), "keep\n")
            self.assertIn("Uninstalled", result.stdout)

    def test_uninstall_refuses_a_live_process_and_a_dangerous_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "opt" / "bandctl"
            prefix.mkdir(parents=True)
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={prefix}/bin\n", encoding="utf-8")
            launcher = Path(tmp) / "fake-bandctl"
            launcher.write_text(
                "#!/bin/bash\nexec -a /usr/local/bin/bandctl /bin/sleep 60\n",
                encoding="utf-8",
            )
            launcher.chmod(0o755)
            proc = subprocess.Popen(
                [str(launcher)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                seen = False
                for _ in range(50):
                    listing = subprocess.run(["ps", "-eo", "args="], check=False, capture_output=True, text=True)
                    if "/usr/local/bin/bandctl" in listing.stdout:
                        seen = True
                        break
                    time.sleep(0.05)
                self.assertTrue(seen, listing.stdout)
                result = _run(
                    ["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(prefix / "bin")]
                )
            finally:
                proc.kill()
                proc.wait(timeout=5)
            self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
            self.assertIn("still running", result.stderr)
            self.assertTrue(prefix.exists())
        result = _run(["bash", str(UNINSTALL), "--prefix", "/", "--bindir", str(Path(tmp) / "bin")])
        self.assertEqual(result.returncode, 1)
        self.assertIn("refusing", result.stderr)

    def test_installed_copy_uninstalls_itself_without_a_prefix_flag(self):
        if os.geteuid() == 0:
            self.skipTest("root would not need the sudo guard this test plants")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "opt" / "bandctl"
            bindir = root / "bin"
            fake_bin = root / "fakebin"
            bindir.mkdir()
            fake_bin.mkdir()
            prefix.mkdir(parents=True)
            sudo = fake_bin / "sudo"
            sudo.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$BANDCTL_SUDO_LOG\"\nexit 0\n", encoding="utf-8")
            sudo.chmod(0o755)
            log = root / "sudo.log"
            env = _quiet_ps(fake_bin)
            (prefix / "install-stamp").write_text(
                f"PREFIX={prefix}\nBINDIR={bindir}\n",
                encoding="utf-8",
            )
            installed = prefix / "uninstall.sh"
            installed.write_text(UNINSTALL.read_text(encoding="utf-8"), encoding="utf-8")
            installed.chmod(0o755)
            (bindir / "bandctl").symlink_to(prefix / "missing")
            env["BANDCTL_SUDO_LOG"] = str(log)
            result = _run(["bash", str(installed)], env=env)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertFalse(log.exists(), "uninstall tried to re-exec sudo")
            self.assertFalse(prefix.exists())
            self.assertFalse((bindir / "bandctl").exists())
            self.assertIn("Uninstalled", result.stdout)

    def test_sudo_command_uses_the_installed_name(self):
        from bandctl.app import BandctlApp

        app = BandctlApp()
        old = sys.argv[:]
        try:
            sys.argv = ["/usr/local/bin/bandctl"]
            self.assertEqual(app.sudo_command(), "sudo bandctl")
            app._iface_arg = "wlan0"
            self.assertEqual(app.sudo_command(), "sudo bandctl --iface=wlan0")
            sys.argv = [old[0]]
            text = app.sudo_command()
        finally:
            sys.argv = old
        self.assertIn(" -m bandctl", text)
        self.assertIn("--iface=wlan0", text)


if __name__ == "__main__":
    unittest.main()
