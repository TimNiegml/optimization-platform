from pathlib import Path
import re


HTML = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    match = re.search(rf"window\.{name}=.*?=>\{{(.*?)\}};", HTML, re.S)
    assert match, f"missing frontend handler {name}"
    return match.group(1)


def test_filtered_bulk_selection_uses_only_visible_values():
    assert "function visibleCheckValues" in HTML
    for handler in ("setObserverChannels", "setSensObjs", "setTuneObjs", "setSolveTargets"):
        assert "visibleCheckValues" in _function_body(handler)
    assert HTML.count("全选筛选结果") >= 4


def test_single_checkbox_changes_do_not_rebuild_long_lists():
    assert "inspectObserver" not in _function_body("toggleObserverChannel")
    assert "renderSensForm" not in _function_body("toggleSensObj")
    assert "renderTuneChannelSelect" not in _function_body("toggleTuneObj")


def test_filter_and_scroll_state_are_restored_after_required_rebuilds():
    assert "const checkFilters={}" in HTML
    assert "function restoreCheckList" in HTML
    assert "restoreCheckList('solveTargetChecks',top)" in HTML
