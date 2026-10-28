# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GTK4 + libadwaita front-end for karamvt.

Requires GTK 4.10+ and libadwaita 1.4+ (for Gtk.FileDialog, Adw.ToolbarView,
Adw.EntryRow / Adw.SpinRow). See the project README for install instructions.
"""

from .. import winsupport

# On Windows the GTK runtime has to be made visible before ``gi`` is imported by any submodule.
winsupport.prepare_runtime()
