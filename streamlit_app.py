import streamlit as st
import pandas as pd
import os
import shutil
import json
import re
from datetime import datetime
from openai import OpenAI, BadRequestError

from google.cloud import storage
from google.oauth2.service_account import Credentials

from models import MODEL_CONFIGS
from utils.prompt_utils import build_structured_output_instruction
from utils.utils import response_generator

# ----------------------------
# Page config
# ----------------------------
st.set_page_config(
    page_title="Beer Game Assistant",
    page_icon=None,
    layout="centered",
    initial_sidebar_state="expanded",
)

MODEL_SELECTED = "gpt-5-mini"
FALLBACK_MODEL = "gpt-4o-mini"

st.title("Beer Game Assistant")
st.write("Ask strategy and concept questions for your Beer Game role.")

# ----------------------------
# OpenAI client
# ----------------------------
openai_api_key = st.secrets["OPENAI_API_KEY"]
openai_client = OpenAI(api_key=openai_api_key)

# ----------------------------
# GCP setup
# ----------------------------
credentials_dict = {
    "type": st.secrets.gcs["type"],
    "project_id": st.secrets.gcs.get("project_id"),
    "client_id": st.secrets.gcs["client_id"],
    "client_email": st.secrets.gcs["client_email"],
    "private_key": st.secrets.gcs["private_key"],
    "private_key_id": st.secrets.gcs["private_key_id"],
    "token_uri": st.secrets.gcs.get("token_uri", "https://oauth2.googleapis.com/token"),
}
credentials_dict["private_key"] = credentials_dict["private_key"].replace("\\n", "\n")

try:
    credentials = Credentials.from_service_account_info(credentials_dict)
    client = storage.Client(credentials=credentials, project="beer-game-488600")
    bucket = client.get_bucket("beergame1")
except Exception as exc:
    st.error(f"GCP setup failed: {exc}")
    st.stop()

# ----------------------------
# Session state init
# ----------------------------
if "start_time" not in st.session_state:
    st.session_state["start_time"] = datetime.now()

if "messages" not in st.session_state:
    st.session_state["messages"] = [
        {"role": "assistant", "content": "Hello, I am your Beer Game coach."}
    ]

if "selected_section" not in st.session_state:
    st.session_state["selected_section"] = "OPMGT 301 A"

if "selected_role" not in st.session_state:
    st.session_state["selected_role"] = ""

if "welcome_role" not in st.session_state:
    st.session_state["welcome_role"] = ""

# Lock role after the first USER message is sent
if "role_locked" not in st.session_state:
    st.session_state["role_locked"] = False

messages = st.session_state["messages"]

# ----------------------------
# Sidebar inputs (Section -> PID -> Role)
# ----------------------------

st.sidebar.markdown("### Instruction")
st.sidebar.info("When you use the chatbot, ...")

SECTION_OPTIONS = ["OPMGT 301 A", "OPMGT 301 B", "OPMGT 301 C"]

section_index = 0
if st.session_state["selected_section"] in SECTION_OPTIONS:
    section_index = SECTION_OPTIONS.index(st.session_state["selected_section"])

user_section = st.sidebar.selectbox(
    "Section",
    SECTION_OPTIONS,
    index=section_index,
    help="Select your class section.",
)
st.session_state["selected_section"] = user_section

user_pid = st.sidebar.text_input("Canvas Group Number")

ROLE_OPTIONS = ["Retailer", "Wholesaler", "Distributor", "Factory"]

role_disabled = (not bool(user_pid.strip())) or st.session_state["role_locked"]

# Selectbox index should reflect previously selected role when possible
role_index = 0
if st.session_state["selected_role"] in ROLE_OPTIONS:
    role_index = ROLE_OPTIONS.index(st.session_state["selected_role"])

user_role = st.sidebar.selectbox(
    "Role",
    ROLE_OPTIONS,
    index=role_index,
    disabled=role_disabled,
    help="Enter Canvas Group Number first. Role will lock after your first message.",
)

selected_mode = "BeerGameQualitative"
system_prompt = MODEL_CONFIGS[selected_mode]["prompt"]

STRUCTURED_RESPONSE_KEYS = [
    "quantitative_reasoning",
    "qualitative_reasoning",
    "short_quantitative_reasoning",
    "short_qualitative_reasoning",
    "quantitative_answer",
    "qualitative_answer",
]

# ----------------------------
# Helpers
# ----------------------------
def sanitize_for_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())


def build_system_prompt(base_prompt: str, role: str) -> str:
    role_text = role.strip() if role else ""
    if not role_text:
        return base_prompt
    return (
        f"{base_prompt}\n\n"
        f"User role in Beer Game: {role_text}.\n"
        "Tailor all guidance to this role's decisions, responsibilities, and tradeoffs."
    )


def build_welcome_message(role: str) -> str:
    role_text = role.strip()
    return (
        f"You are the '{role_text}'. I will help you with making decisions. "
        "Please share the current round context, incoming demand, inventory, backlog, and pipeline orders."
    )


def extract_first_json_object(raw_text: str) -> dict:
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    first_brace = raw_text.find("{")
    last_brace = raw_text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = raw_text[first_brace : last_brace + 1]
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("Model response was not valid JSON.")


def validate_structured_response(payload: dict) -> dict:
    clean_payload = {}
    for key in STRUCTURED_RESPONSE_KEYS:
        value = payload.get(key, "")
        clean_payload[key] = str(value).strip()

    if not clean_payload["short_quantitative_reasoning"]:
        clean_payload["short_quantitative_reasoning"] = clean_payload[
            "quantitative_reasoning"
        ][:240].strip()

    if not clean_payload["short_qualitative_reasoning"]:
        clean_payload["short_qualitative_reasoning"] = clean_payload[
            "qualitative_reasoning"
        ][:240].strip()

    if not re.fullmatch(r"-?\d+", clean_payload["quantitative_answer"]):
        raise ValueError("quantitative_answer must be a single exact integer with no extra text.")

    if re.search(r"\d", clean_payload["qualitative_answer"]):
        raise ValueError("qualitative_answer must be directional only and must not include exact numbers.")

    return clean_payload


def build_user_visible_reply(payload: dict) -> str:
    return (
        f"**Short quantitative reasoning:** {payload['short_quantitative_reasoning']}\n\n"
        f"**Short qualitative reasoning:** {payload['short_qualitative_reasoning']}\n\n"
        f"**Quantitative answer:** {payload['quantitative_answer']}\n\n"
        f"**Qualitative answer:** {payload['qualitative_answer']}"
    )


def generate_assistant_payload(messages_to_send, system_text: str, mode_key: str) -> dict:
    structured_output_instruction = build_structured_output_instruction(mode_key)

    response_input = [{"role": "system", "content": system_text}]
    response_input.append({"role": "system", "content": structured_output_instruction})
    response_input.extend(
        {"role": msg["role"], "content": msg["content"]}
        for msg in messages_to_send
        if msg["role"] in ("user", "assistant")
    )

    try:
        response = openai_client.responses.create(
            model=MODEL_SELECTED,
            input=response_input,
            reasoning = {"effort": "minimal"},
        )
        payload = extract_first_json_object(response.output_text)
        return validate_structured_response(payload)
    except BadRequestError:
        st.sidebar.warning(
            f"Model '{MODEL_SELECTED}' failed for this request. Retrying with '{FALLBACK_MODEL}'."
        )
        fallback_response = openai_client.responses.create(
            model=FALLBACK_MODEL,
            input=response_input,
        )
        payload = extract_first_json_object(fallback_response.output_text)
        return validate_structured_response(payload)
    except Exception as exc:
        raise RuntimeError(f"Assistant request failed: {exc}") from exc


def save_conversation_to_gcp(messages_to_save, mode_key: str, pid: str, role: str, section: str):
    if not pid or not role or not section:
        return None, "missing_required_fields"
    try:
        end_time = datetime.now()
        end_time_str = end_time.strftime("%Y%m%d_%H%M%S")
        start_time = st.session_state["start_time"]
        duration = end_time - start_time

        chat_history_df = pd.DataFrame(messages_to_save)
        metadata_rows = pd.DataFrame(
            [
                {"role": "Mode", "content": mode_key},
                {"role": "Section", "content": section},
                {"role": "Participant Role", "content": role},
                {"role": "Start Time", "content": start_time},
                {"role": "End Time", "content": end_time},
                {"role": "Duration", "content": duration},
            ]
        )
        chat_history_df = pd.concat([chat_history_df, metadata_rows], ignore_index=True)

        created_files_path = f"conv_history_P{pid}"
        os.makedirs(created_files_path, exist_ok=True)

        safe_pid = sanitize_for_filename(pid)
        safe_role = sanitize_for_filename(role)
        safe_section = sanitize_for_filename(section)

        file_name = f"beergame_qualitative_{safe_section}_P{safe_pid}_{safe_role}_{end_time_str}.csv"
        local_path = os.path.join(created_files_path, file_name)

        chat_history_df.to_csv(local_path, index=False)
        blob = bucket.blob(file_name)
        blob.upload_from_filename(local_path)

        shutil.rmtree(created_files_path, ignore_errors=True)
        return file_name, None
    except Exception as exc:
        return None, str(exc)


def save_structured_response_to_gcp(
    structured_payload: dict,
    mode_key: str,
    pid: str,
    role: str,
    section: str,
    user_input: str,
):
    if not pid or not role or not section:
        return None, "missing_required_fields"
    try:
        created_files_path = f"structured_output_P{pid}"
        os.makedirs(created_files_path, exist_ok=True)

        safe_pid = sanitize_for_filename(pid)
        safe_role = sanitize_for_filename(role)
        safe_section = sanitize_for_filename(section)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        file_name = (
            f"beergame_qualitative_structured_{safe_section}_P{safe_pid}_{safe_role}_{timestamp}.json"
        )
        local_path = os.path.join(created_files_path, file_name)

        payload_to_save = {
            "mode": mode_key,
            "section": section,
            "pid": pid,
            "role": role,
            "timestamp": datetime.now().isoformat(),
            "user_input": user_input,
            "assistant_output": structured_payload,
        }
        with open(local_path, "w", encoding="utf-8") as f:
            json.dump(payload_to_save, f, indent=2, ensure_ascii=False)

        blob = bucket.blob(file_name)
        blob.upload_from_filename(local_path)

        shutil.rmtree(created_files_path, ignore_errors=True)
        return file_name, None
    except Exception as exc:
        return None, str(exc)

# ----------------------------
# Role selection behavior:
# - Only reset messages when role changes AND role is not locked
# - Once locked, role cannot be changed (selectbox disabled)
# ----------------------------
if (not st.session_state["role_locked"]) and user_role and (user_role != st.session_state["selected_role"]):
    st.session_state["selected_role"] = user_role
    st.session_state["messages"] = [{"role": "assistant", "content": build_welcome_message(user_role)}]
    st.session_state["welcome_role"] = user_role
    st.session_state["start_time"] = datetime.now()
    messages = st.session_state["messages"]

# ----------------------------
# Manual save button (optional)
# ----------------------------
if st.sidebar.button("End Conversation"):
    saved_file, save_error = save_conversation_to_gcp(
        messages,
        selected_mode,
        user_pid.strip(),
        st.session_state["selected_role"].strip(),
        st.session_state["selected_section"].strip(),
    )
    if save_error == "missing_required_fields":
        st.sidebar.error("Select Section, enter Canvas Group Number, and select a Role first.")
    elif save_error:
        st.sidebar.error(f"Save failed: {save_error}")
    else:
        st.sidebar.success(f"Saved to GCP bucket as {saved_file}")

# ----------------------------
# Render chat history
# ----------------------------
for message in st.session_state["messages"]:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# Require Section + PID + role before chatting
chat_enabled = (
    bool(st.session_state["selected_section"].strip())
    and bool(user_pid.strip())
    and bool(st.session_state["selected_role"].strip())
)
if not chat_enabled:
    st.info("Select a Section, enter Canvas Group Number, and select a Role in the sidebar to start chatting.")

# ----------------------------
# Chat input -> assistant -> autosave ALWAYS
# Also: lock role after the first user message
# ----------------------------
if user_input := st.chat_input("Ask a Beer Game question...", disabled=not chat_enabled):
    # Append user message
    st.session_state["messages"].append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Lock role after first user message (now that they started chatting)
    st.session_state["role_locked"] = True

    # Generate assistant response
    try:
        role_aware_prompt = build_system_prompt(system_prompt, st.session_state["selected_role"])
        assistant_payload = generate_assistant_payload(
            st.session_state["messages"],
            role_aware_prompt,
            selected_mode,
        )
        assistant_text = build_user_visible_reply(assistant_payload)
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    with st.chat_message("assistant"):
        st.write_stream(response_generator(response=assistant_text))

    st.session_state["messages"].append(
        {
            "role": "assistant",
            "content": assistant_text,
            "assistant_output": assistant_payload,
        }
    )

    # Autosave ALWAYS
    saved_file, save_error = save_conversation_to_gcp(
        st.session_state["messages"],
        selected_mode,
        user_pid.strip(),
        st.session_state["selected_role"].strip(),
        st.session_state["selected_section"].strip(),
    )
    if save_error == "missing_required_fields":
        st.sidebar.warning("Select Section, enter Canvas Group Number, and select a Role to enable uploads.")
    elif save_error:
        st.sidebar.error(f"Autosave failed: {save_error}")
    else:
        st.sidebar.caption(f"Autosaved: {saved_file}")

    structured_file, structured_error = save_structured_response_to_gcp(
        assistant_payload,
        selected_mode,
        user_pid.strip(),
        st.session_state["selected_role"].strip(),
        st.session_state["selected_section"].strip(),
        user_input,
    )
    if structured_error == "missing_required_fields":
        st.sidebar.warning("Missing fields for structured JSON upload.")
    elif structured_error:
        st.sidebar.error(f"Structured JSON upload failed: {structured_error}")
    else:
        st.sidebar.caption(f"Structured JSON uploaded: {structured_file}")
