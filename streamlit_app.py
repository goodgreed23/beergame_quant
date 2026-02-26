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

st.set_page_config(
    page_title="Beer Game Assistant",
    page_icon=None,
    layout="centered",
    initial_sidebar_state="expanded",
)

MODEL_SELECTED = "gpt-5-mini"
FALLBACK_MODEL = "gpt-4o-mini"

st.title("Beer Game Assistant")
st.write(
    "Ask strategy and concept questions for your Beer Game role."
)

openai_api_key = st.secrets["OPENAI_API_KEY"]
openai_client = OpenAI(api_key=openai_api_key)

# Initializing GCP credentials and bucket details
credentials_dict = {
    "type": st.secrets.gcs["type"],
    "project_id": st.secrets.gcs.get("project_id"),
    "client_id": st.secrets.gcs["client_id"],
    "client_email": st.secrets.gcs["client_email"],
    "private_key": st.secrets.gcs["private_key"],
    "private_key_id": st.secrets.gcs["private_key_id"],
    # Required by google-auth; default value works for standard service accounts.
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

user_pid = st.sidebar.text_input("Study ID / Team ID")
user_role = st.sidebar.text_input("Role")
selected_mode = "BeerGameQualitative"
system_prompt = MODEL_CONFIGS[selected_mode]["prompt"]
autosave_enabled = st.sidebar.checkbox("Autosave", value=True)

if "start_time" not in st.session_state:
    st.session_state["start_time"] = datetime.now()

if "messages" not in st.session_state:
    st.session_state["messages"] = [
        {
            "role": "assistant",
            "content": (
                "Hello, I am your Beer Game coach."
            ),
        }
    ]

messages = st.session_state["messages"]


def sanitize_for_filename(value):
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value.strip())


def build_system_prompt(base_prompt, role):
    role_text = role.strip() if role else ""
    if not role_text:
        return base_prompt
    return (
        f"{base_prompt}\n\n"
        f"User role in Beer Game: {role_text}.\n"
        "Tailor all guidance to this role's decisions, responsibilities, and tradeoffs."
    )


def build_welcome_message(role):
    role_text = role.strip()
    return (
        f"You are the '{role_text}'. I will help you with making decisions. "
        "Please share the current round context, incoming demand, inventory, backlog, and pipeline orders."
    )


def generate_assistant_text(messages_to_send, system_text):
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
    except BadRequestError as exc:
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


def save_conversation_to_gcp(messages_to_save, mode_key, pid, role):
    if not pid or not role:
        return None, "missing_required_fields"
    try:
        end_time = datetime.now()
        start_time = st.session_state["start_time"]
        duration = end_time - start_time

        chat_history_df = pd.DataFrame(messages_to_save)
        metadata_rows = pd.DataFrame(
            [
                {"role": "Mode", "content": mode_key},
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
        file_name = f"beergame_qualitative_P{safe_pid}_{safe_role}.csv"
        local_path = os.path.join(created_files_path, file_name)

        chat_history_df.to_csv(local_path, index=False)
        blob = bucket.blob(file_name)
        blob.upload_from_filename(local_path)
        shutil.rmtree(created_files_path, ignore_errors=True)
        return file_name, None
    except Exception as exc:
        return None, str(exc)

if user_role.strip() and st.session_state.get("welcome_role") != user_role.strip():
    st.session_state["messages"] = [{"role": "assistant", "content": build_welcome_message(user_role)}]
    st.session_state["welcome_role"] = user_role.strip()
    messages = st.session_state["messages"]
    st.session_state["start_time"] = datetime.now()

if st.sidebar.button("Save Conversation"):
    saved_file, save_error = save_conversation_to_gcp(messages, selected_mode, user_pid, user_role)
    if save_error == "missing_required_fields":
        st.sidebar.error("Enter Study ID / Team ID and Role first.")
    elif save_error:
        st.sidebar.error(f"Save failed: {save_error}")
    else:
        st.sidebar.success(f"Saved to GCP bucket as {saved_file}")

for message in messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

chat_enabled = bool(user_pid.strip()) and bool(user_role.strip())
if not chat_enabled:
    st.info("Enter Study ID / Team ID and Role in the sidebar to start chatting.")

if user_input := st.chat_input("Ask a Beer Game question...", disabled=not chat_enabled):
    messages.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    try:
        role_aware_prompt = build_system_prompt(system_prompt, user_role)
        assistant_text = generate_assistant_text(messages, role_aware_prompt)
    except Exception as exc:
        st.error(str(exc))
        st.stop()

    with st.chat_message("assistant"):
        st.write_stream(response_generator(response=assistant_text))

    messages.append({"role": "assistant", "content": assistant_text})

    if autosave_enabled:
        saved_file, save_error = save_conversation_to_gcp(messages, selected_mode, user_pid, user_role)
        if save_error == "missing_required_fields":
            st.sidebar.warning("Autosave is on. Enter Study ID / Team ID and Role to enable uploads.")
        elif save_error:
            st.sidebar.error(f"Autosave failed: {save_error}")
        else:
            st.sidebar.caption(f"Autosaved: {saved_file}")
