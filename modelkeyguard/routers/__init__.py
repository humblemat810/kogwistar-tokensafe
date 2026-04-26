from .admin_keys import create_router as create_admin_keys_router
from .admin_security import create_router as create_admin_security_router
from .admin_usage import create_router as create_admin_usage_router
from .provider_azure import create_router as create_provider_azure_router
from .provider_gemini import create_router as create_provider_gemini_router
from .provider_ollama import create_router as create_provider_ollama_router
from .provider_openai import create_router as create_provider_openai_router

__all__ = [
    "create_provider_openai_router",
    "create_provider_azure_router",
    "create_provider_ollama_router",
    "create_provider_gemini_router",
    "create_admin_keys_router",
    "create_admin_usage_router",
    "create_admin_security_router",
]
