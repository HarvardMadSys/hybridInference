"""SMTP delivery checks the server's certificate before it sends the password.

``starttls()`` without a context accepts any certificate for any name, so a
man in the middle got the SMTP password and every reset link. These run
``send_email`` against a real SMTP conversation over a socket, with real TLS:
a certificate this host does not trust, or one for another name, ends the
connection before ``AUTH``; a trusted one (here through ``SSL_CERT_FILE``, the
documented remedy for a private authority) still delivers. Port 465 speaks TLS
from the first byte.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import socket
import ssl
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from serving.utils import email as email_mod

if TYPE_CHECKING:
    from pathlib import Path


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


@dataclass
class _Pki:
    ca_file: Path
    cert_file: Path
    key_file: Path


def _issue(tmp_path: Path, *, dns_names: tuple[str, ...], ip: str | None = None) -> _Pki:
    """A private authority, and a server certificate it signed for the given names."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("Test Mail CA"))
        .issuer_name(_name("Test Mail CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    alt_names: list[x509.GeneralName] = [x509.DNSName(name) for name in dns_names]
    if ip is not None:
        alt_names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(dns_names[0]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pki = _Pki(tmp_path / "ca.pem", tmp_path / "server.pem", tmp_path / "server.key")
    pki.ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    pki.cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    pki.key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return pki


@dataclass
class _SmtpServer:
    """One SMTP conversation on 127.0.0.1: STARTTLS, or TLS from the start."""

    pki: _Pki
    implicit_tls: bool = False
    commands: list[str] = field(default_factory=list)
    messages: list[bytes] = field(default_factory=list)
    handshake_completed: bool = False

    def __post_init__(self) -> None:
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(self.pki.cert_file, self.pki.key_file)
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(10)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def join(self) -> None:
        self._thread.join(timeout=10)
        self._listener.close()

    def _handshake(self, conn: socket.socket) -> socket.socket | None:
        try:
            tls = self._context.wrap_socket(conn, server_side=True)
        except (ssl.SSLError, OSError):
            conn.close()
            return None
        self.handshake_completed = True
        return tls

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        conn.settimeout(10)
        tls = False
        if self.implicit_tls:
            wrapped = self._handshake(conn)
            if wrapped is None:
                return
            conn, tls = wrapped, True
        with conn:
            reader = conn.makefile("rb")
            conn.sendall(b"220 smtp.test ESMTP\r\n")
            while line := reader.readline():
                command = line.decode().rstrip("\r\n")
                self.commands.append(command.split(" ", 1)[0].upper())
                verb = self.commands[-1]
                if verb in {"EHLO", "HELO"}:
                    extension = "AUTH PLAIN" if tls else "STARTTLS"
                    conn.sendall(f"250-smtp.test\r\n250 {extension}\r\n".encode())
                elif verb == "STARTTLS":
                    conn.sendall(b"220 go ahead\r\n")
                    wrapped = self._handshake(conn)
                    if wrapped is None:
                        return
                    conn, tls = wrapped, True
                    reader = conn.makefile("rb")
                elif verb == "AUTH":
                    conn.sendall(b"235 accepted\r\n")
                elif verb == "DATA":
                    conn.sendall(b"354 go ahead\r\n")
                    body = bytearray()
                    while (chunk := reader.readline()) not in {b".\r\n", b""}:
                        body += chunk
                    self.messages.append(bytes(body))
                    conn.sendall(b"250 queued\r\n")
                elif verb == "QUIT":
                    conn.sendall(b"221 bye\r\n")
                    return
                else:
                    conn.sendall(b"250 ok\r\n")


@pytest.fixture
def smtp_settings(monkeypatch):
    def configure(port: int) -> None:
        for name, value in {
            "email_provider": "smtp",
            "smtp_host": "localhost",
            "smtp_port": port,
            "smtp_user": "mailer",
            "smtp_password": "the-smtp-password",
            "smtp_from_email": "noreply@acme.test",
            "smtp_from_name": "Acme",
        }.items():
            monkeypatch.setattr(email_mod.settings, name, value, raising=False)

    # The system's authorities only, whatever the environment running the tests set.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    return configure


def _send() -> bool:
    return email_mod.send_email("owner@example.com", "Reset", "<p>link</p>", "link")


def test_an_untrusted_certificate_ends_the_connection_before_the_password(
    tmp_path, smtp_settings, caplog
):
    server = _SmtpServer(_issue(tmp_path, dns_names=("localhost",), ip="127.0.0.1"))
    smtp_settings(server.port)

    with caplog.at_level(logging.ERROR, logger=email_mod.__name__):
        assert _send() is False
    server.join()

    assert not server.handshake_completed
    assert "AUTH" not in server.commands
    assert server.messages == []
    # Loud, and with the way out: a self-signed server used to work.
    [record] = [r for r in caplog.records if r.name == email_mod.__name__]
    assert "did not present a certificate this host trusts" in record.getMessage()
    assert "SSL_CERT_FILE" in record.getMessage()


def test_a_trusted_certificate_still_delivers(tmp_path, smtp_settings, monkeypatch):
    pki = _issue(tmp_path, dns_names=("localhost",), ip="127.0.0.1")
    server = _SmtpServer(pki)
    smtp_settings(server.port)
    monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_file))

    assert _send() is True
    server.join()

    assert server.handshake_completed
    assert server.commands[:3] == ["EHLO", "STARTTLS", "EHLO"]
    assert "AUTH" in server.commands
    [message] = server.messages
    assert b"Subject: Reset" in message


def test_a_certificate_for_another_name_is_refused(tmp_path, smtp_settings, monkeypatch):
    pki = _issue(tmp_path, dns_names=("mail.example.test",))
    server = _SmtpServer(pki)
    smtp_settings(server.port)
    monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_file))  # trusted, but not for "localhost"

    assert _send() is False
    server.join()

    assert "AUTH" not in server.commands
    assert server.messages == []


@pytest.mark.parametrize("trusted", [True, False])
def test_port_465_speaks_tls_from_the_first_byte(tmp_path, smtp_settings, monkeypatch, trusted):
    pki = _issue(tmp_path, dns_names=("localhost",), ip="127.0.0.1")
    server = _SmtpServer(pki, implicit_tls=True)
    smtp_settings(server.port)
    # The listener cannot take 465 itself; the implicit-TLS port is what is tested.
    monkeypatch.setattr(email_mod, "SMTP_IMPLICIT_TLS_PORT", server.port)
    if trusted:
        monkeypatch.setenv("SSL_CERT_FILE", str(pki.ca_file))

    assert _send() is trusted
    server.join()

    assert "STARTTLS" not in server.commands
    assert ("AUTH" in server.commands) is trusted
    assert len(server.messages) == int(trusted)
