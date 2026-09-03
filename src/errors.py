"""Errors whose message is safe -- and useful -- to show to the caller."""


class DealWithItError(Exception):
    """Base class for expected failures.

    Anything raised from this hierarchy is a problem with the *input*, so the
    job reports it verbatim. Everything else is a bug and gets a generic
    message instead of a leaked traceback.
    """
