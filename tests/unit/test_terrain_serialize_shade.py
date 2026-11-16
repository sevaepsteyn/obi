"""Unit tests: terrain.json ``shade`` block serialization (055, additive to 031).

Pins the additive-compatibility contract: no ``shade`` key when the document has none
(pre-055 shape byte-preserved); a full rect table when it does.
"""

from __future__ import annotations

from obi.core.models import TerrainDocument, TerrainShadeMeta, _utcnow
from obi.core.serialize import terrain_to_dict


def test_terrain_dict_omits_shade_when_absent() -> None:
    document = TerrainDocument(generated_at=_utcnow())
    result = terrain_to_dict(document)
    assert "shade" not in result  # pre-055 shape preserved
    assert set(result) == {"generated_at", "profiles", "sites"}


def test_terrain_dict_emits_the_shade_rect_table_when_present() -> None:
    document = TerrainDocument(
        generated_at=_utcnow(),
        shade=TerrainShadeMeta(
            generated_at=_utcnow(),
            width=1600,
            height=1500,
            step_m=30.0,
            sites={"B": (-100.0, -200.0, 300.0, 400.0), "A": (1.0, 2.0, 3.0, 4.0)},
        ),
    )
    shade = terrain_to_dict(document)["shade"]
    assert shade["width"] == 1600 and shade["height"] == 1500 and shade["step_m"] == 30.0
    # sites serialized as JSON lists, key-sorted
    assert list(shade["sites"]) == ["A", "B"]
    assert shade["sites"]["A"] == [1.0, 2.0, 3.0, 4.0]
    assert isinstance(shade["generated_at"], str)
