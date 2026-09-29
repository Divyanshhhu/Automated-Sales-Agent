"""Converts between a profile config and the profile edit form.

Form field names are the config's dotted paths ("icp.geographies"), so
validation errors from config.validate_config -- which start with that same
path -- can be shown next to the field they belong to. List fields are
textareas with one entry per line; the signal taxonomy is one
"category: query template" per line.
"""
import copy
import re
from collections.abc import Mapping

FORM_ERROR = "_form"  # errors that don't belong to a single field

# textarea fields holding a list, one entry per line
LIST_FIELDS = (
    "product.differentiators",
    "product.competitors",
    "icp.industries",
    "icp.industry_keywords",
    "icp.geographies",
    "icp.roles",
)
TEXT_FIELDS = ("product.name", "product.description", "product.problem_solved", "product.pricing")
# optional section: only written to the config if something is filled in
OUTREACH_FIELDS = ("outreach.sender_name", "outreach.signature", "outreach.cta_label", "outreach.cta_url")
# where each form field's errors are shown; employee min/max share one
ERROR_FIELDS = (
    *TEXT_FIELDS,
    *OUTREACH_FIELDS,
    *LIST_FIELDS,
    "icp.employee_range",
    "icp.min_icp_fit_score",
    "signal_taxonomy",
)
_CATEGORY_RE = re.compile(r"^[a-z0-9_]+$")


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _get(config: dict, path: str) -> object:
    section, key = path.split(".", 1)
    value = config.get(section)
    return value.get(key) if isinstance(value, dict) else None


def _set(config: dict, path: str, value: object) -> None:
    section, key = path.split(".", 1)
    if not isinstance(config.get(section), dict):
        config[section] = {}
    config[section][key] = value


def config_to_form(config: dict) -> dict[str, str]:
    """Current values for every form input, as strings."""
    values: dict[str, str] = {}
    for path in (*TEXT_FIELDS, *OUTREACH_FIELDS):
        value = _get(config, path)
        values[path] = value if isinstance(value, str) else ""
    for path in LIST_FIELDS:
        value = _get(config, path)
        values[path] = "\n".join(str(v) for v in value) if isinstance(value, list) else ""

    emp_range = _get(config, "icp.employee_range")
    if isinstance(emp_range, list) and len(emp_range) == 2:
        values["icp.employee_min"], values["icp.employee_max"] = str(emp_range[0]), str(emp_range[1])
    else:
        values["icp.employee_min"] = values["icp.employee_max"] = ""
    score = _get(config, "icp.min_icp_fit_score")
    values["icp.min_icp_fit_score"] = "" if score is None else str(score)

    taxonomy = config.get("signal_taxonomy")
    values["signal_taxonomy"] = (
        "\n".join(f"{k}: {v}" for k, v in taxonomy.items()) if isinstance(taxonomy, dict) else ""
    )
    return values


def _parse_number(text: str) -> int | float:
    number = float(text)
    return int(number) if number.is_integer() else number


def form_to_config(form: Mapping[str, str], base: dict) -> tuple[dict, dict[str, list[str]]]:
    """Builds a config from submitted form values on top of `base`, so keys
    the form doesn't know about survive an edit.

    Returns (config, parse_errors). Parse errors cover input that can't even
    be turned into config values (e.g. "abc" as an employee count); the
    config itself still needs validate_config afterwards.
    """
    config = copy.deepcopy(base)
    errors: dict[str, list[str]] = {}

    for path in TEXT_FIELDS:
        _set(config, path, form.get(path, "").strip())
    outreach = {
        path.split(".", 1)[1]: form.get(path, "").replace("\r\n", "\n").strip() for path in OUTREACH_FIELDS
    }
    if any(outreach.values()) or "outreach" in base:
        config["outreach"] = {**(base.get("outreach") or {}), **outreach}
    for path in LIST_FIELDS:
        _set(config, path, _lines(form.get(path, "")))

    emp_min, emp_max = form.get("icp.employee_min", "").strip(), form.get("icp.employee_max", "").strip()
    try:
        _set(config, "icp.employee_range", [int(emp_min), int(emp_max)])
    except ValueError:
        errors.setdefault("icp.employee_range", []).append(
            "Employee range needs whole numbers for both min and max"
        )

    score_text = form.get("icp.min_icp_fit_score", "").strip()
    if not score_text:
        config.get("icp", {}).pop("min_icp_fit_score", None)  # falls back to the default (50)
    else:
        try:
            _set(config, "icp.min_icp_fit_score", _parse_number(score_text))
        except ValueError:
            errors.setdefault("icp.min_icp_fit_score", []).append("Minimum score must be a number")

    taxonomy: dict[str, str] = {}
    for line in _lines(form.get("signal_taxonomy", "")):
        category, sep, template = line.partition(":")
        category, template = category.strip(), template.strip()
        if not sep or not _CATEGORY_RE.match(category) or not template:
            errors.setdefault("signal_taxonomy", []).append(
                f"Line {line!r} must look like 'category_name: query with {{company}}'"
            )
        elif category in taxonomy:
            errors.setdefault("signal_taxonomy", []).append(f"Category {category!r} appears twice")
        else:
            taxonomy[category] = template
    config["signal_taxonomy"] = taxonomy

    return config, errors


def field_for_error(message: str) -> str:
    """Which form field a validate_config message belongs to, from its
    leading dotted path; FORM_ERROR if it isn't about a single field.
    """
    path = message.split(" ", 1)[0]
    if path.startswith("signal_taxonomy"):
        return "signal_taxonomy"
    return path if path in ERROR_FIELDS else FORM_ERROR


def group_errors(messages: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for message in messages:
        grouped.setdefault(field_for_error(message), []).append(message)
    return grouped
