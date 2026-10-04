"""Outgoing email over SMTP. Without SMTP settings, mail is either written to the log or not sent."""
import logging
import smtplib
import ssl
from email.message import EmailMessage

from . import config

log = logging.getLogger("lanparty.mail")


def enabled() -> bool:
    return bool(config.SMTP_HOST) or config.EMAIL_TO_LOG


def send(to: str, subject: str, body: str) -> bool:
    if not config.SMTP_HOST:
        if config.EMAIL_TO_LOG:
            log.warning("SMTP isn't configured. Email to %s, %r:\n%s", to, subject, body)
            return True
        return False
    msg = EmailMessage()
    msg["From"] = config.SMTP_FROM or config.SMTP_USER
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if config.SMTP_SECURITY == "ssl":
            smtp = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ssl.create_default_context(), timeout=15)
        else:
            smtp = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15)
            if config.SMTP_SECURITY == "starttls":
                smtp.starttls(context=ssl.create_default_context())
        with smtp:
            if config.SMTP_USER:
                smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        log.exception("Couldn't send email to %s", to)
        return False
