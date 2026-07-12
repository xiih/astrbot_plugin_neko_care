import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

plugin_root = Path(__file__).resolve().parents[1]
plugin_package = types.ModuleType("astrbot_plugin_neko_care")
plugin_package.__path__ = [str(plugin_root)]
sys.modules.setdefault("astrbot_plugin_neko_care", plugin_package)
catgirl_module = importlib.import_module("astrbot_plugin_neko_care.catgirl")
CatgirlService = catgirl_module.CatgirlService


def test_save_image_src_allows_astrbot_temp_file(tmp_path, monkeypatch):
    astrbot_temp = tmp_path / "data" / "temp"
    astrbot_temp.mkdir(parents=True)
    source = astrbot_temp / "media_image_test.jpg"
    source.write_bytes(b"astrbot-media")
    destination = tmp_path / "plugin_data" / "pic" / "copy.tmp"

    service = CatgirlService.__new__(CatgirlService)
    service.upload_dir = tmp_path / "plugin_data" / "pic"
    service.astrbot_temp_dir = astrbot_temp

    async def run_inline(func, *args):
        return func(*args)

    monkeypatch.setattr(catgirl_module.asyncio, "to_thread", run_inline)

    asyncio.run(service._save_image_src(str(source), destination))

    assert destination.read_bytes() == b"astrbot-media"


def test_save_image_src_rejects_other_local_file(tmp_path):
    astrbot_temp = tmp_path / "data" / "temp"
    astrbot_temp.mkdir(parents=True)
    source = tmp_path / "private.jpg"
    source.write_bytes(b"private")

    service = CatgirlService.__new__(CatgirlService)
    service.upload_dir = tmp_path / "plugin_data" / "pic"
    service.astrbot_temp_dir = astrbot_temp

    with pytest.raises(PermissionError, match="安全限制"):
        asyncio.run(service._save_image_src(str(source), tmp_path / "copy.tmp"))
