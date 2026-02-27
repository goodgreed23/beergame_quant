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

selected_mode = "BeerGameQuantitative"
system_prompt = MODEL_CONFIGS[selected_mode]["prompt"]

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


def generate_assistant_text(messages_to_send, system_text: str) -> str:
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
        )
        return response.output_text
    except BadRequestError:
        st.sidebar.warning(
            f"Model '{MODEL_SELECTED}' failed for this request. Retrying with '{FALLBACK_MODEL}'."
        )
        fallback_response = openai_client.responses.create(
            model=FALLBACK_MODEL,
            input=response_input,
        )
        return fallback_response.output_text
    except Exception as exc:
        raise RuntimeError(f"Assistant request failed: {exc}") from exc


def save_conversation_to_gcp(messages_to_save, mode_key: str, pid: str, role: str, section: str):
    if not pid or not role or not section:
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

        created_files_path = f"conv_history_P{pid}"
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
        assistant_text = generate_assistant_text(st.session_state["messages"], role_aware_prompt)
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    with st.chat_message("assistant"):
        st.write_stream(response_generator(response=assistant_text))

    st.session_state["messages"].append({"role": "assistant", "content": assistant_text})

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
