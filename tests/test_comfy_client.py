"""Comfy cancel-on-timeout and the Discord timeout message. No live Comfy, no network."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.imagediffusion import (
    GENERATION_CANCEL_GRACE_SEC,
    GENERATION_TIMEOUT_LLM_ENHANCE_SEC,
    GENERATION_TIMEOUT_SEC,
    ImageDiffusion,
)
from cogs.utils import comfy_client
from cogs.utils.image_models import (
    ComfyFlux3KleinInt8Model,
    ComfyKrea2IdentityEditModel,
    ComfyKrea2TurboModel,
    ImageCreation,
    Models,
)

_OUTPUT = {
    "filename": "krea2_identity_edit_bot_00001_.png",
    "subfolder": "",
    "type": "output",
}


class _Clock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds if seconds else 0.01


class _JumpClock:
    """First sleep ends a 600s Comfy wait. Later sleeps advance the cancel grace."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        if self.now < 600:
            self.now = 600
        else:
            self.now += seconds if seconds else 0.01


class _Response:
    def raise_for_status(self):
        return None


def _patch_clock(monkeypatch, clock):
    monkeypatch.setattr(comfy_client.time, "time", clock.time)
    monkeypatch.setattr(comfy_client.time, "sleep", clock.sleep)


def _history_with_images(prompt_id: str) -> dict:
    return {prompt_id: {"outputs": {"9": {"images": [dict(_OUTPUT)]}}}}


def test_wait_for_result_interrupts_a_running_prompt_then_raises(monkeypatch):
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    posts = []
    state = {"interrupted": False}

    def get_history(prompt_id, host=None, port=None):
        if state["interrupted"]:
            return _history_with_images(prompt_id)
        return {}

    def get_queue(host=None, port=None):
        if state["interrupted"]:
            return {"queue_running": [], "queue_pending": []}
        return {"queue_running": [[1, "pid-1", {}, {}, []]], "queue_pending": []}

    def post(url, json=None, timeout=None, **kwargs):
        posts.append((url, json))
        if str(url).endswith("/interrupt"):
            state["interrupted"] = True
        return _Response()

    monkeypatch.setattr(comfy_client, "get_history", get_history)
    monkeypatch.setattr(comfy_client, "get_queue", get_queue)
    monkeypatch.setattr(comfy_client.requests, "post", post)

    with pytest.raises(comfy_client.ComfyPromptTimeout) as caught:
        comfy_client.wait_for_result(
            "pid-1",
            host="127.0.0.1",
            port=8188,
            timeout_sec=0,
            poll_interval=1,
            cancel_grace_sec=30,
        )

    assert isinstance(caught.value, TimeoutError)
    assert caught.value.image_infos == [_OUTPUT]
    assert posts == [("http://127.0.0.1:8188/interrupt", {"prompt_id": "pid-1"})]
    assert state["interrupted"] is True


def test_wait_for_result_deletes_a_pending_prompt_without_interrupt(monkeypatch):
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    posts = []
    state = {"deleted": False}

    def get_history(prompt_id, host=None, port=None):
        return {}

    def get_queue(host=None, port=None):
        if state["deleted"]:
            return {"queue_running": [], "queue_pending": []}
        return {"queue_running": [], "queue_pending": [[2, "pid-1", {}, {}, []]]}

    def post(url, json=None, timeout=None, **kwargs):
        posts.append((url, json))
        if isinstance(json, dict) and "delete" in json:
            state["deleted"] = True
        return _Response()

    monkeypatch.setattr(comfy_client, "get_history", get_history)
    monkeypatch.setattr(comfy_client, "get_queue", get_queue)
    monkeypatch.setattr(comfy_client.requests, "post", post)

    with pytest.raises(comfy_client.ComfyPromptTimeout) as caught:
        comfy_client.wait_for_result(
            "pid-1",
            host="127.0.0.1",
            port=8188,
            timeout_sec=0,
            poll_interval=1,
            cancel_grace_sec=10,
        )

    assert posts == [("http://127.0.0.1:8188/queue", {"delete": ["pid-1"]})]
    assert caught.value.image_infos == []
    assert state["deleted"] is True


def test_wait_for_result_returns_images_already_in_history(monkeypatch):
    def get_history(prompt_id, host=None, port=None):
        return _history_with_images(prompt_id)

    def get_queue(host=None, port=None):
        raise AssertionError("finished prompt must not be interrupted")

    def post(url, json=None, timeout=None, **kwargs):
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(comfy_client, "get_history", get_history)
    monkeypatch.setattr(comfy_client, "get_queue", get_queue)
    monkeypatch.setattr(comfy_client.requests, "post", post)

    images = comfy_client.wait_for_result(
        "pid-1",
        host="127.0.0.1",
        port=8188,
        timeout_sec=0,
        poll_interval=1,
        cancel_grace_sec=5,
    )
    assert images == [_OUTPUT]


def test_wait_for_result_keeps_an_execution_error_when_the_prompt_is_done(monkeypatch):
    clock = _Clock()
    _patch_clock(monkeypatch, clock)
    posts = []

    def get_history(prompt_id, host=None, port=None):
        return {
            prompt_id: {
                "status": {
                    "messages": [
                        [
                            "execution_error",
                            {
                                "exception_message": "boom",
                                "node_type": "VAEEncode",
                                "node_id": "12",
                            },
                        ]
                    ]
                },
                "outputs": {},
            }
        }

    def get_queue(host=None, port=None):
        return {"queue_running": [], "queue_pending": []}

    def post(url, json=None, timeout=None, **kwargs):
        posts.append(url)
        return _Response()

    monkeypatch.setattr(comfy_client, "get_history", get_history)
    monkeypatch.setattr(comfy_client, "get_queue", get_queue)
    monkeypatch.setattr(comfy_client.requests, "post", post)

    with pytest.raises(RuntimeError, match="boom"):
        comfy_client.wait_for_result(
            "pid-1",
            host="127.0.0.1",
            port=8188,
            timeout_sec=1,
            poll_interval=1,
            cancel_grace_sec=5,
        )
    assert posts == []


def test_identity_edit_deletes_input_only_after_the_prompt_is_cancelled(monkeypatch):
    clock = _JumpClock()
    _patch_clock(monkeypatch, clock)
    events = []
    state = {"interrupted": False}

    def get_history(prompt_id, host=None, port=None):
        if state["interrupted"]:
            return _history_with_images(prompt_id)
        return {}

    def get_queue(host=None, port=None):
        if state["interrupted"]:
            return {"queue_running": [], "queue_pending": []}
        return {"queue_running": [[1, "pid-1", {}, {}, []]], "queue_pending": []}

    def post(url, json=None, timeout=None, **kwargs):
        if str(url).endswith("/interrupt"):
            state["interrupted"] = True
            events.append("interrupt")
        else:
            events.append(("post", url, json))
        return _Response()

    def upload_image(*args, **kwargs):
        events.append("upload")
        return {
            "name": "bot_edit_x.png",
            "subfolder": "",
            "type": "input",
            "load_name": "bot_edit_x.png",
        }

    def post_prompt(*args, **kwargs):
        events.append("post_prompt")
        return {"prompt_id": "pid-1"}

    def delete_media_file(filename, subfolder="", image_type="output"):
        events.append(("delete_input", image_type, filename))
        return True

    def delete_media_files(infos):
        events.append(("delete_outputs", tuple(info.get("filename") for info in infos)))
        return len(infos or [])

    monkeypatch.setattr(comfy_client, "get_history", get_history)
    monkeypatch.setattr(comfy_client, "get_queue", get_queue)
    monkeypatch.setattr(comfy_client.requests, "post", post)
    monkeypatch.setattr(comfy_client, "upload_image", upload_image)
    monkeypatch.setattr(comfy_client, "post_prompt", post_prompt)
    monkeypatch.setattr(comfy_client, "delete_media_file", delete_media_file)
    monkeypatch.setattr(comfy_client, "delete_media_files", delete_media_files)

    model = ComfyKrea2IdentityEditModel()
    model._ui_workflow_template = {}
    model.comfy_host = "127.0.0.1"
    model.comfy_port = 8188
    image = ImageCreation(
        "make the coat red",
        Models.KREA2_IDENTITY_EDIT,
        source_image_bytes=b"source-bytes",
        edit_width=832,
        edit_height=1152,
    )

    with pytest.raises(TimeoutError):
        ComfyKrea2IdentityEditModel.generate.__wrapped__(model, image)

    assert events.index("interrupt") < events.index(("delete_input", "input", "bot_edit_x.png"))
    assert ("delete_outputs", ("krea2_identity_edit_bot_00001_.png",)) in events
    assert events.index("interrupt") < events.index(
        ("delete_outputs", ("krea2_identity_edit_bot_00001_.png",))
    )


def test_workflow_generate_deletes_leftover_outputs_on_timeout(monkeypatch):
    infos = [dict(_OUTPUT)]

    def wait_for_result(*args, **kwargs):
        raise comfy_client.ComfyPromptTimeout("timed out", image_infos=infos)

    deleted = []

    def delete_media_files(files):
        deleted.append(list(files))
        return len(files or [])

    monkeypatch.setattr(comfy_client, "wait_for_result", wait_for_result)
    monkeypatch.setattr(comfy_client, "post_prompt", lambda *args, **kwargs: {"prompt_id": "p"})
    monkeypatch.setattr(comfy_client, "delete_media_files", delete_media_files)

    flux = ComfyFlux3KleinInt8Model()
    flux._ui_workflow_template = {}
    image = ImageCreation("a mountain path", Models.FLUX3_KLEIN_INT8)
    with pytest.raises(TimeoutError):
        ComfyFlux3KleinInt8Model.generate.__wrapped__(flux, image)

    krea = ComfyKrea2TurboModel()
    krea._ui_workflow_template = {}
    krea_image = ImageCreation("a mountain path", Models.KREA2_TURBO, llm_prompt_enhance=False)
    with pytest.raises(TimeoutError):
        ComfyKrea2TurboModel.generate.__wrapped__(krea, krea_image)

    assert deleted == [infos, infos]


def test_timeout_message_depends_on_llm_prompt_enhance(monkeypatch):
    import cogs.imagediffusion as imagediffusion

    monkeypatch.setattr(imagediffusion.comfy_client, "is_comfy_running", lambda *args, **kwargs: True)
    seen = {}
    real_wait_for = asyncio.wait_for

    async def spy(awaitable, timeout=None, **kwargs):
        seen["timeout"] = timeout
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(imagediffusion.asyncio, "wait_for", spy)

    async def run(enhance: bool, exc: BaseException):
        seen.clear()
        channel = SimpleNamespace(send=AsyncMock())
        image = ImageCreation(
            "a cat",
            Models.KREA2_TURBO,
            interaction=SimpleNamespace(
                channel=channel,
                user=SimpleNamespace(id=1, mention="<@1>"),
            ),
            llm_prompt_enhance=enhance,
        )

        class _Model:
            async def generate(self, _image):
                raise exc

        cog = SimpleNamespace(models={Models.KREA2_TURBO: _Model()})
        await ImageDiffusion._image_generation_interaction(cog, image)
        return channel.send.await_args.kwargs["content"], seen["timeout"]

    plain, plain_timeout = asyncio.run(run(False, TimeoutError("comfy")))
    assert plain == (
        "Image generation timed out after 10 minutes. "
        "The image may be too large or too complex. "
        "Try a smaller photo or a simpler prompt."
    )
    assert "LLM" not in plain
    assert "enhance" not in plain.lower()
    assert plain_timeout == GENERATION_TIMEOUT_SEC + GENERATION_CANCEL_GRACE_SEC

    enhanced, enhanced_timeout = asyncio.run(run(True, asyncio.TimeoutError("comfy")))
    assert enhanced == (
        "Image generation timed out after 30 minutes. "
        "Try again without LLM enhance or with a simpler prompt."
    )
    assert "too large" not in enhanced
    assert enhanced_timeout == GENERATION_TIMEOUT_LLM_ENHANCE_SEC + GENERATION_CANCEL_GRACE_SEC
