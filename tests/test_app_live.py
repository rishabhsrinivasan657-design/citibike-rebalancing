"""Runs the real app page against live Citi Bike data and the real model.
Needs HF_TOKEN. Run with: uv run --extra app pytest -m app_live -s"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.app_live

APP = Path(__file__).parents[1] / "app" / "streamlit_app.py"


def test_real_app_builds_a_plan_with_the_model():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(APP), default_timeout=180).run()
    assert not at.exception, at.exception
    assert not at.error, [e.value for e in at.error]
    metrics = {m.label: m.value for m in at.metric}
    print("\nMETRICS", metrics)
    print("CAPTION", [c.value for c in at.caption][1])
    assert "Miss out, no vans" in metrics
    assert "Step 4 predictions" in [c.value for c in at.caption][1]
