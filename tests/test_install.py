"""Installer and uninstaller. No root, no apt, no real qdisc."""

from __future__ import annotations

import os
import fcntl
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
    identity = directory / "id"
    identity.write_text("#!/bin/sh\nprintf '0\\n'\n", encoding="utf-8")
    identity.chmod(0o755)
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

    def test_piped_install_removes_download_directory_on_success_and_failure(self):
        for curl_status, child_status, expected in ((0, 0, 0), (0, 7, 7), (9, 0, 9)):
            with self.subTest(curl_status=curl_status, child_status=child_status), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                tools = root / "tools"
                downloads = root / "downloads"
                tools.mkdir()
                downloads.mkdir()
                curl = tools / "curl"
                curl.write_text('#!/bin/sh\nexit "$CURL_STATUS"\n', encoding="utf-8")
                curl.chmod(0o755)
                tar = tools / "tar"
                tar.write_text(textwrap.dedent('''\
                    #!/bin/sh
                    cat > "$3/install.sh" <<'EOF'
                    #!/bin/sh
                    printf '%s\\n' "$@" > "$BANDCTL_ARG_LOG"
                    exit "$CHILD_STATUS"
                    EOF
                    chmod +x "$3/install.sh"
                '''), encoding="utf-8")
                tar.chmod(0o755)
                env = _quiet_ps(tools)
                env.update(TMPDIR=str(downloads), CURL_STATUS=str(curl_status), CHILD_STATUS=str(child_status),
                           BANDCTL_ARG_LOG=str(root / "args"))
                result = _run(["bash", "-s", "--", "--skip-deps"], input=INSTALL.read_text(encoding="utf-8"),
                              cwd=root, env=env)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(list(downloads.iterdir()), [])
                if curl_status == 0:
                    self.assertEqual((root / "args").read_text(encoding="utf-8"), "--skip-deps\n")

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

    def test_install_checks_command_conflicts_before_replacing_venv(self):
        for command in ("bandctl", "bandctl-uninstall"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                prefix, bindir, tools = root / "install", root / "bin", root / "tools"
                (prefix / "venv").mkdir(parents=True)
                bindir.mkdir()
                tools.mkdir()
                existing = prefix / "venv" / "existing"
                existing.write_text("keep", encoding="utf-8")
                (bindir / command).write_text("foreign command", encoding="utf-8")
                attempted = root / "venv-attempted"
                python = tools / "python3"
                python.write_text(f"#!/bin/sh\n[ \"$1\" = -c ] && exit 0\ntouch {attempted}\nexit 1\n", encoding="utf-8")
                python.chmod(0o755)
                result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                               "--skip-deps"], env=_quiet_ps(tools))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("already exists", result.stderr)
                self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
                self.assertFalse(attempted.exists())

    def test_install_refuses_nonempty_unowned_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            prefix.mkdir()
            bindir.mkdir()
            tools.mkdir()
            existing = prefix / ".notes.txt"
            existing.write_text("keep", encoding="utf-8")
            attempted = root / "venv-attempted"
            python = tools / "python3"
            python.write_text(f'#!/bin/sh\n[ "$1" = -c ] && exit 0\ntouch "{attempted}"\nexit 1\n', encoding="utf-8")
            python.chmod(0o755)

            result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                           "--skip-deps"], env=_quiet_ps(tools))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not a bandctl install", result.stderr)
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
            self.assertFalse(attempted.exists())

    def test_install_refuses_stamp_for_another_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            (prefix / "venv").mkdir(parents=True)
            bindir.mkdir()
            tools.mkdir()
            existing = prefix / "venv" / "existing"
            existing.write_text("keep", encoding="utf-8")
            (prefix / "install-stamp").write_text("PREFIX=/other/install\n", encoding="utf-8")

            result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                           "--skip-deps"], env=_quiet_ps(tools))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("install stamp does not match", result.stderr)
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")

    def test_install_refuses_regular_command_inside_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, tools = root / "install", root / "tools"
            bindir = prefix / "bin"
            bindir.mkdir(parents=True)
            tools.mkdir()
            command = bindir / "bandctl"
            command.write_text("foreign command", encoding="utf-8")
            attempted = root / "venv-attempted"
            python = tools / "python3"
            python.write_text(f"#!/bin/sh\n[ \"$1\" = -c ] && exit 0\ntouch {attempted}\nexit 1\n", encoding="utf-8")
            python.chmod(0o755)
            result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                           "--skip-deps"], env=_quiet_ps(tools))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already exists", result.stderr)
            self.assertEqual(command.read_text(encoding="utf-8"), "foreign command")
            self.assertFalse(attempted.exists())

    def test_install_refuses_symlinks_at_managed_files(self):
        for name in ("uninstall.sh", "install-stamp"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                prefix, bindir, tools = root / "install", root / "bin", root / "tools"
                (prefix / "venv").mkdir(parents=True)
                bindir.mkdir()
                tools.mkdir()
                existing = prefix / "venv" / "existing"
                existing.write_text("keep", encoding="utf-8")
                target = root / "foreign"
                target.write_text("keep", encoding="utf-8")
                (prefix / name).symlink_to(target)
                attempted = root / "venv-attempted"
                python = tools / "python3"
                python.write_text(
                    f"#!/bin/sh\n[ \"$1\" = -c ] && exit 0\ntouch {attempted}\n"
                    "mkdir -p \"$3/bin\"\n"
                    "printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/pip\"\n"
                    "printf '#!/bin/sh\\nprintf test-version\\n' > \"$3/bin/bandctl\"\n"
                    "chmod +x \"$3/bin/pip\" \"$3/bin/bandctl\"\n", encoding="utf-8")
                python.chmod(0o755)
                result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                               "--skip-deps"], env=_quiet_ps(tools))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("symlink", result.stderr)
                self.assertEqual(target.read_text(encoding="utf-8"), "keep")
                self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
                self.assertFalse(attempted.exists())

    def test_install_rejects_prefix_resolving_to_a_protected_directory(self):
        for alias in ("symlink", "dot", "parent"):
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                link, bindir, tools = root / "prefix", root / "bin", root / "tools"
                link.symlink_to("/tmp", target_is_directory=True)
                prefix = {"symlink": str(link), "dot": "/tmp/.", "parent": f"{root}/.."}[alias]
                bindir.mkdir()
                tools.mkdir()
                env = _quiet_ps(tools)
                attempted = root / "mkdir-attempted"
                mkdir = tools / "mkdir"
                mkdir.write_text(f"#!/bin/sh\ntouch {attempted}\nexit 1\n", encoding="utf-8")
                mkdir.chmod(0o755)
                python = tools / "python3"
                python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                python.chmod(0o755)
                result = _run(["bash", str(INSTALL), "--prefix", prefix, "--bindir", str(bindir),
                               "--skip-deps"], env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("refusing", result.stderr)
                self.assertFalse(attempted.exists())

    def test_uninstall_rejects_prefix_resolving_to_a_protected_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            link = root / "prefix"
            link.symlink_to("/tmp", target_is_directory=True)
            for prefix in (str(link), "/tmp/.", f"{root}/.."):
                with self.subTest(prefix=prefix):
                    result = _run(["bash", str(UNINSTALL), "--prefix", prefix,
                                   "--bindir", str(root / "bin")])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("refusing", result.stderr)

    def test_install_allows_its_dangling_command_link_before_rebuilding_venv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            prefix.mkdir()
            bindir.mkdir()
            tools.mkdir()
            (bindir / "bandctl").symlink_to(prefix / "venv" / "bin" / "bandctl")
            attempted = root / "venv-attempted"
            python = tools / "python3"
            python.write_text(f"#!/bin/sh\n[ \"$1\" = -c ] && exit 0\ntouch {attempted}\nexit 1\n", encoding="utf-8")
            python.chmod(0o755)
            result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                           "--skip-deps"], env=_quiet_ps(tools))
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(attempted.exists())
            self.assertNotIn("already exists", result.stderr)

    def test_upgrade_accepts_command_under_symlinked_prefix_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual, alias, bindir, tools = root / "actual", root / "alias", root / "bin", root / "tools"
            actual.mkdir()
            alias.symlink_to(actual, target_is_directory=True)
            prefix = alias / "bandctl"
            (prefix / "venv" / "bin").mkdir(parents=True)
            bindir.mkdir()
            tools.mkdir()
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            (bindir / "bandctl").symlink_to(prefix / "venv" / "bin" / "bandctl")
            python = tools / "python3"
            python.write_text(
                "#!/bin/sh\n"
                '[ "$1" = -c ] && exit 0\n'
                'mkdir -p "$3/bin"\n'
                'printf "#!/bin/sh\\nexit 0\\n" > "$3/bin/pip"\n'
                'printf "#!/bin/sh\\necho test-version\\n" > "$3/bin/bandctl"\n'
                'chmod +x "$3/bin/pip" "$3/bin/bandctl"\n', encoding="utf-8")
            python.chmod(0o755)

            result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                           "--skip-deps"], env=_quiet_ps(tools))

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("test-version", result.stdout)

    def test_uninstall_removes_command_under_symlinked_prefix_parent(self):
        for executable_exists in (False, True):
            with self.subTest(executable_exists=executable_exists), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                actual, alias, bindir, tools = root / "actual", root / "alias", root / "bin", root / "tools"
                actual.mkdir()
                alias.symlink_to(actual, target_is_directory=True)
                prefix = alias / "bandctl"
                (prefix / "venv").mkdir(parents=True)
                bindir.mkdir()
                tools.mkdir()
                (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
                command_target = prefix / "venv" / "bin" / "bandctl"
                if executable_exists:
                    command_target.parent.mkdir()
                    command_target.write_text("installed command", encoding="utf-8")
                command = bindir / "bandctl"
                command.symlink_to(command_target)
                env = _quiet_ps(tools)
                (tools / "id").write_text("#!/bin/sh\nprintf '1000\\n'\n", encoding="utf-8")

                result = _run(["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)], env=env)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(command.is_symlink())
                self.assertFalse(prefix.exists())

    def test_uninstall_keeps_command_link_to_another_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            prefix.mkdir()
            bindir.mkdir()
            tools.mkdir()
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            other = root / "install-other" / "bandctl"
            command = bindir / "bandctl"
            command.symlink_to(other)
            env = _quiet_ps(tools)
            (tools / "id").write_text("#!/bin/sh\nprintf '1000\\n'\n", encoding="utf-8")

            result = _run(["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)], env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(command.is_symlink())
            self.assertEqual(command.readlink(), other)
            self.assertIn("not this install", result.stderr)

    def test_failed_install_keeps_previous_virtual_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            (prefix / "venv").mkdir(parents=True)
            bindir.mkdir()
            tools.mkdir()
            existing = prefix / "venv" / "existing"
            existing.write_text("keep", encoding="utf-8")
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            python = tools / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "[ \"$1\" = -c ] && exit 0\n"
                "mkdir -p \"$3/bin\"\n"
                "printf '#!/bin/sh\\nexit 1\\n' > \"$3/bin/pip\"\n"
                "chmod +x \"$3/bin/pip\"\n", encoding="utf-8")
            python.chmod(0o755)
            result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                           "--skip-deps"], env=_quiet_ps(tools))
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(existing.exists())
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
            self.assertEqual(list(prefix.glob(".venv-backup.*")), [])

    def test_successful_install_discards_virtual_environment_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            (prefix / "venv").mkdir(parents=True)
            bindir.mkdir()
            tools.mkdir()
            (prefix / "venv" / "old").write_text("old", encoding="utf-8")
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            python = tools / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "[ \"$1\" = -c ] && exit 0\n"
                "mkdir -p \"$3/bin\"\n"
                "printf '#!/bin/sh\\nexit 0\\n' > \"$3/bin/pip\"\n"
                "printf '#!/bin/sh\\nprintf test-version\\n' > \"$3/bin/bandctl\"\n"
                "chmod +x \"$3/bin/pip\" \"$3/bin/bandctl\"\n", encoding="utf-8")
            python.chmod(0o755)
            result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                           "--skip-deps"], env=_quiet_ps(tools))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((prefix / "venv" / "old").exists())
            self.assertTrue((bindir / "bandctl").exists())
            self.assertEqual(list(prefix.glob(".venv-backup.*")), [])

    def test_failed_version_check_preserves_previous_install_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            (prefix / "venv").mkdir(parents=True)
            bindir.mkdir()
            tools.mkdir()
            (prefix / "venv" / "old").write_text("old venv", encoding="utf-8")
            stamp = prefix / "install-stamp"
            stamp.write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            uninstall = prefix / "uninstall.sh"
            uninstall.write_text("old uninstall script\n", encoding="utf-8")
            wrapper = bindir / "bandctl-uninstall"
            wrapper_text = f'#!/bin/sh\nexec "{prefix}/uninstall.sh" --prefix "{prefix}" --bindir "{bindir}" "$@"\n'
            wrapper.write_text(wrapper_text, encoding="utf-8")
            command = bindir / "bandctl"
            command.symlink_to(prefix / "venv" / "bin" / "bandctl")
            python = tools / "python3"
            python.write_text(
                "#!/bin/sh\n"
                '[ "$1" = -c ] && exit 0\n'
                'mkdir -p "$3/bin"\n'
                'printf "#!/bin/sh\\nexit 0\\n" > "$3/bin/pip"\n'
                'printf "#!/bin/sh\\nexit 1\\n" > "$3/bin/bandctl"\n'
                'chmod +x "$3/bin/pip" "$3/bin/bandctl"\n', encoding="utf-8")
            python.chmod(0o755)

            result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                           "--skip-deps"], env=_quiet_ps(tools))

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((prefix / "venv" / "old").read_text(encoding="utf-8"), "old venv")
            self.assertEqual(uninstall.read_text(encoding="utf-8"), "old uninstall script\n")
            self.assertEqual(wrapper.read_text(encoding="utf-8"), wrapper_text)
            self.assertEqual(stamp.read_text(encoding="utf-8"), f"PREFIX={prefix}\nBINDIR={bindir}\n")
            self.assertEqual(command.readlink(), prefix / "venv" / "bin" / "bandctl")

    def test_install_refuses_uninstall_wrapper_symlinks_before_replacing_venv(self):
        for exists in (False, True):
            with self.subTest(target_exists=exists), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                prefix, bindir, tools = root / "install", root / "bin", root / "tools"
                (prefix / "venv").mkdir(parents=True)
                bindir.mkdir()
                tools.mkdir()
                existing = prefix / "venv" / "existing"
                existing.write_text("keep", encoding="utf-8")
                target = root / "foreign"
                if exists:
                    target.write_text(f"{prefix}/uninstall.sh\nkeep", encoding="utf-8")
                (bindir / "bandctl-uninstall").symlink_to(target)
                python = tools / "python3"
                python.write_text("#!/bin/sh\n[ \"$1\" = -c ] && exit 0\nexit 1\n", encoding="utf-8")
                python.chmod(0o755)
                result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                               "--skip-deps"], env=_quiet_ps(tools))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("already exists", result.stderr)
                self.assertEqual(existing.read_text(encoding="utf-8"), "keep")
                if exists:
                    self.assertEqual(target.read_text(encoding="utf-8"), f"{prefix}/uninstall.sh\nkeep")
                else:
                    self.assertFalse(target.exists())

    def test_install_preserves_unrelated_wrapper_mentioning_uninstall_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            prefix.mkdir()
            bindir.mkdir()
            tools.mkdir()
            wrapper = bindir / "bandctl-uninstall"
            contents = f"#!/bin/sh\necho 'See {prefix}/uninstall.sh'\n"
            wrapper.write_text(contents, encoding="utf-8")
            attempted = root / "venv-attempted"
            python = tools / "python3"
            python.write_text(f'#!/bin/sh\n[ "$1" = -c ] && exit 0\ntouch "{attempted}"\nexit 1\n', encoding="utf-8")
            python.chmod(0o755)

            result = _run(["bash", str(INSTALL), "--prefix", str(prefix), "--bindir", str(bindir),
                           "--skip-deps"], env=_quiet_ps(tools))

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already exists", result.stderr)
            self.assertEqual(wrapper.read_text(encoding="utf-8"), contents)
            self.assertFalse(attempted.exists())

    def test_uninstall_preserves_unrelated_wrapper_mentioning_uninstall_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            prefix.mkdir()
            bindir.mkdir()
            tools.mkdir()
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            wrapper = bindir / "bandctl-uninstall"
            contents = f"#!/bin/sh\necho 'See {prefix}/uninstall.sh'\n"
            wrapper.write_text(contents, encoding="utf-8")
            env = _quiet_ps(tools)
            (tools / "id").write_text("#!/bin/sh\nprintf '1000\\n'\n", encoding="utf-8")

            result = _run(["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)], env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(wrapper.read_text(encoding="utf-8"), contents)
            self.assertIn("left", result.stderr)

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
                    #!/bin/bash
                    printf '%s\\n' "$*" >> {log}
                    exec bash -e -c 'remove_install "$1" "$2"' bandctl-uninstall "$3" "$4"
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
                f'#!/bin/sh\nexec "{prefix}/uninstall.sh" --prefix "{prefix}" --bindir "{bindir}" "$@"\n',
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

    def test_uninstall_preserves_other_files_inside_install_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            (prefix / "venv").mkdir(parents=True)
            bindir.mkdir()
            tools.mkdir()
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            (prefix / "uninstall.sh").write_text("installed script", encoding="utf-8")
            (prefix / ".notes").write_text("keep", encoding="utf-8")
            backup = prefix / ".venv-backup.saved"
            backup.mkdir()
            (backup / "previous").write_text("keep", encoding="utf-8")
            env = _quiet_ps(tools)
            (tools / "id").write_text("#!/bin/sh\nprintf '1000\\n'\n", encoding="utf-8")

            result = _run(["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)], env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((prefix / ".notes").read_text(encoding="utf-8"), "keep")
            self.assertEqual((backup / "previous").read_text(encoding="utf-8"), "keep")
            self.assertFalse((prefix / "venv").exists())
            self.assertFalse((prefix / "uninstall.sh").exists())
            self.assertFalse((prefix / "install-stamp").exists())
            self.assertIn("left", result.stderr)

    def test_uninstall_preserves_regular_bandctl_command_inside_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, tools = root / "install", root / "tools"
            bindir = prefix / "bin"
            bindir.mkdir(parents=True)
            (prefix / "venv").mkdir()
            tools.mkdir()
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            command = bindir / "bandctl"
            command.write_text("foreign command", encoding="utf-8")
            env = _quiet_ps(tools)
            (tools / "id").write_text("#!/bin/sh\nprintf '1000\\n'\n", encoding="utf-8")

            result = _run(["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)], env=env)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(command.read_text(encoding="utf-8"), "foreign command")
            self.assertIn("left", result.stderr)

    def test_uninstall_warns_when_recovery_reports_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            venv_bin = prefix / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            bindir.mkdir()
            tools.mkdir()
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            package = root / "bandctl"
            package.mkdir()
            (package / "__init__.py").write_text(f"__path__.append({str(ROOT / 'bandctl')!r})\n", encoding="utf-8")
            (package / "shaper.py").write_text(
                "from bandctl import session_lock\n"
                "class Shaper:\n"
                "    def __init__(self, iface):\n"
                "        assert session_lock.held\n"
                "        self.recovery_error = 'cleanup failed'\n"
                "    def recover(self): return ['cleanup failed\\x1b[2J\\x85']\n", encoding="utf-8")
            (package / "session_lock.py").write_text(
                "import os\n"
                "held = False\n"
                "class SessionLockError(RuntimeError): pass\n"
                "class SessionLock:\n"
                "    def __enter__(self):\n"
                "        global held\n"
                "        held = True\n"
                "        return self\n"
                "    def __exit__(self, *args):\n"
                "        global held\n"
                "        held = False\n"
                f"        if os.path.exists({str(prefix)!r}): open({str(root / 'lock-released-early')!r}, 'w').close()\n",
                encoding="utf-8")
            python = venv_bin / "python"
            python.symlink_to(sys.executable)
            env = _quiet_ps(tools)
            env["PYTHONPATH"] = str(root)
            result = _run(["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)],
                          env=env, cwd=root)
            self.assertIn("cleanup failed", result.stdout)
            self.assertIn("cleanup failed?[2J?", result.stdout)
            self.assertNotIn("\x1b", result.stdout)
            self.assertIn("could not clear kernel state", result.stderr)
            self.assertFalse(prefix.exists())
            self.assertFalse((root / "lock-released-early").exists())

    def test_uninstall_refuses_recovery_when_session_lock_is_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            venv_bin = prefix / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            bindir.mkdir()
            tools.mkdir()
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            package = root / "bandctl"
            package.mkdir()
            (package / "__init__.py").write_text(f"__path__.append({str(ROOT / 'bandctl')!r})\n", encoding="utf-8")
            (package / "session_lock.py").write_text(
                "class SessionLockError(RuntimeError): pass\n"
                "class SessionLock:\n"
                "    def __enter__(self): raise SessionLockError('session busy')\n"
                "    def __exit__(self, *args): pass\n", encoding="utf-8")
            marker = root / "recovery-called"
            (package / "shaper.py").write_text(
                f"class Shaper:\n    def __init__(self, iface): open({str(marker)!r}, 'w').close()\n"
                "    def recover(self): return []\n", encoding="utf-8")
            (venv_bin / "python").symlink_to(sys.executable)
            env = _quiet_ps(tools)
            env["PYTHONPATH"] = str(root)
            result = _run(["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)],
                          env=env, cwd=root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("session busy", result.stderr)
            self.assertFalse(marker.exists())
            self.assertTrue(prefix.exists())

    def test_uninstall_without_venv_still_checks_session_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            prefix.mkdir()
            bindir.mkdir()
            tools.mkdir()
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            python = tools / "python3"
            python.symlink_to(sys.executable)
            (root / "sitecustomize.py").write_text(
                "import os\n"
                "original_open = os.open\n"
                "def redirected_open(path, flags, mode=0o777, *, dir_fd=None):\n"
                "    if path == '/run/bandctl.lock': path = os.environ['TEST_BANDCTL_LOCK']\n"
                "    return original_open(path, flags, mode, dir_fd=dir_fd)\n"
                "os.open = redirected_open\n", encoding="utf-8")
            env = _quiet_ps(tools)
            env["PYTHONPATH"] = str(root)
            env["TEST_BANDCTL_LOCK"] = str(root / "lock")
            with open(root / "lock", "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = _run(["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)],
                              env=env, cwd=root)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(prefix.exists())
            result = _run(["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)],
                          env=env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("recovery was skipped", result.stderr)
            self.assertFalse(prefix.exists())

    def test_uninstall_with_unimportable_session_lock_uses_fallback_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            venv_bin = prefix / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            bindir.mkdir()
            tools.mkdir()
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            (venv_bin / "python").symlink_to(sys.executable)
            package = root / "bandctl"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (root / "sitecustomize.py").write_text(
                "import os\n"
                "original_open = os.open\n"
                "def redirected_open(path, flags, mode=0o777, *, dir_fd=None):\n"
                "    if path == '/run/bandctl.lock': path = os.environ['TEST_BANDCTL_LOCK']\n"
                "    return original_open(path, flags, mode, dir_fd=dir_fd)\n"
                "os.open = redirected_open\n", encoding="utf-8")
            env = _quiet_ps(tools)
            env["PYTHONPATH"] = str(root)
            env["TEST_BANDCTL_LOCK"] = str(root / "lock")
            command = ["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)]

            with open(root / "lock", "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = _run(command, env=env, cwd=root)
            self.assertNotEqual(busy.returncode, 0)
            self.assertTrue(prefix.exists())
            result = _run(command, env=env, cwd=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("recovery was skipped", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(prefix.exists())

    def test_nonroot_uninstall_skips_privileged_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix, bindir, tools = root / "install", root / "bin", root / "tools"
            venv_bin = prefix / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            bindir.mkdir()
            tools.mkdir()
            (prefix / "install-stamp").write_text(f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
            marker = root / "recovery-called"
            python = venv_bin / "python"
            python.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n", encoding="utf-8")
            python.chmod(0o755)
            env = _quiet_ps(tools)
            (tools / "id").write_text("#!/bin/sh\nprintf '1000\\n'\n", encoding="utf-8")
            result = _run(["bash", str(UNINSTALL), "--prefix", str(prefix), "--bindir", str(bindir)], env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())
            self.assertFalse(prefix.exists())
            self.assertIn("recovery was skipped", result.stderr)

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

    def test_scripts_detect_bandctl_even_when_arguments_name_install_scripts(self):
        for script in (INSTALL, UNINSTALL):
            commands = (f"{launcher} --config {argument}"
                        for launcher in ("/usr/bin/python3 -m bandctl",
                                         "/opt/bandctl/venv/bin/python3 /usr/local/bin/bandctl",
                                         "/usr/local/bin/bandctl")
                        for argument in ("/tmp/install.sh", "/tmp/uninstall.json"))
            for command in commands:
                with self.subTest(script=script.name, command=command), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    prefix, bindir, tools = root / "install", root / "bin", root / "tools"
                    bindir.mkdir()
                    tools.mkdir()
                    env = _quiet_ps(tools)
                    (tools / "ps").write_text(
                        f"#!/bin/sh\nprintf '%s\\n' '{command}'\n",
                        encoding="utf-8")
                    python = tools / "python3"
                    python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                    python.chmod(0o755)
                    if script == UNINSTALL:
                        prefix.mkdir()
                        (prefix / "install-stamp").write_text(
                            f"PREFIX={prefix}\nBINDIR={bindir}\n", encoding="utf-8")
                    result = _run(["bash", str(script), "--prefix", str(prefix), "--bindir", str(bindir),
                                   *(["--skip-deps"] if script == INSTALL else [])], env=env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("running", result.stderr)

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
            python = fake_bin / "python3"
            python.write_text("#!/bin/bash\nexec bash -e -c 'remove_install \"$1\" \"$2\"' bandctl-uninstall \"$3\" \"$4\"\n", encoding="utf-8")
            python.chmod(0o755)
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
