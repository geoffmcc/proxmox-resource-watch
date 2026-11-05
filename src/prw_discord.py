"""Optional Discord transport. Never include transport exceptions in diagnostics."""
import json
import math
import time
from email.utils import parsedate_to_datetime
import os
import re
import secrets
import unicodedata
import urllib.error
import urllib.request


def enabled():
    return os.environ.get('PRW_DISCORD_ENABLED', 'false').lower() == 'true'


def clean(value, limit=800):
    text = str(value)
    # Redact URLs, including any accidentally embedded webhook credential.
    text = re.sub(r'https?://\S+', '[URL removed]', text, flags=re.I)
    text = ''.join(c if c == '\n' or unicodedata.category(c)[0] != 'C' else ' ' for c in text)
    text = re.sub(r'[@<>`*_~|\\\[\]]', ' ', text)
    return text[:limit] or 'unavailable'


def clean_identifier(value, limit=80):
    """Names are single-line metadata, unlike formatted message bodies."""
    return clean(' '.join(str(value).splitlines()), limit)


def payload(title, description, color):
    return {'allowed_mentions': {'parse': [], 'users': [], 'roles': [], 'replied_user': False},
            'embeds': [{'title': clean(title, 120), 'description': clean(description, 1800), 'color': color}]}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send(message, *, daily=False, attachment=None):
    """Return (safe error or None, retry delay seconds). One bounded attempt only."""
    if not enabled():
        return None, 0
    url = os.environ.get('PRW_DISCORD_WEBHOOK_URL', '')
    if not re.fullmatch(r'https://discord\.com/api(?:/v10)?/webhooks/[0-9]+/[A-Za-z0-9_-]+', url):
        return 'Discord configuration invalid or missing', 60
    try:
        if attachment is not None:
            # Discord displays ordinary message content before file attachments;
            # embeds appear after them. Keep daily delivery in a single request.
            embed = message['embeds'][0]
            message = {'allowed_mentions': message['allowed_mentions'],
                       'content': f"**{embed['title']}**\n{embed['description']}"}
        data = json.dumps(message, ensure_ascii=True).encode('ascii')
        content_type = 'application/json'
        if attachment is not None:
            contents = attachment.encode('utf-8')
            if len(contents) > 2 * 1024 * 1024:
                return 'Discord report attachment too large', 60
            boundary = 'prw-' + secrets.token_hex(24)
            marker = ('--' + boundary).encode('ascii')
            data = (marker + b'\r\nContent-Disposition: form-data; name="payload_json"\r\n'
                    b'Content-Type: application/json\r\n\r\n' + data + b'\r\n' + marker +
                    b'\r\nContent-Disposition: form-data; name="files[0]"; filename="resource-report.md"\r\n'
                    b'Content-Type: text/markdown; charset=utf-8\r\n\r\n' + contents + b'\r\n' + marker + b'--\r\n')
            content_type = 'multipart/form-data; boundary=' + boundary
        request = urllib.request.Request(url + '?wait=true', data=data,
            headers={'Content-Type': content_type, 'User-Agent': 'ProxmoxResourceWatch/0.2.0'}, method='POST')
        # No environment proxies or redirects: credentials stay at the validated endpoint.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=5) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536:
                return 'Discord unexpected response', 60
            result = json.loads(body)
            if not isinstance(result, dict) or not isinstance(result.get('id'), str) or not result['id'].isdigit():
                return 'Discord malformed response', 60
            if attachment is not None:
                attachments = result.get('attachments')
                if not isinstance(attachments, list) or not any(
                    isinstance(item, dict) and item.get('filename') == 'resource-report.md' for item in attachments
                ):
                    return 'Discord report attachment not confirmed', 60
        return None, 0
    except urllib.error.HTTPError as exc:
        status = exc.code
        delay = 60
        if status == 429:
            try:
                raw = exc.headers.get('Retry-After', '60')
                if daily:
                    try:
                        requested = float(raw)
                    except ValueError:
                        requested = parsedate_to_datetime(raw).timestamp() - time.time()
                    delay = max(60, requested) if math.isfinite(requested) else 172800
                else:
                    delay = max(60, min(86400, float(raw)))
            except (ValueError, TypeError, OverflowError, AttributeError):
                if daily:
                    delay = 172800
        exc.close()
        return f'Discord HTTP {status}', delay
    except Exception:
        return 'Discord connection or response failure', 60
