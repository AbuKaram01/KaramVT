# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The first-run welcome guide: what KaramVT does, then getting the models it needs.

The guide has three pages::

    1. Welcome        what the app does, in three lines
    2. Models         the same download rows as Preferences, with progress and stop
    3. Finish         what is ready and what is still missing

It opens by itself the first time KaramVT starts without models, and from the main menu ("Welcome Guide") whenever
the user wants it again. Downloads belong to the application's ``DownloadManager``, so pressing Close while a model
downloads does not interrupt it.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk

from ..config import AppConfig
from ..services import DownloadEvent, DownloadManager
from .model_downloads import ModelRows
from .ocr_engine_row import OcrEngineRow

# Positions of the pages in the carousel.
_PAGE_WELCOME, _PAGE_MODELS, _PAGE_FINISH = 0, 1, 2
# Shown instead of the application icon when the icon theme does not provide it.
_FALLBACK_ICON = "video-x-generic-symbolic"

# The rows of the welcome page: icon name, title and description.
_FEATURES = (
    ("video-x-generic-symbolic", "Videos and subtitles",
     "Turn a video, or a whole course folder, into subtitles, and translate them into one or several languages."),
    ("text-x-generic-symbolic", "Text and images",
     "Translate text directly, or pull the text out of an image first with OCR."),
    ("security-high-symbolic", "Private by design",
     "Speech recognition, translation and OCR all run on your computer. Your files are never uploaded."),
)


def mark_welcome_done() -> None:
    """Record that the guide has been dealt with. Read-modify-write, like every other settings change."""
    config = AppConfig.load()
    if not config.welcome_done:
        config.welcome_done = True
        config.save()


def should_show_welcome() -> bool:
    """Return whether the guide should open at start-up: only for someone who has not seen it and has no models.

    Anyone who already has both models (an existing installation) never sees it, and is recorded as done so that
    deleting a model later does not bring it up either.
    """
    config = AppConfig.load()
    if config.welcome_done:
        return False
    if config.models_ready:
        mark_welcome_done()
        return False
    return True


def _scrolled(content: Gtk.Widget) -> Gtk.ScrolledWindow:
    """Wrap ``content`` as a page body: centred, in a comfortable column, scrolling when the dialog is short."""
    clamp = Adw.Clamp(maximum_size=460, tightening_threshold=360, margin_start=12, margin_end=12)
    clamp.set_child(content)
    # hexpand is needed because a carousel gives a page only its natural width unless the page asks for all of it.
    return Gtk.ScrolledWindow(
        child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True, vexpand=True,
    )


class WelcomeDialog(ModelRows, Adw.Dialog):
    """The welcome guide dialog: an introduction, the model download rows and a summary of what is ready.

    Args:
        downloads: The application's download manager, so that downloads continue when the guide is closed.
        app_icon: Name of the application icon shown on the first page.
    """

    def __init__(self, downloads: DownloadManager, *, app_icon: str) -> None:
        super().__init__(title="Welcome", content_width=520, content_height=700)
        self._init_rows(self, downloads, self._on_models_changed)
        self._app_icon = app_icon
        self._index = _PAGE_WELCOME

        self._carousel = Adw.Carousel(
            interactive=False, allow_scroll_wheel=False, allow_mouse_drag=False, allow_long_swipes=False, vexpand=True,
        )
        self._pages = [self._welcome_page(), self._models_page(), self._finish_page()]
        for page in self._pages:
            self._carousel.append(page)

        toolbar = Adw.ToolbarView(content=self._build_body())
        toolbar.add_top_bar(Adw.HeaderBar(show_title=False))
        toolbar.set_top_bar_style(Adw.ToolbarStyle.FLAT)
        self.set_child(toolbar)
        self.set_default_widget(self._next)

        self.refresh()
        self._update_finish_page()
        self._sync_buttons()
        downloads.subscribe(self._on_event)
        self.connect("closed", self._on_closed)

    # --- Building ---

    def _build_body(self) -> Gtk.Box:
        # The toasts sit on the pages, so they never cover the Back / Next buttons.
        self._toasts = Adw.ToastOverlay(child=self._carousel, vexpand=True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self._toasts)
        body.append(Adw.CarouselIndicatorDots(carousel=self._carousel, margin_top=6, margin_bottom=6))
        body.append(self._build_buttons())
        return body

    def _build_buttons(self) -> Gtk.Box:
        self._back = Gtk.Button(label="Back", css_classes=["flat", "pill"])
        self._back.connect("clicked", lambda _b: self._go(self._index - 1))
        self._next = Gtk.Button(label="Next", css_classes=["suggested-action", "pill"], width_request=180)
        self._next.connect("clicked", self._on_next)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=6, margin_bottom=24)
        buttons.append(self._back)
        buttons.append(self._next)
        return buttons

    # --- Pages ---

    def _welcome_page(self) -> Gtk.Widget:
        theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        icon = self._app_icon if theme.has_icon(self._app_icon) else _FALLBACK_ICON
        features = Adw.PreferencesGroup(margin_top=6, margin_bottom=12)
        for icon_name, title, subtitle in _FEATURES:
            row = Adw.ActionRow(title=title, subtitle=subtitle, subtitle_lines=0, activatable=False)
            row.add_prefix(Gtk.Image(icon_name=icon_name, icon_size=Gtk.IconSize.LARGE))
            features.add(row)
        page = Adw.StatusPage(
            icon_name=icon, title="Welcome to KaramVT",
            description="Translate videos, subtitles, text and images on your own computer.",
            child=features,
        )
        return _scrolled(page)

    def _models_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=12, margin_bottom=12)
        box.append(Gtk.Label(label="Download the models", css_classes=["title-1"], wrap=True))
        box.append(Gtk.Label(
            label=(
                "KaramVT needs two models: one to listen and one to translate. "
                "They are downloaded once from Hugging Face and then work offline."
            ),
            wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"],
        ))

        speech = Adw.PreferencesGroup(
            title="Speech recognition", description="Listens to the video and writes down what is said.",
        )
        for row in self._whisper_rows():
            speech.add(row)
        box.append(speech)

        translation = Adw.PreferencesGroup(
            title="Translation",
            description="Translates the text. Larger models translate better but need more memory.",
        )
        for expander in self._llm_expanders():
            translation.add(expander)
        box.append(translation)

        self._note = Gtk.Label(wrap=True, xalign=0, css_classes=["caption", "dim-label"])
        box.append(self._note)
        return _scrolled(box)

    def _finish_page(self) -> Gtk.Widget:
        # Reading text from images needs the Tesseract program, which is not a download: say whether it is there.
        self._ocr_row = OcrEngineRow(lambda toast: self.add_toast(toast))
        ocr = Adw.PreferencesGroup(title="Text from images", margin_top=12, margin_bottom=12)
        ocr.add(self._ocr_row)
        self._finish = Adw.StatusPage(icon_name="object-select-symbolic", title="", description="", child=ocr)
        return _scrolled(self._finish)

    # --- State ---

    def refresh(self) -> None:
        """Refresh the state of the model rows and the note about where the models are saved."""
        super().refresh()
        self._note.set_label(
            f"Saved to {AppConfig.load().models_folder()}. Models download one at a time, and a download "
            "carries on in the background. Already have models? Skip this step and choose them in Preferences."
        )

    def _on_event(self, event: DownloadEvent) -> None:
        super()._on_event(event)
        self._update_finish_page()
        self._sync_buttons()

    def _on_models_changed(self) -> None:
        self._update_finish_page()
        self._sync_buttons()

    def _update_finish_page(self) -> None:
        config = AppConfig.load()
        if config.models_ready:
            icon, title = "object-select-symbolic", "You're all set"
            text = "Open the Video tab to translate a video or a subtitle file, or the Text tab for text and images."
        else:
            icon, title, text = "folder-download-symbolic", "Almost there", self._what_is_missing(config)
        self._finish.set_icon_name(icon)
        self._finish.set_title(title)
        self._finish.set_description(text)

    def _what_is_missing(self, config: AppConfig) -> str:
        missing = [name for name, ready in (("a speech recognition model", config.whisper_ready),
                                             ("a translation model", config.llm_ready)) if not ready]
        text = (
            f"KaramVT still needs {' and '.join(missing)}. Get {'it' if len(missing) == 1 else 'them'} "
            "any time from the main menu, under Download Models."
        )
        if self._downloads.active:
            text += (
                " A download is in progress; it carries on in the background "
                "and the model is selected when it finishes."
            )
        return text

    # --- Navigation ---

    def _go(self, index: int) -> None:
        self._index = max(_PAGE_WELCOME, min(index, _PAGE_FINISH))
        self._carousel.scroll_to(self._pages[self._index], True)
        if self._index == _PAGE_MODELS:
            self.refresh()
        if self._index == _PAGE_FINISH:
            self._update_finish_page()
            self._ocr_row.update()               # It may have been installed while the guide was open.
        self._sync_buttons()

    def _on_next(self, _button: Gtk.Button) -> None:
        if self._index == _PAGE_FINISH:
            self.close()
        else:
            self._go(self._index + 1)

    def _sync_buttons(self) -> None:
        self._back.set_visible(self._index > _PAGE_WELCOME)
        if self._index == _PAGE_FINISH:
            self._next.set_label("Start Translating" if AppConfig.load().models_ready else "Close")
        else:
            self._next.set_label("Next")

    # --- Shared with the rest of the application ---

    def add_toast(self, toast: Adw.Toast) -> None:
        """Show a toast inside the guide, where the user is looking."""
        self._toasts.add_toast(toast)

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        self.detach()
        mark_welcome_done()
