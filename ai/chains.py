"""LangChain chains (LCEL): prompt | model | parser."""
import json
import re

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser, StrOutputParser

from . import prompts, providers
from .schemas import ScreeningResult

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def clean_text(text):
    """Strip reasoning blocks and stray code fences that some open models add."""
    text = _THINK.sub("", text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def extract_json(text):
    """Return the first balanced {...} object in a model reply as a JSON string."""
    text = clean_text(text)
    start = text.find("{")
    if start < 0:
        raise OutputParserException(f"No JSON object found in model output: {text[:200]!r}")
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            esc = (ch == "\\" and not esc)
            if ch == '"' and not esc:
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise OutputParserException("Unterminated JSON object in model output")


# --------------------------------------------------------------------------- JD

def jd_chain(revise=False):
    prompt = prompts.JD_REVISE if revise else prompts.JD_GENERATE
    return prompt | providers.get_chat_model("jd") | StrOutputParser()


# --------------------------------------------------------------------------- screening

def _parse(parser, raw):
    data = json.loads(extract_json(raw))
    return parser.pydantic_object.model_validate(data)


def screen_structured(inputs):
    """Run the screening prompt and return a validated ScreeningResult.

    Paid providers with native structured output use with_structured_output(). Everything
    else (free HF models, local pipelines, Ollama) gets JSON format instructions in the prompt,
    a tolerant parser, and one automatic repair attempt if the JSON is malformed.
    """
    llm = providers.get_chat_model("screen")
    parser = PydanticOutputParser(pydantic_object=ScreeningResult)

    if providers.supports_native_structured_output():
        chain = prompts.SCREEN.partial(format_instructions="") | llm.with_structured_output(ScreeningResult)
        return chain.invoke(inputs)

    fmt = parser.get_format_instructions()
    raw = (prompts.SCREEN.partial(format_instructions=fmt) | llm | StrOutputParser()).invoke(inputs)
    try:
        return _parse(parser, raw)
    except (OutputParserException, ValueError) as first_error:
        repaired = (prompts.JSON_REPAIR | llm | StrOutputParser()).invoke(
            {"format_instructions": fmt, "error": str(first_error)[:500], "output": raw[:6000]})
        return _parse(parser, repaired)
