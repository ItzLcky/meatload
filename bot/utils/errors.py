"""Errors whose message is safe (and intended) to show the user verbatim."""

from __future__ import annotations

from discord.ext import commands


class FriendlyError(commands.CommandError):
    """Raise this for an expected failure with a message written for humans.

    The global error handler renders it as a plain red embed with no traceback
    and no logging, which keeps genuine bugs visible in the logs.
    """
