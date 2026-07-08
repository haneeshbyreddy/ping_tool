from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

CA_KEY_NAME = "ca.key"
CA_CERT_NAME = "ca.crt"
CA_DAYS = 3650
CERT_DAYS = 825

class PkiError(RuntimeError):
    pass

def _openssl(args: list[str]) -> None:
    if shutil.which("openssl") is None:
        raise PkiError(
            "openssl not found on PATH — required to manage the mTLS CA "
            "(init-ca/enroll-edge); install it, or supply a pre-issued CA/cert pair "
            "via WISP_CENTRAL_TLS_CERT/_KEY/_CLIENT_CA directly"
        )
    proc = subprocess.run(["openssl", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise PkiError(f"openssl {' '.join(args)} failed: {proc.stderr.strip()}")

def ensure_ca(pki_dir: Path) -> tuple[Path, Path]:
    pki_dir.mkdir(parents=True, exist_ok=True)
    ca_key, ca_cert = pki_dir / CA_KEY_NAME, pki_dir / CA_CERT_NAME
    if ca_key.exists() and ca_cert.exists():
        return ca_key, ca_cert
    _openssl([
        "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(ca_key), "-out", str(ca_cert),
        "-days", str(CA_DAYS), "-subj", "/CN=wisp-central-ca",
    ])
    ca_key.chmod(0o600)
    return ca_key, ca_cert

def issue_cert(pki_dir: Path, common_name: str, out_key: Path, out_cert: Path, *,
              days: int = CERT_DAYS, san: list[str] | None = None) -> None:
    ca_key, ca_cert = ensure_ca(pki_dir)
    out_key.parent.mkdir(parents=True, exist_ok=True)
    out_cert.parent.mkdir(parents=True, exist_ok=True)
    csr = out_key.with_suffix(".csr")
    req_args = [
        "req", "-new", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(out_key), "-out", str(csr), "-subj", f"/CN={common_name}",
    ]
    x509_args = [
        "x509", "-req", "-in", str(csr), "-CA", str(ca_cert), "-CAkey", str(ca_key),
        "-CAcreateserial", "-out", str(out_cert), "-days", str(days),
    ]
    if san:
        req_args += ["-addext", f"subjectAltName={','.join(san)}"]
        x509_args += ["-copy_extensions", "copy"]
    try:
        _openssl(req_args)
        _openssl(x509_args)
    finally:
        csr.unlink(missing_ok=True)
    out_key.chmod(0o600)

def edge_common_name(org_id: str, node_id: str) -> str:
    return f"{org_id}:{node_id}"

def peer_identity(peer_cert: dict | None) -> tuple[str, str] | None:
    if not peer_cert:
        return None
    cn = None
    for rdn in peer_cert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                cn = value
    if not cn or ":" not in cn:
        return None
    org, _, node = cn.partition(":")
    return (org, node) if org and node else None
