import emails

from dynaconf import settings
from loguru import logger


class Email:
    """ Responsible for using emails package """

    def send(self, to, subject, message):
        message = message.replace('\n', '<br>\n')

        email = emails.html(
            html=message,
            subject=subject,
            mail_from=(
                settings.EMAILS.Info.From[0],
                settings.EMAILS.Info.From[1],
            )
        )

        try:
            email.send(
                to=to,
                smtp={
                    "host": settings.EMAILS.Info.Host[0],
                    "port": settings.EMAILS.Info.Host[1],
                    "user": settings.EMAILS.Info.Credentials[0],
                    "password": settings.EMAILS.Info.Credentials[1],
                }
            )
        except Exception:
            logger.error("Error occured when sending email")
