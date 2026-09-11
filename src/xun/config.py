from __future__ import annotations
import json
import os
import functools
from pathlib import Path
from dotenv import load_dotenv
import openai
from pydantic import BaseModel, ConfigDict
from typing import Self, Literal
from string import Template

from .types import ModelCapabilityType


BRAND = "XUN"
ASSET_DIR = Path(__file__).parent / "assets"

def get_home_dir() -> Path:
    home_dir = os.environ.get(f"{BRAND}_HOME")
    if home_dir:
        return Path(home_dir)
    else:
        return Path.home() / f".{BRAND.lower()}"

def get_internal_env(key: str) -> str | None:
    return os.environ.get(f"_{BRAND}_{key}")
def get_internal_env_bool(key: str) -> bool | None:
    v = get_internal_env(key)
    if v is None:
        return None
    return v.lower() in ("1", "true", "yes")

class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def to_json(self) -> str:
        return self.model_dump_json(indent=4)
    
    @classmethod
    def from_json(cls, json_str: str) -> Self:
        return cls.model_validate_json(json_str)
    
    def clone(self) -> Self:
        return self.model_copy(deep=True)

class ProviderConfig(ConfigModel):
    openai_base_url: str
    openai_api_key: str


class ModelConfig(ConfigModel):
    name: str
    capabilities: set[ModelCapabilityType]
    temperature: float | None = None
    reasoning_field: str | None = None
    """The keyword used to indicate the reasoning field in the model's payload. e.g. `reasoning` or `reasoning_content`. 
    Set `None` to auto-detect based on the model's first occurrence of the reasoning field.
    """
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    """Some may not usable with certain models, e.g. qwen3.8 only support 'low' / 'medium' / 'xhigh' """

    def _assign_primary_model(self, client: openai.OpenAI) -> None:
        if not self.name:
            models = client.models.list()
            if models and len(models.data) > 0:
                self.name = models.data[0].id
                if not len(models.data) == 1:
                    print(f"Warning: Multiple models found in the provider, but no model name specified in the config. Defaulting to the first model: {self.name}.")
            else:
                raise RuntimeError(f"Failed to infer OpenAI model from provider. Please specify a model in the config.")

# allow the model to stay unset, so it can be auto-detected from the provider
FALLBACK_ENV = { f"{BRAND}_OPENAI_MODEL": "" }
class AgentConfig(ConfigModel):
    auto_confirm: bool
    provider: ProviderConfig
    model: ModelConfig

    @classmethod
    def from_template(
        cls, 
        template_str: str, 
        fallback_env: dict[str, str] | None = None
        ) -> Self:
        template = Template(template_str)
        placeholders = template.get_identifiers()   # > python3.11
        env_vars = {}
        for placeholder in placeholders:
            if not placeholder.startswith(f"{BRAND}_"):
                raise RuntimeError(f"Invalid placeholder '{placeholder}' in config template. All placeholders must start with '{BRAND}_'.")
            env_var_value = os.environ.get(placeholder)
            if env_var_value is None and fallback_env is not None:
                env_var_value = fallback_env.get(placeholder)
            if env_var_value is None:
                raise RuntimeError(
                    f"Missing environment variable '{placeholder}' required by a config placeholder. "
                    f"Set it (e.g. in your .env file) or remove the placeholder."
                    )
            env_vars[placeholder] = env_var_value
        config_json = template.safe_substitute(env_vars)
        return cls.from_json(config_json)
    
    @classmethod
    def default(cls) -> Self:
        return cls.from_template(
            _default_config_template().to_json(),
            fallback_env=FALLBACK_ENV
            )

def _default_config_template() -> AgentConfig:
    return AgentConfig(
        auto_confirm=False,
        provider=ProviderConfig(
            openai_base_url=r"${XUN_OPENAI_BASE_URL}",
            openai_api_key=r"${XUN_OPENAI_API_KEY}",
        ),
        model=ModelConfig(
            name=r"${XUN_OPENAI_MODEL}",
            capabilities={'vision'},
        ),
    )

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge `override` into `base` (override wins).

    Nested dicts are merged per-key so a config file only needs to specify
    the fields it wants to change. Other value types are replaced wholesale.
    """
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(base[key], value)
        else:
            base[key] = value
    return base

def _load_config_file(config_path: Path) -> AgentConfig:
    """Load a config file, merging it over the built-in default template.

    The file may contain a full config or just the fields to override, e.g.
    `{"model": {"name": "my-model"}}`. Unknown top-level or nested keys are
    rejected by the final `from_template`/`from_json` validation.
    """
    with open(config_path, "r") as f:
        try:
            user_config = json.loads(f.read())
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON in config file {config_path}: {e}")
    if not isinstance(user_config, dict):
        raise RuntimeError(f"Config file {config_path} must contain a JSON object.")
    merged = _deep_merge(
        json.loads(_default_config_template().to_json()),
        user_config,
    )
    return AgentConfig.from_template(
        json.dumps(merged, indent=4),
        fallback_env=FALLBACK_ENV,
    )

@functools.lru_cache(maxsize=1)
def _load_config(_cache_id: str | None = None) -> AgentConfig:
    load_dotenv()
    home_dir = get_home_dir()
    config_path = home_dir / "config.json"

    if config_path.exists():
        return _load_config_file(config_path)
    else:
        return AgentConfig.default()

def load_config(force_reload: bool = False) -> AgentConfig:
    """Load the global config (cached per-process).

    Serves as the default config source for new agents; individual agents
    may hold their own (cloned) config and should be read via `agent.config`.
    """
    cache_id = None
    if force_reload:
        cache_id = str(os.urandom(16))
    return _load_config(cache_id)