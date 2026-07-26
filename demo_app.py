import streamlit as st
from state_manager import (
    init_state, get_state, set_state, set_states, commit_on_change,
    undo_redo_widget, history_debug_view, login_session_picker,
)

st.title("Time-travel counter demo")

# --- fake login (swap for your real auth) --------------------------------
if "login_user" not in st.session_state:
    st.session_state["login_user"] = st.text_input("Username", value="alice")
    #st.stop()

login_user = st.session_state["login_user"]

# Ask ONCE per browser tab whether to resume a past session for this user.
login_session_picker(login_user)

st.caption(f"Logged in as **{login_user}**")

# --- your original example, rewritten with the new API -------------------
init_state("total", 0)

col1, col2 = st.columns(2)
with col1:
    if st.button("+1", use_container_width=True):
        set_state("total", get_state("total") + 1)
with col2:
    if st.button("-1", use_container_width=True):
        set_state("total", get_state("total") - 1)

st.metric("Total", get_state("total"))

st.divider()

# --- widget-bound state: multiselect + text box, both undo-able --------
init_state("tags", [])
st.multiselect(
    "Tags",
    options=["red", "green", "blue", "yellow", "purple"],
    key="tags",
    on_change=commit_on_change("tags"),
)

init_state("note", "")
st.text_input(
    "Note",
    key="note",
    on_change=commit_on_change("note"),
)

st.divider()

# --- form submit: batched into ONE history row -------------------------
init_state("profile_name", "")
init_state("profile_email", "")
with st.form("profile_form"):
    name = st.text_input("Name", value=get_state("profile_name"))
    email = st.text_input("Email", value=get_state("profile_email"))
    submitted = st.form_submit_button("Save profile")
if submitted:
    set_states({
        "profile_name": name,
        "profile_email": email,
        "last_action": "profile_submit",
    })
    st.success("Saved.")

st.divider()
undo_redo_widget()

with st.expander("history (debug)"):
    history_debug_view()
