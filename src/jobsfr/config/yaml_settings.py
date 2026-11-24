from collections.abc import Hashable
from pathlib import Path

import yaml
from pydantic_settings import SettingsError


class _SettingsLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, object]:
        seen: set[str] = set()
        for key, _ in node.value:
            if isinstance(key, yaml.ScalarNode) and key.tag == "tag:yaml.org,2002:str":
                if key.value in seen:
                    mark = key.start_mark
                    raise SettingsError(f"Duplicate YAML key at line {mark.line + 1}, column {mark.column + 1}.")
                seen.add(key.value)
        return super().construct_mapping(node, deep=deep)


def read_yaml_settings(path: Path) -> dict[str, object]:
    try:
        contents = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SettingsError(f"Configuration file must be UTF-8: {path}") from exc
    data = yaml.load(contents, Loader=_SettingsLoader)
    if data is None:
        return {}
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise SettingsError(f"Expected a YAML mapping of setting names in {path}.")
    return data
