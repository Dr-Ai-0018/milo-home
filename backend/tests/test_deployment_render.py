"""The public deployment entry produces reviewable files without live resources."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "render-deployment.py"
spec = importlib.util.spec_from_file_location("deployment_renderer", SCRIPT)
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


@pytest.fixture
def deployment(tmp_path):
    code = tmp_path / "installed code"
    code.mkdir()
    executable = code / "python"
    executable.symlink_to(sys.executable)
    owner, session = str(uuid4()), str(uuid4())
    config = tmp_path / "site.toml"
    config.write_text(
        f'[app]\ndatabase="{tmp_path}/state/home.sqlite3"\n'
        'origins=["https://home.example.test"]\nsecure_cookie=true\n'
        f'[paths]\ncode_root="{code}"\nstatic_root="{tmp_path}/web"\n'
        f'protected_roots=["{tmp_path}/personal"]\n'
        '[execution]\ndatabase_user="synthetic_web"\n'
        f'run_directory="{tmp_path}/native"\nreply_directory="{tmp_path}/replies"\n'
        f'workspace="{tmp_path}/workspace"\nprofile="{tmp_path}/provider.toml"\n'
        f'credential_file="{tmp_path}/private key"\n'
        f'[maintenance]\nfile="{tmp_path}/observation/summary.json"\nowner="{owner}"\n'
    )
    config.chmod(0o640)
    arguments = [
        "--config",
        str(config),
        "--python",
        str(executable),
        "--working-directory",
        str(code),
        "--domain",
        "home.example.test",
        "--acme-root",
        str(tmp_path / "acme"),
        "--certificate",
        str(tmp_path / "tls" / "cert.pem"),
        "--certificate-key",
        str(tmp_path / "tls" / "key.pem"),
    ]
    return arguments, owner, session


def test_api_preview_uses_file_only_and_single_restricted_cpu(deployment, monkeypatch):
    arguments, _, _ = deployment
    monkeypatch.setenv("MILO_DATABASE", "/do-not-use/home.sqlite3")
    monkeypatch.setenv("MILO_ORIGINS", "https://do-not-use.invalid")
    files = renderer.render(renderer.parser().parse_args(arguments), available={7})
    assert set(files) == {*renderer.API_FILES, "manifest.json"}
    api = files["milo-home-api.service"]
    assert "CPUAffinity=7" in api and "AllowedCPUs=7" in api and "CPUQuota=50%" in api
    assert 'ExecStart="' in api and 'installed code/python" -m uvicorn' in api
    assert "do-not-use" not in "".join(files.values())
    assert "LoadCredential" not in api and "ProtectSystem=strict" in api
    assert "server_name home.example.test;" in files["nginx-https.conf"]
    assert 'root "' in files["nginx-http.conf"] and "return 503;" in files["nginx-http.conf"]


def test_workers_keep_native_binding_and_have_no_fixed_model(deployment):
    arguments, owner, session = deployment
    args = renderer.parser().parse_args(
        arguments + ["--execution", "--owner", owner, "--session", session]
    )
    files = renderer.render(args, available={2, 4, 6, 8})
    assert len(files) == 10
    reply = files["milo-home-reply.service"]
    assert f"--owner {owner}" in reply and "--native-home" in reply
    assert "--model" not in reply and "gpt-" not in reply
    assert 'LoadCredential="provider-key:' in reply and 'private key"' in reply
    assert "%d/provider-key" in reply and "--execute" in reply
    assert "AllowedCPUs=2,4" in reply and "CPUAffinity=2 4" in reply
    assert "LoadCredential" not in files["milo-home-maintenance.service"]
    manifest = json.loads(files["manifest.json"])
    assert manifest["installed"] is False and len(manifest["files"]) == 9


def test_cpu_override_cannot_escape_current_cpuset():
    assert renderer.cpu_selection("9,3,9", {3, 9}) == [3, 9]
    with pytest.raises(ValueError):
        renderer.cpu_selection("0", {3, 9})


@pytest.mark.parametrize("field,value", [("domain", "home.test; include other;"), ("port", 65536)])
def test_invalid_preview_never_writes_output(deployment, field, value, tmp_path):
    arguments, _, _ = deployment
    args = renderer.parser().parse_args(arguments)
    setattr(args, field, value)
    with pytest.raises(ValueError):
        renderer.render(args, available={1})
    assert not (tmp_path / "state").exists()


def test_execution_requires_real_explicit_bindings(deployment):
    arguments, _, _ = deployment
    args = renderer.parser().parse_args(arguments + ["--execution"])
    with pytest.raises(ValueError, match="owner and session"):
        renderer.render(args, available={1})


def test_real_cli_writes_new_review_directory_and_refuses_overwrite(deployment, tmp_path):
    arguments, _, _ = deployment
    target = tmp_path / "rendered"
    command = [sys.executable, str(SCRIPT), *arguments, "--out", str(target)]
    first = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert first.returncode == 0, first.stderr
    original = (target / "manifest.json").read_bytes()
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in target.iterdir())
    second = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert second.returncode == 1 and (target / "manifest.json").read_bytes() == original
    assert not (tmp_path / "state").exists()


def test_systemd_verifies_quoted_executable_and_all_generated_units(deployment, tmp_path):
    arguments, owner, session = deployment
    args = renderer.parser().parse_args(
        arguments + ["--execution", "--owner", owner, "--session", session]
    )
    files = renderer.render(args, available=os.sched_getaffinity(0))
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    # verify parses but does not start any unit, create a user or open credentials.
    units = [str(tmp_path / name) for name in files if name.endswith((".service", ".timer"))]
    result = subprocess.run(
        ["systemd-analyze", "verify", *units], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "state").exists()


def test_nginx_checks_both_rendered_configurations_without_starting(deployment, tmp_path):
    arguments, _, _ = deployment
    args = renderer.parser().parse_args(arguments)
    files = renderer.render(args, available={1})
    args.certificate.parent.mkdir()
    certificate = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-subj",
            "/CN=home.example.test",
            "-days",
            "1",
            "-keyout",
            str(args.certificate_key),
            "-out",
            str(args.certificate),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert certificate.returncode == 0, certificate.stderr
    for name in ("nginx-http.conf", "nginx-https.conf"):
        config = tmp_path / name
        config.write_text(
            f'pid "{tmp_path}/unused.pid";\nerror_log stderr;\nevents {{}}\nhttp {{\n'
            + files[name]
            + "}\n"
        )
        result = subprocess.run(
            ["/usr/sbin/nginx", "-t", "-c", str(config), "-p", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
    # nginx -t may create its named pid file, but never writes a master PID.
    assert not (tmp_path / "unused.pid").read_bytes()
