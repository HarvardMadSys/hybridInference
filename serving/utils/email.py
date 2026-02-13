"""Email sending utilities for user verification and password reset."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlparse

from serving.config.settings import settings
from serving.utils.logging import get_logger

logger = get_logger(__name__)


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
    """Check if email sending is enabled (SMTP credentials configured).

    Returns:
        True if SMTP is configured, False otherwise.
    """
    return bool(settings.smtp_user and settings.smtp_password)


def send_email(to_email: str, subject: str, html_body: str, text_body: str | None = None) -> bool:
    """Send an email using SMTP.

    Args:
        to_email: Recipient email address.
        subject: Email subject.
        html_body: HTML email body.
        text_body: Plain text email body (optional, defaults to stripped HTML).

    Returns:
        True if email sent successfully, False otherwise.
    """
    if not is_email_enabled():
        logger.warning("Email sending disabled: SMTP not configured")
        return False

    config = get_smtp_config()

    try:
        # Create message
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{config['from_name']} <{config['from_email']}>"
        msg["To"] = to_email
        msg["Subject"] = subject

        # Add text and HTML parts
        if text_body:
            msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        # Send email
        with smtplib.SMTP(config["host"], config["port"]) as server:
            server.starttls()
            server.login(config["user"], config["password"])
            server.send_message(msg)

        logger.info(f"Email sent successfully to {to_email}")
        return True

    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        return False


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

    subject = "Verify your FreeInference account"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #2563eb;">Welcome to FreeInference!</h2>
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
Welcome to FreeInference!

Thank you for signing up. Please verify your email address by visiting:

{verification_url}

This link will expire in 24 hours.

If you didn't create an account, you can safely ignore this email.
    """

    return send_email(to_email, subject, html_body, text_body)


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

    subject = "Reset your FreeInference password"

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

    return send_email(to_email, subject, html_body, text_body)
