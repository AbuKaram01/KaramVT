# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The "OCR Settings" dialog behind the gear button.

Opened from the gear next to the "Import from image" button in the Text tab (and from a row in Preferences). It
holds everything about how Tesseract reads an imported image:

* Tesseract: whether the program itself is installed, and how to install it when it is not.
* Language data: where language files come from (System or a downloaded quality tier) and the per-language
  download and delete list (``LanguageManagerDialog``).
* Recognition engine: engine mode (OEM), page segmentation (PSM), a list of raw Tesseract variables, and free-form
  custom parameters.

Every change is saved to the config file immediately, without an "Apply" step, and always as a read-modify-write of
the current file, so it can never overwrite something changed from Preferences in the meantime.
"""

from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from ..config import AppConfig, update_saved_config
from ..core import tessdata_manager
from ..core.ocr_options import (
    OEM_OPTIONS,
    PSM_OPTIONS,
    TESS_TOGGLES,
    oem_index,
    psm_index,
    psm_needs_osd,
)
from .language_manager_dialog import LanguageManagerDialog
from .ocr_engine_row import OcrEngineRow
from .widgets import button_row, combo_row, flat_button

# Parallel lists for the language source row, indexed by the selected position: "system" first, then one entry per
# downloaded quality tier.
_SOURCE_IDS = ["system"] + [tier for tier, _label in tessdata_manager.QUALITY_TIERS]
_SOURCE_LABELS = ["System"] + [tier.capitalize() for tier, _label in tessdata_manager.QUALITY_TIERS]
_SOURCE_DESCRIPTIONS = [
    "Uses tesseract-ocr-<lang> packages already installed via apt / dnf / pacman / brew"
] + [label for _tier, label in tessdata_manager.QUALITY_TIERS]
# Parallel lists for the engine mode and page segmentation rows, in the order of core.ocr_options.
_OEM_LABELS = [label for _value, label, _description in OEM_OPTIONS]
_OEM_DESCRIPTIONS = [description for _value, _label, description in OEM_OPTIONS]
_PSM_LABELS = [label for _value, label, _description in PSM_OPTIONS]
_PSM_DESCRIPTIONS = [description for _value, _label, description in PSM_OPTIONS]

# Delay in milliseconds after the last keystroke before the custom parameters are saved.
_SAVE_CUSTOM_AFTER_MS = 500

# The one OCR Settings dialog currently open, if any. Several entry points (the gear button, Preferences) lead here,
# so this stops a second copy from stacking on top of the first.
_open_dialog: "OcrSettingsDialog | None" = None


def present_ocr_settings(parent, on_changed=None) -> None:
    """Open the OCR Settings dialog over *parent*, unless one is already open.

    Args:
        parent: The widget the dialog is presented over.
        on_changed: Called without arguments when the language source changes or a language pack is downloaded
            or deleted.
    """
    global _open_dialog
    if _open_dialog is not None:
        return

    def _forget(_dialog):
        global _open_dialog
        _open_dialog = None

    dialog = OcrSettingsDialog(on_changed=on_changed)
    dialog.connect("closed", _forget)
    _open_dialog = dialog
    dialog.present(parent)


# --- Rows ---

def _icon_prefix(icon_name: str) -> Gtk.Image:
    image = Gtk.Image.new_from_icon_name(icon_name)
    image.add_css_class("dim-label")
    return image


def _show_description(combo: Adw.ComboRow, descriptions: list[str]) -> None:
    """Put the explanation of the chosen entry under the row's title."""
    index = combo.get_selected()
    if 0 <= index < len(descriptions):
        combo.set_subtitle(descriptions[index])


def _switch_action_row(title: str, switch: Gtk.Switch) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title)
    row.add_suffix(switch)
    row.set_activatable_widget(switch)
    return row


def _toggle_row(var_name: str, field: str, active: bool) -> Adw.ActionRow:
    """Build a row with a switch for a named Tesseract variable, wired straight to its AppConfig field.

    The raw variable name is the title on purpose; see ``core.ocr_options.TESS_TOGGLES``.
    """
    switch = Gtk.Switch(active=bool(active), valign=Gtk.Align.CENTER)
    switch.connect("notify::active", lambda sw, _param: update_saved_config(**{field: sw.get_active()}))
    return _switch_action_row(var_name, switch)


def _edges_scale(value: int, enabled: bool) -> Gtk.Scale:
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
    scale.set_value(value)
    scale.set_draw_value(True)
    scale.set_value_pos(Gtk.PositionType.RIGHT)
    scale.set_hexpand(True)
    scale.set_sensitive(enabled)
    scale.set_margin_start(12)
    scale.set_margin_end(12)
    scale.set_margin_top(4)
    scale.set_margin_bottom(8)
    scale.connect("value-changed", lambda sc: update_saved_config(ocr_edges_max_children_value=int(sc.get_value())))
    return scale


def _format_hint() -> Adw.ActionRow:
    row = Adw.ActionRow(
        title="Format",
        subtitle=(
            "key=value pairs, separated by semicolons \u2014 "
            "e.g. tessedit_char_whitelist=0123456789; user_defined_dpi=300"
        ),
    )
    row.set_subtitle_lines(0)
    row.add_css_class("dim-label")
    return row


def _osd_dialog(label: str, source: str) -> Adw.AlertDialog:
    """Build the question whether to fetch the orientation data for *source*, asked because mode *label* needs it."""
    dialog = Adw.AlertDialog(
        heading="Orientation data needed",
        body=(
            f"\u201c{label}\u201d uses orientation and script detection, which needs a "
            "separate data file (about 10 MB) that isn\u2019t part of any language pack.\n\n"
            f"Download it for the {source.capitalize()} source now? Without it, OCR in this "
            "mode will report an error until you download it or choose another mode."
        ),
    )
    dialog.add_response("later", "Not Now")
    dialog.add_response("download", "Download")
    dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("download")
    dialog.set_close_response("later")
    return dialog


class OcrSettingsDialog(Adw.PreferencesDialog):
    """Dialog for the language source, engine mode, page segmentation and advanced Tesseract options.

    Args:
        on_changed: Called without arguments when the language source changes or a language pack is downloaded or
            deleted, for callers that care which packs exist.
    """

    def __init__(self, on_changed=None) -> None:
        super().__init__(title="OCR Settings")
        self.set_search_enabled(False)
        self.set_content_width(560)
        self.set_content_height(680)
        self._on_changed = on_changed
        self._osd_download_active = False
        # The custom parameters are saved shortly after the user stops typing (and when the dialog closes), not on
        # every keystroke, which would rewrite the whole settings file for each character.
        self._pending_custom: str | None = None
        self._custom_timer: int | None = None

        config = AppConfig.load()
        page = Adw.PreferencesPage()
        self.add(page)
        self._add_program_group(page)
        self._add_language_group(page, config)
        self._add_engine_group(page, config)
        self.connect("closed", lambda *_: self._flush_custom())

    # --- Building ---

    def _add_program_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Tesseract", description="The program that reads the text in images")
        page.add(group)
        self._engine_row = OcrEngineRow(lambda toast: self.add_toast(toast))
        group.add(self._engine_row)

    def _add_language_group(self, page: Adw.PreferencesPage, config: AppConfig) -> None:
        group = Adw.PreferencesGroup(title="Language data", description="Where Tesseract loads language files from")
        page.add(group)

        saved_source = config.ocr_language_source
        self._source_combo = combo_row(
            "Language source", _SOURCE_LABELS,
            _SOURCE_IDS.index(saved_source) if saved_source in _SOURCE_IDS else 0,
            self._on_source_changed,
        )
        self._source_combo.add_prefix(_icon_prefix("preferences-desktop-locale-symbolic"))
        group.add(self._source_combo)
        _show_description(self._source_combo, _SOURCE_DESCRIPTIONS)

        self._manage_row = button_row(
            "Manage downloaded languages", flat_button("Open", self._on_manage_clicked), activatable=True
        )
        self._manage_row.add_prefix(_icon_prefix("folder-download-symbolic"))
        group.add(self._manage_row)
        self._update_manage_row()

    def _add_engine_group(self, page: Adw.PreferencesPage, config: AppConfig) -> None:
        group = Adw.PreferencesGroup(
            title="Recognition engine",
            description="Advanced Tesseract tuning \u2014 the defaults work well for most images",
        )
        page.add(group)

        self._oem_combo = combo_row("Engine mode (OEM)", _OEM_LABELS, oem_index(config.ocr_oem), self._on_oem_changed)
        group.add(self._oem_combo)
        _show_description(self._oem_combo, _OEM_DESCRIPTIONS)

        # psm_index() maps unknown values (for example from a hand-edited config) to the default mode instead of a
        # wrong list position.
        self._psm_combo = combo_row(
            "Page segmentation (PSM)", _PSM_LABELS, psm_index(config.ocr_psm), self._on_psm_changed
        )
        group.add(self._psm_combo)
        _show_description(self._psm_combo, _PSM_DESCRIPTIONS)

        group.add(self._build_tesseract_options(config))
        group.add(self._build_custom_parameters(config))
        group.add(_format_hint())

    def _build_tesseract_options(self, config: AppConfig) -> Adw.ExpanderRow:
        expander = Adw.ExpanderRow(
            title="Tesseract Options",
            subtitle=(
                "Advanced, internal Tesseract tuning variables \u2014 "
                "leave off unless you know what a specific one does"
            ),
        )
        expander.add_row(_toggle_row("preserve_interword_spaces", "ocr_preserve_spaces", config.ocr_preserve_spaces))
        for field, var_name in TESS_TOGGLES:
            expander.add_row(_toggle_row(var_name, field, getattr(config, field)))

        self._edges_switch = Gtk.Switch(active=config.ocr_edges_max_children_enabled, valign=Gtk.Align.CENTER)
        expander.add_row(_switch_action_row("edges_max_children_per_outline", self._edges_switch))
        self._edges_scale = _edges_scale(config.ocr_edges_max_children_value, self._edges_switch.get_active())
        self._edges_switch.connect("notify::active", self._on_edges_switch_toggled)
        expander.add_row(self._edges_scale)
        return expander

    def _build_custom_parameters(self, config: AppConfig) -> Adw.EntryRow:
        row = Adw.EntryRow(title="Custom parameters")
        row.set_text(config.ocr_custom_params)
        row.connect("changed", self._on_custom_changed)
        return row

    # --- Custom parameters (debounced save) ---

    def _on_custom_changed(self, row) -> None:
        self._pending_custom = row.get_text()
        if self._custom_timer is not None:
            GLib.source_remove(self._custom_timer)
        self._custom_timer = GLib.timeout_add(_SAVE_CUSTOM_AFTER_MS, self._flush_custom_timer)

    def _flush_custom_timer(self) -> bool:
        self._custom_timer = None
        self._flush_custom()
        return False

    def _flush_custom(self) -> None:
        if self._custom_timer is not None:
            GLib.source_remove(self._custom_timer)
            self._custom_timer = None
        if self._pending_custom is not None:
            text, self._pending_custom = self._pending_custom, None
            update_saved_config(ocr_custom_params=text)

    # --- Showing what is chosen ---

    def _selected_source(self) -> str:
        return _SOURCE_IDS[self._source_combo.get_selected()]

    def _update_manage_row(self) -> None:
        source = self._selected_source()
        self._manage_row.set_sensitive(source != "system")
        if source == "system":
            self._manage_row.set_subtitle("Choose a downloaded source above to manage its languages")
        else:
            count = len(tessdata_manager.list_downloaded(source))
            self._manage_row.set_subtitle(f"{count} language(s) downloaded for this tier")

    def _notify_changed(self) -> None:
        if self._on_changed:
            self._on_changed()

    # --- Change handlers ---

    def _on_source_changed(self, *_args) -> None:
        update_saved_config(ocr_language_source=self._selected_source())
        _show_description(self._source_combo, _SOURCE_DESCRIPTIONS)
        self._update_manage_row()
        self._notify_changed()
        self._offer_osd_if_needed()

    def _on_oem_changed(self, *_args) -> None:
        update_saved_config(ocr_oem=OEM_OPTIONS[self._oem_combo.get_selected()][0])
        _show_description(self._oem_combo, _OEM_DESCRIPTIONS)

    def _on_psm_changed(self, *_args) -> None:
        update_saved_config(ocr_psm=PSM_OPTIONS[self._psm_combo.get_selected()][0])
        _show_description(self._psm_combo, _PSM_DESCRIPTIONS)
        self._offer_osd_if_needed()

    def _on_edges_switch_toggled(self, switch, _param) -> None:
        enabled = switch.get_active()
        update_saved_config(ocr_edges_max_children_enabled=enabled)
        self._edges_scale.set_sensitive(enabled)

    def _on_manage_clicked(self, *_args) -> None:
        source = self._selected_source()
        if source != "system":
            LanguageManagerDialog(source, on_changed=self._on_languages_changed).present(self)

    def _on_languages_changed(self) -> None:
        self._update_manage_row()
        self._notify_changed()

    # --- Script/orientation data (osd) ---

    def _offer_osd_if_needed(self) -> None:
        """Offer to download the orientation data when the selected page segmentation mode needs it.

        PSM 1 and 12 use orientation and script detection, which needs ``osd.traineddata``, a file that no language
        pack contains. With a downloaded language source it is absent unless fetched separately, so the offer is
        made as soon as such a mode is selected, or the source changes to one that lacks the file. The System
        source is left alone: its packages normally include the file, and OCR reports the exact install command if
        they do not.

        Auto-detect of the source language needs the file too, but it fetches the file itself the first time it
        runs.
        """
        index = self._psm_combo.get_selected()
        if not 0 <= index < len(PSM_OPTIONS):
            return
        psm, label, _description = PSM_OPTIONS[index]
        source = self._selected_source()
        if self._lacks_osd(psm, source):
            dialog = _osd_dialog(label, source)
            dialog.connect("response", self._on_osd_response, source)
            dialog.present(self)

    def _lacks_osd(self, psm: int, source: str) -> bool:
        """Return whether the orientation data is needed and missing, and no download of it is already running."""
        return (
            psm_needs_osd(psm)
            and source != "system"
            and not tessdata_manager.has_osd(source)
            and not self._osd_download_active
        )

    def _on_osd_response(self, _dialog, response: str, source: str) -> None:
        if response != "download" or self._osd_download_active:
            return
        self._osd_download_active = True
        self.add_toast(Adw.Toast(title="Downloading orientation data\u2026", timeout=3))
        threading.Thread(target=self._download_osd, args=(source,), daemon=True).start()

    def _download_osd(self, source: str) -> None:
        """Download the orientation data. Runs on a worker thread and reports back on the main thread."""
        try:
            tessdata_manager.download(source, tessdata_manager.OSD_CODE)
        except Exception as exc:  # Reported to the user as a toast.
            GLib.idle_add(self._on_osd_download_done, str(exc))
        else:
            GLib.idle_add(self._on_osd_download_done, None)

    def _on_osd_download_done(self, error: str | None) -> bool:
        self._osd_download_active = False
        if error:
            self.add_toast(Adw.Toast(title=f"Could not download orientation data: {error}", timeout=5))
        else:
            self.add_toast(Adw.Toast(title="Orientation data downloaded \u2713", timeout=3))
        return False
