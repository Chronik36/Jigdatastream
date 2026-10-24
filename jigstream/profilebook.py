"""Building and organizing profiles by hand: groups, saved addresses, copies, and Mass Distribute.

Every value comes from what you type or paste. Nothing here makes up names, addresses or cards,
and card numbers are still never stored (they're added at export time; see export.py).
"""
import re

ADDRESS_PARTS = ("address1", "address2", "city", "state", "zip", "country")
EDITABLE = ("name", "group_name", "first_name", "last_name", "phone", "email", "card_holder", "card_id",
            *(f"bill_{k}" for k in ADDRESS_PARTS), *(f"ship_{k}" for k in ADDRESS_PARTS))

# Mass Distribute fields: key -> (section, label, help). Address fields go to billing and/or shipping.
DISTRIBUTE_FIELDS = {
    "name": ("Profile", "Profile names", "one name per profile; names must be unique"),
    "email": ("Contact", "Email", "one email per line"),
    "full_name": ("Contact", "Name", "first and last name, e.g. Jacob Smith"),
    "first_name": ("Contact", "First name", "one per line"),
    "last_name": ("Contact", "Last name", "one per line"),
    "phone": ("Contact", "Phone", "one per line"),
    "address1": ("Address", "Address line 1", "one per line"),
    "address2": ("Address", "Address line 2", "Apt, Suite, etc."),
    "city": ("Address", "City", "one per line"),
    "state": ("Address", "State", "a state name or 2-letter code"),
    "zip": ("Address", "ZIP", "one per line"),
    "country": ("Address", "Country", "e.g. US"),
    "card_holder": ("Payment", "Name on card", "one per line"),
}


def groups(con):
    named = {r[0] for r in con.execute("SELECT name FROM profile_groups")}
    counts = {r[0]: r[1] for r in con.execute(
        "SELECT COALESCE(group_name, ''), COUNT(*) FROM profiles GROUP BY COALESCE(group_name, '')")}
    names = sorted(named | {g for g in counts if g}, key=str.lower)
    return [(g, counts.get(g, 0)) for g in names], counts.get("", 0)


def add_group(con, name):
    name = name.strip()
    if name:
        con.execute("INSERT OR IGNORE INTO profile_groups VALUES (?)", (name,))
        con.commit()
    return name


def rename_group(con, old, new):
    new = new.strip()
    if not new:
        return
    con.execute("INSERT OR IGNORE INTO profile_groups VALUES (?)", (new,))
    con.execute("UPDATE profiles SET group_name=? WHERE group_name=?", (new, old))
    con.execute("DELETE FROM profile_groups WHERE name=?", (old,))
    con.commit()


def delete_group(con, name):
    """The group goes; its profiles stay, ungrouped."""
    con.execute("UPDATE profiles SET group_name=NULL WHERE group_name=?", (name,))
    con.execute("DELETE FROM profile_groups WHERE name=?", (name,))
    con.commit()


def move(con, names, group):
    if group:
        add_group(con, group)
    con.executemany("UPDATE profiles SET group_name=? WHERE name=?", [(group or None, n) for n in names])
    con.commit()


def save_profile(con, form, original=None):
    """Create (original=None) or update a profile from the form. Returns (name, error)."""
    f = {k: (form.get(k) or "").strip() for k in EDITABLE}
    if not f["name"]:
        return None, "Give the profile a name."
    if form.get("same"):
        for k in ADDRESS_PARTS:
            f["ship_" + k] = f["bill_" + k]
    clash = con.execute("SELECT 1 FROM profiles WHERE name=?", (f["name"],)).fetchone()
    if clash and f["name"] != original:
        return None, f"There's already a profile called {f['name']}."
    if f["group_name"]:
        add_group(con, f["group_name"])
    f["group_name"] = f["group_name"] or None
    f["card_id"] = int(f["card_id"]) if f["card_id"].isdigit() else None
    if f["card_id"]:  # a card belongs to one profile at a time
        con.execute("UPDATE profiles SET card_id=NULL WHERE card_id=? AND name != ?", (f["card_id"], original or ""))
    if original:
        con.execute(f"UPDATE profiles SET {', '.join(k + '=?' for k in EDITABLE)} WHERE name=?",
                    [f[k] for k in EDITABLE] + [original])
        if f["name"] != original:  # orders matched to the profile follow the new name
            if "profile_name" in {r[1] for r in con.execute("PRAGMA table_info(tracked_orders)")}:
                con.execute("UPDATE tracked_orders SET profile_name=? WHERE profile_name=?", (f["name"], original))
    else:
        con.execute(f"INSERT INTO profiles ({', '.join(EDITABLE)}, bot) VALUES ({', '.join('?' * len(EDITABLE))}, '')",
                    [f[k] for k in EDITABLE])
    con.commit()
    return f["name"], None


def duplicate(con, name, count):
    """Copies of a profile, numbered on from its name (skipping names already taken). Copies don't
    take the original's card: a card belongs to one profile."""
    row = con.execute("SELECT * FROM profiles WHERE name=?", (name,)).fetchone()
    if not row:
        return []
    m = re.match(r"(.*?)(\d+)$", name)  # "Target 1" -> Target 2, Target 3; "VCC07" -> VCC08
    prefix, i, width = (m.group(1), int(m.group(2)) + 1, len(m.group(2))) if m else (name + " ", 2, 1)
    taken = {r[0] for r in con.execute("SELECT name FROM profiles")}
    made = []
    cols = row.keys()
    while len(made) < count:
        new = f"{prefix}{str(i).zfill(width)}"
        i += 1
        if new in taken:
            continue
        values = dict(row)
        values["name"] = new
        values["card_id"] = None
        if "bot_name" in values:
            values["bot_name"] = None  # a copy is its own profile in the bot too
        con.execute(f"INSERT INTO profiles ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    [values[c] for c in cols])
        taken.add(new)
        made.append(new)
    con.commit()
    return made


def distribute(con, names, field, values, billing=True, shipping=True):
    """Spread pasted values over the selected profiles in order (repeating the list if it's shorter).
    Returns (number of profiles changed, error)."""
    values = [v.strip() for v in values if v.strip()]
    if field not in DISTRIBUTE_FIELDS or not names or not values:
        return 0, "Pick profiles, a field, and paste at least one value."
    if field == "name":
        if len(values) < len(names):
            return 0, f"Profile names must be unique: paste at least {len(names)} names."
        if len(set(values)) < len(values):
            return 0, "Some pasted profile names are repeated."
        others = {r[0] for r in con.execute("SELECT name FROM profiles")} - set(names)
        clash = [v for v in values[:len(names)] if v in others]
        if clash:
            return 0, f"These names are already used by other profiles: {', '.join(clash[:5])}"
    if field in ADDRESS_PARTS and not (billing or shipping):
        return 0, "Tick billing, shipping or both."
    for i, name in enumerate(names):
        value = values[i % len(values)]
        if field == "full_name":
            first, _, last = value.partition(" ")
            con.execute("UPDATE profiles SET first_name=?, last_name=? WHERE name=?", (first, last.strip(), name))
        elif field in ADDRESS_PARTS:
            cols = [p + field for p, on in (("bill_", billing), ("ship_", shipping)) if on]
            con.execute(f"UPDATE profiles SET {', '.join(c + '=?' for c in cols)} WHERE name=?",
                        [value] * len(cols) + [name])
        else:
            con.execute(f"UPDATE profiles SET {field}=? WHERE name=?", (value, name))
    con.commit()
    return len(names), None


def saved_addresses(con):
    return [dict(r) for r in con.execute("SELECT * FROM saved_addresses ORDER BY nickname")]


def save_address(con, form, prefix="bill_"):
    nickname = (form.get("nickname") or "").strip()
    if not nickname:
        return "Give the address a nickname first."
    con.execute("INSERT OR REPLACE INTO saved_addresses VALUES (?,?,?,?,?,?,?)",
                [nickname] + [(form.get(prefix + k) or "").strip() for k in ADDRESS_PARTS])
    con.commit()
    return None
