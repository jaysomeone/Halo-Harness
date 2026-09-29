from __future__ import annotations
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from rolo_claude.config.paths import claude_config_dir, managed_settings_files

# autoMemoryDirectory is never sourced from projectSettings, regardless of
# trust (finding 2, [bin sec.12]: "policy/flag/local/user, never project").
_PROJECT_EXCLUDED_KEYS = frozenset({"autoMemoryDirectory"})

# Keys dropped from an UNTRUSTED project/local layer before merging (finding
# 6/D-CFG); deny/ask stay -- only allow/additionalDirectories/env/hooks/
# autoMemoryDirectory require trust.
_TRUST_GATED_PERMISSION_KEYS = frozenset({"allow", "additionalDirectories"})
_TRUST_GATED_TOP_KEYS = frozenset({"env", "hooks", "autoMemoryDirectory"})


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
        permission_rule_origins: "Optional[dict[str, dict[str, Path]]]" = None,
    ):
        self._raw = raw
        self._layers = layers
        self._errors = errors
        # finding 15: {"allow"|"ask"|"deny": {rule_text: base_dir}} -- which
        # LAYER's base_dir a given permission rule STRING should resolve a
        # relative path value against (first TRUSTED-FILTERED layer to
        # mention that exact string wins; see _merge_layers). None for a
        # Settings built directly (e.g. `Settings(raw=..., layers=[],
        # errors=[])` in a test) -- permissions.build_rules_from_settings
        # falls back to `cwd` for every rule in that case, same as before.
        self._permission_rule_origins = permission_rule_origins or {}

    def permission_rule_base_dir(self, action: str, rule_text: str) -> Optional[Path]:
        return self._permission_rule_origins.get(action, {}).get(rule_text)

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
        mode = self._raw.get("permissions", {}).get("defaultMode")
        return "default" if mode == "manual" else mode

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
        """`instructionFiles` is an option of the built-in `agents-md`
        plugin, stored at `pluginConfigs[<plugin>].options.instructionFiles`
        -- NOT a top-level settings key [bin sec.8, finding 9]. A legacy
        top-level `projectInstructions` maps onto the same vocabulary. A
        bare top-level `instructionFiles` (not real Claude Code shape, but
        convenient for our own tests/settings authors) is tried last."""
        legacy_map = {
            "none": "managed-only", "claude": "claude-md",
            "agents-fallback": "claude-md-or-agents-md", "both": "claude-md-and-agents-md",
        }
        legacy = self._raw.get("projectInstructions")
        default = legacy_map.get(legacy, "claude-md-or-agents-md") if legacy else "claude-md-or-agents-md"

        plugin_configs = self._raw.get("pluginConfigs")
        if isinstance(plugin_configs, dict):
            for cfg in plugin_configs.values():
                if isinstance(cfg, dict):
                    opts = cfg.get("options")
                    if isinstance(opts, dict) and "instructionFiles" in opts:
                        return opts["instructionFiles"]
        return self._raw.get("instructionFiles", default)

    @property
    def effective_env(self) -> dict[str, str]:
        """shell <- user <- trusted project/local <- flag <- policy [D-CFG]:
        the shell's own environment is the floor, and every value the merged
        settings chain sets (already trust-filtered by `resolve_settings`)
        overrides it -- "settings env beats the shell env"."""
        result = dict(os.environ)
        result.update({str(k): str(v) for k, v in self.env.items()})
        return result

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
    def input_needed_notif_enabled(self) -> bool:
        """U5 scope D: "terminal bell + notify-send/toast when input is
        needed and `inputNeededNotifEnabled`" -- the bell itself
        (`app.bell()`) always fires (a terminal bell is cheap and
        harmless even when unwanted); this key gates ONLY the extra
        `notify-send`/toast desktop notification, matching the brief's own
        naming. Defaults False (opt-in -- a desktop notification is a
        bigger interruption than a bell, so it stays off until asked for)."""
        return bool(self._raw.get("inputNeededNotifEnabled", False))

    @property
    def respect_gitignore(self) -> bool:
        return self._raw.get("respectGitignore", True)

    def effort_level(self, model: str) -> Optional[str]:
        return self._raw.get("modelSettings", {}).get(model, {}).get("effortLevel")

    @property
    def effort_level_default(self) -> Optional[str]:
        """H14 scope C: the top-level `effortLevel` key (Claude Code's
        work-box settings.json carries this alongside `model`/
        `modelSettings`) -- the settings-wide default effort when no
        per-model `modelSettings.<id>.effortLevel` overrides it."""
        value = self._raw.get("effortLevel")
        return value if isinstance(value, str) and value else None

    def resolved_effort_level(self) -> Optional[str]:
        """H14 scope C: the effort a session should default to from
        settings alone (`--effort` on the command line always wins over
        this -- callers only consult it when nothing more specific was
        given). `modelSettings.<id>.effortLevel`, keyed by the top-level
        `model` setting's own value (a trailing `[1m]`-style context-window
        suffix stripped first, matching Claude Code's own convention -- see
        tests/helpers/fake_home.py's `"claude-fable-5-1[1m]"` /
        `modelSettings["claude-fable-5-1"]` pair), wins over the plain
        top-level `effortLevel` fallback; None when neither is set (the
        provider's own default effort applies, unchanged)."""
        model_setting = self._raw.get("model")
        if isinstance(model_setting, str) and model_setting:
            bare = model_setting.split("[", 1)[0]
            per_model = self.effort_level(bare)
            if per_model:
                return per_model
        return self.effort_level_default

    @property
    def plans_directory(self) -> Optional[str]:
        """H6 scope C: `plansDirectory` -- an override for where plan-mode
        plan files are written (default `~/.claude/plans`,
        `config.paths.plans_dir()`); None means "use the default"."""
        return self._raw.get("plansDirectory")

    @property
    def subagent_model(self) -> Optional[str]:
        """H6 scope A: a settings-level `subagentModel` fallback in the
        model-resolution chain (invocation -> frontmatter -> here/
        `CLAUDE_CODE_SUBAGENT_MODEL` -> parent). Real Claude Code sources
        this from the env var only; a settings key is this harness's own
        convenience extension, tried AFTER the env var (config/agents_md.py
        owns the actual precedence)."""
        return self._raw.get("subagentModel")


def _read_json_file(path: Path) -> tuple[dict, Optional[SettingsError]]:
    """Read JSON file (utf-8-sig -- a PowerShell 5.1 `Out-File` BOM must not
    make `json.loads` drop the whole file [finding 11]), return (data,
    error). Missing file is not an error. A syntactically valid JSON
    document whose ROOT isn't an object (e.g. `[]` or a bare string) is
    reported as an error and treated as an empty layer rather than handed
    to callers that assume every layer is dict-shaped [finding 11]."""
    if not path.exists():
        return {}, None

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
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
    if not isinstance(data, dict):
        error = SettingsError(path=path, message=f"settings file must contain a JSON object, got {type(data).__name__}")
        return {}, error
    return data, None


def _apply_default_mode_filter(layer_data: dict, layer_name: str) -> dict:
    """Filter defaultMode for project/local layers. `manual` is Claude
    Code's `default` display alias [bin sec.1] -- allowed through here
    unchanged; `Settings.permissions_default_mode` normalizes it."""
    if layer_name not in ("projectSettings", "localSettings"):
        return layer_data

    permissions = layer_data.get("permissions", {})
    if not isinstance(permissions, dict):
        return layer_data
    default_mode = permissions.get("defaultMode")
    if default_mode not in ("default", "acceptEdits", "plan", "dontAsk", "manual"):
        # Remove the key from a copy
        filtered = layer_data.copy()
        if "permissions" in filtered:
            filtered_perms = filtered["permissions"].copy()
            if "defaultMode" in filtered_perms:
                del filtered_perms["defaultMode"]
                filtered["permissions"] = filtered_perms
        return filtered
    return layer_data


def _apply_trust_filter(layer_data: dict, layer_name: str, trusted: bool) -> dict:
    """Drop the keys that require a trusted folder from an untrusted
    project/local layer before it ever reaches `_merge_layers` (finding 6):
    `permissions.allow`/`additionalDirectories`, `env`, `hooks`,
    `autoMemoryDirectory`. `permissions.deny`/`ask` are always kept."""
    if trusted or layer_name not in ("projectSettings", "localSettings"):
        return layer_data
    filtered = dict(layer_data)
    for key in _TRUST_GATED_TOP_KEYS:
        filtered.pop(key, None)
    permissions = filtered.get("permissions")
    if isinstance(permissions, dict):
        filtered_perms = dict(permissions)
        for key in _TRUST_GATED_PERMISSION_KEYS:
            filtered_perms.pop(key, None)
        filtered["permissions"] = filtered_perms
    return filtered


def _merge_layers(layers: list[SettingsLayer], trusted: bool = True) -> "tuple[dict, dict]":
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
        filtered_data = _apply_trust_filter(filtered_data, layer.name, trusted)
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
    # finding 15: {"allow"|"ask"|"deny": {rule_text: base_dir}} -- first
    # trusted-filtered layer to mention an exact rule STRING wins; a
    # relative path-rule value must resolve against the layer it actually
    # came from (e.g. userSettings' own ~/.claude), not always `cwd`.
    perm_origin: dict[str, dict] = {"allow": {}, "ask": {}, "deny": {}}

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
            # autoMemoryDirectory is never sourced from projectSettings,
            # trusted or not (finding 2) -- simply never applied from there,
            # so whatever a lower/higher layer set for it is left standing.
            if key in _PROJECT_EXCLUDED_KEYS and layer.name == "projectSettings":
                continue

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
                            if pkey in perm_origin:
                                for item in pvalue:
                                    if isinstance(item, str) and item not in perm_origin[pkey]:
                                        perm_origin[pkey][item] = layer.base_dir
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

    return result, perm_origin


def resolve_settings(
    cwd: Union[str, Path],
    *,
    settings_flag: Optional[str] = None,
    setting_sources: Optional[list[str]] = None,
    trusted: bool = True,
) -> Settings:
    """
    Resolve settings from all layers.

    Args:
        cwd: Current working directory
        settings_flag: Optional JSON string or file path for flagSettings
        setting_sources: Optional list of sources to include from {user, project, local}
        trusted: whether `cwd` is a trusted folder (finding 6) -- an untrusted
            project/local layer has its allow/additionalDirectories/env/hooks/
            autoMemoryDirectory keys dropped before merging; deny/ask survive.
            Defaults to True so existing callers that don't know about trust
            yet (or trust it themselves, e.g. tests) see unchanged behavior.

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

    # userSettings (finding 13: routed through claude_config_dir(), which
    # honors CLAUDE_CONFIG_DIR, not a hardcoded home()/".claude")
    if "user" in standard_sources:
        user_path = claude_config_dir() / "settings.json"
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
    merged, perm_origin = _merge_layers(layers, trusted=trusted)

    return Settings(raw=merged, layers=layers, errors=errors, permission_rule_origins=perm_origin)
