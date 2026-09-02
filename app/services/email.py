import smtplib
from email.message import EmailMessage

from app.config import Settings


def send_password_reset(recipient: str, reset_code: str, app_settings: Settings) -> None:
    message = EmailMessage()
    message["Subject"] = "Copernicus GLO-30 Viewshed API password reset"
    message["From"] = app_settings.smtp_from
    message["To"] = recipient
    message.set_content(
        "Open /reset-password and paste this reset code:\n\n"
        f"{reset_code}\n\n"
        "The code expires in 30 minutes."
    )
    with smtplib.SMTP(app_settings.smtp_host, app_settings.smtp_port, timeout=10) as smtp:
        smtp.send_message(message)
