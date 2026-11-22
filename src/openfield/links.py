"""Signed links: URLs that carry their own permission, for one upload or one output.

An agent often has openField's key only inside its MCP client config, not in its shell. The upload URL that
create_upload hands back, and the download URLs of outputs, therefore carry an HMAC token: `curl` works with no
key and no environment variable, the same way in bash, PowerShell and cmd. A token grants only that one upload
(send bytes, read its state) or that one output file (download), and output links expire.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("openfield.links")
REDACT = re.compile(r"((?:token|sig)=)[^&\s\"]+")


class Signer:
    def __init__(self, path: Path):
        self.path = path
        self._key: Optional[bytes] = None

    @property
    def key(self) -> bytes:
        if self._key is None:
            try:
                self._key = bytes.fromhex(self.path.read_text().strip())
            except (OSError, ValueError):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(secrets.token_hex(32))
                if os.name != "nt":
                    tmp.chmod(0o600)
                os.replace(tmp, self.path)
                self._key = bytes.fromhex(self.path.read_text().strip())
        return self._key

    def sign(self, message: str) -> str:
        return hmac.new(self.key, message.encode(), hashlib.sha256).hexdigest()[:40]

    def check(self, message: str, sig: Optional[str]) -> bool:
        return bool(sig) and hmac.compare_digest(self.sign(message), str(sig))

    # one upload: valid as long as the upload exists
    def upload_token(self, uid: str) -> str:
        return self.sign(f"upload:{uid}")

    def check_upload(self, uid: str, token: Optional[str]) -> bool:
        return self.check(f"upload:{uid}", token)

    # one output file: valid until exp
    def output_query(self, jid: str, name: str, ttl_s: float) -> str:
        exp = int(time.time() + ttl_s)
        return f"exp={exp}&sig={self.sign(f'output:{jid}/{name}:{exp}')}"

    def check_output(self, jid: str, name: str, exp: Optional[str], sig: Optional[str]) -> bool:
        try:
            if int(exp or 0) < time.time():
                return False
        except ValueError:
            return False
        return self.check(f"output:{jid}/{name}:{exp}", sig)


class RedactLinks(logging.Filter):
    """Keep link tokens out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(REDACT.sub(r"\1…", a) if isinstance(a, str) else a for a in record.args)
        return True
