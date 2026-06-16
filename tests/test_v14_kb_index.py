"""
契约测试 — references/database/cancerrisk/json/kb/index.json 所有映射指向实际存在的 MD 文件
T7: on-demand MD knowledge base index
"""
import json
import pathlib
import pytest

pytestmark = pytest.mark.skip(reason="v2.0 重构：kb/index.json 已拆为 references/database/index.json + 各级 index.json，本测试待按新结构重写")

KB = pathlib.Path("references/database/cancerrisk/json/kb")


def test_index_maps_to_existing_md():
    """index.json 中 cancers 和 topics 的每个映射都指向实际存在的 MD 文件"""
    idx = json.loads((KB / "index.json").read_text(encoding="utf-8"))
    for _id, rel in {**idx.get("cancers", {}), **idx.get("topics", {})}.items():
        assert (KB / rel).exists(), f"{_id} -> {rel} missing"
