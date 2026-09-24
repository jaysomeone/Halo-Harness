from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from rolo_claude.config.paths import home, managed_settings_files


@dataclass
class SettingsError:
    path: Union[Path, str]
    message: str
    line: Optional[int] = None
    col: Optional[int] = None


@dataclass
class SettingsLayer:
    name: str
    path: Optional[Path]
    base_dir: Path
    data: dict
    error: Optional[SettingsError] = None


class Settings:
    def __init__(
        self,
        raw: dict,
        layers: list[SettingsLayer],
        errors: list[SettingsError],
    ):
        self._raw = raw
        self._layers = layers
        self._errors = errors

    @property
    def raw(self) -> dict:
        return self._raw

    @property
    def layers(self) -> list[SettingsLayer]:
        return self._layers

    @property
    def errors(self) -> list[SettingsError]:
        return self._errors

    @property
    def model(self) -> Optional[str]:
        return self._raw.get("model")

    @property
    def permissions_allow(self) -> list[str]:
        return self._raw.get("permissions", {}).get("allow", [])

    @property
    def permissions_ask(self) -> list[str]:
        return self._raw.get("permissions", {}).get("ask", [])

    @property
    def permissions_deny(self) -> list[str]:
        return self._raw.get("permissions", {}).get("deny", [])

    @property
    def permissions_additional_directories(self) -> list[str]:
        return self._raw.get("permissions", {}).get("additionalDirectories", [])

    @property
    def permissions_default_mode(self) -> Optional[str]:
        return self._raw.get("permissions", {}).get("defaultMode")

    @property
    def env(self) -> dict[str, str]:
        return self._raw.get("env", {})

    @property
    def hooks(self) -> dict:
        return self._raw.get("hooks", {})

    @property
    def disable_all_hooks(self) -> bool:
        return self._raw.get("disableAllHooks", False)

    @property
    def auto_memory_enabled(self) -> bool:
        # Real key is the flat `autoMemoryEnabled` (see finding C's key
        # inventory) -- not a nested `autoMemory.enabled` object.
        return self._raw.get("autoMemoryEnabled", True)

    @property
    def auto_memory_directory(self) -> Optional[str]:
        return self._raw.get("autoMemoryDirectory")

    @property
    def claude_md_excludes(self) -> list[str]:
        return self._raw.get("claudeMdExcludes", [])

    @property
    def instruction_files(self) -> str:
        return self._raw.get("instructionFiles", "claude-md-or-agents-md")

    @property
    def theme(self) -> Optional[str]:
        return self._raw.get("theme")

    @property
    def tui(self) -> Optional[str]:
        return self._raw.get("tui")

    @property
    def attribution(self) -> Optional[object]:
        return self._raw.get("attribution")

    @property
    def statusline(self) -> Optional[dict]:
        return self._raw.get("statusLine")

    @property
    def respect_gitignore(self) -> bool:
        return self._raw.get("respectGitignore", True)

    def effort_level(self, model: str) -> Optional[str]:
        return self._raw.get("modelSettings", {}).get(model, {}).get("effortLevel")


def _read_json_file(path: Path) -> tuple[dict, Optional[SettingsError]]:
    """Read JSON file, return (data, error). Missing file is not an error."""
    if not path.exists():
        return {}, None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data, None
    except json.JSONDecodeError as e:
        error = SettingsError(
            path=path,
            message=str(e),
            line=e.lineno if hasattr(e, "lineno") else None,
            col=e.colno if hasattr(e, "colno") else None,
        )
        return {}, error
    except Exception as e:
        error = SettingsError(path=path, message=str(e))
        return {}, error


def _apply_default_mode_filter(layer_data: dict, layer_name: str) -> dict:
    """Filter defaultMode for project/local layers."""
    if layer_name not in ("projectSettings", "localSettings"):
        return layer_data

    permissions = layer_data.get("permissions", {})
    default_mode = permissions.get("defaultMode")
    if default_mode not in ("default", "acceptEdits", "plan", "dontAsk"):
        # Remove the key from a copy
        filtered = layer_data.copy()
        if "permissions" in filtered:
            filtered_perms = filtered["permissions"].copy()
            if "defaultMode" in filtered_perms:
                del filtered_perms["defaultMode"]
                filtered["permissions"] = filtered_perms
        return filtered
    return layer_data


def _merge_layers(layers: list[SettingsLayer]) -> dict:
    """Merge layers low-to-high precedence (`layers` must already be in that
    order) per the spec's algorithm.

    `last_is_list` tracks, per key, whether the MOST RECENTLY (so far)
    processed layer that set this key gave it a list value -- this is what
    the final pass uses to decide list-concatenation vs. plain overwrite,
    so a key that flips type between layers always resolves to whatever the
    HIGHEST layer touching it actually set, regardless of type, instead of
    a naive "once a list, always accumulate" rule that could let a stale
    lower-layer scalar survive under a higher layer's list (or vice versa).
    """
    filtered_layers = []
    for layer in layers:
        filtered_data = _apply_default_mode_filter(layer.data, layer.name)
        filtered_layers.append(SettingsLayer(
            name=layer.name,
            path=layer.path,
            base_dir=layer.base_dir,
            data=filtered_data,
            error=layer.error,
        ))

    result: dict[str, Any] = {}
    list_accumulator: dict[str, list[Any]] = {}
    last_is_list: dict[str, bool] = {}
    env_accumulator: dict[str, str] = {}
    hooks_accumulator: dict[str, list[dict]] = {}
    # `permissions.allow/ask/deny/additionalDirectories` are list-valued but
    # live ONE LEVEL DOWN, inside the top-level "permissions" dict -- not
    # top-level keys themselves. Merged with the exact same list-
    # concatenation-with-type-flip-safety rule as everything else, just
    # scoped to this one sub-dict, then folded into `result["permissions"]`
    # at the end alongside its own scalar sub-keys (defaultMode,
    # disableBypassPermissionsMode, blockReadsOutsideWorkingDirectories, ...).
    perm_result: dict[str, Any] = {}
    perm_list_accumulator: dict[str, list[Any]] = {}
    perm_last_is_list: dict[str, bool] = {}

    def _dedupe_preserve_order(items: list) -> list:
        seen: set = set()
        deduped = []
        for item in items:
            try:
                already_seen = item in seen
            except TypeError:
                already_seen = False  # unhashable item (e.g. a dict) -- never dedupe it
            if not already_seen:
                try:
                    seen.add(item)
                except TypeError:
                    pass
                deduped.append(item)
        return deduped

    for layer in filtered_layers:
        for key, value in layer.data.items():
            # Whole-value keys (highest wins outright, never merged).
            if key in ("fallbackModel", "modelPicker", "availableModels", "modelSettings"):
                result[key] = value
                last_is_list[key] = False
                continue

            if key == "env":
                if isinstance(value, dict):
                    env_accumulator.update(value)
                continue

            if key == "hooks":
                if isinstance(value, dict):
                    for event_name, matcher_groups in value.items():
                        if isinstance(matcher_groups, list):
                            hooks_accumulator.setdefault(event_name, [])
                            for group in matcher_groups:
                                if isinstance(group, dict):
                                    tagged_group = dict(group)
                                    tagged_group["_source"] = layer.name
                                    hooks_accumulator[event_name].append(tagged_group)
                continue

            if key == "permissions":
                if isinstance(value, dict):
                    for pkey, pvalue in value.items():
                        if isinstance(pvalue, list):
                            perm_list_accumulator.setdefault(pkey, []).extend(pvalue)
                            perm_last_is_list[pkey] = True
                        else:
                            perm_result[pkey] = pvalue
                            perm_last_is_list[pkey] = False
                continue

            if isinstance(value, list):
                list_accumulator.setdefault(key, []).extend(value)
                last_is_list[key] = True
            else:
                result[key] = value
                last_is_list[key] = False

    # Resolve list-vs-scalar per key based on the LAST (highest-precedence)
    # layer that touched it, dedupe-preserving-order for the list case.
    for key, is_list in last_is_list.items():
        if not is_list:
            continue  # result[key] already holds the correct scalar value
        result[key] = _dedupe_preserve_order(list_accumulator.get(key, []))

    for pkey, is_list in perm_last_is_list.items():
        if not is_list:
            continue
        perm_result[pkey] = _dedupe_preserve_order(perm_list_accumulator.get(pkey, []))

    if perm_result:
        result["permissions"] = perm_result
    if env_accumulator:
        result["env"] = env_accumulator
    if hooks_accumulator:
        result["hooks"] = hooks_accumulator

    return result


def resolve_settings(
    cwd: Union[str, Path],
    *,
    settings_flag: Optional[str] = None,
    setting_sources: Optional[list[str]] = None,
) -> Settings:
    """
    Resolve settings from all layers.
    
    Args:
        cwd: Current working directory
        settings_flag: Optional JSON string or file path for flagSettings
        setting_sources: Optional list of sources to include from {user, project, local}
    
    Returns:
        Settings object with merged configuration
    """
    cwd_path = Path(cwd).resolve()
    layers: list[SettingsLayer] = []
    errors: list[SettingsError] = []

    # Helper to create a layer
    def add_layer(name: str, path: Optional[Path], base_dir: Path, data: dict, error: Optional[SettingsError] = None):
        layer = SettingsLayer(
            name=name,
            path=path,
            base_dir=base_dir,
            data=data,
            error=error
        )
        layers.append(layer)
        if error:
            errors.append(error)

    # Determine which standard sources to include
    standard_sources = ["user", "project", "local"]
    if setting_sources is not None:
        standard_sources = [s for s in standard_sources if s in setting_sources]

    # userSettings
    if "user" in standard_sources:
        user_path = home() / ".claude" / "settings.json"
        data, error = _read_json_file(user_path)
        add_layer("userSettings", user_path, user_path.parent, data, error)

    # projectSettings
    if "project" in standard_sources:
        project_path = cwd_path / ".claude" / "settings.json"
        data, error = _read_json_file(project_path)
        add_layer("projectSettings", project_path, project_path.parent, data, error)

    # localSettings
    if "local" in standard_sources:
        local_path = cwd_path / ".claude" / "settings.local.json"
        data, error = _read_json_file(local_path)
        add_layer("localSettings", local_path, local_path.parent, data, error)

    # flagSettings (always included if provided)
    if settings_flag is not None:
        base_dir = cwd_path
        path: Optional[Path] = None
        data: dict = {}
        error: Optional[SettingsError] = None

        # First try to parse as JSON
        try:
            data = json.loads(settings_flag)
            # Successfully parsed as JSON string
        except json.JSONDecodeError:
            # Not JSON, try as file path
            try:
                flag_path = Path(settings_flag)
                if flag_path.is_absolute():
                    path = flag_path
                else:
                    path = cwd_path / settings_flag
                data, read_error = _read_json_file(path)
                if read_error:
                    error = read_error
                if path.exists():
                    base_dir = path.parent
            except Exception as e:
                error = SettingsError(
                    path=settings_flag,
                    message=f"Failed to parse flag: {e}"
                )
                data = {}

        add_layer("flagSettings", path, base_dir, data, error)

    # policySettings (always included)
    for policy_path in managed_settings_files():
        data, error = _read_json_file(policy_path)
        add_layer("policySettings", policy_path, policy_path.parent, data, error)

    # Merge all layers
    merged = _merge_layers(layers)

    return Settings(raw=merged, layers=layers, errors=errors)
