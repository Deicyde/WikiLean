"""The WikiLean contributor CLI.

One volunteer, one machine, their own Claude subscription and their own WikiLean
agent token: ``wikilean work`` does exactly one unit of review, tag or locate
work per round against the live site's public routes.

The package is standard library only. Nothing here holds a model credential,
and the WikiLean agent token never reaches the model call.
"""

VERSION = "0.1.0"

USER_AGENT = f"wikilean-cli/{VERSION} (+https://github.com/Deicyde/WikiLean)"
