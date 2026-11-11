#!/usr/bin/env python3
"""Create or validate an isolated production deployment configuration outside the checkout."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import subprocess
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
FILES = ("compose.env", "db.env", "migration.env", "runtime.env", "recovery.env", "redis.env", "backup.env")


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise ValueError(f"Invalid or duplicate setting in {path.name}")
        values[key] = value
    return values


def private_file(path: Path) -> None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}:
        raise ValueError(f"{path.name} must be a regular private file with mode 0400 or 0600")


def validate_hostname(host: str, allow_test: bool = False) -> None:
    if (len(host) > 253 or host != host.lower() or "." not in host
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split("."))):
        raise ValueError("Supply a lowercase fully qualified application hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Use a DNS hostname covered by the TLS certificate")
    if host.endswith((".invalid", ".localhost")) or host in {"example.com", "example.org", "example.net"}:
        raise ValueError("Reserved example hostnames cannot be deployed")
    if host.endswith(".test") and not allow_test:
        raise ValueError("Test-only hostnames require --allow-test-hostname")


def validate_tls(cert: Path, key: Path, host: str) -> None:
    if not cert.is_absolute() or not key.is_absolute() or cert.is_symlink() or key.is_symlink():
        raise ValueError("TLS inputs must be absolute regular file paths without symlinks")
    private_file(key)
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
    for arguments in (["-checkhost", host], ["-checkend", "86400"]):
        result = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", *arguments],
                                capture_output=True, timeout=10)
        if result.returncode:
            raise ValueError("TLS certificate must cover the hostname and remain valid for at least one day")


def validate_network(subnet: str, peer: str) -> None:
    network = ipaddress.ip_network(subnet, strict=True)
    address = ipaddress.ip_address(peer)
    private_ranges = [ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    if (network.version != 4 or not any(network.subnet_of(block) for block in private_ranges)
            or not 24 <= network.prefixlen <= 29
            or address not in network or address in {network.network_address, network.broadcast_address}
            or address == network.network_address + 1):
        raise ValueError("Use an RFC1918 IPv4 /24 through /29 network and a non-gateway proxy address in it")


def write_env(path: Path, values: dict[str, str]) -> None:
    if any("\n" in value or "\r" in value or "$" in value for value in values.values()):
        raise ValueError("Generated configuration cannot contain multiline or interpolated values")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")


def check(directory: Path) -> dict:
    if directory.is_symlink() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise ValueError("The configuration directory must have mode 0700 and not be a symlink")
    if directory.resolve().is_relative_to(ROOT):
        raise ValueError("Production credentials must live outside the repository")
    for name in FILES:
        private_file(directory / name)
    compose = read_env(directory / "compose.env")
    host = compose["DATAFLOW_PUBLIC_HOST"]
    validate_hostname(host, compose.get("DATAFLOW_TEST_HOSTNAME") == "true")
    validate_network(compose["DATAFLOW_PROXY_SUBNET"], compose["DATAFLOW_PROXY_IP"])
    validate_tls(Path(compose["DATAFLOW_TLS_CERT_FILE"]), Path(compose["DATAFLOW_TLS_KEY_FILE"]), host)
    if compose["DATAFLOW_CONFIG_DIR"] != str(directory.resolve()):
        raise ValueError("Configuration directory does not match its Compose configuration")
    db = read_env(directory / "db.env")
    migration = read_env(directory / "migration.env")
    runtime = read_env(directory / "runtime.env")
    recovery = read_env(directory / "recovery.env")
    redis = read_env(directory / "redis.env")
    expected = ((migration, "dataflow_owner"), (runtime, "dataflow_runtime"), (recovery, "dataflow_backup"))
    passwords = []
    for environment, role in expected:
        url = urlsplit(environment["DATABASE_URL"])
        if (url.scheme != "postgresql+psycopg2" or url.hostname != "db" or url.port != 5432
                or url.path != "/analytics" or url.username != role or not re.fullmatch(r"[a-f0-9]{64}", url.password or "")):
            raise ValueError("Database identities must use separate generated roles and private db endpoints")
        passwords.append(url.password)
    if len(set(passwords)) != 3 or passwords[0] != db["POSTGRES_PASSWORD"]:
        raise ValueError("Database passwords must be distinct and match the PostgreSQL initialization secret")
    if (migration["DATAFLOW_RUNTIME_PASSWORD"] != passwords[1]
            or migration["DATAFLOW_BACKUP_PASSWORD"] != passwords[2]):
        raise ValueError("Bootstrap credentials do not match runtime/recovery identities")
    if any(key in runtime for key in ("POSTGRES_PASSWORD", "DATAFLOW_RUNTIME_PASSWORD", "DATAFLOW_BACKUP_PASSWORD", "AWS_SECRET_ACCESS_KEY")):
        raise ValueError("Runtime configuration must not include administrative or backup credentials")
    if any(key in recovery for key in ("SECRET_KEY", "SMTP_PASSWORD", "OPERATIONS_METRICS_TOKEN")):
        raise ValueError("Recovery configuration must not include application secrets")
    if (runtime["ENVIRONMENT"] != "production" or runtime["FORWARDED_ALLOW_IPS"] != compose["DATAFLOW_PROXY_IP"]
            or json.loads(runtime["CORS_ORIGINS"]) != [f"https://{host}"]
            or runtime["PASSWORD_RESET_URL"] != f"https://{host}/"):
        raise ValueError("Runtime HTTPS origins, mode and exact trusted proxy must match the deployment")
    for key in ("SECRET_KEY", "OPERATIONS_METRICS_TOKEN"):
        if not re.fullmatch(r"[a-f0-9]{64}", runtime[key]):
            raise ValueError("Application secrets must contain independently generated 32-byte hex values")
    if not re.fullmatch(r"[a-f0-9]{64}", redis["REDIS_PASSWORD"]):
        raise ValueError("Redis requires a generated 32-byte hex password")
    if runtime["REDIS_URL"] != f"redis://:{redis['REDIS_PASSWORD']}@redis:6379/0":
        raise ValueError("Runtime Redis credential does not match the isolated broker")
    independent_secrets = passwords + [runtime["SECRET_KEY"], runtime["OPERATIONS_METRICS_TOKEN"], redis["REDIS_PASSWORD"]]
    if len(set(independent_secrets)) != len(independent_secrets):
        raise ValueError("Database, broker, signing and metrics credentials must be independent")
    backup = read_env(directory / "backup.env")
    if (backup.get("DATAFLOW_PROJECT_DIR") != str(ROOT)
            or backup.get("DATAFLOW_BACKUP_COMPOSE_FILE") != "docker-compose.production.yml"
            or backup.get("DATAFLOW_COMPOSE_ENV_FILE") != str(directory / "compose.env")
            or backup.get("DATAFLOW_BACKUP_DIR") != compose["DATAFLOW_BACKUP_DIR"]):
        raise ValueError("Backup maintenance must select this exact standalone deployment")
    return {"status": "configuration_verified", "hostname": host,
            "independent_backup_configured": bool(backup.get("DATAFLOW_BACKUP_S3_BUCKET")),
            "smtp_configured": bool(runtime.get("SMTP_HOST")),
            "external_services_validated": False}


def create(args) -> dict:
    directory = args.directory.absolute()
    if directory.exists() or directory.is_symlink():
        raise ValueError("Use a new configuration directory; existing secrets are never replaced")
    if directory.resolve().is_relative_to(ROOT):
        raise ValueError("Production credentials must live outside the repository")
    validate_hostname(args.hostname, args.allow_test_hostname)
    validate_network(args.proxy_subnet, args.proxy_ip)
    validate_tls(args.tls_cert, args.tls_key, args.hostname)
    directory.mkdir(mode=0o700, parents=True)
    backups = directory / "backups"
    backups.mkdir(mode=0o700)
    owner, runtime, backup, broker = [secrets.token_hex(32) for _ in range(4)]
    def database_url(role, password):
        return f"postgresql+psycopg2://{role}:{password}@db:5432/analytics"
    write_env(directory / "compose.env", {
        "DATAFLOW_CONFIG_DIR": str(directory), "DATAFLOW_PUBLIC_HOST": args.hostname,
        "DATAFLOW_TLS_CERT_FILE": str(args.tls_cert), "DATAFLOW_TLS_KEY_FILE": str(args.tls_key),
        "DATAFLOW_PROXY_SUBNET": args.proxy_subnet, "DATAFLOW_PROXY_IP": args.proxy_ip,
        "DATAFLOW_COMPOSE_PROJECT_NAME": "dataflow-production", "DATAFLOW_BACKUP_DIR": str(backups),
        "DATAFLOW_TEST_HOSTNAME": str(args.allow_test_hostname).lower(),
    })
    write_env(directory / "db.env", {"POSTGRES_DB": "analytics", "POSTGRES_USER": "dataflow_owner", "POSTGRES_PASSWORD": owner})
    write_env(directory / "migration.env", {
        "DATABASE_URL": database_url("dataflow_owner", owner), "ENVIRONMENT": "production", "SECRET_KEY": secrets.token_hex(32),
        "DATAFLOW_RUNTIME_PASSWORD": runtime, "DATAFLOW_BACKUP_PASSWORD": backup,
    })
    write_env(directory / "runtime.env", {
        "DATABASE_URL": database_url("dataflow_runtime", runtime), "ENVIRONMENT": "production",
        "SECRET_KEY": secrets.token_hex(32), "OPERATIONS_METRICS_TOKEN": secrets.token_hex(32),
        "REDIS_URL": f"redis://:{broker}@redis:6379/0", "FORWARDED_ALLOW_IPS": args.proxy_ip,
        "CORS_ORIGINS": json.dumps([f"https://{args.hostname}"], separators=(",", ":")),
        "PASSWORD_RESET_URL": f"https://{args.hostname}/", "SMTP_HOST": "", "SMTP_TLS_MODE": "starttls",
        "UPLOAD_DIR": "/app/data/uploads", "REPORT_DIR": "/app/data/reports",
        "DATABASE_POOL_SIZE": "3", "DATABASE_MAX_OVERFLOW": "2", "CELERY_WORKER_CONCURRENCY": "1",
        "MAX_CONCURRENT_UPLOAD_REQUESTS": "1", "DATASET_PREVIEW_MAX_CONCURRENT": "1",
        "DASHBOARD_QUERY_MAX_CONCURRENT": "1", "TRANSFORMATION_ENGINE_THREADS": "1",
    })
    write_env(directory / "recovery.env", {"DATABASE_URL": database_url("dataflow_backup", backup)})
    write_env(directory / "redis.env", {"REDIS_PASSWORD": broker})
    write_env(directory / "backup.env", {
        "DATAFLOW_PROJECT_DIR": str(ROOT), "DATAFLOW_BACKUP_DIR": str(backups),
        "DATAFLOW_BACKUP_ENV_FILE": str(directory / "backup.env"),
        "DATAFLOW_BACKUP_LOCK_FILE": "/run/lock/dataflow-production-backup.lock",
        "DATAFLOW_BACKUP_COMPOSE_FILE": "docker-compose.production.yml",
        "DATAFLOW_COMPOSE_ENV_FILE": str(directory / "compose.env"),
        "DATAFLOW_BACKUP_REQUIRE_REMOTE": "true", "DATAFLOW_BACKUP_S3_BUCKET": "",
    })
    return check(directory)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", type=Path, help="Validate an existing private configuration directory")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--hostname")
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--proxy-subnet", default="172.30.83.0/24")
    parser.add_argument("--proxy-ip", default="172.30.83.2")
    parser.add_argument("--allow-test-hostname", action="store_true", help="Allow .test only for isolated local/CI TLS checks")
    args = parser.parse_args(argv)
    if not args.check and not all((args.directory, args.hostname, args.tls_cert, args.tls_key)):
        parser.error("Creation requires directory, hostname, TLS certificate and private key")
    try:
        report = check(args.check.absolute()) if args.check else create(args)
    except (OSError, ValueError, KeyError, ssl.SSLError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "configuration_rejected", "error": str(exc) if isinstance(exc, ValueError) else "Check configuration paths, permissions and TLS inputs"}))
        return 1
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
