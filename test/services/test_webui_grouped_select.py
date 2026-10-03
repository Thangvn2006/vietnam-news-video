from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import streamlit as st
from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import voice


ROOT_DIR = Path(__file__).parent.parent.parent
WEBUI_MAIN = ROOT_DIR / "webui" / "Main.py"


class _GroupedSelectHarness:
    """Only replace the video source component, keeping the real implementation of v2 of other Components in the page."""

    def __init__(self):
        self.selected = None
        self.calls = []
        self.declaration = None
        self._original_component = st.components.v2.component

    def declare(self, name, *args, **kwargs):
        # Third-party components such as novice guidance also use Components v2. Transmitting these statements avoids
        # The test stub changes other functions of the page and only controls the video source selection results that this use case is concerned about.
        if name != "mpt_grouped_select":
            return self._original_component(name, *args, **kwargs)

        self.declaration = kwargs

        def render(**render_kwargs):
            self.calls.append(render_kwargs)
            return SimpleNamespace(selected=self.selected)

        return render


@contextmanager
def _running_app(harness, *, saved_video_source="pexels"):
    """Keep components, configurations, and external patch queries isolated throughout the use case."""
    test_app_config = dict(config.app, video_source=saved_video_source)
    test_ui_config = dict(config.ui, language="en")
    with (
        patch(
            "streamlit.components.v2.component",
            side_effect=harness.declare,
        ),
        patch.object(config, "app", test_app_config),
        patch.object(config, "ui", test_ui_config),
        patch.object(config, "try_save_config", return_value=True),
        patch.object(
            voice,
            "get_all_azure_voices",
            return_value=["en-US-JennyNeural-Female"],
        ),
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        yield app


def test_grouped_video_source_applies_first_change_and_allows_switching_back():
    """A change event should update the business status, and users cannot be asked to make repeated selections."""
    harness = _GroupedSelectHarness()
    with _running_app(harness) as app:
        assert app.session_state["video_source_select_en"] == "pexels"
        assert harness.calls[-1]["data"]["value"] == "pexels"

        harness.selected = "pixabay"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        assert app.session_state["video_source_select_en"] == "pixabay"
        # grouped_selectbox will actively rerun in the event round; the last rendering must put the new value
        # Passed back to the front end, otherwise the component may still be overwritten by old data.
        assert harness.calls[-1]["data"]["value"] == "pixabay"

        harness.selected = "pexels"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        assert app.session_state["video_source_select_en"] == "pexels"
        assert harness.calls[-1]["data"]["value"] == "pexels"


def test_grouped_video_source_ignores_unknown_event_and_repairs_saved_value():
    """Neither expired configuration nor forged events can cause the page to enter the unknown source state."""
    harness = _GroupedSelectHarness()
    with _running_app(harness, saved_video_source="removed-provider") as app:
        assert app.session_state["video_source_select_en"] == "pexels"
        assert harness.calls[-1]["data"]["value"] == "pexels"

        harness.selected = "unknown-provider"
        app.run()
        assert [str(item.value) for item in app.exception] == []
        assert app.session_state["video_source_select_en"] == "pexels"
        assert harness.calls[-1]["data"]["value"] == "pexels"


def test_grouped_video_source_keeps_groups_and_accessible_label_binding():
    """Component data should maintain grouping order and provide stable control IDs for visible labels."""
    harness = _GroupedSelectHarness()
    with _running_app(harness):
        data = harness.calls[-1]["data"]
        assert data["controlId"] == "video_source_select_en_control"
        assert [group["label"] for group in data["groups"]] == [
            "Stock Video",
            "AI Video",
            "AI Image",
            "Local Files",
        ]
        assert [
            option["value"] for group in data["groups"] for option in group["options"]
        ] == [
            "pexels",
            "pixabay",
            "coverr",
            "metaso_minimax",
            "ofox",
            "loomloom",
            "volcengine_seedance",
            "wavespeed",
            "muapi",
            "openai_image",
            "local",
        ]

        # AppTest currently does not expose the internal DOM of Components v2, so it also verifies component declarations
        # Do use controlId to associate label/select, and allow label rows to wrap naturally in narrow screens.
        assert "label.htmlFor = data.controlId" in harness.declaration["js"]
        assert "select.id = data.controlId" in harness.declaration["js"]
        assert "flex-wrap: wrap" in harness.declaration["css"]


def test_stock_concurrency_only_appears_for_stock_sources():
    """Inventory concurrency is only displayed for three inventory materials, and the explicit settings are retained when switching sources."""
    harness = _GroupedSelectHarness()
    with _running_app(harness) as app:
        stock = next(
            item for item in app.selectbox
            if item.key.startswith("material_concurrency_select_")
        )
        clip = next(
            item for item in app.selectbox
            if item.key.startswith("clip_rendering_concurrency_select_")
        )
        assert stock.value == 1
        assert clip.value == 1

        stock.set_value(4).run()
        clip = next(
            item for item in app.selectbox
            if item.key.startswith("clip_rendering_concurrency_select_")
        )
        clip.set_value(2).run()
        assert config.app["material_concurrency"] == 4
        assert config.app["video_clip_concurrency"] == 2

        for source, show_stock in (
            ("pixabay", True),
            ("coverr", True),
            ("wavespeed", False),
            ("local", False),
            ("pexels", True),
        ):
            harness.selected = source
            app.run()
            assert [str(item.value) for item in app.exception] == []
            stock_widgets = [
                item for item in app.selectbox
                if item.key.startswith("material_concurrency_select_")
            ]
            assert bool(stock_widgets) is show_stock
            if show_stock:
                assert stock_widgets[0].value == 4
            assert next(
                item for item in app.selectbox
                if item.key.startswith("clip_rendering_concurrency_select_")
            ).value == 2
            assert config.app["material_concurrency"] == 4
