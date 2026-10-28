# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Entry point for ``python -m karamvt.gui``."""

import sys

from .application import main

if __name__ == "__main__":
    sys.exit(main())
