"""Direct HTTPS only: verified certificates, no redirects, proxies, or URL credentials."""

import http.client
import ssl
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .config import endpoint_url, read_token


class TransportError(RuntimeError):
    pass


@dataclass
class Response:
    status: int
    body: bytes
    retry_after: float = 0


def retry_after_seconds(value, now=None):
    if not value:
        return 0
    now = time.time() if now is None else now
    try:
        if value.isdigit():
            return max(0, int(value))
        return max(0, parsedate_to_datetime(value).timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return 0


class HTTPSClient:
    def __init__(self, config):
        self.config = config
        self.url = urlsplit(endpoint_url(config.endpoint))
        self.context = ssl.create_default_context(cafile=config.ca_file)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2

    def send(self, body):
        token = read_token(self.config)  # Token rotation takes effect on the next request.
        authorization = f"Bearer {token}" if self.config.token_header == "Authorization" else token
        connection = http.client.HTTPSConnection(
            self.url.hostname, self.url.port, timeout=self.config.timeout, context=self.context
        )
        try:
            connection.request(
                "POST",
                self.url.path,
                body,
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                    "User-Agent": "weewx-php-ingest/0.2.0",
                    self.config.token_header: authorization,
                },
            )
            response = connection.getresponse()
            deadline = time.monotonic() + self.config.timeout
            chunks, size = [], 0
            # read1 makes progress with a bounded total body deadline, even on trickling responses.
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError("response_timeout")
                if connection.sock:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(65536, 262145 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 262144:
                    raise TransportError("response_too_large")
            return Response(
                response.status,
                b"".join(chunks),
                retry_after_seconds(response.getheader("Retry-After")),
            )
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            # Never include request/response text or secrets in errors.
            raise TransportError("https_failed") from exc
        finally:
            connection.close()
