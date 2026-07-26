"""
state_manager.py
----------------
A tiny undo/redo-capable persistence layer on top of Streamlit's
st.session_state, backed by SQLite.

Core ideas
==========
1. Every "logical" variable you want time-travel for is registered once
   with `init_state(key, default)`.
2. You read it with `get_state(key)` and write it with `set_state(key, value)`.
3. Every REAL write (`set_state`) takes a full snapshot of *all* tracked
   variables and appends it to a history table, tagged with:
     - a per-session monotonically increasing `seq` (sequential number)
     - a `ts` unix timestamp
4. `undo()` / `redo()` just move a pointer and reload an old snapshot back
   into st.session_state -- they NEVER touch the history table, so
   navigating back and forth never pollutes the log.
5. If you `set_state` again after having undone, the redo tail (any
   snapshots with seq > current pointer) is deleted first -- this is the
   normal "branching" behavior of undo/redo systems (editors, git reflog,
   etc). History then resumes recording forward from that point, and
   redo becomes unavailable again until you undo further.
"""

import json
import sqlite3
import time
import uuid
import streamlit as st

DB_PATH = "state_history.db"  # default; override with configure(db_path=...)
_CONFIG = {"db_path": DB_PATH}


def configure(db_path=None):
    """Optional: call once, at the very top of your app, before any
    other state_manager function -- lets you point at a different
    SQLite file (or, after swapping the connection logic, a different
    backend entirely) than the module default.

        import state_manager as tt
        tt.configure(db_path="/data/my_app_history.db")
    """
    if db_path is not None:
        _CONFIG["db_path"] = db_path


# --------------------------------------------------------------------------
# low level DB helpers
# --------------------------------------------------------------------------

def _get_conn():
    # cache the connection in session_state so we don't reopen it every rerun
    if "_db_conn" not in st.session_state:
        conn = sqlite3.connect(_CONFIG["db_path"], check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS state_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                seq        INTEGER NOT NULL,
                ts         REAL NOT NULL,
                data       TEXT NOT NULL,
                login_user TEXT,
                UNIQUE(session_id, seq)
            )
        """)
        # backward-compat: if you're upgrading an existing state_history.db
        # that predates login_user, add the column instead of failing.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(state_history)").fetchall()]
        if "login_user" not in cols:
            conn.execute("ALTER TABLE state_history ADD COLUMN login_user TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_login_user ON state_history(login_user)"
        )
        conn.commit()
        st.session_state["_db_conn"] = conn
    return st.session_state["_db_conn"]


def _max_seq(conn, session_id):
    row = conn.execute(
        "SELECT MAX(seq) FROM state_history WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return row[0] or 0


def _load_snapshot(conn, session_id, seq):
    row = conn.execute(
        "SELECT data FROM state_history WHERE session_id=? AND seq=?",
        (session_id, seq),
    ).fetchone()
    return json.loads(row[0]) if row else {}


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def _bootstrap():
    """Ensure bookkeeping keys exist. Called by every public function."""
    if "_session_id" not in st.session_state:
        st.session_state["_session_id"] = str(uuid.uuid4())
    if "_tracked_keys" not in st.session_state:
        st.session_state["_tracked_keys"] = []
    if "_pointer" not in st.session_state:
        st.session_state["_pointer"] = 0  # 0 == "nothing saved yet"


def init_state(key, default):
    """Register `key` as tracked and give it a default if missing.
    Safe to call on every rerun (idempotent) -- mirrors the
    `if key not in st.session_state: ...` pattern."""
    _bootstrap()
    if key not in st.session_state["_tracked_keys"]:
        st.session_state["_tracked_keys"].append(key)
    if key not in st.session_state:
        st.session_state[key] = default
        _commit()  # first-ever snapshot for this key


def get_state(key, default=None):
    _bootstrap()
    return st.session_state.get(key, default)


def set_state(key, value):
    """The one function you call on every real, user-driven change."""
    _bootstrap()
    if key not in st.session_state["_tracked_keys"]:
        st.session_state["_tracked_keys"].append(key)
    st.session_state[key] = value
    _commit()


def set_states(mapping):
    """Like set_state, but for several keys at once -- commits ONCE.
    Perfect for a form submit: update every field's value together and
    get a single history row (with its own seq + timestamp) marking
    that the submit happened, instead of one row per field.

        with st.form("my_form"):
            name = st.text_input("Name")
            email = st.text_input("Email")
            submitted = st.form_submit_button("Submit")
        if submitted:
            set_states({
                "name": name,
                "email": email,
                "last_action": "submit",   # optional marker for auditing
            })
    """
    _bootstrap()
    for key, value in mapping.items():
        if key not in st.session_state["_tracked_keys"]:
            st.session_state["_tracked_keys"].append(key)
        st.session_state[key] = value
    _commit()


def commit_on_change(key):
    """Use this as the `on_change` callback for a widget you bind
    directly to session_state via `key=`.

    Example (multiselect):
        init_state("tags", [])
        st.multiselect(
            "Pick tags", options=["a", "b", "c"],
            key="tags", on_change=commit_on_change("tags"),
        )

    Example (text box):
        init_state("note", "")
        st.text_input("Note", key="note", on_change=commit_on_change("note"))

    Why a callback and not `set_state`: once a widget owns a key,
    Streamlit updates st.session_state[key] itself; `on_change` fires
    *after* that update, so all we need to do here is snapshot -- and
    since callbacks run before the script body re-renders, this is safe
    even though the widget with that key hasn't been redrawn yet.
    """
    def _cb():
        if key not in st.session_state["_tracked_keys"]:
            st.session_state["_tracked_keys"].append(key)
        _commit()
    return _cb


def _commit():
    """Snapshot every tracked key, discard any redo tail, append a new
    row, advance the pointer. This is the ONLY function that writes new
    rows -- undo/redo never call it."""
    conn = _get_conn()
    session_id = st.session_state["_session_id"]
    pointer = st.session_state["_pointer"]

    snapshot = {
        k: st.session_state[k]
        for k in st.session_state["_tracked_keys"]
        if k in st.session_state
    }
    data = json.dumps(snapshot, default=str)

    # Branching: throw away any "future" that existed past our current
    # position -- we're writing a new present now.
    conn.execute(
        "DELETE FROM state_history WHERE session_id=? AND seq > ?",
        (session_id, pointer),
    )
    new_seq = pointer + 1
    conn.execute(
        "INSERT INTO state_history (session_id, seq, ts, data, login_user) VALUES (?,?,?,?,?)",
        (session_id, new_seq, time.time(), data, st.session_state.get("_login_user")),
    )
    conn.commit()
    st.session_state["_pointer"] = new_seq


def can_undo():
    _bootstrap()
    return st.session_state["_pointer"] > 1


def can_redo():
    _bootstrap()
    conn = _get_conn()
    session_id = st.session_state["_session_id"]
    return st.session_state["_pointer"] < _max_seq(conn, session_id)


def undo():
    if not can_undo():
        return
    conn = _get_conn()
    session_id = st.session_state["_session_id"]
    new_pointer = st.session_state["_pointer"] - 1
    snapshot = _load_snapshot(conn, session_id, new_pointer)
    for k, v in snapshot.items():
        st.session_state[k] = v
    st.session_state["_pointer"] = new_pointer


def redo():
    if not can_redo():
        return
    conn = _get_conn()
    session_id = st.session_state["_session_id"]
    new_pointer = st.session_state["_pointer"] + 1
    snapshot = _load_snapshot(conn, session_id, new_pointer)
    for k, v in snapshot.items():
        st.session_state[k] = v
    st.session_state["_pointer"] = new_pointer


def undo_redo_widget(labels=("⏪ Undo", "⏩ Redo")):
    """Drop-in pair of buttons. Uses on_click callbacks (rather than
    `if st.button(...)`) so the state mutation happens before the script
    body re-renders -- this avoids Streamlit's "widget already
    instantiated" errors if any of your tracked keys are also bound to
    widgets via `key=`."""
    _bootstrap()
    c1, c2 = st.columns(2)
    with c1:
        st.button(labels[0], disabled=not can_undo(), on_click=undo,
                  use_container_width=True)
    with c2:
        st.button(labels[1], disabled=not can_redo(), on_click=redo,
                  use_container_width=True)


def history_debug_view():
    """Optional helper: show the raw history table for the current
    session. Handy while developing."""
    conn = _get_conn()
    session_id = st.session_state["_session_id"]
    rows = conn.execute(
        "SELECT seq, ts, data FROM state_history WHERE session_id=? ORDER BY seq",
        (session_id,),
    ).fetchall()
    st.write(f"pointer = {st.session_state['_pointer']}")
    for seq, ts, data in rows:
        marker = "→" if seq == st.session_state["_pointer"] else " "
        st.text(
            f"{marker} seq={seq}  t={time.strftime('%H:%M:%S', time.localtime(ts))}  {data}"
        )


# --------------------------------------------------------------------------
# per-user session picker
# --------------------------------------------------------------------------

def set_login_user(login_user):
    """Call right after your own auth check succeeds, before deciding
    whether to resume an old session or start a fresh one."""
    _bootstrap()
    st.session_state["_login_user"] = login_user


def list_user_sessions(login_user, limit=20):
    """One row per PAST SESSION (not per snapshot) for this user,
    newest-active first: (session_id, started_at, last_active, steps)."""
    conn = _get_conn()
    return conn.execute(
        """
        SELECT session_id,
               MIN(ts)   AS started_at,
               MAX(ts)   AS last_active,
               COUNT(*)  AS steps
        FROM state_history
        WHERE login_user = ?
        GROUP BY session_id
        ORDER BY last_active DESC
        LIMIT ?
        """,
        (login_user, limit),
    ).fetchall()


def resume_session(session_id):
    """Rehydrate the CURRENT browser tab from a previous session --
    same session_id, same pointer, same tracked keys -- so Undo/Redo
    keep working exactly as if the user never left."""
    conn = _get_conn()
    pointer = _max_seq(conn, session_id)
    snapshot = _load_snapshot(conn, session_id, pointer)
    st.session_state["_session_id"] = session_id
    st.session_state["_pointer"] = pointer
    st.session_state["_tracked_keys"] = list(snapshot.keys())
    for k, v in snapshot.items():
        st.session_state[k] = v


def login_session_picker(login_user, title="Resume a previous session?"):
    """Call this ONCE, right after login, before any init_state() calls
    for the fields you want possibly restored. Shows a modal listing
    past sessions for this user (requires Streamlit >= 1.31 for
    st.dialog); lets them resume one, or start fresh. Only asks once
    per browser tab -- re-running the script afterwards is a no-op.
    """
    set_login_user(login_user)

    if st.session_state.get("_session_choice_made"):
        return  # already decided for this browser tab

    sessions = list_user_sessions(login_user)
    if not sessions:
        st.session_state["_session_choice_made"] = True
        return

    @st.dialog(title)
    def _picker():
        st.write(f"Found **{len(sessions)}** previous session(s) for `{login_user}`:")
        for session_id, started_at, last_active, steps in sessions:
            label = (
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(last_active))} "
                f"· {steps} step(s) · started "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(started_at))}"
            )
            if st.button(label, key=f"resume_{session_id}", use_container_width=True):
                resume_session(session_id)
                st.session_state["_session_choice_made"] = True
                st.rerun()
        st.divider()
        if st.button("Start a brand-new session", use_container_width=True):
            st.session_state["_session_choice_made"] = True
            st.rerun()

    _picker()
