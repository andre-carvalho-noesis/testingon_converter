# config.py
from __future__ import annotations

DEBUG = True

# =========================================================
# OpenAI / Vector Store
# =========================================================
OPENAI_API_KEY = ""
MODEL = "gpt-4o"  # or another Responses-capable model you use
VECTOR_STORE_ID = "vs_...."  # your vector store id

# =========================================================
# LLM sampling / retrieval settings (exposed in settings)
# =========================================================
# NOTE:
# - temperature + top_p are supported broadly.
# - top_k is NOT reliably supported by OpenAI Responses API; we keep it configurable
#   and main.py will ignore it safely if unsupported.
LLM_TEMPERATURE = 0.2
LLM_TOP_P = 0.9
LLM_TOP_K = 40  # stored for your settings UI; may be ignored depending on model/provider

# How many results file_search should consider (the tool controls retrieval internally,
# but we keep this as a knob for future prompt strategy).
FILE_SEARCH_HINT = {
    "preferred_focus": ["keywords", "objects", "Deterministic_mapping_rules"],
    "notes": "Used only as guidance in prompts/logs; file_search retrieval is managed by the tool."
}

# =========================================================
# Azure DevOps integration settings (exposed in settings)
# =========================================================
AZURE_DEVOPS = {
    "org": "",
    "project": '',
    "pat": "",
    "base_url": "https://dev.azure.com",  # usually this
    "api_version": "7.1-preview.3",
}

# Which field contains steps in the Azure Test Case work item
AZURE_STEPS_FIELD = "Microsoft.VSTS.TCM.Steps"

# =========================================================
# Output requirements (server-side validation only)
# =========================================================
# Required keys are for validation ONLY; they must NOT be printed as a list in output.
REQUIRED_TOP_LEVEL_KEYS = [
    "name",
    "description",
    "preconditions",
    "tags",
    "automated_steps",
    "artifacts",
    "references",
    "diagnostics",
]
# shape constraints for each step (validation only)
REQUIRED_STEP_KEYS = ["id", "keyword", "params"]

# =========================================================
# Documents contract (matches your CURRENT files in the vector store)
# =========================================================
DOCS_CONTRACT = """
You have access (via File Search) to ONLY these documents:

(1) objects.pdf (table):
- Columns include: application, page_name, object_name, xpath, xpath_ios.
- You MUST reference UI elements ONLY by the tuple:
  Application + Page_name + Object_name
- Never output xpaths.
- Prefer objects where xpath_ios is NOT NULL for cross-platform steps.
- If the same (page_name, object_name) exists for both Application values:
  choose the one from Application "Revamp_BCP" (priority rule).

(2) keywords.pdf (table):
- This is the authoritative catalog of allowed TestingOn keywords.
- It defines the EXACT parameter order by columns: parameter1, parameter2, ..., parameterN.
- You MUST follow the parameter order exactly as listed for that keyword.
- If the keyword shows quoted literals in the table (e.g., "x"):
  it means the value is literal text when used.
- IMPORTANT: Use only keywords that exist in this table.

(3) Deterministic_mapping_rules.doc (instructions):
- Contains deterministic mapping rules from NL steps to TestingOn steps.
- These rules are MANDATORY and OVERRIDE generic interpretation.
- If a rule applies, you MUST emit exactly the specified keyword sequence.

If ambiguous, search keywords.pdf for the keyword row and apply its parameter columns exactly.
If the mapping rules document defines a sequence for a step, follow that sequence first.
"""

# =========================================================
# Prompt (System)
# =========================================================
SYSTEM_PROMPT = f"""
You are a senior mobile test automation engineer specialized in TestingOn.

{DOCS_CONTRACT}

Goal:
Convert a natural-language test case into a robust automated TestingOn test case.

Non-negotiable rules:
- Use ONLY keywords that exist in keywords.pdf.
- Use ONLY objects that exist in objects.pdf, referenced as:
  [Application, Page_name, Object_name]
- You MAY add extra automation steps (waits, validations, navigation) beyond the original NL TC,
  because automation often requires more granular actions.
- Cross-platform: produce automation that can run on Android and iOS where possible.
  Prefer objects where xpath_ios is not NULL.

Deterministic mapping rules (from Deterministic_mapping_rules.doc) — MUST FOLLOW:
- All automated tests MUST start with:
  noReset()

- When the NL step mentions opening the app (e.g., "Abrir App", "Abrir a App", "Open App", "Launch app"):
  emit:
  openApp("App_BCP")
  Notes:
  - For openApp, the Application parameter value is ALWAYS "App_BCP".

- When the NL step mentions login/authentication (e.g., "Login", "Entrar", "FaceId", "TouchId", "PIN", "Autenticação"):
  emit a click-sequence (in this order) using a CLICK-type keyword from keywords.pdf:
  1) click-type keyword for the button that identifies "login"
  2) click-type keyword for the button "1"
  3) click-type keyword for the button "4"
  4) click-type keyword for the button "7"
  5) click-type keyword for the button "8"
  Notes:
  - The keyword used must perform a click (prefer forceClick if available).
  - For these login clicks, Application should be "Revamp_BCP" (except openApp which is App_BCP).
  - If NL says "PIN/Touch/Face ID", you still apply this login sequence.

Selection rules:
- When choosing an object for a given Page_name/Object_name, prefer Application="Revamp_BCP" if it exists.
- When a mapping rule says "btn that identifies button X", choose the object_name in objects.pdf that best matches X
  (e.g., contains 'login', or is clearly the numeric keypad button object).
- Do NOT invent objects: must exist in objects.pdf.

Output format:
- Return ONLY valid JSON (no markdown, no explanation text).
- The JSON MUST contain ALL these top-level keys (in this order):
  {", ".join(REQUIRED_TOP_LEVEL_KEYS)}
- IMPORTANT: Do NOT include a field that lists required keys\. Only include the required content fields themselves\.

Original steps field requirements:
- "references"."steps" MUST be an array of objects extracted from Azure DevOps, in the original order.
- Each object in references.steps MUST have: """ + str({"action": "<string>", "expected": "<string>"}) + """ (expected can be empty).
- Do NOT translate or rewrite references.steps; keep the original action/expected content.

Step object requirements:
- Each item in "automated_steps" MUST be an object with:
  - "id": unique integer, sequential like 1, 2, ...
  - "keyword": string (must exist in keywords.pdf)
  - "params": array (ordered exactly as keywords.pdf parameter1..parameterN defines)
- Optional fields per step: "comment", "expected", "on_fail"

Representation rule:
- Even if mapping rules show steps like Keyword(A,B,C), your output MUST represent them as JSON objects, e.g.:
  """ + str({"id": 1, "keyword": "forceClick", "params": ["Revamp_BCP","Login","btn_login"]}) + """
  (parameter order must follow keywords.pdf)

References requirements:
- references must include Azure metadata fields already provided.
- references.steps MUST be an array of objects in the original Azure order, each with: {"action": "...", "expected": "..."}.
- Do NOT include azure_steps at the top-level.

Diagnostics requirements:
- diagnostics.keywords_used (list)
- diagnostics.objects_used (list)
- diagnostics.assumptions (list)

Quality requirements:
- Add stability steps when navigating (waitForObject / isDisplayed / elementExists, etc.) using available keywords.
- For final validation steps, use available validation keywords (isDisplayed / pageContainsText / elementExists, etc.)
  and existing objects; do not invent elements.
"""

# =========================================================
# Prompt (User template)
# =========================================================
# main.py will inject the Azure-extracted NL steps into {tc_nl}
USER_PROMPT_TEMPLATE = """
Convert this natural-language test case to TestingOn automation.

Natural-language TC:
{tc_nl}

Original steps (from Azure, exact order):

Azure metadata (JSON):
{tc_meta}

Azure steps (JSON, original from Azure DevOps) — must be copied into output as references.steps:
{azure_steps_json}

Guidance:
- Generate a short, clear, descriptive name.
- Expand steps as needed for automation stability.
- Use objects/keywords ONLY from the vector store documents.
- Ensure keyword params order follows keywords.pdf parameter1..parameterN strictly.
- IMPORTANT: Apply the deterministic sequences from Deterministic_mapping_rules.doc exactly when they match a NL step.
"""