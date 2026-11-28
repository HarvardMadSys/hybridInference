"""Email sending utilities for user verification and password reset."""

import html as html_lib
import smtplib
import socket
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote, urlparse

from markdown_it import MarkdownIt

from serving.config.settings import settings
from serving.config.site_identity import get_site_identity
from serving.utils import cloudflare_email
from serving.utils.delivery import EmailDelivery, EmailMessage, get_email_provider
from serving.utils.logging import get_logger

logger = get_logger(__name__)

#: The submission port that speaks TLS from the first byte (RFC 8314). Every
#: other port is plain SMTP upgraded with STARTTLS, which a server that does not
#: offer it refuses, as it always did.
SMTP_IMPLICIT_TLS_PORT = 465


def _smtp_tls_context() -> ssl.SSLContext:
    """Return the TLS settings for the SMTP connection: the server must prove its name.

    ``starttls()`` without a context checks neither the certificate nor the
    host name, so anyone on the path could present their own certificate and
    read the SMTP password and every reset link. The default context checks
    both against the system's trusted authorities, or against the bundle the
    standard ``SSL_CERT_FILE`` variable names (how a server with a private
    authority's certificate is trusted).
    """
    return ssl.create_default_context()


def get_smtp_config() -> dict[str, str | int]:
    """Get SMTP configuration from settings.

    Returns:
        Dictionary with SMTP configuration.
    """
    return {
        "host": settings.smtp_host,
        "port": settings.smtp_port,
        "user": settings.smtp_user,
        "password": settings.smtp_password,
        "from_email": settings.smtp_from_email,
        "from_name": settings.smtp_from_name,
    }


def is_email_enabled() -> bool:
    """Check whether the selected email delivery provider is configured.

    Returns:
        True if the selected provider is configured, False otherwise.
    """
    if settings.email_provider == "smtp":
        return bool(settings.smtp_host and settings.smtp_user and settings.smtp_password)
    if settings.email_provider == "cloudflare":
        return cloudflare_email.is_configured()
    provider = get_email_provider(settings.email_provider)
    return provider is not None and provider.is_configured()


#: What each built-in transport needs before it sends: (Settings field, setting name).
_TRANSPORT_SETTINGS: dict[str, tuple[tuple[str, str], ...]] = {
    "smtp": (
        ("smtp_host", "SMTP_HOST"),
        ("smtp_user", "SMTP_USER"),
        ("smtp_password", "SMTP_PASSWORD"),
    ),
    "cloudflare": (
        ("cloudflare_account_id", "CLOUDFLARE_ACCOUNT_ID"),
        ("cloudflare_email_api_token", "CLOUDFLARE_EMAIL_API_TOKEN"),
    ),
}

#: How the console names each built-in transport.
TRANSPORT_LABELS: dict[str, str] = {"smtp": "SMTP", "cloudflare": "Cloudflare Email Sending"}


def _joined(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def email_configuration_problem() -> str | None:
    """Say why the selected transport cannot send, or ``None`` when it can.

    Agrees with :func:`is_email_enabled`, and names the settings to fill in
    where the transport is a built-in one.
    """
    if is_email_enabled():
        return None
    name = settings.email_provider
    needed = _TRANSPORT_SETTINGS.get(name)
    if needed is not None:
        # SMTP's check is the one is_email_enabled makes; Cloudflare's strips.
        missing = [
            key
            for field, key in needed
            if not (
                getattr(settings, field).strip()
                if name == "cloudflare"
                else getattr(settings, field)
            )
        ]
        return f"{TRANSPORT_LABELS[name]} needs {_joined(missing) or 'its settings'}."
    if get_email_provider(name) is None:
        return f"The email provider {name!r} is not available: no loaded extension provides it."
    return f"The {name} email provider is not configured."


def send_email(
    to_email: str | None,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    *,
    template: str = "notice",
    template_data: dict[str, str] | None = None,
) -> bool:
    """Send an email using the configured delivery provider.

    Args:
        to_email: Recipient email address. None or blank (an account created
            without an email address) sends nothing.
        subject: Email subject.
        html_body: HTML email body.
        text_body: Plain text email body (optional, defaults to stripped HTML).
        template: Account event kind for template transports; ignored by SMTP
            and Cloudflare, which send the rendered bodies.
        template_data: Optional scalar account event variables for transports.

    Returns:
        True if email sent successfully, False otherwise.
        :func:`deliver_email` also says why it was not.
    """
    return deliver_email(
        to_email,
        subject,
        html_body,
        text_body,
        template=template,
        template_data=template_data,
    ).sent


def deliver_email(
    to_email: str | None,
    subject: str,
    html_body: str,
    text_body: str | None = None,
    *,
    template: str = "notice",
    template_data: dict[str, str] | None = None,
) -> EmailDelivery:
    """Send an email like :func:`send_email`, and say why it was not sent.

    Never falls back to another transport. The reason is for an
    administrator: it names settings, a host or a provider's error code, never
    a credential or the message.
    """
    if not to_email or not to_email.strip():
        logger.warning("Email not sent: the recipient has no email address")
        return EmailDelivery(False, "There is no address to send the message to.")

    problem = email_configuration_problem()
    if problem is not None:
        logger.warning("Email sending disabled: delivery provider not configured")
        return EmailDelivery(False, problem)

    message = EmailMessage(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        template=template,
        template_data=template_data,
    )
    name = settings.email_provider
    if name == "smtp":
        return _deliver_smtp(message)
    if name == "cloudflare":
        return cloudflare_email.deliver(message)
    # An extension's transport: the configuration check above found it registered.
    provider = get_email_provider(name)
    if provider is not None and provider.send(message):
        return EmailDelivery(True)
    return EmailDelivery(
        False,
        f"The {name} email provider did not send the message. The backend log says why.",
    )


#: What the SMTP conversation was doing, in the words a failure reports.
_SMTP_STAGES = {
    "connect": "while connecting",
    "starttls": "while starting TLS",
    "login": "while logging in",
    "send": "while sending the message",
}


def _deliver_smtp(message: EmailMessage) -> EmailDelivery:
    """Send *message* over SMTP with TLS that checks the server, and say what happened."""
    config = get_smtp_config()
    to_email = message.to_email
    port = int(config["port"])
    where = f"{config['host']}:{port}"
    stage = "connect"

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{config['from_name']} <{config['from_email']}>"
        msg["To"] = to_email
        msg["Subject"] = message.subject

        if message.text_body:
            msg.attach(MIMEText(message.text_body, "plain"))
        msg.attach(MIMEText(message.html_body, "html"))

        # Send email, over TLS that checks the server's certificate and name.
        context = _smtp_tls_context()
        if port == SMTP_IMPLICIT_TLS_PORT:
            connection = smtplib.SMTP_SSL(config["host"], port, timeout=10, context=context)
        else:
            connection = smtplib.SMTP(config["host"], port, timeout=10)
        with connection as server:
            if port != SMTP_IMPLICIT_TLS_PORT:
                stage = "starttls"
                try:
                    server.starttls(context=context)
                except ssl.SSLError:
                    # A failed handshake leaves no channel to say QUIT on:
                    # closed here, the failure reported is the handshake's.
                    server.close()
                    raise
            stage = "login"
            server.login(config["user"], config["password"])
            stage = "send"
            server.send_message(msg)

        logger.info(f"Email sent successfully to {to_email}")
        return EmailDelivery(True)

    except ssl.SSLCertVerificationError as e:
        # Said in full: until this check existed any certificate was accepted,
        # so a server with a self-signed one worked and now stops sending.
        logger.error(
            "Failed to send email to %s: the SMTP server %s:%s did not present a certificate "
            "this host trusts for its name (%s). Give it a certificate from a public "
            "authority, or point SSL_CERT_FILE at a bundle with the system's authorities "
            "and the one that signed it.",
            to_email,
            config["host"],
            config["port"],
            e.verify_message or e,
        )
        return EmailDelivery(
            False,
            f"The SMTP server {where} did not present a certificate this host trusts for its "
            f"name ({e.verify_message or 'verification failed'}). Give it a certificate from a "
            "public authority, or point SSL_CERT_FILE at a bundle with the system's "
            "authorities and the one that signed it.",
        )

    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return EmailDelivery(False, _smtp_failure(e, stage, where, str(config["from_email"])))


def _smtp_failure(error: Exception, stage: str, where: str, sender: str) -> str:
    """Say what an SMTP failure means and where to look, without the server's own text."""
    during = _SMTP_STAGES.get(stage, "")
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return (
            f"The SMTP server {where} rejected the username or password (SMTP {error.smtp_code})."
        )
    if isinstance(error, smtplib.SMTPNotSupportedError):
        if stage == "starttls":
            return (
                f"The SMTP server {where} does not offer STARTTLS. Use port 465 if it expects "
                "TLS from the first byte, or a port that offers STARTTLS, such as 587."
            )
        return f"The SMTP server {where} does not accept a login on this connection."
    if isinstance(error, smtplib.SMTPSenderRefused):
        return (
            f"The SMTP server {where} refused the sender address {sender} "
            f"(SMTP {error.smtp_code}). Use an address the server may send from."
        )
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return f"The SMTP server {where} refused the recipient address."
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return (
            f"The SMTP server {where} closed the connection {during}. If it expects TLS from "
            "the first byte, use port 465."
        )
    if isinstance(error, smtplib.SMTPResponseException):
        return f"The SMTP server {where} answered SMTP {error.smtp_code} {during}."
    if isinstance(error, ssl.SSLError):
        return (
            f"The TLS handshake with the SMTP server {where} failed. Port 465 expects TLS from "
            "the first byte and other ports use STARTTLS: check that the port matches the server."
        )
    if isinstance(error, socket.gaierror):
        return f"The SMTP host in {where} could not be found. Check the host name."
    if isinstance(error, TimeoutError):
        return (
            f"The SMTP server {where} did not answer within 10 seconds. Check the host and "
            "port, and that this host may connect to it."
        )
    if isinstance(error, ConnectionRefusedError):
        return f"The SMTP server {where} refused the connection. Check the host and port."
    if isinstance(error, OSError):
        return f"Could not reach the SMTP server {where} ({type(error).__name__})."
    return f"The message could not be sent through SMTP {during} ({type(error).__name__})."


def render_test_email(transport: str) -> tuple[str, str, str]:
    """Return the subject, HTML and text of the admin console's test email.

    Fixed apart from the site's name and the transport: a test can prove the
    settings send, and cannot carry anything else.
    """
    site_name = get_site_identity().name
    label = TRANSPORT_LABELS.get(transport, transport)
    subject = f"Test email from {site_name}"
    lines = (
        f"This is a test message from {site_name}, sent from the admin console's email settings.",
        f"It was sent through {label}. Sign-up verification, password reset and "
        "notification emails go out the same way.",
        "No action is needed.",
    )
    paragraphs = "".join(f"<p>{html_lib.escape(line)}</p>" for line in lines)
    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #111827;">Email delivery works</h2>
            {paragraphs}
        </div>
    </body>
    </html>
    """
    text_body = "Email delivery works\n\n" + "\n\n".join(lines) + "\n"
    return subject, html_body, text_body


def send_verification_email(to_email: str, verification_token: str, base_url: str) -> bool:
    """Send email verification link to user.

    Args:
        to_email: Recipient email address.
        verification_token: Verification token.
        base_url: Backend base URL or request origin. Used for basic validation and logging.

    Behavior:
        - The verification link embedded in the email targets the frontend URL configured
          in settings.frontend_url, for example:
          {frontend}/verify-email?token=...

    Returns:
        True if email sent successfully, False otherwise.

    Security:
        - base_url is validated and warns on insecure http in non-local environments
        - Token is URL-safe and cryptographically random
        - Link expires after 24 hours
    """
    # Validate base_url format
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        logger.error(f"Invalid base_url format: {base_url}")
        return False

    # Warn if using http in production (should use https)
    if (
        parsed.scheme == "http"
        and "localhost" not in parsed.netloc
        and "127.0.0.1" not in parsed.netloc
    ):
        logger.warning(f"Using insecure http protocol for verification email: {base_url}")

    # Link to frontend page (not backend API)
    # Frontend will call backend API to verify the token
    verification_url = f"{settings.frontend_url}/verify-email?token={verification_token}"

    site_name = get_site_identity().name
    subject = f"Verify your {site_name} account"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #2563eb;">Welcome to {site_name}!</h2>
            <p>Thank you for signing up. Please verify your email address by clicking the link below:</p>
            <p style="margin: 30px 0;">
                <a href="{verification_url}"
                   style="background-color: #2563eb; color: white; padding: 12px 24px;
                          text-decoration: none; border-radius: 4px; display: inline-block;">
                    Verify Email Address
                </a>
            </p>
            <p style="color: #666; font-size: 14px;">
                Or copy and paste this link into your browser:<br>
                <a href="{verification_url}">{verification_url}</a>
            </p>
            <p style="color: #666; font-size: 14px;">
                This link will expire in 24 hours.
            </p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #999; font-size: 12px;">
                If you didn't create an account, you can safely ignore this email.
            </p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
Welcome to {site_name}!

Thank you for signing up. Please verify your email address by visiting:

{verification_url}

This link will expire in 24 hours.

If you didn't create an account, you can safely ignore this email.
    """

    return send_email(
        to_email,
        subject,
        html_body,
        text_body,
        template="verification",
        template_data={"site_name": site_name, "token": quote(verification_token, safe="")},
    )


def send_password_reset_email(to_email: str, reset_token: str, base_url: str) -> bool:
    """Send password reset link to user.

    Args:
        to_email: Recipient email address.
        reset_token: Password reset token.
        base_url: Backend base URL or request origin. Used for basic validation and logging.

    Behavior:
        - The reset link embedded in the email targets the frontend URL configured
          in settings.frontend_url, for example:
          {frontend}/reset-password?token=...

    Returns:
        True if email sent successfully, False otherwise.

    Security:
        - base_url is validated and warns on insecure http in non-local environments
        - Token is URL-safe and cryptographically random
        - Link expires after 1 hour
    """
    # Validate base_url format
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.netloc:
        logger.error(f"Invalid base_url format: {base_url}")
        return False

    # Warn if using http in production
    if (
        parsed.scheme == "http"
        and "localhost" not in parsed.netloc
        and "127.0.0.1" not in parsed.netloc
    ):
        logger.warning(f"Using insecure http protocol for password reset email: {base_url}")

    # Link to frontend page (not backend API)
    reset_url = f"{settings.frontend_url}/reset-password?token={reset_token}"

    site_name = get_site_identity().name
    subject = f"Reset your {site_name} password"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #2563eb;">Password Reset Request</h2>
            <p>We received a request to reset your password. Click the link below to set a new password:</p>
            <p style="margin: 30px 0;">
                <a href="{reset_url}"
                   style="background-color: #2563eb; color: white; padding: 12px 24px;
                          text-decoration: none; border-radius: 4px; display: inline-block;">
                    Reset Password
                </a>
            </p>
            <p style="color: #666; font-size: 14px;">
                Or copy and paste this link into your browser:<br>
                <a href="{reset_url}">{reset_url}</a>
            </p>
            <p style="color: #666; font-size: 14px;">
                This link will expire in 1 hour.
            </p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #999; font-size: 12px;">
                If you didn't request a password reset, you can safely ignore this email.
            </p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
Password Reset Request

We received a request to reset your password. Visit this link to set a new password:

{reset_url}

This link will expire in 1 hour.

If you didn't request a password reset, you can safely ignore this email.
    """

    return send_email(
        to_email,
        subject,
        html_body,
        text_body,
        template="password_reset",
        template_data={"site_name": site_name, "token": quote(reset_token, safe="")},
    )


def send_approval_email(to_email: str) -> bool:
    """Notify user that their registration has been approved.

    Args:
        to_email: User's email address.

    Returns:
        True if email sent successfully, False otherwise.
    """
    login_url = f"{settings.frontend_url}/login"

    site_name = get_site_identity().name
    subject = f"Your {site_name} account has been approved"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #10b981;">Account Approved</h2>
            <p>Your {site_name} account has been approved by an administrator.</p>
            <p>You can now log in and start using the API.</p>
            <p style="margin: 30px 0;">
                <a href="{login_url}"
                   style="background-color: #2563eb; color: white; padding: 12px 24px;
                          text-decoration: none; border-radius: 4px; display: inline-block;">
                    Log In Now
                </a>
            </p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #999; font-size: 12px;">
                If you did not register for {site_name}, please ignore this email.
            </p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
Account Approved

Your {site_name} account has been approved by an administrator.

You can now log in and start using the API:
{login_url}

If you did not register for {site_name}, please ignore this email.
    """

    return send_email(
        to_email,
        subject,
        html_body,
        text_body,
        template="approval",
        template_data={"site_name": site_name},
    )


def send_rejection_email(to_email: str, reason: str) -> bool:
    """Notify user that their registration has been rejected.

    Args:
        to_email: User's email address.
        reason: Rejection reason provided by admin.

    Returns:
        True if email sent successfully, False otherwise.
    """
    site_name = get_site_identity().name
    subject = f"Your {site_name} registration update"
    # Free text an admin typed, shown as text: a reason that mentions an
    # address in angle brackets must not vanish into an unknown tag. The
    # template transport escapes its own variables, so it gets the raw reason.
    safe_reason = html_lib.escape(reason)

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #ef4444;">Registration Not Approved</h2>
            <p>Unfortunately, your {site_name} registration was not approved at this time.</p>
            <div style="background: #fef2f2; border-left: 4px solid #ef4444; padding: 12px 16px;
                        margin: 20px 0; border-radius: 4px;">
                <strong>Reason:</strong> {safe_reason}
            </div>
            <p>If you believe this was a mistake, please contact the administrator.</p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #999; font-size: 12px;">
                If you did not register for {site_name}, please ignore this email.
            </p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
Registration Not Approved

Unfortunately, your {site_name} registration was not approved at this time.

Reason: {reason}

If you believe this was a mistake, please contact the administrator.
    """

    return send_email(
        to_email,
        subject,
        html_body,
        text_body,
        template="rejection",
        template_data={"site_name": site_name, "reason": reason},
    )


def send_account_deleted_email(to_email: str) -> bool:
    """Tell an account's owner that the account was deleted from its settings.

    Sent after the deletion, so someone who did not ask for it hears about it.

    Args:
        to_email: The deleted account's email address.

    Returns:
        True if email sent successfully, False otherwise.
    """
    identity = get_site_identity()
    site_name = identity.name
    subject = f"Your {site_name} account has been deleted"
    contact = (
        f"contact {identity.support_email}"
        if identity.support_email
        else "contact the administrator"
    )
    safe_email = html_lib.escape(to_email)
    safe_contact = html_lib.escape(contact)

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #ef4444;">Account Deleted</h2>
            <p>The {site_name} account {safe_email} was deleted from its account settings.
               Its API keys were revoked and it was signed out everywhere.</p>
            <p>If you did not do this, please {safe_contact} right away.</p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
Account Deleted

The {site_name} account {to_email} was deleted from its account settings.
Its API keys were revoked and it was signed out everywhere.

If you did not do this, please {contact} right away.
    """

    return send_email(to_email, subject, html_body, text_body)


def send_email_changed_email(old_email: str, new_email: str) -> bool:
    """Tell an account's previous address that its sign-in email was replaced.

    Sent to the address that lost the account, so an owner whose session was
    used to change it hears about it.

    Args:
        old_email: The address the account used to have.
        new_email: The address it has now.

    Returns:
        True if email sent successfully, False otherwise.
    """
    identity = get_site_identity()
    site_name = identity.name
    subject = f"Your {site_name} sign-in email was changed"
    contact = (
        f"contact {identity.support_email}"
        if identity.support_email
        else "contact the administrator"
    )
    safe_old = html_lib.escape(old_email)
    safe_new = html_lib.escape(new_email)
    safe_contact = html_lib.escape(contact)

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #ef4444;">Sign-in Email Changed</h2>
            <p>The {site_name} account that used {safe_old} now signs in with {safe_new}.
               This address will no longer receive its password resets.</p>
            <p>If you did not do this, please {safe_contact} right away.</p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
Sign-in Email Changed

The {site_name} account that used {old_email} now signs in with {new_email}.
This address will no longer receive its password resets.

If you did not do this, please {contact} right away.
    """

    return send_email(old_email, subject, html_body, text_body)


def send_password_changed_email(to_email: str, *, first_password: bool = False) -> bool:
    """Tell an account's owner that its password was changed, or first set.

    Sent after a change made from a signed-in session, so an owner whose
    session was used to do it hears about it.

    Args:
        to_email: The account's verified email address.
        first_password: Whether the account had no password before.

    Returns:
        True if email sent successfully, False otherwise.
    """
    identity = get_site_identity()
    site_name = identity.name
    verb = "set" if first_password else "changed"
    subject = f"Your {site_name} password was {verb}"
    contact = (
        f"contact {identity.support_email}"
        if identity.support_email
        else "contact the administrator"
    )
    reset_url = f"{settings.frontend_url}/forgot-password"
    safe_contact = html_lib.escape(contact)
    safe_reset_url = html_lib.escape(reset_url)

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #ef4444;">Password {verb.capitalize()}</h2>
            <p>The password of your {site_name} account was {verb}.
               Every other device signed in to it has been signed out.</p>
            <p>If you did not do this, <a href="{safe_reset_url}">reset your password</a>
               and {safe_contact} right away.</p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
Password {verb.capitalize()}

The password of your {site_name} account was {verb}.
Every other device signed in to it has been signed out.

If you did not do this, reset your password at {reset_url}
and {contact} right away.
    """

    return send_email(to_email, subject, html_body, text_body)


def send_new_registration_admin_email(
    to_email: str,
    user_email: str,
    user_name: str | None,
    user_id: str,
    use_case: str | None = None,
) -> bool:
    """Notify admin of a new user registration pending approval.

    Args:
        to_email: Admin email address.
        user_email: New user's email.
        user_name: New user's display name (if provided).
        user_id: New user's ID.
        use_case: Free-text use case the user supplied at signup.

    Returns:
        True if email sent successfully, False otherwise.
    """
    admin_url = f"{settings.frontend_url}/dashboard/admin"
    display_name = user_name or "(not provided)"
    use_case_text = (use_case or "").strip() or "(not provided)"
    # Every field below comes from an anonymous signup form, and this mail goes
    # out from the deployment's own domain to its admins: escaped, a display
    # name like ``<a href=...>`` reads as text instead of planting a link or a
    # tracking pixel. The use case also keeps its line breaks.
    safe_email = html_lib.escape(user_email)
    safe_name = html_lib.escape(display_name)
    safe_user_id = html_lib.escape(user_id)
    use_case_html = html_lib.escape(use_case_text).replace("\r\n", "\n").replace("\n", "<br>")

    site_name = get_site_identity().name
    subject = f"[{site_name}] New registration pending approval: {user_email}"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #f59e0b;">New Registration Pending Approval</h2>
            <p>A new user has registered and is waiting for approval:</p>
            <table style="border-collapse: collapse; margin: 20px 0;">
                <tr>
                    <td style="padding: 6px 16px 6px 0; font-weight: bold;">Email:</td>
                    <td style="padding: 6px 0;">{safe_email}</td>
                </tr>
                <tr>
                    <td style="padding: 6px 16px 6px 0; font-weight: bold;">Name:</td>
                    <td style="padding: 6px 0;">{safe_name}</td>
                </tr>
                <tr>
                    <td style="padding: 6px 16px 6px 0; font-weight: bold;">User ID:</td>
                    <td style="padding: 6px 0; font-family: monospace; font-size: 13px;">{safe_user_id}</td>
                </tr>
                <tr>
                    <td style="padding: 6px 16px 6px 0; font-weight: bold; vertical-align: top;">Use case:</td>
                    <td style="padding: 6px 0;">{use_case_html}</td>
                </tr>
            </table>
            <p style="margin: 30px 0;">
                <a href="{admin_url}"
                   style="background-color: #f59e0b; color: #1f2937; padding: 12px 24px;
                          text-decoration: none; border-radius: 4px; display: inline-block;
                          font-weight: bold;">
                    Review in Admin Panel
                </a>
            </p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
New Registration Pending Approval

A new user has registered and is waiting for approval:

Email: {user_email}
Name: {display_name}
User ID: {user_id}
Use case: {use_case_text}

Review at: {admin_url}
    """

    return send_email(
        to_email,
        subject,
        html_body,
        text_body,
        template="admin_signup",
        template_data={"site_name": site_name, "identifier": user_email},
    )


# ── Broadcast email templates ──────────────────────────────────────────────

# Shared markdown renderer for custom broadcast bodies. CommonMark base plus a
# few GFM-ish niceties (autolinks, soft-break-as-<br>, tables, strikethrough).
_md = (
    MarkdownIt("commonmark", {"linkify": True, "breaks": True})
    .enable("table")
    .enable("strikethrough")
)


def render_markdown_email(markdown_src: str) -> tuple[str, str]:
    """Render markdown into a wrapped HTML email body plus a plaintext version.

    Returns:
        Tuple of ``(wrapped_html, plain_text)``. The HTML fragment produced by
        the markdown renderer is wrapped in the same email shell + footer used
        by EMAIL_TEMPLATES; the plaintext is just the (stripped) markdown source,
        which is already human-readable.
    """
    fragment = _md.render(markdown_src)
    site_name = get_site_identity().name
    wrapped_html = (
        '<html><body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">'
        f'<div style="max-width: 600px; margin: 0 auto; padding: 20px;">{fragment}'
        '<hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">'
        '<p style="color: #999; font-size: 12px;">You received this because you have an '
        f"active {site_name} account.</p>"
        "</div></body></html>"
    )
    return wrapped_html, markdown_src.strip()


class _SafeDict(dict):
    """dict subclass that returns '{key}' for missing keys instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


EMAIL_TEMPLATES: dict[str, dict[str, str]] = {
    "maintenance": {
        "subject": "Scheduled maintenance on {date}",
        "body_html": """
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #f59e0b;">Scheduled Maintenance</h2>
                <p>We are planning scheduled maintenance on <strong>{date}</strong> lasting approximately <strong>{duration}</strong>.</p>
                <p>During this time the service will be unavailable. We apologize for any inconvenience.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
                <p style="color: #999; font-size: 12px;">You received this because you have an active {site_name} account.</p>
            </div>
        </body>
        </html>
        """,
        "body_text": "Scheduled Maintenance\n\nWe are planning scheduled maintenance on {date} lasting approximately {duration}.\n\nDuring this time the service will be unavailable. We apologize for any inconvenience.",
    },
    "announcement": {
        "subject": "Announcing {feature_name}",
        "body_html": """
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #2563eb;">New: {feature_name}</h2>
                <p>{description}</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
                <p style="color: #999; font-size: 12px;">You received this because you have an active {site_name} account.</p>
            </div>
        </body>
        </html>
        """,
        "body_text": "New: {feature_name}\n\n{description}",
    },
    "quota_change": {
        "subject": "Your {site_name} quota has been updated",
        "body_html": """
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #10b981;">Quota Updated</h2>
                <p>Your daily usage quota has been updated to <strong>{new_quota}</strong>.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
                <p style="color: #999; font-size: 12px;">You received this because you have an active {site_name} account.</p>
            </div>
        </body>
        </html>
        """,
        "body_text": "Quota Updated\n\nYour daily usage quota has been updated to {new_quota}.",
    },
}


def render_broadcast_template(
    template_key: str | None,
    template_vars: dict,
    *,
    custom_subject: str = "",
    custom_body_html: str = "",
    custom_body_text: str = "",
    custom_body_markdown: str = "",
) -> dict[str, str]:
    """Render a broadcast email from a template key or custom content.

    For custom content (``template_key is None``), markdown takes precedence:
    when ``custom_body_markdown`` is non-empty it is rendered to HTML (and its
    stripped source used as the plaintext), overriding ``custom_body_html`` /
    ``custom_body_text``. Otherwise the custom HTML/text are passed through.

    Raises:
        ValueError: If template_key is provided but not in EMAIL_TEMPLATES.
    """
    if template_key is None:
        if custom_body_markdown.strip():
            body_html, body_text = render_markdown_email(custom_body_markdown)
            return {
                "subject": custom_subject,
                "body_html": body_html,
                "body_text": body_text,
            }
        return {
            "subject": custom_subject,
            "body_html": custom_body_html,
            "body_text": custom_body_text,
        }

    if template_key not in EMAIL_TEMPLATES:
        raise ValueError(f"Unknown template: {template_key!r}. Available: {list(EMAIL_TEMPLATES)}")

    tmpl = EMAIL_TEMPLATES[template_key]
    # site_name last so template callers cannot spoof the operator identity.
    safe = _SafeDict({**template_vars, "site_name": get_site_identity().name})
    return {
        "subject": tmpl["subject"].format_map(safe),
        "body_html": tmpl["body_html"].format_map(safe),
        "body_text": tmpl["body_text"].format_map(safe),
    }
