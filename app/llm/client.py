from llama_index.llms.ollama import Ollama
from app.config import settings

def make_llm():
    """
    Construct and return an Ollama LLM client instance.
    Uses model + parameters defined in settings.

    Returns:
        Ollama client configured with:
          - model: settings.llm_model
          - request_timeout: 480 seconds
          - temperature: 0.2
          - additional_kwargs: keep_alive, num_predict
    """
    return Ollama(
        model=settings.llm_model,
        request_timeout=480,
        temperature=0.2,
        additional_kwargs={
            "keep_alive": -1,
            "num_predict": 160,
        },
    )
