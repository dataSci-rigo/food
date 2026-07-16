#!/usr/bin/env python3
"""Web dashboard — Flask server at port 9000.

Routes:
    /              Hub: links to all sections
    /food          Nutrition log (editable — edit grams/name, delete, add)
    /workout       Workout log (delete)
    /meds          Medication catalog + today's doses
    /mealprep      Fridge inventory + recent activity

Start: python web.py  (or via systemd alongside hub.py)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone  # timezone used in food_add

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import db
import meds_db
import mealprep_db
import workout_db
from flask import Flask, abort, redirect, render_template_string, request, url_for

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(config.TZ).strftime("%Y-%m-%d")


def _adj(date_str: str, days: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")


def _fmt_time(utc_str: str) -> str:
    """Convert stored UTC ISO string to local HH:MM."""
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        return dt.astimezone(config.TZ).strftime("%H:%M")
    except Exception:
        return utc_str[11:16] if len(utc_str) > 15 else utc_str


def _bar(val: float, lim: float) -> str:
    pct = min(int(val / lim * 100), 100) if lim else 0
    color = "#e74c3c" if pct >= 100 else "#f39c12" if pct >= 80 else "#27ae60"
    return (
        f'<div class="bar-outer">'
        f'<div class="bar-fill" style="width:{pct}%;background:{color}"></div>'
        f'</div>'
    )


app.jinja_env.globals["fmt_time"] = _fmt_time
app.jinja_env.globals["bar"] = _bar


# ---------------------------------------------------------------------------
# Shared CSS
# ---------------------------------------------------------------------------

_STYLE = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:#fff; --surface:#f5f5f5; --border:#e0e0e0;
  --text:#212121; --muted:#757575; --accent:#27ae60;
  --danger:#e74c3c; --warn:#f39c12;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#121212; --surface:#1e1e1e; --border:#333;
    --text:#e0e0e0; --muted:#9e9e9e; --accent:#4caf50;
    --danger:#f44336; --warn:#ff9800;
  }
}
body { font-family:system-ui,sans-serif; background:var(--bg); color:var(--text);
       max-width:780px; margin:0 auto; padding:16px; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.back { display:inline-block; margin-bottom:14px; font-size:.9em; padding:4px 10px;
        border:1px solid var(--border); border-radius:20px; }
.back:hover { background:var(--surface); text-decoration:none; }
h1 { font-size:1.4em; margin-bottom:16px; }
h2 { font-size:1.05em; margin:22px 0 10px; color:var(--muted); text-transform:uppercase;
     letter-spacing:.05em; }
.date-nav { display:flex; align-items:center; gap:12px; margin-bottom:18px; }
.date-nav a.nav-arrow { font-size:1.6em; line-height:1; color:var(--text); }
.date-nav strong { font-size:1.05em; }
.date-nav .today-link { font-size:.8em; color:var(--muted); }

/* Stat cards */
.stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
             gap:10px; margin-bottom:22px; }
.stat-card { background:var(--surface); border:1px solid var(--border); border-radius:8px;
             padding:10px 12px; }
.stat-card .label { font-size:.72em; color:var(--muted); margin-bottom:3px; }
.stat-card .value { font-size:.95em; font-weight:600; margin-bottom:6px; }
.bar-outer { background:var(--border); border-radius:3px; height:5px; }
.bar-fill  { height:5px; border-radius:3px; min-width:3px; }

/* Table */
table { width:100%; border-collapse:collapse; font-size:.9em; }
th { text-align:left; padding:7px 6px; color:var(--muted); font-weight:500;
     border-bottom:2px solid var(--border); white-space:nowrap; }
td { padding:8px 6px; border-bottom:1px solid var(--border); vertical-align:middle; }
tr.clickable:hover > td { background:var(--surface); cursor:pointer; }
tr.edit-row > td { background:var(--surface); padding:12px 8px; }

/* Edit / add forms */
.edit-form, .add-form {
  display:flex; flex-wrap:wrap; gap:8px; align-items:flex-end;
}
.field { display:flex; flex-direction:column; gap:3px; }
.field span { font-size:.75em; color:var(--muted); }
.field input[type=text], .field input[type=number] {
  padding:5px 8px; border:1px solid var(--border); border-radius:4px;
  background:var(--bg); color:var(--text); font-size:.9em;
}
.add-box { background:var(--surface); border:1px solid var(--border);
           border-radius:8px; padding:14px; margin-top:6px; }
details > summary { list-style:none; cursor:pointer; font-size:.9em; color:var(--accent);
                    margin-top:20px; }
details > summary::-webkit-details-marker { display:none; }

/* Buttons */
.btn { display:inline-flex; align-items:center; padding:5px 12px; border:none;
       border-radius:4px; cursor:pointer; font-size:.85em; font-family:inherit; }
.btn-sm  { padding:3px 8px; font-size:.8em; }
.btn-save { background:var(--accent); color:#fff; }
.btn-del  { background:transparent; color:var(--danger); border:1px solid var(--danger); }

/* Hub cards */
.hub-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(155px,1fr)); gap:16px; }
.hub-card { background:var(--surface); border:1px solid var(--border); border-radius:10px;
            padding:20px 16px; text-align:center; transition:box-shadow .15s; }
.hub-card:hover { box-shadow:0 2px 8px rgba(0,0,0,.12); }
.hub-card .icon { font-size:2em; margin-bottom:8px; }
.hub-card .title { font-weight:600; margin-bottom:4px; }
.hub-card .sub { font-size:.8em; color:var(--muted); }

/* Misc */
.tag { display:inline-block; font-size:.7em; padding:1px 5px; border-radius:3px;
       background:var(--border); color:var(--muted); margin-left:3px; }
.empty { color:var(--muted); padding:20px 0; }
</style>
"""


# ---------------------------------------------------------------------------
# Hub  /
# ---------------------------------------------------------------------------

@app.route("/")
def hub():
    today = _today()
    cal = int(db.get_day_totals(today).get("calories", 0))
    n_workouts = len(workout_db.get_day_log(today))
    n_doses    = len(meds_db.get_today_doses())
    n_fridge   = len(mealprep_db.get_fridge())
    return render_template_string("""\
<!doctype html><html><head>{{ style|safe }}<title>Wellness Hub</title></head><body>
<h1>🍱 Wellness Hub</h1>
<div class="hub-grid">
  {% for href, icon, title, sub in cards %}
  <a href="{{ href }}" style="text-decoration:none">
    <div class="hub-card">
      <div class="icon">{{ icon }}</div>
      <div class="title">{{ title }}</div>
      <div class="sub">{{ sub }}</div>
    </div>
  </a>
  {% endfor %}
</div>
</body></html>""",
        style=_STYLE,
        cards=[
            ("/food",     "🥗", "Food Log",  f"{cal} kcal today"),
            ("/workout",  "💪", "Workout",   f"{n_workouts} exercise{'s' if n_workouts != 1 else ''} today"),
            ("/meds",     "💊", "Meds",      f"{n_doses} dose{'s' if n_doses != 1 else ''} today"),
            ("/mealprep", "🧊", "Fridge",    f"{n_fridge} item{'s' if n_fridge != 1 else ''}"),
        ],
    )


# ---------------------------------------------------------------------------
# Food  /food
# ---------------------------------------------------------------------------

@app.route("/food")
def food():
    date   = request.args.get("date", _today())
    rows   = db.get_day_log(date)
    totals = db.get_day_totals(date)
    hist   = db.get_history_totals(7)
    lim    = config.DAILY_LIMITS
    today  = _today()

    return render_template_string("""\
<!doctype html><html><head>{{ style|safe }}<title>Food Log — {{ date }}</title></head><body>
<a class="back" href="/">← Hub</a>
<h1>🥗 Food Log</h1>

<div class="date-nav">
  <a class="nav-arrow" href="/food?date={{ prev }}">‹</a>
  <strong>{{ date }}</strong>
  <a class="nav-arrow" href="/food?date={{ next }}">›</a>
  {% if date != today %}<a class="today-link" href="/food">[today]</a>{% endif %}
</div>

<div class="stat-grid">
  {% for label, val, lim_val, unit in stats %}
  <div class="stat-card">
    <div class="label">{{ label }}</div>
    <div class="value">{{ val }} / {{ lim_val }} {{ unit }}</div>
    {{ bar(val, lim_val)|safe }}
  </div>
  {% endfor %}
</div>

{% if rows %}
<table>
  <thead><tr>
    <th>Time</th><th>Food</th><th style="text-align:right">g</th>
    <th style="text-align:right">kcal</th><th></th>
  </tr></thead>
  <tbody>
  {% for row in rows %}
  <tr class="clickable" onclick="toggleEdit({{ row.id }})">
    <td style="color:var(--muted);white-space:nowrap">{{ fmt_time(row.logged_at) }}</td>
    <td>
      {{ row.food_name }}
      {% if row.source == 'catalog' %}<span class="tag">📋</span>{% endif %}
      {% if row.source == 'ai_estimate' %}<span class="tag">🤖</span>{% endif %}
      {% if row.source == 'manual' %}<span class="tag">✏️</span>{% endif %}
      {% if row.liked == 1 %}<span style="color:var(--accent)"> 👍</span>
      {% elif row.liked == 0 %}<span style="color:var(--danger)"> 👎</span>{% endif %}
    </td>
    <td style="text-align:right;color:var(--muted)">
      {{ "%.0f"|format(row.grams_eaten) if row.grams_eaten else "—" }}
    </td>
    <td style="text-align:right;font-weight:500">
      {{ "%.0f"|format(row.calories) if row.calories else "—" }}
    </td>
    <td>
      <form method="post" action="/food/delete/{{ row.id }}" style="display:inline">
        <input type="hidden" name="date" value="{{ date }}">
        <button type="submit" class="btn btn-del btn-sm"
                onclick="event.stopPropagation()" title="Delete">🗑</button>
      </form>
    </td>
  </tr>
  <tr id="edit-{{ row.id }}" class="edit-row" style="display:none">
    <td colspan="5">
      <form method="post" action="/food/edit/{{ row.id }}" class="edit-form">
        <input type="hidden" name="date" value="{{ date }}">
        <div class="field">
          <span>Food name</span>
          <input type="text" name="food_name" value="{{ row.food_name }}"
                 style="min-width:200px">
        </div>
        <div class="field">
          <span>Grams</span>
          <input type="number" name="grams_eaten" step="0.1" style="width:80px"
                 value="{{ "%.1f"|format(row.grams_eaten) if row.grams_eaten else '' }}"
                 placeholder="—">
        </div>
        <div class="field">
          <span>kcal (override)</span>
          <input type="number" name="calories" step="1" style="width:90px"
                 value="{{ "%.0f"|format(row.calories) if row.calories else '' }}"
                 placeholder="—">
        </div>
        <div style="display:flex;gap:6px;align-self:flex-end">
          <button type="submit" class="btn btn-save">Save</button>
          <button type="button" class="btn"
                  style="border:1px solid var(--border)"
                  onclick="toggleEdit({{ row.id }})">Cancel</button>
        </div>
      </form>
      <p style="font-size:.75em;color:var(--muted);margin-top:6px">
        Changing grams rescales all nutrients proportionally.
        Edit kcal directly to override without rescaling.
      </p>
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="empty">Nothing logged on {{ date }}.</p>
{% endif %}

<details>
  <summary>+ Add entry manually</summary>
  <div class="add-box">
    <form method="post" action="/food/add" class="add-form">
      <input type="hidden" name="date" value="{{ date }}">
      <div class="field" style="min-width:160px">
        <span>Food name *</span>
        <input type="text" name="food_name" required placeholder="Banana">
      </div>
      <div class="field">
        <span>Grams</span>
        <input type="number" name="grams" step="0.1" style="width:80px" placeholder="100">
      </div>
      <div class="field">
        <span>Calories *</span>
        <input type="number" name="calories" required step="1" style="width:80px" placeholder="90">
      </div>
      <div class="field">
        <span>Carbs g</span>
        <input type="number" name="carbs_g" step="0.1" style="width:75px" placeholder="0">
      </div>
      <div class="field">
        <span>Sat fat g</span>
        <input type="number" name="sat_fat_g" step="0.1" style="width:75px" placeholder="0">
      </div>
      <div class="field">
        <span>Sodium mg</span>
        <input type="number" name="sodium_mg" step="1" style="width:75px" placeholder="0">
      </div>
      <div class="field">
        <span>Time (optional)</span>
        <input type="time" name="time" style="width:100px" title="Leave blank for now">
      </div>
      <div class="field" style="justify-content:flex-end;padding-bottom:2px">
        <label style="display:flex;align-items:center;gap:5px;font-size:.85em;cursor:pointer">
          <input type="checkbox" name="cheat" value="1"> 🍕 Cheat
        </label>
      </div>
      <div style="align-self:flex-end">
        <button type="submit" class="btn btn-save">Add</button>
      </div>
    </form>
  </div>
</details>

{% if hist %}
<h2>7-Day History</h2>
<table>
  <thead><tr><th>Date</th><th style="text-align:right">kcal</th>
  <th style="text-align:right">%</th></tr></thead>
  <tbody>
  {% for r in hist %}
  <tr>
    <td><a href="/food?date={{ r.date }}">{{ r.date }}</a></td>
    <td style="text-align:right">{{ r.calories|int }}</td>
    <td style="text-align:right">
      <span style="color:{{ '#e74c3c' if r.calories > lim.calories else 'var(--accent)' }}">
        {{ (r.calories / lim.calories * 100)|int }}%
      </span>
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}

<script>
function toggleEdit(id) {
  const row = document.getElementById('edit-' + id);
  const open = row.style.display === 'table-row';
  document.querySelectorAll('.edit-row').forEach(r => r.style.display = 'none');
  if (!open) row.style.display = 'table-row';
}
</script>
</body></html>""",
        style=_STYLE,
        date=date, today=today,
        prev=_adj(date, -1), next=_adj(date, 1),
        rows=rows, hist=hist, lim=lim,
        stats=[
            ("Calories",  int(totals["calories"]),  lim["calories"],  "kcal"),
            ("Sat fat",   round(totals["sat_fat_g"], 1), lim["sat_fat_g"], "g"),
            ("Sodium",    int(totals["sodium_mg"]),  lim["sodium_mg"],  "mg"),
            ("Carbs",     int(totals["carbs_g"]),    lim["carbs_g"],    "g"),
            ("Sugar",     int(totals["sugar_g"]),    lim["sugar_g"],    "g"),
        ],
    )


@app.route("/food/delete/<int:entry_id>", methods=["POST"])
def food_delete(entry_id: int):
    date = request.form.get("date", _today())
    db.delete_log(entry_id)
    return redirect(url_for("food", date=date))


@app.route("/food/edit/<int:entry_id>", methods=["POST"])
def food_edit(entry_id: int):
    date = request.form.get("date", _today())
    rows = db.get_day_log(date)
    row  = next((r for r in rows if r["id"] == entry_id), None)
    if row is None:
        abort(404)

    food_name  = (request.form.get("food_name") or "").strip() or row["food_name"]
    old_grams  = row.get("grams_eaten")

    try:
        new_grams = float(request.form["grams_eaten"]) if request.form.get("grams_eaten", "").strip() else None
    except ValueError:
        new_grams = old_grams

    # Rescale all nutrients if grams changed and old grams is known
    if new_grams and old_grams and old_grams > 0 and abs(new_grams - old_grams) > 0.01:
        scale = new_grams / old_grams
        nutrients = {
            "calories":  (row.get("calories")  or 0) * scale,
            "sat_fat_g": (row.get("sat_fat_g") or 0) * scale,
            "sodium_mg": (row.get("sodium_mg") or 0) * scale,
            "carbs_g":   (row.get("carbs_g")   or 0) * scale,
            "sugar_g":   (row.get("sugar_g")   or 0) * scale,
            "fiber_g":   (row.get("fiber_g")   or 0) * scale,
        }
    else:
        # Grams unchanged — allow direct calorie override
        try:
            new_cal = float(request.form["calories"]) if request.form.get("calories", "").strip() else (row.get("calories") or 0)
        except ValueError:
            new_cal = row.get("calories") or 0
        nutrients = {
            "calories":  new_cal,
            "sat_fat_g": row.get("sat_fat_g") or 0,
            "sodium_mg": row.get("sodium_mg") or 0,
            "carbs_g":   row.get("carbs_g")   or 0,
            "sugar_g":   row.get("sugar_g")   or 0,
            "fiber_g":   row.get("fiber_g")   or 0,
        }

    db.update_log_entry(entry_id, food_name, new_grams if new_grams else old_grams, nutrients)
    return redirect(url_for("food", date=date))


@app.route("/food/add", methods=["POST"])
def food_add():
    date      = request.form.get("date", _today())
    food_name = (request.form.get("food_name") or "").strip()
    if not food_name:
        return redirect(url_for("food", date=date))

    def _n(key: str, default: float = 0.0) -> float:
        try:
            return float(request.form.get(key) or default)
        except ValueError:
            return default

    nutrients = {
        "calories":  _n("calories"),
        "carbs_g":   _n("carbs_g"),
        "sat_fat_g": _n("sat_fat_g"),
        "sodium_mg": _n("sodium_mg"),
        "sugar_g":   0.0,
        "fiber_g":   0.0,
    }
    grams      = _n("grams") or None
    cheat      = request.form.get("cheat") == "1"
    time_str   = (request.form.get("time") or "").strip()
    logged_at  = None
    if time_str:
        try:
            naive     = datetime.strptime(f"{date} {time_str}", "%Y-%m-%d %H:%M")
            logged_at = naive.replace(tzinfo=config.TZ).astimezone(timezone.utc).isoformat()
        except ValueError:
            pass

    db.log_food(
        date=date, user_input=food_name, food_name=food_name,
        source="manual", grams=grams, nutrients=nutrients,
        gi=None, gi_source=None, logged_at=logged_at, cheat=cheat,
    )
    return redirect(url_for("food", date=date))


# ---------------------------------------------------------------------------
# Workout  /workout
# ---------------------------------------------------------------------------

@app.route("/workout")
def workout():
    date  = request.args.get("date", _today())
    rows  = workout_db.get_day_log(date)
    hist  = workout_db.get_history(7)
    today = _today()
    return render_template_string("""\
<!doctype html><html><head>{{ style|safe }}<title>Workout — {{ date }}</title></head><body>
<a class="back" href="/">← Hub</a>
<h1>💪 Workout</h1>

<div class="date-nav">
  <a class="nav-arrow" href="/workout?date={{ prev }}">‹</a>
  <strong>{{ date }}</strong>
  <a class="nav-arrow" href="/workout?date={{ next }}">›</a>
  {% if date != today %}<a class="today-link" href="/workout">[today]</a>{% endif %}
</div>

{% if rows %}
<table>
  <thead><tr>
    <th>Time</th><th>Exercise</th><th>Sets×Reps</th>
    <th>Weight</th><th>Duration</th><th>Distance</th><th></th>
  </tr></thead>
  <tbody>
  {% for row in rows %}
  <tr>
    <td style="color:var(--muted);white-space:nowrap">{{ fmt_time(row.logged_at) }}</td>
    <td>
      {{ row.exercise }}
      {% if row.notes %}
        <span style="color:var(--muted);font-size:.85em"> ({{ row.notes }})</span>
      {% endif %}
    </td>
    <td>
      {% if row.sets and row.reps %}{{ row.sets }}×{{ row.reps }}
      {% elif row.reps %}{{ row.reps }} reps
      {% else %}—{% endif %}
    </td>
    <td>{{ "%.1f kg"|format(row.weight_kg) if row.weight_kg else "—" }}</td>
    <td>{{ "%.0f min"|format(row.duration_min) if row.duration_min else "—" }}</td>
    <td>{{ "%.1f km"|format(row.distance_km) if row.distance_km else "—" }}</td>
    <td>
      <form method="post" action="/workout/delete/{{ row.id }}">
        <input type="hidden" name="date" value="{{ date }}">
        <button type="submit" class="btn btn-del btn-sm" title="Delete">🗑</button>
      </form>
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="empty">No workout logged on {{ date }}.</p>
{% endif %}

{% if hist %}
<h2>7-Day History</h2>
<table>
  <thead><tr><th>Date</th><th style="text-align:right">Exercises</th></tr></thead>
  <tbody>
  {% for r in hist %}
  <tr>
    <td><a href="/workout?date={{ r.date }}">{{ r.date }}</a></td>
    <td style="text-align:right">{{ r.exercises }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}
</body></html>""",
        style=_STYLE,
        date=date, today=today,
        prev=_adj(date, -1), next=_adj(date, 1),
        rows=rows, hist=hist,
    )


@app.route("/workout/delete/<int:entry_id>", methods=["POST"])
def workout_delete(entry_id: int):
    date = request.form.get("date", _today())
    workout_db.delete_exercise(entry_id)
    return redirect(url_for("workout", date=date))


# ---------------------------------------------------------------------------
# Meds  /meds
# ---------------------------------------------------------------------------

@app.route("/meds")
def meds():
    catalog = meds_db.get_catalog()
    doses   = meds_db.get_today_doses()
    meds_list  = [m for m in catalog if m["category"] == "medication"]
    supps_list = [m for m in catalog if m["category"] != "medication"]
    return render_template_string("""\
<!doctype html><html><head>{{ style|safe }}<title>Meds &amp; Supplements</title></head><body>
<a class="back" href="/">← Hub</a>
<h1>💊 Meds &amp; Supplements</h1>

{# ---- Add form ---- #}
<details id="add-details">
  <summary>+ Add medication or supplement</summary>
  <div class="add-box" style="margin-top:10px">
    <form method="post" action="/meds/add" class="add-form" style="flex-direction:column;gap:12px">

      <div style="display:flex;flex-wrap:wrap;gap:8px">
        <div class="field" style="min-width:180px">
          <span>Name *</span>
          <input type="text" name="name" required placeholder="e.g. Vitamin D">
        </div>
        <div class="field" style="width:90px">
          <span>Dose amount</span>
          <input type="number" name="dose_amount" step="any" placeholder="2000">
        </div>
        <div class="field" style="width:80px">
          <span>Unit</span>
          <input type="text" name="dose_unit" placeholder="IU / mg / g">
        </div>
        <div class="field" style="min-width:140px">
          <span>Notes</span>
          <input type="text" name="notes" placeholder="optional">
        </div>
      </div>

      <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
        <strong style="font-size:.85em">Type:</strong>
        <label style="display:flex;gap:6px;align-items:center;cursor:pointer;font-size:.9em">
          <input type="radio" name="category" value="supplement" checked> Supplement
        </label>
        <label style="display:flex;gap:6px;align-items:center;cursor:pointer;font-size:.9em">
          <input type="radio" name="category" value="medication"> Medication
        </label>
        <button type="submit" class="btn btn-save" style="margin-left:auto">Add</button>
      </div>

    </form>
  </div>
</details>

{# ---- Medications ---- #}
<h2>💊 Medications</h2>
{% if meds_list %}
<table>
  <thead><tr><th>Name</th><th>Dose</th><th>Notes</th><th></th></tr></thead>
  <tbody>
  {% for m in meds_list %}
  <tr>
    <td>{{ m.name }}</td>
    <td>{{ "%.4g %s"|format(m.dose_amount, m.dose_unit) if m.dose_amount else "—" }}</td>
    <td style="color:var(--muted)">{{ m.notes or "" }}</td>
    <td>
      <form method="post" action="/meds/remove/{{ m.name|urlencode }}">
        <button type="submit" class="btn btn-del btn-sm" title="Remove">🗑</button>
      </form>
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="empty" style="padding:12px 0">No medications yet.</p>
{% endif %}

{# ---- Supplements ---- #}
<h2>🌿 Supplements</h2>
{% if supps_list %}
<table>
  <thead><tr><th>Name</th><th>Dose</th><th>Notes</th><th></th></tr></thead>
  <tbody>
  {% for m in supps_list %}
  <tr>
    <td>{{ m.name }}</td>
    <td>{{ "%.4g %s"|format(m.dose_amount, m.dose_unit) if m.dose_amount else "—" }}</td>
    <td style="color:var(--muted)">{{ m.notes or "" }}</td>
    <td>
      <form method="post" action="/meds/remove/{{ m.name|urlencode }}">
        <button type="submit" class="btn btn-del btn-sm" title="Remove">🗑</button>
      </form>
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="empty" style="padding:12px 0">No supplements yet.</p>
{% endif %}

{# ---- Today's doses ---- #}
<h2>Today's Doses</h2>
{% if doses %}
<table>
  <thead><tr><th>Time</th><th>Name</th><th>Dose</th></tr></thead>
  <tbody>
  {% for d in doses %}
  <tr>
    <td style="color:var(--muted);white-space:nowrap">{{ fmt_time(d.logged_at) }}</td>
    <td>{{ d.med_name }}</td>
    <td>{{ "%.4g %s"|format(d.dose_amount, d.dose_unit) if d.dose_amount else "—" }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="empty">No doses logged today.</p>
{% endif %}
</body></html>""",
        style=_STYLE,
        catalog=catalog, doses=doses,
        meds_list=meds_list, supps_list=supps_list,
    )


@app.route("/meds/add", methods=["POST"])
def meds_add():
    name = (request.form.get("name") or "").strip()
    if not name:
        return redirect(url_for("meds"))
    try:
        dose_amount = float(request.form["dose_amount"]) if request.form.get("dose_amount", "").strip() else None
    except ValueError:
        dose_amount = None
    dose_unit = (request.form.get("dose_unit") or "").strip() or None
    category  = request.form.get("category", "supplement")
    if category not in ("medication", "supplement"):
        category = "supplement"
    notes = (request.form.get("notes") or "").strip() or None
    meds_db.add_med(name, dose_amount, dose_unit, category, notes)
    return redirect(url_for("meds"))


@app.route("/meds/remove/<name>", methods=["POST"])
def meds_remove(name: str):
    meds_db.remove_med(name)
    return redirect(url_for("meds"))


# ---------------------------------------------------------------------------
# Meal Prep / Fridge  /mealprep
# ---------------------------------------------------------------------------

@app.route("/mealprep")
def mealprep():
    items = mealprep_db.get_fridge()
    log   = mealprep_db.get_fridge_log(20)
    return render_template_string("""\
<!doctype html><html><head>{{ style|safe }}<title>Fridge</title></head><body>
<a class="back" href="/">← Hub</a>
<h1>🧊 Fridge Inventory</h1>

{% if items %}
<table>
  <thead><tr><th>Item</th><th style="text-align:right">Qty</th>
  <th>Unit</th><th>Updated</th></tr></thead>
  <tbody>
  {% for item in items %}
  <tr>
    <td>{{ item.item_name }}</td>
    <td style="text-align:right;font-weight:500">{{ "%.0f"|format(item.quantity) }}</td>
    <td style="color:var(--muted)">{{ item.unit }}</td>
    <td style="color:var(--muted);font-size:.85em">{{ item.updated_at[5:16] }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="empty">🧊 Fridge is empty.</p>
{% endif %}

<h2>Recent Activity</h2>
{% if log %}
<table>
  <thead><tr><th>Time</th><th>Action</th><th>Item</th><th style="text-align:right">Qty</th></tr></thead>
  <tbody>
  {% for entry in log %}
  <tr>
    <td style="color:var(--muted);white-space:nowrap">{{ entry.logged_at[5:16] }}</td>
    <td>
      {% if entry.action == 'add' %}<span style="color:var(--accent)">+ add</span>
      {% elif entry.action == 'eat' %}<span style="color:var(--warn)">🍽 eat</span>
      {% else %}<span style="color:var(--danger)">- remove</span>{% endif %}
    </td>
    <td>{{ entry.item_name }}</td>
    <td style="text-align:right;color:var(--muted)">
      {{ "%.0f %s"|format(entry.quantity, entry.unit) if entry.quantity else "—" }}
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% else %}
<p class="empty">No activity yet.</p>
{% endif %}
</body></html>""",
        style=_STYLE, items=items, log=log,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    db.init_db()
    meds_db.init_db()
    workout_db.init_db()
    mealprep_db.init_db()
    app.run(host="0.0.0.0", port=9000, debug=False)
