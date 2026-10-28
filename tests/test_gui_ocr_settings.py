# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The OCR Settings dialog (see gui_support.py).

Covers what it shows of the saved settings, that each change is saved at once, the delayed save of the custom
parameters, the offer of the script-detection data, and that only one copy of the dialog is open at a time.
"""

# Fixtures are imported so that pytest finds them by name; used as arguments, they look unused (F401) and
# redefined (F811) to ruff.
# ruff: noqa: F401, F811

import threading
from types import SimpleNamespace

import pytest
from gui_support import Adw, AppConfig, Gtk, capture_toasts, downloader, env, pump, walk

from karamvt.core import tessdata_manager
from karamvt.core.ocr import OEM_OPTIONS, PSM_OPTIONS, TESS_TOGGLES
from karamvt.gui import ocr_settings_dialog as module
from karamvt.gui.ocr_settings_dialog import OcrSettingsDialog

TIERS = [tier for tier, _label in tessdata_manager.QUALITY_TIERS]
PSM_WITH_OSD = next(index for index, (psm, _label, _desc) in enumerate(PSM_OPTIONS) if psm == 1)
PSM_WITHOUT_OSD = next(index for index, (psm, _label, _desc) in enumerate(PSM_OPTIONS) if psm == 6)


class Disk:
    """What language data is downloaded (instead of looking at the disk), and the downloads asked for."""

    def __init__(self):
        self.languages: dict[str, list[str]] = {}
        self.osd: set[str] = set()
        self.asked: list[tuple[str, str]] = []
        self.failure: Exception | None = None
        self.hold = threading.Event()
        self.hold.set()


@pytest.fixture
def disk(monkeypatch):
    """Replace the language-data lookups and downloads of tessdata_manager with a Disk that records them."""
    fake = Disk()
    monkeypatch.setattr(tessdata_manager, "list_downloaded", lambda source: fake.languages.get(source, []))
    monkeypatch.setattr(tessdata_manager, "has_osd", lambda source: source in fake.osd)

    def download(source, code, **_kwargs):
        fake.asked.append((source, code))
        fake.hold.wait(3)
        if fake.failure is not None:
            raise fake.failure

    monkeypatch.setattr(tessdata_manager, "download", download)
    yield fake
    fake.hold.set()


@pytest.fixture(autouse=True)
def only_one_open(monkeypatch):
    """Start every test with no OCR Settings dialog open."""
    monkeypatch.setattr(module, "_open_dialog", None)


@pytest.fixture
def open_dialog(env, disk):
    """A function that opens the dialog over the window (its rows are only built then); they are closed afterwards."""
    opened: list[OcrSettingsDialog] = []

    def open_one(**kwargs) -> OcrSettingsDialog:
        dialog = OcrSettingsDialog(**kwargs)
        dialog.present(env[1])
        pump(0.3)
        opened.append(dialog)
        return dialog

    yield open_one
    for dialog in opened:
        dialog.force_close()
    pump(0.1)


@pytest.fixture
def dialog(open_dialog):
    """The dialog, opened with its default arguments."""
    return open_dialog()


@pytest.fixture
def alerts(monkeypatch):
    """Collect the alert dialogs that would be presented, as ``(alert, parent)`` pairs."""
    shown: list = []
    monkeypatch.setattr(Adw.AlertDialog, "present", lambda alert, parent=None: shown.append((alert, parent)))
    return shown


def saved() -> AppConfig:
    """The settings as they are saved now."""
    return AppConfig.load()


def toggles_of(dialog) -> dict[str, Gtk.Switch]:
    """The switches of the Tesseract variables, by the variable's name."""
    found = {}
    for row in walk(dialog):
        if isinstance(row, Adw.ActionRow) and isinstance(row.get_activatable_widget(), Gtk.Switch):
            found[row.get_title()] = row.get_activatable_widget()
    return found


# --- What it shows ---

def test_the_groups_and_rows_come_in_this_order(dialog):
    groups = [w.get_title() for w in walk(dialog) if isinstance(w, Adw.PreferencesGroup) and w.get_title()]
    assert groups == ["Tesseract", "Language data", "Recognition engine"]
    expected = ["Language source", "Manage downloaded languages", "Engine mode (OEM)", "Page segmentation (PSM)",
                "Tesseract Options", "preserve_interword_spaces", *(var for _field, var in TESS_TOGGLES),
                "edges_max_children_per_outline", "Custom parameters", "Format"]
    titles = []
    for widget in walk(dialog):
        if isinstance(widget, Adw.PreferencesRow) and widget.get_title() in expected:
            if widget.get_title() not in titles:
                titles.append(widget.get_title())
    assert titles == expected


def test_the_defaults_are_shown_when_nothing_was_saved(dialog):
    assert dialog._source_combo.get_selected() == 0
    assert dialog._source_combo.get_subtitle().startswith("Uses tesseract-ocr-<lang> packages already installed")
    assert not dialog._manage_row.get_sensitive()
    assert dialog._manage_row.get_subtitle() == "Choose a downloaded source above to manage its languages"
    assert OEM_OPTIONS[dialog._oem_combo.get_selected()][0] == AppConfig().ocr_oem
    assert PSM_OPTIONS[dialog._psm_combo.get_selected()][0] == AppConfig().ocr_psm
    assert dialog._oem_combo.get_subtitle() == OEM_OPTIONS[dialog._oem_combo.get_selected()][2]
    assert dialog._psm_combo.get_subtitle() == PSM_OPTIONS[dialog._psm_combo.get_selected()][2]
    assert all(not switch.get_active() for switch in toggles_of(dialog).values())
    assert not dialog._edges_switch.get_active() and not dialog._edges_scale.get_sensitive()
    assert dialog._edges_scale.get_value() == AppConfig().ocr_edges_max_children_value


def test_the_saved_settings_are_shown(open_dialog, disk):
    disk.languages["best"] = ["ara", "eng", "deu"]
    AppConfig(
        ocr_language_source="best", ocr_oem=0, ocr_psm=11, ocr_preserve_spaces=True, ocr_chop_enable=True,
        ocr_edges_max_children_enabled=True, ocr_edges_max_children_value=70, ocr_custom_params="a=1; b=2",
    ).save()
    dialog = open_dialog()
    assert dialog._source_combo.get_selected() == 1 + TIERS.index("best")
    assert dialog._source_combo.get_subtitle() == dict(tessdata_manager.QUALITY_TIERS)["best"]
    assert dialog._manage_row.get_sensitive()
    assert dialog._manage_row.get_subtitle() == "3 language(s) downloaded for this tier"
    assert OEM_OPTIONS[dialog._oem_combo.get_selected()][0] == 0
    assert PSM_OPTIONS[dialog._psm_combo.get_selected()][0] == 11
    toggles = toggles_of(dialog)
    assert toggles["preserve_interword_spaces"].get_active() and toggles["chop_enable"].get_active()
    assert not toggles["use_new_state_cost"].get_active()
    assert dialog._edges_switch.get_active() and dialog._edges_scale.get_sensitive()
    assert dialog._edges_scale.get_value() == 70
    entry = next(w for w in walk(dialog) if isinstance(w, Adw.EntryRow) and w.get_title() == "Custom parameters")
    assert entry.get_text() == "a=1; b=2"


def test_a_source_or_mode_the_program_does_not_know_is_shown_as_the_default(open_dialog, disk):
    AppConfig(ocr_language_source="mystery", ocr_psm=99).save()
    dialog = open_dialog()
    assert dialog._source_combo.get_selected() == 0
    assert PSM_OPTIONS[dialog._psm_combo.get_selected()][0] == AppConfig().ocr_psm


def test_the_format_hint_is_shown_in_full_and_dimmed(dialog):
    hint = next(w for w in walk(dialog) if isinstance(w, Adw.ActionRow) and w.get_title() == "Format")
    assert hint.get_subtitle_lines() == 0 and hint.has_css_class("dim-label")
    assert "tessedit_char_whitelist=0123456789" in hint.get_subtitle()


# --- Changes are saved at once ---

def test_choosing_a_source_saves_it_and_shows_what_it_holds(dialog, disk):
    heard = []
    dialog._on_changed = lambda: heard.append("changed")
    disk.languages["fast"] = ["eng"]
    disk.osd.add("fast")
    dialog._source_combo.set_selected(1 + TIERS.index("fast"))
    assert saved().ocr_language_source == "fast"
    assert dialog._source_combo.get_subtitle() == dict(tessdata_manager.QUALITY_TIERS)["fast"]
    assert dialog._manage_row.get_sensitive()
    assert dialog._manage_row.get_subtitle() == "1 language(s) downloaded for this tier"
    assert heard == ["changed"]
    dialog._source_combo.set_selected(0)
    assert saved().ocr_language_source == "system" and not dialog._manage_row.get_sensitive()
    assert heard == ["changed", "changed"]


def test_the_engine_and_the_page_layout_are_saved_with_their_explanations(dialog, disk):
    dialog._oem_combo.set_selected(1)
    assert saved().ocr_oem == OEM_OPTIONS[1][0] and dialog._oem_combo.get_subtitle() == OEM_OPTIONS[1][2]
    dialog._psm_combo.set_selected(PSM_WITHOUT_OSD)
    psm = PSM_OPTIONS[PSM_WITHOUT_OSD]
    assert saved().ocr_psm == psm[0] and dialog._psm_combo.get_subtitle() == psm[2]


def test_every_tesseract_variable_has_a_switch_that_saves_its_own_field(dialog):
    toggles = toggles_of(dialog)
    assert list(toggles)[:1] == ["preserve_interword_spaces"]
    fields = {"preserve_interword_spaces": "ocr_preserve_spaces", **{var: field for field, var in TESS_TOGGLES}}
    for name, field in fields.items():
        toggles[name].set_active(True)
        assert getattr(saved(), field) is True, name
    toggles["chop_enable"].set_active(False)
    assert saved().ocr_chop_enable is False and saved().ocr_preserve_spaces is True


def test_the_edges_setting_has_a_switch_that_unlocks_its_slider(dialog):
    dialog._edges_switch.set_active(True)
    assert saved().ocr_edges_max_children_enabled is True and dialog._edges_scale.get_sensitive()
    dialog._edges_scale.set_value(55.0)
    assert saved().ocr_edges_max_children_value == 55 and isinstance(saved().ocr_edges_max_children_value, int)
    dialog._edges_switch.set_active(False)
    assert saved().ocr_edges_max_children_enabled is False and not dialog._edges_scale.get_sensitive()


def test_a_change_here_keeps_what_was_changed_elsewhere_in_the_meantime(dialog):
    config = saved()
    config.cpu_threads = 9
    config.save()
    dialog._oem_combo.set_selected(1)
    assert saved().cpu_threads == 9


# --- The custom parameters are saved a moment after typing stops ---

@pytest.fixture
def writes(monkeypatch):
    """Every save the dialog makes, as the fields it changed."""
    seen: list[dict] = []
    original = module.update_saved_config
    monkeypatch.setattr(module, "update_saved_config", lambda **changes: (seen.append(changes), original(**changes))[1])
    return seen


def entry_of(dialog) -> Adw.EntryRow:
    """The Custom parameters entry of the dialog."""
    return next(w for w in walk(dialog) if isinstance(w, Adw.EntryRow) and w.get_title() == "Custom parameters")


def test_typing_is_saved_once_when_it_pauses_and_not_on_every_key(dialog, writes):
    entry = entry_of(dialog)
    for text in ("a", "a=", "a=1"):
        entry.set_text(text)
    assert writes == []
    assert pump(2.0, until=lambda: writes != [])
    pump(0.7)
    assert writes == [{"ocr_custom_params": "a=1"}] and saved().ocr_custom_params == "a=1"


def test_what_is_still_pending_is_saved_when_the_dialog_closes_and_only_once(dialog, writes):
    entry_of(dialog).set_text("user_defined_dpi=300")
    dialog.emit("closed")
    assert writes == [{"ocr_custom_params": "user_defined_dpi=300"}]
    pump(0.8)
    assert len(writes) == 1


def test_closing_with_nothing_pending_saves_nothing(dialog, writes):
    dialog.emit("closed")
    dialog.emit("closed")
    assert writes == []


def test_clearing_the_field_saves_an_empty_text(open_dialog, disk, writes):
    AppConfig(ocr_custom_params="a=1").save()
    dialog = open_dialog()
    entry_of(dialog).set_text("")
    dialog.emit("closed")
    assert writes == [{"ocr_custom_params": ""}] and saved().ocr_custom_params == ""


# --- The language manager ---

@pytest.fixture
def manager(monkeypatch):
    """Replace LanguageManagerDialog with a stand-in that records how it was created and presented."""
    opened = SimpleNamespace(made=[], presented=[])

    class FakeManager:
        def __init__(self, source, on_changed=None):
            self.source, self.on_changed = source, on_changed
            opened.made.append(self)

        def present(self, parent):
            opened.presented.append(parent)

    monkeypatch.setattr(module, "LanguageManagerDialog", FakeManager)
    return opened


def test_the_manage_button_opens_the_manager_of_the_chosen_source_over_the_dialog(dialog, manager, disk):
    dialog._source_combo.set_selected(1 + TIERS.index("best"))
    button = dialog._manage_row.get_activatable_widget()
    button.emit("clicked")
    [made] = manager.made
    assert made.source == "best" and manager.presented == [dialog]


def test_what_the_manager_changes_is_shown_and_passed_on(dialog, manager, disk):
    heard = []
    dialog._on_changed = lambda: heard.append("changed")
    dialog._source_combo.set_selected(1 + TIERS.index("best"))
    heard.clear()
    dialog._on_manage_clicked()
    disk.languages["best"] = ["ara", "eng"]
    manager.made[0].on_changed()
    assert dialog._manage_row.get_subtitle() == "2 language(s) downloaded for this tier" and heard == ["changed"]


def test_the_manager_does_not_open_for_the_system_source(dialog, manager):
    dialog._on_manage_clicked()
    assert manager.made == []


def test_nobody_has_to_be_told_about_the_changes(dialog, manager, disk):
    dialog._source_combo.set_selected(1)
    dialog._on_manage_clicked()
    manager.made[0].on_changed()  # No callback was given, so nothing breaks.
    assert manager.presented == [dialog]


# --- The script-detection data ---

def choose_downloaded_source(dialog, tier="fast"):
    """Select the language-data source of the quality tier ``tier`` in the dialog."""
    dialog._source_combo.set_selected(1 + TIERS.index(tier))


def test_a_mode_that_needs_the_data_the_source_lacks_offers_to_download_it(dialog, alerts, disk):
    choose_downloaded_source(dialog)
    assert alerts == []  # The default mode does not need it.
    dialog._psm_combo.set_selected(PSM_WITH_OSD)
    [(alert, parent)] = alerts
    label = PSM_OPTIONS[PSM_WITH_OSD][1]
    assert parent is dialog and alert.get_heading() == "Orientation data needed"
    assert alert.get_body() == (
        f"“{label}” uses orientation and script detection, which needs a "
        "separate data file (about 10 MB) that isn’t part of any language pack.\n\n"
        "Download it for the Fast source now? Without it, OCR in this "
        "mode will report an error until you download it or choose another mode."
    )
    assert [alert.get_response_label(r) for r in ("later", "download")] == ["Not Now", "Download"]
    assert alert.get_response_appearance("download") == Adw.ResponseAppearance.SUGGESTED
    assert alert.get_default_response() == "download" and alert.get_close_response() == "later"


def test_changing_to_a_source_that_lacks_the_data_offers_it_too(dialog, alerts, disk):
    dialog._psm_combo.set_selected(PSM_WITH_OSD)
    assert alerts == []  # The system source is left alone.
    choose_downloaded_source(dialog, "best")
    assert len(alerts) == 1 and "Best source" in alerts[0][0].get_body()


@pytest.mark.parametrize("why", ["has the data", "does not need it", "download running"])
def test_nothing_is_offered_when_it_would_be_pointless(dialog, alerts, disk, why):
    if why == "has the data":
        disk.osd.add("fast")
    if why == "download running":
        dialog._osd_download_active = True
    choose_downloaded_source(dialog)
    dialog._psm_combo.set_selected(PSM_WITH_OSD if why != "does not need it" else PSM_WITHOUT_OSD)
    assert alerts == []


def answer(alerts, response, source="fast"):
    """Answer the alert that was shown last with ``response``."""
    alert, _parent = alerts.pop()
    alert.emit("response", response)


def test_declining_the_offer_downloads_nothing(dialog, alerts, disk):
    choose_downloaded_source(dialog)
    dialog._psm_combo.set_selected(PSM_WITH_OSD)
    answer(alerts, "later")
    pump(0.2)
    assert disk.asked == [] and not dialog._osd_download_active


def test_accepting_downloads_the_data_for_that_source_and_says_so_when_it_is_there(dialog, alerts, disk):
    toasts = capture_toasts(dialog)
    choose_downloaded_source(dialog, "standard")
    dialog._psm_combo.set_selected(PSM_WITH_OSD)
    answer(alerts, "download")
    assert [(t.get_title(), t.get_timeout()) for t in toasts] == [("Downloading orientation data…", 3)]
    assert dialog._osd_download_active
    assert pump(2.0, until=lambda: len(toasts) == 2)
    assert disk.asked == [("standard", tessdata_manager.OSD_CODE)]
    assert (toasts[1].get_title(), toasts[1].get_timeout()) == ("Orientation data downloaded ✓", 3)
    assert not dialog._osd_download_active


def test_a_download_that_fails_tells_why_and_can_be_tried_again(dialog, alerts, disk):
    toasts = capture_toasts(dialog)
    disk.failure = RuntimeError("Download failed: HTTP 503")
    choose_downloaded_source(dialog)
    dialog._psm_combo.set_selected(PSM_WITH_OSD)
    answer(alerts, "download")
    assert pump(2.0, until=lambda: len(toasts) == 2)
    assert (toasts[1].get_title(), toasts[1].get_timeout()) == (
        "Could not download orientation data: Download failed: HTTP 503", 5,
    )
    assert not dialog._osd_download_active
    disk.failure = None
    dialog._psm_combo.set_selected(PSM_WITHOUT_OSD)
    dialog._psm_combo.set_selected(PSM_WITH_OSD)
    assert len(alerts) == 1


def test_a_second_acceptance_while_a_download_runs_starts_nothing(dialog, alerts, disk):
    toasts = capture_toasts(dialog)
    disk.hold.clear()
    choose_downloaded_source(dialog)
    dialog._psm_combo.set_selected(PSM_WITH_OSD)
    first = alerts.pop()[0]
    first.emit("response", "download")
    first.emit("response", "download")
    assert pump(2.0, until=lambda: disk.asked != [])
    disk.hold.set()
    assert pump(2.0, until=lambda: len(toasts) == 2)
    assert len(disk.asked) == 1


# --- Only one copy at a time ---

def test_the_dialog_is_opened_once_over_the_window_and_again_after_it_is_closed(env, disk, monkeypatch):
    _app, window = env
    presented = []
    monkeypatch.setattr(OcrSettingsDialog, "present", lambda self, parent=None: presented.append((self, parent)))
    module.present_ocr_settings(window)
    module.present_ocr_settings(window)
    assert len(presented) == 1 and presented[0][1] is window
    first = presented[0][0]
    first.emit("closed")
    module.present_ocr_settings(window)
    assert len(presented) == 2 and presented[1][0] is not first


def test_the_caller_is_told_when_a_language_source_or_pack_changes(env, disk, monkeypatch):
    _app, window = env
    heard = []
    presented = []
    monkeypatch.setattr(OcrSettingsDialog, "present", lambda self, parent=None: presented.append(self))
    module.present_ocr_settings(window, on_changed=lambda: heard.append("changed"))
    presented[0]._source_combo.set_selected(1)
    assert heard == ["changed"]
