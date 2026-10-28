# SPDX-FileCopyrightText: 2026 AbuKaram01
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The OCR choices and how saved settings become a Tesseract command line (core/ocr_options.py)."""

import types

import pytest

from karamvt.core import ocr, ocr_options
from karamvt.core.ocr import (
    DEFAULT_OEM,
    DEFAULT_PSM,
    OEM_OPTIONS,
    PSM_OPTIONS,
    TESS_TOGGLES,
    normalize_psm,
    oem_index,
    parse_custom_params,
    psm_index,
    psm_needs_osd,
    tess_vars_from_config,
)


def test_the_options_are_defined_in_their_own_module_and_still_importable_from_ocr():
    for name in ("PSM_OPTIONS", "OEM_OPTIONS", "TESS_TOGGLES", "DEFAULT_PSM", "DEFAULT_OEM", "normalize_psm",
                 "psm_index", "oem_index", "psm_needs_osd", "parse_custom_params", "tess_vars_from_config"):
        assert getattr(ocr, name) is getattr(ocr_options, name), name


def test_every_option_has_a_value_a_short_label_and_a_description():
    for table in (PSM_OPTIONS, OEM_OPTIONS):
        values = [value for value, _label, _description in table]
        assert len(values) == len(set(values))
        assert all(label and description for _value, label, description in table)


def test_the_defaults_are_among_the_options():
    assert DEFAULT_PSM in [value for value, _l, _d in PSM_OPTIONS]
    assert DEFAULT_OEM in [value for value, _l, _d in OEM_OPTIONS]


def test_the_modes_that_produce_no_text_are_not_offered():
    assert not {0, 2} & {value for value, _l, _d in PSM_OPTIONS}


@pytest.mark.parametrize("value, expected", [(6, 6), ("4", 4), (13, 13), (0, DEFAULT_PSM), (2, DEFAULT_PSM),
                                              (99, DEFAULT_PSM), ("x", DEFAULT_PSM), (None, DEFAULT_PSM)])
def test_a_page_mode_that_is_not_offered_becomes_the_default(value, expected):
    assert normalize_psm(value) == expected


def test_the_index_of_a_mode_is_its_place_in_the_table():
    assert [psm_index(value) for value, _l, _d in PSM_OPTIONS] == list(range(len(PSM_OPTIONS)))
    assert psm_index(2) == psm_index(DEFAULT_PSM)  # Not offered: the default's place.
    assert [oem_index(value) for value, _l, _d in OEM_OPTIONS] == list(range(len(OEM_OPTIONS)))
    assert oem_index(42) == oem_index(DEFAULT_OEM)


@pytest.mark.parametrize("value, needs", [(1, True), (12, True), (3, False), (6, False), (13, False)])
def test_only_two_page_modes_load_the_osd_data(value, needs):
    assert psm_needs_osd(value) is needs


@pytest.mark.parametrize("raw, pairs", [
    ("", []),
    ("chop_enable=0", [("chop_enable", "0")]),
    (" a = 1 ; b=2 ", [("a", "1"), ("b", "2")]),
    ("a=1\nb=2;c=3", [("a", "1"), ("b", "2"), ("c", "3")]),
    ("novalue; =5; 9bad=1; has space=1; ok_1=x=y", [("ok_1", "x=y")]),  # Only the first "=" splits.
    (";;\n;", []),
])
def test_custom_parameters_are_split_and_what_is_malformed_is_skipped(raw, pairs):
    assert parse_custom_params(raw) == pairs


def config(**changes):
    values = {"ocr_preserve_spaces": False, "ocr_edges_max_children_enabled": False,
              "ocr_edges_max_children_value": 40, "ocr_custom_params": ""}
    values.update({field: False for field, _variable in TESS_TOGGLES})
    values.update(changes)
    return types.SimpleNamespace(**values)


def test_nothing_switched_on_means_no_variables():
    assert tess_vars_from_config(config()) == []


def test_each_switch_that_is_on_adds_its_variable_in_order():
    saved = config(ocr_preserve_spaces=True, ocr_chop_enable=True, ocr_enable_new_segsearch=True,
                   ocr_edges_max_children_enabled=True, ocr_edges_max_children_value=12.0)
    assert tess_vars_from_config(saved) == [
        ("preserve_interword_spaces", "1"), ("chop_enable", "1"), ("enable_new_segsearch", "1"),
        ("edges_max_children_per_outline", "12"),
    ]


def test_custom_parameters_come_last_so_that_they_can_override_a_switch():
    saved = config(ocr_chop_enable=True, ocr_custom_params="chop_enable=0")
    assert tess_vars_from_config(saved) == [("chop_enable", "1"), ("chop_enable", "0")]


def test_the_command_line_has_the_modes_the_folder_and_each_variable():
    assert ocr_options.build_config(6, 3, None, []) == "--psm 6 --oem 3"
    assert ocr_options.build_config(1, 0, "/data/fast", [("a", "1"), ("b", "x y")]) == (
        "--psm 1 --oem 0 --tessdata-dir /data/fast -c a=1 -c 'b=x y'"  # Quoted, as shlex.split reads it back.
    )
