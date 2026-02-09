# main.py
from __future__ import annotations

import html
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify

from openai import OpenAI

import config

app = Flask(__name__)


# -------------------------
# Logging
# -------------------------
def debug(msg: str) -> None:
    if getattr(config, "DEBUG", False):
        print(f"[DEBUG] {msg}")


# -------------------------
# Validation helpers
# -------------------------
def require_runtime_config() -> None:
    missing = []

    api_key = os.getenv("OPENAI_API_KEY", "").strip() or config.OPENAI_API_KEY.strip()
    if not api_key or "YOUR_OPENAI_API_KEY" in api_key:
        missing.append("OPENAI_API_KEY (env or config.py)")

    if not config.MODEL:
        missing.append("MODEL")

    if not config.VECTOR_STORE_ID or "vs_" not in config.VECTOR_STORE_ID:
        missing.append("VECTOR_STORE_ID")

    az = config.AZURE_DEVOPS
    if not az.get("org"):
        missing.append("AZURE_DEVOPS.org")
    if not az.get("project"):
        missing.append("AZURE_DEVOPS.project")
    if not az.get("pat"):
        missing.append("AZURE_DEVOPS.pat")

    if missing:
        raise RuntimeError("Missing required settings: " + ", ".join(missing))


def extract_json_object(text: str) -> Dict[str, Any]:
    text = (text or "").strip()

    # direct
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
        raise ValueError("Top-level JSON must be an object/dict.")
    except Exception:
        pass

    # fallback: first {...}
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not m:
        raise ValueError("No JSON object found in model output.")
    candidate = m.group(0)

    obj = json.loads(candidate)
    if not isinstance(obj, dict):
        raise ValueError("Extracted JSON is not an object/dict.")
    return obj


def ensure_step_defaults(payload: Dict[str, Any]) -> None:
    """
    Make output robust:
    - Fill missing step ids (1, 2, ...)
    - Ensure 'keyword' and 'params' exist with sane defaults
    This prevents API failures due to minor model formatting misses.
    """
    steps = payload.get("automated_steps")
    if not isinstance(steps, list):
        return

    for i, st in enumerate(steps, start=1):
        if not isinstance(st, dict):
            continue

        # Fill missing id
        if not st.get("id"):
            st["id"] = f"S{i:03d}"

        # Ensure required keys exist
        if "keyword" not in st or st["keyword"] is None:
            st["keyword"] = ""
        if "params" not in st or st["params"] is None:
            st["params"] = []
        elif not isinstance(st["params"], list):
            st["params"] = [st["params"]]


def validate_payload(payload: Dict[str, Any]) -> None:
    missing = [k for k in config.REQUIRED_TOP_LEVEL_KEYS if k not in payload]
    if missing:
        raise ValueError(f"Output JSON missing required top-level keys: {missing}")


    steps = payload.get("automated_steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Output JSON must include non-empty 'automated_steps' list.")

    for i, st in enumerate(steps):
        if not isinstance(st, dict):
            raise ValueError(f"AutomatedStep[{i}] must be an object.")
        for k in config.REQUIRED_STEP_KEYS:
            if k not in st:
                raise ValueError(f"AutomatedStep[{i}] missing '{k}'.")
        if not isinstance(st.get("params"), list):
            raise ValueError(f"AutomatedStep[{i}].params must be a list.")


# -------------------------
# Azure DevOps
# -------------------------
def azure_auth_header(pat: str) -> Dict[str, str]:
    # Azure DevOps uses Basic auth with PAT as password and empty username.
    import base64
    token = base64.b64encode(f":{pat}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}


def fetch_azure_testcase_workitem(tc_id: int) -> Dict[str, Any]:
    az = config.AZURE_DEVOPS
    org = az["org"]
    project = az["project"]
    base_url = az.get("base_url", "https://dev.azure.com").rstrip("/")
    api_version = az.get("api_version", "7.1-preview.3")

    url = f"{base_url}/{org}/{project}/_apis/wit/workitems/{tc_id}"
    params = {"api-version": api_version, "$expand": "fields"}

    headers = {
        **azure_auth_header(az["pat"]),
        "Accept": "application/json",
    }

    debug(f"Fetching Azure work item {tc_id} ...")
    r = requests.get(url, headers=headers, params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Azure fetch failed ({r.status_code}): {r.text}")

    return r.json()


def parse_steps_from_tcm_field(steps_field_value: str) -> List[Dict[str, str]]:
    """
    Robust Azure DevOps Test Case steps parser.
    Handles:
      - steps stored as raw XML
      - steps stored as HTML-escaped XML
      - undefined HTML entities (&nbsp; etc.) that break XML parsers
      - HTML tags embedded inside parameterizedString (use itertext)
    Returns list of {action, expected}.
    """
    if not steps_field_value:
        return []

    import xml.etree.ElementTree as ET
    import html as _html
    import re as _re

    def _clean(x: str) -> str:
        x = x or ""
        x = _html.unescape(x)
        # remove HTML tags and normalize whitespace
        x = _re.sub(r"<[^>]+>", " ", x)
        x = _re.sub(r"\s+", " ", x).strip()
        return x

    def _sanitize_for_xml(s: str) -> str:
        # Remove control chars
        s = _re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", s)

        # Escape non-XML entities
        s = _re.sub(
            r"&(?!(lt|gt|amp|apos|quot);|#\d+;|#x[0-9A-Fa-f]+;)",
            "&amp;",
            s,
            flags=_re.IGNORECASE
        )
        return s

    def _try_parse(xml_text: str) -> Optional[ET.Element]:
        try:
            return ET.fromstring(xml_text)
        except Exception:
            return None

    raw = (steps_field_value or "").strip()

    # Strategy A: parse as raw XML first
    root = _try_parse(raw)

    # Strategy B: if escaped or failed, unescape + sanitize + parse
    if root is None or "&lt;steps" in raw.lower():
        unescaped = _html.unescape(raw)
        m = _re.search(r"(<steps\b.*?</steps>)", unescaped, flags=_re.DOTALL | _re.IGNORECASE)
        xml_blob = m.group(1) if m else unescaped
        xml_blob = _sanitize_for_xml(xml_blob)
        root = _try_parse(xml_blob)

    if root is None:
        return []

    out: List[Dict[str, str]] = []

    for step in root.findall(".//step"):
        ps = step.findall("./parameterizedString")

        def text_of(elem) -> str:
            if elem is None:
                return ""
            return "".join(elem.itertext())

        action_raw = text_of(ps[0]) if len(ps) > 0 else ""
        expected_raw = text_of(ps[1]) if len(ps) > 1 else ""

        action = _clean(action_raw)
        expected = _clean(expected_raw)

        if action or expected:
            out.append({"action": action, "expected": expected})

    return out


def compile_nl_tc_from_azure(tc_id: int, wi: Dict[str, Any]) -> Tuple[str, Dict[str, Any], List[Dict[str, str]]]:
    fields = wi.get("fields", {}) or {}
    title = fields.get("System.Title", f"Azure TC {tc_id}")

    steps_field = fields.get(config.AZURE_STEPS_FIELD, "") or ""
    steps = parse_steps_from_tcm_field(steps_field)

    # Build:
    # - azure_steps_structured: exact extracted steps (action/expected) in order
    # - tc_nl: TEST_CASE_NL-like string (one action per line) used as conversion input
    azure_steps_structured: List[Dict[str, str]] = []
    lines: List[str] = []

    for s in steps:
        a = (s.get("action") or "").strip()
        e = (s.get("expected") or "").strip()

        if not a and not e:
            continue

        azure_steps_structured.append({
            "action": a,
            "expected": e
        })

        # Conversion input: keep it close to your original TEST_CASE_NL style (actions only)
        if a:
            lines.append(a)

    tc_nl = "".join([ln for ln in lines if ln]).strip()

    tc_meta = {
        "azure_tc_id": tc_id,
        "azure_title": title,
        "azure_url": wi.get("url"),
        "project": config.AZURE_DEVOPS.get("project"),
        "org": config.AZURE_DEVOPS.get("org"),
    }
    return tc_nl, tc_meta, azure_steps_structured


# -------------------------
# OpenAI conversion
# -------------------------
def check_vector_store(client: OpenAI) -> None:
    debug(f"Checking vector store: {config.VECTOR_STORE_ID}")
    client.vector_stores.retrieve(config.VECTOR_STORE_ID)
    files_page = client.vector_stores.files.list(vector_store_id=config.VECTOR_STORE_ID, limit=10)
    files = getattr(files_page, "data", None) or []
    if not files:
        raise RuntimeError(f"Vector store '{config.VECTOR_STORE_ID}' has no files.")
    debug(f"Vector store OK. Files found (sample): {len(files)}")


def build_user_prompt(tc_nl: str, tc_meta: Dict[str, Any], azure_steps: List[Dict[str, str]]) -> str:
    return config.USER_PROMPT_TEMPLATE.format(
        tc_nl=tc_nl.strip(),
        azure_steps_json=json.dumps(azure_steps, ensure_ascii=False, indent=2),
        tc_meta=json.dumps(tc_meta, ensure_ascii=False, indent=2),
    ).strip()


def run_conversion(client: OpenAI, tc_nl: str, tc_meta: Dict[str, Any], azure_steps: List[Dict[str, str]]) -> Dict[str, Any]:
    tools = [{"type": "file_search", "vector_store_ids": [config.VECTOR_STORE_ID]}]

    create_kwargs: Dict[str, Any] = {
        "model": config.MODEL,
        "input": [
            {"role": "system", "content": config.SYSTEM_PROMPT.strip()},
            {"role": "user", "content": build_user_prompt(tc_nl, tc_meta, azure_steps)},
        ],
        "tools": tools,
        "temperature": config.LLM_TEMPERATURE,
        "top_p": config.LLM_TOP_P,
    }

    # top_k may be ignored; keep config for your settings UI
    if getattr(config, "LLM_TOP_K", None) is not None:
        debug("Note: LLM_TOP_K is set in config, but may be ignored (not supported by OpenAI Responses API).")

    debug("Calling OpenAI Responses API...")
    resp = client.responses.create(**create_kwargs)

    output_text = getattr(resp, "output_text", None) or str(resp)

    payload = extract_json_object(output_text)

    # Ensure missing step ids / keys are repaired before validation
    ensure_step_defaults(payload)
    # Merge Azure steps (action/expected) into references.steps without overwriting existing references
    refs = payload.get("references")
    if not isinstance(refs, dict):
        refs = {}
    # Preserve existing references fields and add/overwrite only references["steps"]
    refs["steps"] = list(azure_steps)
    payload["references"] = refs

    # Remove any accidental schema-meta fields (required keys should never be printed)
    payload.pop("required_top_level_keys", None)
    payload.pop("required_keys", None)

    # Remove any top-level azure_steps (we only keep them under references.steps)
    payload.pop("azure_steps", None)

    validate_payload(payload)
    return payload


# -------------------------
# API
# -------------------------
@app.get("/api/convert/<int:azure_tc_id>")
def api_convert(azure_tc_id: int):
    try:
        require_runtime_config()

        api_key = os.getenv("OPENAI_API_KEY", "").strip() or config.OPENAI_API_KEY.strip()
        client = OpenAI(api_key=api_key)

        check_vector_store(client)

        wi = fetch_azure_testcase_workitem(azure_tc_id)
        tc_nl, tc_meta, azure_steps = compile_nl_tc_from_azure(azure_tc_id, wi)

        if not tc_nl.strip():
            fields = wi.get("fields", {}) or {}
            steps_raw = (fields.get(config.AZURE_STEPS_FIELD) or "")
            return jsonify({
                "error": "Azure TC has no readable steps in Microsoft.VSTS.TCM.Steps (or parsing failed).",
                "azure_tc_id": azure_tc_id,
                "azure_title": fields.get("System.Title"),
                "steps_field_present": bool(steps_raw),
                "steps_field_preview": steps_raw[:500],
            }), 400

        converted = run_conversion(client, tc_nl, tc_meta, azure_steps)
        return jsonify(converted), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def main():
    port = int(os.getenv("PORT", "8006"))
    debug(f"Starting API on port {port}")
    app.run(host="127.0.0.1", port=port, debug=config.DEBUG)


if __name__ == "__main__":
    main()
