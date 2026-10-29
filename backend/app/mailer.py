import logging
from email.message import EmailMessage

import aiosmtplib

from app.config import get_settings

log = logging.getLogger(__name__)


async def send_email(to: str, subject: str, body: str) -> None:
    """Отправка через SMTP (mailpit в dev). Ошибки пробрасываются — outbox повторит."""
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.mail_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    await aiosmtplib.send(msg, hostname=s.smtp_host, port=s.smtp_port)
    log.info("email sent", extra={"to": to, "subject": subject})
