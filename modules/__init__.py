"""Feature modules split out of http_config.py.

The upstream server is one 10k-line script; the code this fork adds lives here instead, one module
per feature, and http_config imports it back by name so the existing call sites read unchanged.
A module here may reach the host through `http_config` (see _Host in tournaments.py) but must not
require it at import time.
"""
