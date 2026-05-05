import os
from dataclasses import dataclass
import functools
import subprocess
from dotenv import load_dotenv
from .util import is_in_container

def get_docker_host_ip():
    try:
        result = subprocess.run("ip route | grep default | awk '{print $3}'", shell=True, capture_output=True, text=True)
        return result.stdout.strip()
    except Exception as e:
        print(f"Error getting Docker host IP: {e}")
        return "127.0.0.1"

@dataclass
class ProviderConfig:
    openai_base_url: str
    openai_api_key: str
    openai_model: str

@dataclass
class AppConfig:
    auto_confirm: bool
    auto_confirm_timeout: int
    provider: ProviderConfig

    def dict(self):
        def _to_dict(obj):
            if isinstance(obj, list):
                return [_to_dict(item) for item in obj]
            elif hasattr(obj, "__dataclass_fields__"):
                return {field: _to_dict(getattr(obj, field)) for field in obj.__dataclass_fields__}
            else:
                return obj
        return _to_dict(self)

BRAND = "XUN"
@functools.lru_cache(maxsize=1)
def app_config():
    load_dotenv()

    def to_bool(value: str) -> bool:
        return value.lower() in {"true", "1", "yes", "y"}
    provider = ProviderConfig(
        openai_base_url = os.environ.get(
            f"{BRAND}_OPENAI_BASE_URL", 
            f"http://{get_docker_host_ip()}:8000/v1" if is_in_container() else "http://localhost:8000/v1"
            ),
        openai_api_key = os.environ.get(f"{BRAND}_OPENAI_API_KEY", ""),
        openai_model = os.environ.get(f"{BRAND}_OPENAI_MODEL", ""),
    )

    # try infer from listing models endpoint
    def infer_update_openai_model(provider: ProviderConfig):
        import openai
        client = openai.OpenAI(base_url=provider.openai_base_url, api_key=provider.openai_api_key)
        models = client.models.list()
        if models and len(models.data) > 0:
            if not len(models.data) == 1:
                print(f"Warning: Multiple models found in the provider, but no {BRAND}_OPENAI_MODEL specified. Defaulting to the first model.")
            provider.openai_model = models.data[0].id
        else:
            raise RuntimeError(f"Failed to infer OpenAI model from provider. Please specify a model using the {BRAND}_OPENAI_MODEL environment variable.")
    if provider.openai_model == "":
        infer_update_openai_model(provider)
        
    return AppConfig(
        auto_confirm = to_bool(os.environ.get(f"{BRAND}_AUTO_CONFIRM", "false")),
        auto_confirm_timeout = int(os.environ.get(f"{BRAND}_AUTO_CONFIRM_TIMEOUT", "3")),
        provider = provider
    )

