"""Exercise isolated configuration creation using real temporary TLS inputs."""
import importlib.util
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("production_configure", ROOT / "ops/production/configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


@pytest.fixture
def settings(tmp_path):
    key = tmp_path / "key.pem"
    cert = tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3",
                    "-subj", "/CN=dataflow.test", "-addext", "subjectAltName=DNS:dataflow.test",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True, timeout=20)
    key.chmod(0o600)
    return SimpleNamespace(directory=tmp_path / "config", hostname="dataflow.test", allow_test_hostname=True,
                           proxy_subnet="172.30.83.0/24", proxy_ip="172.30.83.2", tls_key=key, tls_cert=cert)


def test_generated_configuration_keeps_credentials_separate_and_never_replaces_them(settings):
    report = configure.create(settings)
    assert report == {"status": "configuration_verified", "hostname": "dataflow.test",
                      "independent_backup_configured": False, "smtp_configured": False,
                      "external_services_validated": False}
    runtime = configure.read_env(settings.directory / "runtime.env")
    recovery = configure.read_env(settings.directory / "recovery.env")
    assert "dataflow_runtime:" in runtime["DATABASE_URL"]
    assert "dataflow_backup:" in recovery["DATABASE_URL"]
    assert "SECRET_KEY" not in recovery and "DATAFLOW_BACKUP_PASSWORD" not in runtime
    before = {name: (settings.directory / name).read_bytes() for name in configure.FILES}
    for name in configure.FILES:
        assert stat.S_IMODE((settings.directory / name).stat().st_mode) == 0o600
    with pytest.raises(ValueError, match="existing secrets"):
        configure.create(settings)
    assert {name: (settings.directory / name).read_bytes() for name in configure.FILES} == before


def test_configuration_rejects_owner_identity_in_runtime(settings):
    configure.create(settings)
    runtime = settings.directory / "runtime.env"
    runtime.write_text(runtime.read_text().replace("dataflow_runtime:", "dataflow_owner:"))
    with pytest.raises(ValueError, match="separate generated roles"):
        configure.check(settings.directory)


def test_configuration_rejects_wrong_tls_hostname(settings):
    settings.hostname = "different.test"
    with pytest.raises(ValueError, match="TLS certificate"):
        configure.create(settings)
    assert not settings.directory.exists()


def test_configuration_rejects_unsafe_proxy_changes(settings):
    configure.create(settings)
    runtime = settings.directory / "runtime.env"
    runtime.write_text(runtime.read_text().replace("FORWARDED_ALLOW_IPS=172.30.83.2", "FORWARDED_ALLOW_IPS=*"))
    with pytest.raises(ValueError, match="trusted proxy"):
        configure.check(settings.directory)


def test_configuration_reserves_distinct_api_and_proxy_addresses(settings):
    configure.create(settings)
    compose = configure.read_env(settings.directory / "compose.env")
    assert compose["DATAFLOW_API_PROXY_IP"] == "172.30.83.3"
    assert configure.api_proxy_address("172.30.83.0/29", "172.30.83.3") == "172.30.83.2"
    path = settings.directory / "compose.env"
    path.write_text(path.read_text().replace("DATAFLOW_API_PROXY_IP=172.30.83.3", "DATAFLOW_API_PROXY_IP=172.30.83.2"))
    with pytest.raises(ValueError, match="distinct private addresses"):
        configure.check(settings.directory)


@pytest.mark.parametrize("subnet,peer", [("127.0.0.0/24", "127.0.0.2"), ("169.254.0.0/24", "169.254.0.2"),
                                       ("172.30.83.0/30", "172.30.83.2"), ("172.30.83.0/24", "172.30.83.1")])
def test_unusable_proxy_networks_are_rejected(subnet, peer):
    with pytest.raises(ValueError, match="RFC1918"):
        configure.validate_network(subnet, peer)
