import copy
import json
from pathlib import Path

import pytest

from src.web.forms import FORM_ERROR, config_to_form, field_for_error, form_to_config, group_errors

STARTER = json.loads(
    (Path(__file__).resolve().parent.parent / "config" / "product_profile.json").read_text(encoding="utf-8")
)


def test_roundtrip_preserves_full_config() -> None:
    config, errors = form_to_config(config_to_form(STARTER), {})
    assert errors == {}
    assert config == STARTER


def test_empty_outreach_is_not_added_to_a_profile_without_one() -> None:
    config, _ = form_to_config(config_to_form(STARTER), STARTER)
    assert "outreach" not in config


def test_likely_need_phrases_field() -> None:
    base = {k: v for k, v in copy.deepcopy(STARTER).items() if k != "signals"}
    form = config_to_form(base)
    assert form["signals.likely_need_phrases"] == ""
    config, _ = form_to_config(form, base)
    assert "signals" not in config  # left empty: not added

    form["signals.likely_need_phrases"] = "telecaller\n\n click-to-whatsapp "
    config, errors = form_to_config(form, base)
    assert errors == {}
    assert config["signals"] == {"likely_need_phrases": ["telecaller", "click-to-whatsapp"]}


def test_outreach_roundtrip_keeps_multiline_signature() -> None:
    base = {**copy.deepcopy(STARTER), "outreach": {
        "sender_name": "Divyanshu", "signature": "Divyanshu\nFounder", "cta_label": "", "cta_url": "https://cal.com/x",
    }}
    form = config_to_form(base)
    form["outreach.signature"] = "Divyanshu\r\nFounder, InvisibleCTO"
    config, errors = form_to_config(form, base)
    assert errors == {}
    assert config["outreach"]["signature"] == "Divyanshu\nFounder, InvisibleCTO"
    assert config["outreach"]["cta_url"] == "https://cal.com/x"


def test_unknown_keys_in_base_survive_an_edit() -> None:
    base = copy.deepcopy(STARTER)
    base["icp"]["future_field"] = "keep me"
    base["notes"] = {"owner": "me"}
    config, _ = form_to_config(config_to_form(base), base)
    assert config["icp"]["future_field"] == "keep me"
    assert config["notes"] == {"owner": "me"}


def test_list_fields_split_lines_and_drop_blanks() -> None:
    form = config_to_form(STARTER)
    form["icp.geographies"] = "  Mumbai \n\n Pune\r\n"
    config, _ = form_to_config(form, {})
    assert config["icp"]["geographies"] == ["Mumbai", "Pune"]


def test_taxonomy_query_may_contain_colons() -> None:
    form = config_to_form(STARTER)
    form["signal_taxonomy"] = "tech_adoption: {company} site:linkedin.com CRM"
    config, errors = form_to_config(form, {})
    assert errors == {}
    assert config["signal_taxonomy"] == {"tech_adoption": "{company} site:linkedin.com CRM"}


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("no colon here", "must look like"),
        ("Bad Name: {company} x", "must look like"),
        ("empty_query:", "must look like"),
        ("dup: {company} a\ndup: {company} b", "appears twice"),
    ],
)
def test_taxonomy_parse_errors(text: str, fragment: str) -> None:
    form = config_to_form(STARTER)
    form["signal_taxonomy"] = text
    _, errors = form_to_config(form, {})
    assert any(fragment in e for e in errors["signal_taxonomy"])


def test_non_numeric_employee_range_is_a_parse_error() -> None:
    form = config_to_form(STARTER)
    form["icp.employee_min"] = "fifty"
    _, errors = form_to_config(form, {})
    assert "icp.employee_range" in errors


def test_empty_min_score_falls_back_to_default() -> None:
    form = config_to_form(STARTER)
    form["icp.min_icp_fit_score"] = ""
    config, errors = form_to_config(form, STARTER)
    assert errors == {}
    assert "min_icp_fit_score" not in config["icp"]


def test_decimal_min_score_kept_integer_when_whole() -> None:
    form = config_to_form(STARTER)
    form["icp.min_icp_fit_score"] = "55.0"
    config, _ = form_to_config(form, {})
    assert config["icp"]["min_icp_fit_score"] == 55


@pytest.mark.parametrize(
    ("message", "field"),
    [
        ("icp.employee_range must satisfy 0 <= min <= max", "icp.employee_range"),
        ("product.name must be a non-empty string", "product.name"),
        ("signal_taxonomy.hiring must be a string containing '{company}'", "signal_taxonomy"),
        ("missing required section 'icp'", FORM_ERROR),
        ("config must be a JSON object", FORM_ERROR),
    ],
)
def test_field_for_error(message: str, field: str) -> None:
    assert field_for_error(message) == field


def test_group_errors() -> None:
    grouped = group_errors(["product.name must be x", "product.name must be y", "weird"])
    assert grouped == {
        "product.name": ["product.name must be x", "product.name must be y"],
        FORM_ERROR: ["weird"],
    }
