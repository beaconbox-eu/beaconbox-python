"""A webhook receiver you can point BeaconBox at, for one sitting.

``webhook.site`` shows you that *something* arrived. It cannot tell you whether the signature
would have verified — which is the only part a real integration has to get right, and the part a
broken one fails silently. This does verify, with the same
:func:`beaconbox.webhooks.verify` a merchant would use, and prints a verdict per request.

    export BEACONBOX_WEBHOOK_SECRET=whsec_...     # shown once, when the endpoint was created
    uv run python examples/receiver.py            # listens on :8080
    ngrok http 8080                               # in another terminal; use the https URL

Answers 200 on a good signature and 400 on a bad one, which is also what your own handler should
do: a 400 tells BeaconBox not to keep retrying something that will never verify.

Not production code — one thread, no concurrency, stdlib only so there is nothing to install.
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

from beaconbox import WebhookVerificationError, webhooks

SECRET = os.environ.get("BEACONBOX_WEBHOOK_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        # The RAW body. Re-serializing anything parsed is the single most common way a correct
        # signature fails to verify, so this example cannot afford to take the shortcut.
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        signature = self.headers.get("X-BeaconBox-Signature", "")

        try:
            event = webhooks.verify(body, signature, SECRET)
        except WebhookVerificationError as exc:
            print(f"  REJECTED  {exc}", flush=True)
            self.send_response(400)
            self.end_headers()
            return

        print(f"  VERIFIED  {event.type}  id={event.id}", flush=True)
        for key, value in sorted(event.data.items()):
            print(f"              {key}: {value}", flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        """Silence the default access log — the lines above are the ones worth reading."""


def main() -> int:
    if not SECRET:
        print(
            "BEACONBOX_WEBHOOK_SECRET is unset — every delivery would be rejected.", file=sys.stderr
        )
        return 2
    print(f"Listening on http://127.0.0.1:{PORT} — expose it with: ngrok http {PORT}\n", flush=True)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
