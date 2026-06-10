import streamlit as st
import random
import re
import scrython
import time
from collections import Counter

st.set_page_config(page_title="MTG Opening Hand Simulator", layout="wide")
st.title("🃏 MTG Opening Hand Simulator")

# ─── Scryfall helpers ────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def fetch_card(name):
    """Fetch card data from Scryfall via Scrython.

    Returns a plain dict with the fields we need (including a "_error" key
    if all lookups failed, for debugging). Works across Scrython versions
    by reading individual accessor methods.
    """
    def attr(card, field):
        """Read a Scrython field that may be a method (1.x) or property (2.x)."""
        try:
            val = getattr(card, field)
        except Exception:
            return None
        if callable(val):
            try:
                return val()
            except Exception:
                return None
        return val

    def build(card):
        d = {}
        d["type_line"] = attr(card, "type_line") or ""
        cmc = attr(card, "cmc")
        # ensure plain float/int, not a numpy or object type
        d["cmc"] = float(cmc) if isinstance(cmc, (int, float)) else None
        d["name"] = attr(card, "name") or name
        # Reduce card_faces to a list of plain dicts with just the fields we use.
        faces = attr(card, "card_faces")
        if faces:
            face_list = []
            for f in faces:
                if isinstance(f, dict):
                    tl = f.get("type_line", "")
                else:
                    tl = getattr(f, "type_line", "") or ""
                face_list.append({"type_line": str(tl)})
            d["card_faces"] = face_list
        return d

    # Normalize: strip whitespace, convert curly apostrophes to straight,
    # and drop anything after a "//" (some exports use "Front // Back").
    clean = name.strip().replace("\u2019", "'").replace("\u2018", "'")
    clean = clean.split("//")[0].strip()

    last_err = None
    candidates = [name, clean] if clean != name else [name]
    for cand in candidates:
        for kwargs in ({"exact": cand}, {"fuzzy": cand}):
            for attempt in range(3):
                try:
                    card = scrython.cards.Named(**kwargs)
                    return build(card)
                except Exception as e:
                    last_err = e
                    msg = str(e).lower()
                    if "rate" in msg or "429" in msg:
                        # Respect Scryfall: wait before retrying
                        time.sleep(2.0 * (attempt + 1))
                        continue
                    else:
                        # Not a rate-limit error (e.g. card not found) -> stop retrying this query
                        break
    return {"_error": repr(last_err), "name": name}


def _face_field(face, field):
    """Read a field from a card face that may be a dict or an object."""
    if isinstance(face, dict):
        return face.get(field, "")
    return getattr(face, field, "")


def get_type_line(card_data):
    """Get type line, handling double-faced cards (use front face)."""
    type_line = card_data.get("type_line", "")
    if not type_line and card_data.get("card_faces"):
        type_line = _face_field(card_data["card_faces"][0], "type_line")
    return type_line or ""


def is_land(card_data):
    """A card is a land if its type line contains 'Land' (front face for DFCs)."""
    return "Land" in get_type_line(card_data)


def get_cmc(card_data):
    """Get CMC. For DFCs the top-level cmc is authoritative if present."""
    cmc = card_data.get("cmc")
    if cmc is not None:
        return cmc
    # fallback: front face mana cost
    if "card_faces" in card_data:
        face = card_data["card_faces"][0]
        mana = face.get("mana_cost", "")
        # count pips roughly — just return None and let caller handle
        return None
    return None


# ─── Decklist Parser ─────────────────────────────────────────────────────────

def parse_decklist(text):
    deck = []
    errors = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        if re.match(r'^(sideboard|deck|commander|companion)$', line, re.I):
            continue
        m = re.match(r'^(\d+)[xX]?\s+(.+)$', line)
        if m:
            count, name = int(m.group(1)), m.group(2).strip()
            deck.extend([name] * count)
        else:
            errors.append(line)
    return deck, errors


# ─── Session state init ───────────────────────────────────────────────────────

if "card_info" not in st.session_state:
    st.session_state.card_info = {}   # name -> scryfall data or None
if "groups" not in st.session_state:
    st.session_state.groups = {}      # group_name -> set of card names
if "clauses" not in st.session_state:
    st.session_state.clauses = []     # list of dicts: {group, op, value}
if "deck" not in st.session_state:
    st.session_state.deck = []
if "unique_cards" not in st.session_state:
    st.session_state.unique_cards = []

# ─── Sidebar: Decklist ───────────────────────────────────────────────────────

with st.sidebar:
    st.header("📋 Decklist")
    decklist_text = st.text_area(
        "Paste your decklist",
        height=280,
        placeholder="4 Lightning Bolt\n4 Goblin Guide\n20 Mountain\n...",
    )
    load_btn = st.button("Load & Fetch Card Data", type="primary", use_container_width=True)

    st.divider()
    st.header("⚙️ Simulation")
    n_sims = st.slider("Simulations", 1000, 100000, 10000, step=1000)
    hand_size = st.number_input("Hand size", 1, 7, 7)

if load_btn and decklist_text.strip():
    fetch_card.clear()  # clear cached failures from previous attempts
    deck, errors = parse_decklist(decklist_text)
    unique = list(dict.fromkeys(deck))  # preserve order, unique

    progress = st.sidebar.progress(0, text="Fetching card data from Scryfall…")
    card_info = {}
    for i, name in enumerate(unique):
        card_info[name] = fetch_card(name)
        time.sleep(0.15)  # ~6-7 req/sec, safely under Scryfall's 10/sec limit
        progress.progress((i + 1) / len(unique), text=f"Fetching: {name}")
    progress.empty()

    # Auto-build groups — check land first, then 0-CMC non-lands
    lands = set()
    cmc0  = set()
    failed = []
    fail_errors = {}
    for n in unique:
        data = card_info.get(n)
        if data is None or "_error" in data:
            failed.append(n)
            if data and "_error" in data:
                fail_errors[n] = data["_error"]
            continue
        if is_land(data):
            lands.add(n)
        else:
            cmc = get_cmc(data)
            if cmc is not None and cmc == 0:
                cmc0.add(n)
    if failed:
        st.sidebar.warning(f"Scryfall lookup failed for: {', '.join(failed)}")
        for n, err in fail_errors.items():
            st.sidebar.caption(f"  • {n}: {err}")

    groups = {}
    if lands: groups["Lands"] = lands
    if cmc0:  groups["0-CMC (non-land)"] = cmc0

    st.session_state.deck = deck
    st.session_state.unique_cards = unique
    st.session_state.card_info = card_info
    st.session_state.groups = groups
    st.session_state.clauses = []

    if errors:
        st.sidebar.warning(f"Skipped lines: {errors}")
    st.sidebar.success(f"Loaded {len(deck)} cards. Auto-detected {len(lands)} lands, {len(cmc0)} 0-CMC cards.")

deck = st.session_state.deck
unique_cards = st.session_state.unique_cards
card_info = st.session_state.card_info
groups = st.session_state.groups

if not deck:
    st.info("👈 Paste a decklist and click **Load & Fetch Card Data** to get started.")
    st.stop()

st.success(f"Deck loaded: **{len(deck)} cards**, {len(unique_cards)} unique")

with st.expander("🔍 Debug: Scryfall data per card"):
    for name in unique_cards:
        data = card_info.get(name)
        if data and "_error" not in data:
            type_line = get_type_line(data)
            cmc = get_cmc(data)
            st.write(f"**{name}** — type: `{type_line}` | cmc: `{cmc}` | is_land: `{is_land(data)}`")
        elif data and "_error" in data:
            st.write(f"**{name}** — ❌ failed: `{data['_error']}`")
        else:
            st.write(f"**{name}** — ❌ Scryfall lookup failed (no data)")

# ─── Card Groups ─────────────────────────────────────────────────────────────

st.divider()
st.header("🗂️ Card Groups")
st.caption("Groups let you refer to sets of cards in your condition. Lands and 0-CMC cards are auto-detected.")

col_gl, col_gr = st.columns([1, 2])

with col_gl:
    new_group_name = st.text_input("New group name", placeholder="e.g. Interaction")
    new_group_cards = st.multiselect("Cards in group", unique_cards, key="new_group_cards")
    if st.button("➕ Add Group") and new_group_name and new_group_cards:
        groups[new_group_name] = set(new_group_cards)
        st.session_state.groups = groups
        st.rerun()

with col_gr:
    if groups:
        for gname, gcards in list(groups.items()):
            with st.expander(f"**{gname}** ({len(gcards)} cards)"):
                st.write(", ".join(sorted(gcards)))
                if st.button(f"🗑️ Delete '{gname}'", key=f"del_{gname}"):
                    del st.session_state.groups[gname]
                    st.rerun()
    else:
        st.info("No groups yet.")

# ─── DNF Condition Builder ────────────────────────────────────────────────────

st.divider()
st.header("🎯 Condition (DNF)")
st.caption(
    "Build clauses below. Each clause is a set of AND-rules. "
    "The hand passes if **any clause** is fully satisfied (OR of ANDs = DNF)."
)

group_names = list(groups.keys())
all_targets = group_names + unique_cards  # groups first, then individual cards
op_options = [">=", "<=", "==", ">", "<"]

clauses = st.session_state.clauses

# Add a new clause
if st.button("➕ Add Clause"):
    clauses.append([])  # empty clause = list of rules
    st.session_state.clauses = clauses
    st.rerun()

for ci, clause in enumerate(clauses):
    with st.container(border=True):
        c1, c2 = st.columns([8, 1])
        c1.markdown(f"**Clause {ci + 1}**")
        if c2.button("🗑️", key=f"del_clause_{ci}"):
            clauses.pop(ci)
            st.session_state.clauses = clauses
            st.rerun()

        # Existing rules
        for ri, rule in enumerate(clause):
            r1, r2, r3, r4 = st.columns([4, 2, 2, 1])
            new_target = r1.selectbox("Target", all_targets, index=all_targets.index(rule["target"]) if rule["target"] in all_targets else 0, key=f"t_{ci}_{ri}")
            new_op     = r2.selectbox("Op", op_options, index=op_options.index(rule["op"]), key=f"op_{ci}_{ri}")
            new_val    = r3.number_input("Count", 0, hand_size, rule["value"], key=f"v_{ci}_{ri}")
            if r4.button("✕", key=f"del_rule_{ci}_{ri}"):
                clause.pop(ri)
                st.session_state.clauses = clauses
                st.rerun()
            # update in place
            clause[ri] = {"target": new_target, "op": new_op, "value": new_val}

        # Add rule to this clause
        st.markdown("&nbsp;")
        ra, rb, rc, rd = st.columns([4, 2, 2, 2])
        add_target = ra.selectbox("Target", all_targets, key=f"new_t_{ci}")
        add_op     = rb.selectbox("Op", op_options, key=f"new_op_{ci}")
        add_val    = rc.number_input("Count", 0, hand_size, 1, key=f"new_v_{ci}")
        if rd.button("➕ Add Rule", key=f"add_rule_{ci}"):
            clause.append({"target": add_target, "op": add_op, "value": add_val})
            st.session_state.clauses = clauses
            st.rerun()

        # Human-readable preview
        if clause:
            parts = [f"`{r['target']} {r['op']} {r['value']}`" for r in clause]
            st.caption("Clause: " + " **AND** ".join(parts))

if len(clauses) > 1:
    previews = []
    for ci, clause in enumerate(clauses):
        if clause:
            parts = [f"{r['target']} {r['op']} {r['value']}" for r in clause]
            previews.append("(" + " AND ".join(parts) + ")")
    if previews:
        st.info("**Full condition (DNF):** " + " **OR** ".join(previews))

# ─── Evaluate a hand ─────────────────────────────────────────────────────────

def count_target(hand, target, groups):
    if target in groups:
        s = groups[target]
        return sum(1 for c in hand if c in s)
    return sum(1 for c in hand if c == target)


def eval_op(val, op, threshold):
    if op == ">=": return val >= threshold
    if op == "<=": return val <= threshold
    if op == "==": return val == threshold
    if op == ">":  return val > threshold
    if op == "<":  return val < threshold
    return False


def eval_dnf(hand, clauses, groups):
    if not clauses:
        return False
    for clause in clauses:
        if not clause:
            continue
        if all(eval_op(count_target(hand, r["target"], groups), r["op"], r["value"]) for r in clause):
            return True
    return False


def draw_hand(deck, size):
    return random.sample(deck, min(size, len(deck)))

# ─── Simulate ────────────────────────────────────────────────────────────────

st.divider()
st.header("🎲 Simulate")

if not clauses or all(len(c) == 0 for c in clauses):
    st.warning("Add at least one clause with at least one rule to simulate.")
    st.stop()

if st.button("▶️ Run Simulation", type="primary", use_container_width=True):
    with st.spinner(f"Simulating {n_sims:,} hands…"):
        hits = 0
        clause_hits = [0] * len(clauses)
        land_dist = Counter()

        for _ in range(n_sims):
            hand = draw_hand(deck, hand_size)
            # per-clause
            for ci, clause in enumerate(clauses):
                if clause and all(eval_op(count_target(hand, r["target"], groups), r["op"], r["value"]) for r in clause):
                    clause_hits[ci] += 1
            if eval_dnf(hand, clauses, groups):
                hits += 1
            if "Lands" in groups:
                lc = sum(1 for c in hand if c in groups["Lands"])
                land_dist[lc] += 1

    pct = hits / n_sims * 100
    st.success(f"### ✅ Condition met: **{pct:.2f}%** of opening hands ({hits:,} / {n_sims:,})")

    # Per-clause breakdown
    if len(clauses) > 1:
        st.subheader("Clause breakdown")
        cols = st.columns(len(clauses))
        for ci, (col, ch) in enumerate(zip(cols, clause_hits)):
            col.metric(f"Clause {ci+1}", f"{ch/n_sims*100:.2f}%", f"{ch:,} hands")

    # Land distribution chart
    if land_dist:
        import matplotlib.pyplot as plt
        st.subheader("🗺️ Land distribution in opening hand")
        xs = sorted(land_dist)
        ys = [land_dist[x] / n_sims * 100 for x in xs]
        fig, ax = plt.subplots(figsize=(7, 3))
        bars = ax.bar(xs, ys, color="#4C72B0")
        for bar, y in zip(bars, ys):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.3,
                    f"{y:.1f}%", ha='center', fontsize=9, fontweight='bold')
        ax.set_xlabel("Lands in hand")
        ax.set_ylabel("% of hands")
        ax.set_xticks(xs)
        ax.grid(axis='y', linestyle='--', alpha=0.4)
        plt.tight_layout()
        st.pyplot(fig)

    # Sample hands
    st.subheader("🖐️ Sample Hands")
    n_samples = st.slider("How many sample hands to show", 10, 200, 50, step=10)

    sample_hands = []
    attempts = 0
    while len(sample_hands) < n_samples and attempts < n_samples * 100:
        hand = draw_hand(deck, hand_size)
        met = eval_dnf(hand, clauses, groups)
        sample_hands.append((hand, met))
        attempts += 1

    with st.container(height=400):
        for i, (hand, met) in enumerate(sample_hands):
            icon = "✅" if met else "❌"
            with st.expander(f"{icon} Hand {i+1}: {', '.join(hand)}"):
                for ci, clause in enumerate(clauses):
                    if not clause:
                        continue
                    clause_met = all(eval_op(count_target(hand, r["target"], groups), r["op"], r["value"]) for r in clause)
                    parts = [f"{r['target']} {r['op']} {r['value']} (got {count_target(hand, r['target'], groups)})" for r in clause]
                    st.write(f"{'✅' if clause_met else '❌'} Clause {ci+1}: " + " AND ".join(parts))