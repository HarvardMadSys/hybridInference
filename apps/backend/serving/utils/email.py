"""Email sending utilities for user verification and password reset."""

import html as html_lib
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
        with smtplib.SMTP(config["host"], config["port"], timeout=10) as server:
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


def send_approval_email(to_email: str) -> bool:
    """Notify user that their registration has been approved.

    Args:
        to_email: User's email address.

    Returns:
        True if email sent successfully, False otherwise.
    """
    login_url = f"{settings.frontend_url}/login"

    subject = "Your FreeInference account has been approved"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #10b981;">Account Approved</h2>
            <p>Your FreeInference account has been approved by an administrator.</p>
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
                If you did not register for FreeInference, please ignore this email.
            </p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
Account Approved

Your FreeInference account has been approved by an administrator.

You can now log in and start using the API:
{login_url}

If you did not register for FreeInference, please ignore this email.
    """

    return send_email(to_email, subject, html_body, text_body)


def send_rejection_email(to_email: str, reason: str) -> bool:
    """Notify user that their registration has been rejected.

    Args:
        to_email: User's email address.
        reason: Rejection reason provided by admin.

    Returns:
        True if email sent successfully, False otherwise.
    """
    subject = "Your FreeInference registration update"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #ef4444;">Registration Not Approved</h2>
            <p>Unfortunately, your FreeInference registration was not approved at this time.</p>
            <div style="background: #fef2f2; border-left: 4px solid #ef4444; padding: 12px 16px;
                        margin: 20px 0; border-radius: 4px;">
                <strong>Reason:</strong> {reason}
            </div>
            <p>If you believe this was a mistake, please contact the administrator.</p>
            <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
            <p style="color: #999; font-size: 12px;">
                If you did not register for FreeInference, please ignore this email.
            </p>
        </div>
    </body>
    </html>
    """

    text_body = f"""
Registration Not Approved

Unfortunately, your FreeInference registration was not approved at this time.

Reason: {reason}

If you believe this was a mistake, please contact the administrator.
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
    # HTML-escape the user-supplied use case and preserve line breaks.
    use_case_html = (
        html_lib.escape(use_case_text).replace("\r\n", "\n").replace("\n", "<br>")
    )

    subject = f"[FreeInference] New registration pending approval: {user_email}"

    html_body = f"""
    <html>
    <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
        <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
            <h2 style="color: #f59e0b;">New Registration Pending Approval</h2>
            <p>A new user has registered and is waiting for approval:</p>
            <table style="border-collapse: collapse; margin: 20px 0;">
                <tr>
                    <td style="padding: 6px 16px 6px 0; font-weight: bold;">Email:</td>
                    <td style="padding: 6px 0;">{user_email}</td>
                </tr>
                <tr>
                    <td style="padding: 6px 16px 6px 0; font-weight: bold;">Name:</td>
                    <td style="padding: 6px 0;">{display_name}</td>
                </tr>
                <tr>
                    <td style="padding: 6px 16px 6px 0; font-weight: bold;">User ID:</td>
                    <td style="padding: 6px 0; font-family: monospace; font-size: 13px;">{user_id}</td>
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

    return send_email(to_email, subject, html_body, text_body)


# ── Broadcast email templates ──────────────────────────────────────────────


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
                <p style="color: #999; font-size: 12px;">You received this because you have an active FreeInference account.</p>
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
                <p style="color: #999; font-size: 12px;">You received this because you have an active FreeInference account.</p>
            </div>
        </body>
        </html>
        """,
        "body_text": "New: {feature_name}\n\n{description}",
    },
    "quota_change": {
        "subject": "Your FreeInference quota has been updated",
        "body_html": """
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px;">
                <h2 style="color: #10b981;">Quota Updated</h2>
                <p>Your daily usage quota has been updated to <strong>{new_quota}</strong>.</p>
                <hr style="border: none; border-top: 1px solid #eee; margin: 30px 0;">
                <p style="color: #999; font-size: 12px;">You received this because you have an active FreeInference account.</p>
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
) -> dict[str, str]:
    """Render a broadcast email from a template key or custom content.

    Raises:
        ValueError: If template_key is provided but not in EMAIL_TEMPLATES.
    """
    if template_key is None:
        return {
            "subject": custom_subject,
            "body_html": custom_body_html,
            "body_text": custom_body_text,
        }

    if template_key not in EMAIL_TEMPLATES:
        raise ValueError(f"Unknown template: {template_key!r}. Available: {list(EMAIL_TEMPLATES)}")

    tmpl = EMAIL_TEMPLATES[template_key]
    safe = _SafeDict(template_vars)
    return {
        "subject": tmpl["subject"].format_map(safe),
        "body_html": tmpl["body_html"].format_map(safe),
        "body_text": tmpl["body_text"].format_map(safe),
    }
