import smtplib
import ssl
from email.message import EmailMessage

from .inventory import product_url
from .stores import STORES


def send_message(settings, subject, body):
    if not settings['enabled']:
        raise ValueError('Email is turned off in global settings.')
    if not settings['host'] or not settings['sender'] or not settings['recipient']:
        raise ValueError('Complete the global email configuration first.')
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = settings['sender']
    message['To'] = settings['recipient']
    message.set_content(body)
    context = ssl.create_default_context()
    if settings['security'] == 'ssl':
        client = smtplib.SMTP_SSL(settings['host'], settings['port'], timeout=20, context=context)
    else:
        client = smtplib.SMTP(settings['host'], settings['port'], timeout=20)
    with client as smtp:
        if settings['security'] == 'starttls':
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if settings['username']:
            smtp.login(settings['username'], settings['password'])
        smtp.send_message(message)


def notify(settings, alert, inventory):
    kind = 'Open box' if alert['open_box'] else 'New stock'
    send_message(settings, f'{kind} available: {inventory.title[:140]}',
                 f'{inventory.title}\n\n{inventory.detail}\nStore: {STORES[alert["store_id"]]}\n\n'
                 f'{product_url(alert["url"], alert["store_id"], bool(alert["open_box"]))}\n\n'
                 'Availability can change. This alert does not reserve an item.\n— StockSmart')


def mail_error(exc):
    # Never render raw provider errors or submitted credentials back to clients/logs.
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'Email login failed. Check your username and app password in global settings.'
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'The mail server rejected the recipient address.'
    if isinstance(exc, ValueError):
        return 'Complete and enable the global email configuration.'
    return 'Email delivery failed. Check your SMTP settings and server connectivity; delivery will retry.'
