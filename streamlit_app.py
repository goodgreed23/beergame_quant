import streamlit as st
import pandas as pd
import os
import shutil
from datetime import datetime
from openai import OpenAI, BadRequestError

from google.cloud import storage
from google.oauth2.service_account import Credentials

from models import MODEL_CONFIGS
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

st.markdown(
    """
    <style>
        section[data-testid="stSidebar"] {
            width: 420px !important;
        }
        section[data-testid="stSidebar"] > div:first-child {
            width: 420px !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

MODEL_SELECTED = "gpt-5-mini"
FALLBACK_MODEL = "gpt-4o-mini"

st.title("Beer Game Assistant")
st.write("Ask ordering strategy questions for your Beer Game role.")

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
# Constants
# ----------------------------
SECTION_OPTIONS = ["OPMGT 301 A", "OPMGT 301 B", "OPMGT 301 C"]
ROLE_PLACEHOLDER = "Select your role..."
ROLE_OPTIONS = [ROLE_PLACEHOLDER, "Retailer", "Wholesaler", "Distributor", "Factory"]

selected_mode = "BeerGameQuantitative"
system_prompt = MODEL_CONFIGS[selected_mode]["prompt"]

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
    st.session_state["selected_section"] = SECTION_OPTIONS[0]

if "selected_role" not in st.session_state:
    st.session_state["selected_role"] = ROLE_PLACEHOLDER

if "welcome_role" not in st.session_state:
    st.session_state["welcome_role"] = ""

# Lock role after the first USER message is sent
if "role_locked" not in st.session_state:
    st.session_state["role_locked"] = False

# Persist PID in session state (prevents weird rerun behavior)
if "pid" not in st.session_state:
    st.session_state["pid"] = ""

# ----------------------------
# Helpers
# ----------------------------
def sanitize_for_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())


def build_system_prompt(base_prompt: str, role: str) -> str:
    role_text = role.strip() if role else ""
    if not role_text or role_text == ROLE_PLACEHOLDER:
        return base_prompt
    return (
        f"{base_prompt}\n\n"
        f"User role in Beer Game: {role_text}.\n"
        "Tailor all guidance to this role's decisions, responsibilities, and tradeoffs."
    )


def build_welcome_message(role: str) -> str:
    role_text = role.strip()
    return (
        f"You are the '{role_text}'. I will help you with making ordering decisions.\n\n"
        "Please share the current week’s context including **Week, Demand, Inv/Bk (inventory or backlog), "
        "Incoming shipment, Relevant recent orders**."
    )


def generate_assistant_text(messages_to_send, system_text: str) -> str:
    """
    Plain, non-JSON call:
    - system message includes your base + role guidance
    - then we send the running chat history (user/assistant)
    """
    response_input = [{"role": "system", "content": system_text}]
    response_input.extend(
        {"role": msg["role"], "content": msg["content"]}
        for msg in messages_to_send
        if msg["role"] in ("user", "assistant")
    )

    try:
        response = openai_client.responses.create(
            model=MODEL_SELECTED,
            input=response_input,
            reasoning={"effort": "medium"},
        )
        text = (response.output_text or "").strip()
        if not text:
            raise RuntimeError("Empty response from model.")
        return text
    except BadRequestError:
        st.sidebar.warning(
            f"Model '{MODEL_SELECTED}' failed for this request. Retrying with '{FALLBACK_MODEL}'."
        )
        fallback_response = openai_client.responses.create(
            model=FALLBACK_MODEL,
            input=response_input,
        )
        text = (fallback_response.output_text or "").strip()
        if not text:
            raise RuntimeError("Empty response from fallback model.")
        return text
    except Exception as exc:
        raise RuntimeError(f"Assistant request failed: {exc}") from exc


def save_conversation_to_gcp(messages_to_save, mode_key: str, pid: str, role: str, section: str):
    if not pid or not role or not section or role == ROLE_PLACEHOLDER:
        return None, "missing_required_fields"
    try:
        end_time = datetime.now()
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

        created_files_path = f"conv_history_P{sanitize_for_filename(pid)}"
        os.makedirs(created_files_path, exist_ok=True)

        safe_pid = sanitize_for_filename(pid)
        safe_role = sanitize_for_filename(role)
        safe_section = sanitize_for_filename(section)

        file_name = f"beergame_quantitative_{safe_section}_P{safe_pid}_{safe_role}.csv"
        local_path = os.path.join(created_files_path, file_name)

        chat_history_df.to_csv(local_path, index=False)
        blob = bucket.blob(file_name)
        blob.upload_from_filename(local_path)

        shutil.rmtree(created_files_path, ignore_errors=True)
        return file_name, None
    except Exception as exc:
        return None, str(exc)

# ----------------------------
# Sidebar inputs (Section -> PID -> Role)
# ----------------------------
st.sidebar.markdown("### Instruction")
st.sidebar.info(
    """
When you use the Beer Game Assistant, keep the following in mind:

- Do **not** change your role mid-game (the assistant will reset).
- Do **not** refresh the page (the assistant will reset).
- Responses may take a moment—please be patient.
- For best advice, share the current week’s context during each interaction. This can include **Week, Demand, Inv/Bk (inventory or backlog), Incoming shipment, Relevant recent orders**.
- If something looks wrong or you hit a technical issue, **raise your hand**.
"""
)

# Section
section_index = (
    SECTION_OPTIONS.index(st.session_state["selected_section"])
    if st.session_state["selected_section"] in SECTION_OPTIONS
    else 0
)
st.sidebar.selectbox(
    "Section",
    SECTION_OPTIONS,
    index=section_index,
    help="Select your class section.",
    key="selected_section",
)

# PID (Canvas Group Number)
st.sidebar.text_input("Canvas Group Number", key="pid")

# Role (requires PID; locks after first message)
role_disabled = (not bool(st.session_state["pid"].strip())) or st.session_state["role_locked"]

role_index = (
    ROLE_OPTIONS.index(st.session_state["selected_role"])
    if st.session_state["selected_role"] in ROLE_OPTIONS
    else 0
)
st.sidebar.selectbox(
    "Role",
    ROLE_OPTIONS,
    index=role_index,
    disabled=role_disabled,
    key="selected_role",
    help="Enter Canvas Group Number first. Role will lock after your first message.",
)

# ----------------------------
# Role selection behavior:
# - Only reset messages when role changes AND role is not locked
# - Once locked, role cannot be changed (selectbox disabled)
# - Ignore placeholder as a 'real' role
# ----------------------------
current_role = st.session_state["selected_role"]
if (not st.session_state["role_locked"]) and (current_role != st.session_state.get("welcome_role", "")):
    if current_role != ROLE_PLACEHOLDER:
        st.session_state["messages"] = [{"role": "assistant", "content": build_welcome_message(current_role)}]
        st.session_state["welcome_role"] = current_role
        st.session_state["start_time"] = datetime.now()

# ----------------------------
# Manual save button (optional)
# ----------------------------
if st.sidebar.button("End Conversation"):
    saved_file, save_error = save_conversation_to_gcp(
        st.session_state["messages"],
        selected_mode,
        st.session_state["pid"].strip(),
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

# ----------------------------
# Require Section + PID + role before chatting
# (explicit placeholder check prevents default Retailer behavior)
# ----------------------------
chat_enabled = (
    bool(st.session_state["selected_section"].strip())
    and bool(st.session_state["pid"].strip())
    and (st.session_state["selected_role"] != ROLE_PLACEHOLDER)
)

if not chat_enabled:
    st.info("Select a Section, enter Canvas Group Number, and select a Role in the sidebar to start chatting.")

# ----------------------------
# Chat input -> assistant -> autosave ALWAYS (CSV)
# Also: lock role after the first user message
# ----------------------------
if user_input := st.chat_input("Ask a Beer Game question...", disabled=not chat_enabled):
    # Append user message
    st.session_state["messages"].append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    # Lock role after first user message (now that they started chatting)
    st.session_state["role_locked"] = True

    # Generate assistant response (plain text)
    try:
        role_aware_prompt = build_system_prompt(system_prompt, st.session_state["selected_role"])
        assistant_text = generate_assistant_text(
            st.session_state["messages"],
            role_aware_prompt,
        )
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    with st.chat_message("assistant"):
        st.write_stream(response_generator(response=assistant_text))

    st.session_state["messages"].append(
        {
            "role": "assistant",
            "content": assistant_text,
        }
    )

    # Autosave ALWAYS (CSV)
    saved_file, save_error = save_conversation_to_gcp(
        st.session_state["messages"],
        selected_mode,
        st.session_state["pid"].strip(),
        st.session_state["selected_role"].strip(),
        st.session_state["selected_section"].strip(),
    )
    if save_error == "missing_required_fields":
        st.sidebar.warning("Select Section, enter Canvas Group Number, and select a Role to enable uploads.")
    elif save_error:
        st.sidebar.error(f"Autosave failed: {save_error}")
    else:
        st.sidebar.caption(f"Autosaved: {saved_file}")
