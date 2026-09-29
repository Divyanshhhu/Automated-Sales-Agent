"""Loads and validates product_profile.json.

The config is human-edited, so a typo or missing key should fail up front
with a readable message listing every problem -- not as a raw KeyError deep
inside discovery after API credits have already been spent.
"""
import json
from typing import Any


class ConfigError(ValueError):
    """Raised when a config is missing, unparseable, or invalid.

    `errors` lists each problem separately, each starting with the dotted
    path of the offending field (e.g. "icp.employee_range must ..."), so a
    form can show every message next to its own field.
    """

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or [message]


def _check_str(section: dict, key: str, path: str, errors: list[str]) -> None:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}.{key} must be a non-empty string")


def _check_str_list(section: dict, key: str, path: str, errors: list[str]) -> None:
    value = section.get(key)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v.strip() for v in value)
    ):
        errors.append(f"{path}.{key} must be a non-empty list of non-empty strings")


def _validate_product(product: Any, errors: list[str]) -> None:
    if not isinstance(product, dict):
        errors.append("product must be an object")
        return
    for key in ("name", "description", "problem_solved"):
        _check_str(product, key, "product", errors)
    _check_str_list(product, "differentiators", "product", errors)


def _validate_icp(icp: Any, errors: list[str]) -> None:
    if not isinstance(icp, dict):
        errors.append("icp must be an object")
        return
    for key in ("industry_keywords", "geographies"):
        _check_str_list(icp, key, "icp", errors)

    emp_range = icp.get("employee_range")
    if (
        not isinstance(emp_range, list)
        or len(emp_range) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in emp_range)
    ):
        errors.append("icp.employee_range must be a list of two integers [min, max]")
    elif not 0 <= emp_range[0] <= emp_range[1]:
        errors.append("icp.employee_range must satisfy 0 <= min <= max")

    if "min_icp_fit_score" in icp:
        score = icp["min_icp_fit_score"]
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 100:
            errors.append("icp.min_icp_fit_score must be a number between 0 and 100")


def _validate_signal_taxonomy(taxonomy: Any, errors: list[str]) -> None:
    if not isinstance(taxonomy, dict) or not taxonomy:
        errors.append("signal_taxonomy must be a non-empty object of category -> query template")
        return
    for category, template in taxonomy.items():
        if not isinstance(template, str) or "{company}" not in template:
            errors.append(f"signal_taxonomy.{category} must be a string containing '{{company}}'")
            continue
        try:
            template.format(company="x")
        except (KeyError, IndexError, ValueError):
            errors.append(
                f"signal_taxonomy.{category} has placeholders other than '{{company}}' "
                "(escape literal braces as '{{' and '}}')"
            )


def validate_config(config: Any) -> None:
    """Raises ConfigError listing every problem found, or returns None if valid."""
    if not isinstance(config, dict):
        raise ConfigError("config must be a JSON object")
    errors: list[str] = []
    for section in ("product", "icp", "signal_taxonomy"):
        if section not in config:
            errors.append(f"missing required section '{section}'")
    if "product" in config:
        _validate_product(config["product"], errors)
    if "icp" in config:
        _validate_icp(config["icp"], errors)
    if "signal_taxonomy" in config:
        _validate_signal_taxonomy(config["signal_taxonomy"], errors)
    if errors:
        raise ConfigError("Invalid config:\n  - " + "\n  - ".join(errors), errors)


def load_config(config_path: str) -> dict:
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON ({config_path}): {exc}") from exc
    validate_config(config)
    return config
