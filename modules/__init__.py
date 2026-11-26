"""Feature modules split out of http_config.py.

The upstream server is one 10k-line script. Everything this fork adds lives here instead, one
module per feature, and http_config imports it back at the top so every call site below reads as
before:

    localization.py  responses/localization.json: LANGUAGE, tr() / trf(), the table overlays
    team_picker.py   the nation -> league -> club wheels (SORTNATION / SORTLEAGUE / SORTTEAM) and
                     the authentic-squad preview behind them
    tournaments.py   event cups (World Cup / Euro): config, draw, squads, format, instance
    cup_api.py       the /cup/* endpoints every cup screen goes through, for club cups and
                     tournaments alike

Dependency order: tournaments is the leaf; team_picker and cup_api both sit on it, and cup_api
does not touch the team wheels. A module may reach http_config through `hc` (below), never at
import time: http_config imports these modules in the middle of its own body, so a plain
`import http_config` there would hand back a half-built module, and importing http_config first
would deadlock the other way round. `hc` resolves on first attribute access instead, so either
import order works.
"""


class _Host:
    """http_config, resolved on first use (see the module docstring)."""

    def __getattr__(self, name):
        import http_config  # noqa: PLC0415 -- the point: late, never at module import

        return getattr(http_config, name)


hc = _Host()
