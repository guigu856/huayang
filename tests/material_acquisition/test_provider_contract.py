"""八个素材源对统一协议的结构回归测试。"""

from collections.abc import Callable

import pytest

from components.material_acquisition.archive_org import ArchiveOrgSource
from components.material_acquisition.coverr import CoverrSource
from components.material_acquisition.mixkit import MixkitSource
from components.material_acquisition.pexels import PexelsSource
from components.material_acquisition.pixabay import PixabayVideoSource
from components.material_acquisition.pond5_pd import Pond5PDSource
from components.material_acquisition.unsplash import UnsplashSource
from components.material_acquisition.videvo import VidevoSource


@pytest.mark.parametrize(
    "provider_factory",
    [
        PexelsSource,
        PixabayVideoSource,
        CoverrSource,
        UnsplashSource,
        MixkitSource,
        VidevoSource,
        Pond5PDSource,
        ArchiveOrgSource,
    ],
)
def test_provider_implements_search_and_download(
    provider_factory: Callable[[], object],
) -> None:
    provider = provider_factory()

    assert isinstance(getattr(provider, "name", None), str)
    assert callable(getattr(provider, "is_available", None))
    assert callable(getattr(provider, "search", None))
    assert callable(getattr(provider, "download", None))
