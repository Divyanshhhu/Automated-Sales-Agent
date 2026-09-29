import json
from pathlib import Path

import pytest

from src.config import ConfigError, load_config, validate_config


def test_valid_config_passes(valid_config: dict) -> None:
    validate_config(valid_config)


def test_shipped_product_profile_is_valid() -> None:
    path = Path(__file__).resolve().parent.parent / "config" / "product_profile.json"
    load_config(str(path))


def test_missing_sections_are_all_reported() -> None:
    with pytest.raises(ConfigError) as exc:
        validate_config({})
    message = str(exc.value)
    for section in ("product", "icp", "signal_taxonomy"):
        assert f"'{section}'" in message


def test_multiple_errors_reported_together(valid_config: dict) -> None:
    del valid_config["product"]["name"]
    valid_config["icp"]["employee_range"] = [2000, 51]
    with pytest.raises(ConfigError) as exc:
        validate_config(valid_config)
    assert "product.name" in str(exc.value)
    assert "icp.employee_range" in str(exc.value)


@pytest.mark.parametrize(
    "emp_range", [[51], [51, "2000"], "51-2000", [True, 10], [-1, 10]]
)
def test_bad_employee_range(valid_config: dict, emp_range: object) -> None:
    valid_config["icp"]["employee_range"] = emp_range
    with pytest.raises(ConfigError, match="employee_range"):
        validate_config(valid_config)


@pytest.mark.parametrize("score", [-1, 101, "50", True])
def test_bad_min_icp_fit_score(valid_config: dict, score: object) -> None:
    valid_config["icp"]["min_icp_fit_score"] = score
    with pytest.raises(ConfigError, match="min_icp_fit_score"):
        validate_config(valid_config)


def test_min_icp_fit_score_is_optional(valid_config: dict) -> None:
    del valid_config["icp"]["min_icp_fit_score"]
    validate_config(valid_config)


def test_empty_keyword_list_rejected(valid_config: dict) -> None:
    valid_config["icp"]["industry_keywords"] = []
    with pytest.raises(ConfigError, match="industry_keywords"):
        validate_config(valid_config)


def test_taxonomy_template_without_company_placeholder(valid_config: dict) -> None:
    valid_config["signal_taxonomy"]["hiring"] = "hiring sales head"
    with pytest.raises(ConfigError, match="signal_taxonomy.hiring"):
        validate_config(valid_config)


def test_taxonomy_template_with_unknown_placeholder(valid_config: dict) -> None:
    valid_config["signal_taxonomy"]["hiring"] = "{company} hiring in {city}"
    with pytest.raises(ConfigError, match="placeholders other than"):
        validate_config(valid_config)


def test_outreach_section_is_optional_and_may_be_empty(valid_config: dict) -> None:
    validate_config(valid_config)
    valid_config["outreach"] = {"sender_name": "", "signature": "", "cta_label": "", "cta_url": ""}
    validate_config(valid_config)
    valid_config["outreach"]["cta_url"] = "https://calendly.com/me/15min"
    validate_config(valid_config)


@pytest.mark.parametrize(
    ("outreach", "fragment"),
    [
        ({"cta_url": "calendly.com/me"}, "outreach.cta_url"),
        ({"cta_url": "javascript:alert(1)"}, "outreach.cta_url"),
        ({"signature": 42}, "outreach.signature"),
        ({"sender_name": "x" * 1001}, "outreach.sender_name"),
        ("not an object", "outreach must be an object"),
    ],
)
def test_bad_outreach_section(valid_config: dict, outreach: object, fragment: str) -> None:
    valid_config["outreach"] = outreach
    with pytest.raises(ConfigError, match=fragment):
        validate_config(valid_config)


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.json"))


def test_load_config_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(str(path))


def test_load_config_roundtrip(tmp_path: Path, valid_config: dict) -> None:
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(valid_config), encoding="utf-8")
    assert load_config(str(path)) == valid_config
