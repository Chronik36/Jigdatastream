"""The Manual page: a plain-language guide to every page, with annotated screenshots.

The screenshots in static/manual/ come from tools/manual_screenshots.py, which runs the app on
made-up sample data. A figure whose image hasn't been taken yet is simply left out, and so is a
chapter about a page this copy of the app doesn't have (Bank, phone access).
"""
from pathlib import Path

from flask import Blueprint, current_app, render_template

bp = Blueprint("manual", __name__)
SHOTS = Path(__file__).parent / "static" / "manual"


@bp.get("/manual")
def manual():
    return render_template("manual.html", shots={p.stem for p in SHOTS.glob("*.png")},
                           pages=set(current_app.view_functions))
