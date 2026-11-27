"""double_click / right_click step types: executor (real Chrome), recorder dedupe, models."""

import asyncio
from pathlib import Path

import pytest

from browser_platform.authoring.from_task import events_to_steps
from browser_platform.authoring.recorder import RecordedEvent
from browser_platform.models.workflow import ACTION_TYPES, TERMINAL_TYPES, WorkflowStep

CHROME = Path("/usr/bin/google-chrome")
PAGE = """<html><body>
<div id="t" style="width:120px;height:40px">Target</div><p id="out">none</p>
<script>
const t=document.getElementById('t'), o=document.getElementById('out');
t.addEventListener('click',()=>{ if(o.textContent==='none') o.textContent='single'; });
t.addEventListener('dblclick',()=>{ o.textContent='double'; });
t.addEventListener('contextmenu',(e)=>{ e.preventDefault(); o.textContent='context'; });
</script></body></html>"""


def test_types_are_actions():
    assert {"double_click", "right_click"} <= ACTION_TYPES <= TERMINAL_TYPES


def test_recorded_dblclick_replaces_its_single_clicks():
    el = {"css": "#t", "text": "Target"}
    events = [
        RecordedEvent(type="navigation", url="https://example.org/"),
        RecordedEvent(type="click", element=el, text="Target"),
        RecordedEvent(type="click", element=el, text="Target"),
        RecordedEvent(type="double_click", element=el, text="Target"),
        RecordedEvent(type="right_click", element={"css": "#u"}, text="Row"),
    ]
    steps = [s for s in events_to_steps(events) if s.type != "extract"]
    assert [s.type for s in steps] == ["navigation", "double_click", "right_click"]
    assert steps[1].description == "Double-click Target" and steps[2].description == "Right-click Row"


@pytest.mark.skipif(not CHROME.exists(), reason="no chrome")
@pytest.mark.parametrize("typ,expected", [("double_click", "double"), ("right_click", "context"), ("click", "single")])
def test_executor_runs_click_variants(typ, expected):
    from playwright.async_api import async_playwright

    from browser_platform.executor.deterministic import run_step
    from browser_platform.executor.selectors import build_selector_plan

    async def go():
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=str(CHROME), args=["--no-sandbox"])
            page = await browser.new_page()
            await page.set_content(PAGE)
            step = WorkflowStep(type=typ, cssSelector="#t", target_text="Target")
            await run_step(page, step, variables={}, plan=build_selector_plan(step))
            text = await page.inner_text("#out")
            await browser.close()
            return text

    assert asyncio.run(go()) == expected
