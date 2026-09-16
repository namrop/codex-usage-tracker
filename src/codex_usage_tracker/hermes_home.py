"""Where Hermes state lives, for every reader in this package.

One policy, used by ``auth.py`` (``auth.json``) and ``token_correlation.py``
(``state.db``), so the two can never disagree about which Hermes install they
are describing:

1. ``HERMES_HOME`` — the explicit route. The live service wrapper sets it to
   the Sol canonical home, and a Hermes profile is only addressable this way.
2. ``/var/lib/hermes/primary`` when that root actually exists — the Sol
   canonical home. Without this step an unwrapped invocation on Sol (an ad-hoc
   CLI run, a cron entry that inherits no env) resolved to ``~/.hermes``, which
   on Sol is a stale non-canonical copy: the tracker would read credentials
   and session rows belonging to no live gateway.
3. ``~/.hermes`` — the portable fallback that keeps legacy desktop installs and
   any other host working.

Step 2 stats the absolute canonical path, never one derived from ``HOME``, so
it identifies the real Sol install rather than any home directory that happens
to be pointed somewhere else.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

# The Sol canonical Hermes home. Absolute on purpose — see the module docstring.
SOL_PRIMARY_HERMES_HOME = Path("/var/lib/hermes/primary")

# The portable fallback for every other host.
PORTABLE_HERMES_HOME_RELATIVE_PATH = ".hermes"


def _sol_primary_present() -> bool:
    """True when this host really carries the Sol canonical Hermes root.

    A directory stat only; no credential file is opened. This is the seam tests
    mock, so the suite never depends on the host it runs on.
    """
    return SOL_PRIMARY_HERMES_HOME.is_dir()


def resolve_hermes_home(environ: Optional[Mapping[str, str]] = None) -> Path:
    """The Hermes home this process should read, by the policy above."""
    env = os.environ if environ is None else environ
    explicit = env.get("HERMES_HOME")
    if explicit:
        return Path(explicit).expanduser()
    if _sol_primary_present():
        return SOL_PRIMARY_HERMES_HOME
    return Path.home() / PORTABLE_HERMES_HOME_RELATIVE_PATH


__all__ = [
    "PORTABLE_HERMES_HOME_RELATIVE_PATH",
    "SOL_PRIMARY_HERMES_HOME",
    "resolve_hermes_home",
]
