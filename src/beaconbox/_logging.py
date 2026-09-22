"""How this library logs, and the rules it will not break.

A library's logging is not an application's logging. The rules below are the standard ones, and
each is here because breaking it steals a decision from the person integrating this SDK:

* **One logger per module, named after the module.** Everything lands under the ``beaconbox``
  hierarchy, so an application silences or raises the whole SDK with one line:

  .. code-block:: python

      logging.getLogger("beaconbox").setLevel(logging.DEBUG)

* **A** :class:`~logging.NullHandler` **on the root of that hierarchy, and nothing else.** Added in
  :mod:`beaconbox.__init__`. It means importing this SDK produces no output, ever, until the
  application asks for some.
* **Never configure logging.** No ``basicConfig``, no handlers, no formatters, no level set on the
  root logger. A library that calls ``basicConfig()`` at import silently hijacks the logging of
  every application that imports it.
* **Never log anything sensitive.** See :func:`safe_extra`. This is the rule with teeth: BeaconBox
  requests carry an API key, a recipient's email address and the text of a message to a real
  person. A log line is copied to an aggregator, retained for months and read by people who were
  never meant to see any of that.

What actually gets logged, all of it on the ``beaconbox._transport`` logger:

===========  ===========================================================================
``DEBUG``    every request, and every response, with its status and elapsed time
``WARNING``  a retry, with the reason and the backoff
===========  ===========================================================================

Nothing is logged at ``INFO`` or above in normal operation. An SDK that chatters at ``INFO`` is an
SDK whose users filter it out, which is how the ``WARNING`` that mattered gets filtered out too.
"""

from __future__ import annotations

import logging
from typing import Any

LOGGER_NAME = "beaconbox"


def get_logger(name: str) -> logging.Logger:
    """The logger for a module inside this package. Always use this rather than a bare
    ``getLogger``, so nothing accidentally logs outside the ``beaconbox`` hierarchy."""
    return logging.getLogger(name)


def install_null_handler() -> None:
    """Attach a :class:`~logging.NullHandler` to ``beaconbox``, once.

    Without it, a warning emitted before the application configures logging would either print
    ``No handlers could be found`` or fall through to the root logger's ``lastResort`` handler and
    appear on stderr, uninvited.
    """
    root = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(handler, logging.NullHandler) for handler in root.handlers):
        root.addHandler(logging.NullHandler())


def safe_extra(**fields: Any) -> dict[str, Any]:
    """The allow-list for structured log fields.

    Deliberately an allow-list rather than a redaction pass. Redaction is a list of things somebody
    remembered to hide, and the field that gets added next year is not on it. This way a value has
    to be named here to ever reach a log line.

    **What is never logged, and why:**

    * the API key, or any header (``Authorization`` is a header);
    * the request or response body (a message body is text written to a named customer);
    * the query string (``?recipient_email=buyer@example.com``);
    * the interpolated URL path (``/recipients/buyer@example.com/sms``). The templated
      :attr:`~beaconbox._core.Call.route` is logged instead;
    * webhook secrets, and the payload of a webhook.

    The idempotency key is logged, because it is a random value of the caller's choosing that
    identifies one attempt, and correlating retries without it is guesswork. If a caller passes
    something identifying as their own key, that is their value in their logs.
    """
    return {"beaconbox": fields}
