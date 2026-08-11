from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_LOGO_FILENAME = "NYIST.png"
DEFAULT_BACKGROUND_FILENAME = "ACM.jpg"
DEFAULT_NOTICE_BG_FILENAME = "AC.jpg"


@dataclass
class PluginPaths:
    plugin_root: Path
    package_root: Path
    assets_root: Path
    fonts_root: Path
    images_root: Path
    data_root: Path
    database_path: Path

    @classmethod
    def from_root(cls, plugin_root: Path, data_root: Path | None = None) -> PluginPaths:
        plugin_root = Path(plugin_root).resolve()
        package_root = plugin_root
        assets_root = plugin_root / "assets"
        fonts_root = assets_root / "fonts"
        images_root = assets_root / "images"

        if data_root is None:
            data_root = plugin_root / "data"
        data_root = Path(data_root).resolve()
        database_path = data_root / "nyoj_rank.db"

        return cls(
            plugin_root=plugin_root,
            package_root=package_root,
            assets_root=assets_root,
            fonts_root=fonts_root,
            images_root=images_root,
            data_root=data_root,
            database_path=database_path,
        )

    def ensure(self) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
