# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""KaramVT: offline video and subtitle transcription and translation.

Everything runs locally: speech is transcribed with faster-whisper and text is translated with a local GGUF model
through llama-cpp-python. The core functionality does not depend on any paid third-party service.
"""

__version__ = "0.1.0"

# The application ID: the D-Bus name and icon name on Linux, the notification and taskbar identity on Windows.
APP_ID = "io.github.AbuKaram01.KaramVT"
