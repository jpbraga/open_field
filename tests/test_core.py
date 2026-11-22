from pathlib import Path

import pytest

from openfield.adapters.shell import build_command
from openfield.config import Config, ProgressSpec, ServiceSpec, StepSpec
from openfield.expr import ExprError, evaluate, render, render_str, snap_up
from openfield.params import ParamError, resolve
from openfield.resources import Locks
from openfield.runner import StepProgress

CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_expressions():
    assert evaluate("50 + 6*max(0, width*height/1048576 - 1)", {"width": 2048, "height": 2048}) == 68
    assert evaluate("guidance if guidance is not None else (2.5 if negative_prompt else None)",
                    {"guidance": None, "negative_prompt": "blurry"}) == 2.5
    assert evaluate("r.status == 'done' and r['progress'] > 0.5", {"r": {"status": "done", "progress": 0.7}})
    assert snap_up(120, 124, 17, 345) == 124 and snap_up(240, 124, 17, 345) == 243 and snap_up(346, 124, 17, 345) is None
    with pytest.raises(ExprError):
        evaluate("__import__('os')", {})
    with pytest.raises(ExprError):
        evaluate("x.__class__", {"x": {}})


def test_templates():
    assert render("{a}", {"a": 3}) == 3
    assert render("w={a}x{b} {{x}}", {"a": 1, "b": 2.5}) == "w=1x2.5 {x}"
    assert render({"k": ["{a}", "n"]}, {"a": True}) == {"k": [True, "n"]}
    with pytest.raises(ExprError):
        render_str("{missing}", {})


def test_service_configs_load():
    config = Config(CONFIG).load()
    assert not config.errors, config.errors
    assert {"qwen-image", "qwen-image-edit", "minimax-h3-video", "video-upscale", "omlx"} <= set(config.services)


def test_params_qwen_and_h3():
    config = Config(CONFIG).load()
    q = resolve(config.services["qwen-image"], {"prompt": " a cat ", "negative_prompt": "blur"}, {})
    assert q["prompt"] == "a cat" and q["width"] == 1024 and q["guidance"] == 2.5 and isinstance(q["seed"], int)
    with pytest.raises(ParamError, match="multiple of 16"):
        resolve(config.services["qwen-image"], {"prompt": "x", "width": 1000}, {})
    with pytest.raises(ParamError, match="unknown parameter"):
        resolve(config.services["qwen-image"], {"prompt": "x", "bogus": 1}, {})
    h = resolve(config.services["minimax-h3-video"], {"prompt": "x", "duration_s": 10}, {"first_frame": 1})
    assert h["num_frames"] == 243 and h["first_frame"] == "<upload>"
    assert resolve(config.services["minimax-h3-video"], {"prompt": "x", "duration_s": 14.4}, {})["num_frames"] == 345
    with pytest.raises(ParamError, match="1344x768"):
        resolve(config.services["minimax-h3-video"], {"prompt": "x", "width": 1344, "height": 1344}, {})
    with pytest.raises(ParamError, match="both width and height"):
        resolve(config.services["qwen-image-edit"], {"prompt": "x", "width": 512}, {"images": 2})
    with pytest.raises(ParamError, match="required"):
        resolve(config.services["qwen-image-edit"], {"prompt": "x"}, {})


def test_command_building_expands_lists_and_never_globs_user_text():
    step = StepSpec(name="s", command=["~/bin/tool", "--image-paths", "{images}", "--prompt", "{prompt}", "--out", "{work}/o.png"],
                    optional_args=[{"when": "steps", "args": ["--steps", "{steps}"]}])
    cmd = build_command(step, {"images": ["/a.png", "/b.png"], "prompt": "~/* a cat [x]", "work": "/w", "steps": 4})
    assert cmd[0].endswith("/bin/tool") and not cmd[0].startswith("~")
    assert cmd[1:4] == ["--image-paths", "/a.png", "/b.png"]
    assert cmd[5] == "~/* a cat [x]" and cmd[-2:] == ["--steps", "4"] and "/w/o.png" in cmd


def test_progress_parsing():
    p = StepProgress(ProgressSpec(kind="regex", regex=r"\|\s*(?P<done>\d+)\s*/\s*(?P<total>\d+)\s*-\s*[\d.]+\s*(?:it/s|s/it)",
                                  start_after=r"generate_video", total_equals="steps", scale_from=0.04, scale_to=0.92,
                                  label="denoising {done}/{total}",
                                  phases=[{"regex": "sampling completed", "progress": 0.93, "label": "decoding"}]),
                     {"steps": 20})
    assert p.feed("|###| 532/532 - 400.0MB/s") is None
    assert p.feed("  |==>  |  1/20 -  63.51s/it - ETA") is None  # before generate_video
    p.feed("[I] generate_video 864x480x124")
    frac, label = p.feed("  |==>  |  10/20 -  63.51s/it - ETA")
    assert abs(frac - (0.04 + 0.88 * 0.5)) < 1e-9 and label == "denoising 10/20"
    assert p.feed("[I] sampling completed, taking 900s") == (0.93, "decoding")
    q = StepProgress(ProgressSpec(kind="regex", regex=r"(?P<done>\d+)/(?P<total>\d+) \[", total_equals="[20, 21]"), {})
    assert q.feed("Loading 4/4 [00:01]") is None and q.feed(" 50%| 10/20 [00:10<00:10]")[0] == 0.5


EXCL = ServiceSpec(id="img", name="i", kind="shell", resources=["gpu"], steps=[{"name": "s", "command": ["x"]}])
VID = ServiceSpec(id="vid", name="v", kind="shell", resources=["gpu"], steps=[{"name": "s", "command": ["x"]}])
LLM = ServiceSpec(id="llm", name="l", kind="http_proxy", resources=["gpu"], lock="shared", max_concurrency=2,
                  proxy={"upstream": "http://x"})


def test_locks_one_slot():
    locks = Locks()
    assert locks.try_acquire(LLM, "r1") is None and locks.try_acquire(LLM, "r2") is None
    assert "its limit" in locks.try_acquire(LLM, "r3")["reason"]
    assert locks.try_acquire(EXCL, "j1")["services"] == ["llm"]
    locks.release(LLM, "r1"), locks.release(LLM, "r2")
    assert locks.try_acquire(EXCL, "j1") is None
    assert "in use by img" in locks.try_acquire(LLM, "r4")["reason"]
    assert "already running" in locks.try_acquire(EXCL, "j2")["reason"]


def test_locks_parallel_slots_and_external_tools():
    slots = {"gpu": 2}
    locks = Locks(capacity=lambda r: slots[r])
    assert locks.try_acquire(EXCL, "j1") is None and locks.try_acquire(VID, "j2") is None
    full = locks.try_acquire(LLM, "r1")
    assert full["used"] == 2 and "all 2 gpu slots" in full["reason"]
    locks.release(VID, "j2")
    assert locks.try_acquire(LLM, "r1") is None and locks.try_acquire(LLM, "r2") is None  # calls share one slot
    assert locks.snapshot(["gpu"])["gpu"]["used"] == 2
    locks.release(LLM, "r1"), locks.release(LLM, "r2")
    assert locks.try_acquire(VID, "j3", external={"gpu": 1}) is not None  # a tool outside the daemon takes a slot
    slots["gpu"] = 3
    assert locks.try_acquire(VID, "j3", external={"gpu": 1}) is None
