"""Shared OpenAI call for structured (JSON-schema) output, with retries on
transient failures. Used by memo and outreach-email generation.
"""
import json
import os

from openai import APIConnectionError, APITimeoutError, InternalServerError, OpenAI, RateLimitError

from .retry import call_with_retry

MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")


def is_retriable_openai_error(exc: Exception) -> bool:
    return isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError))


def call_structured(
    *, instructions: str, user_content: str, schema: dict, schema_name: str, context: str
) -> dict:
    """One model call whose output must match `schema`. Returns the parsed JSON."""
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    def _do_call():
        return client.responses.create(
            model=MODEL,
            instructions=instructions,
            input=user_content,
            text={"format": {"type": "json_schema", "name": schema_name, "strict": True, "schema": schema}},
        )

    response = call_with_retry(_do_call, is_retriable=is_retriable_openai_error, context=context)
    return json.loads(response.output_text)
