import ollama
import json
import logging
from .llm_interface import AbstractLLMClient, LLMResponse

logger = logging.getLogger(__name__)

class OllamaProvider(AbstractLLMClient):
    """
    The concrete implementation for the Ollama provider.
    """
    
    def __init__(self, config: dict):
        self.model = config.get('model', 'llama3')
        self.default_system_prompt = config.get('default_system_prompt', 'You are a helpful assistant that provides only a clean JSON output about GDPR and privacy.')
        self.client = ollama.AsyncClient() 
        logger.info(f"OllamaProvider initialized with model: {self.model}")

    async def query_json(self, 
                         user_prompt: str, 
                         system_prompt: str = None) -> LLMResponse:
        
        system_prompt = system_prompt or self.default_system_prompt
        raw_content = "" 

        try:
            response = await self.client.chat(
                model=self.model,
                messages=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt}
                ],
                format='json',
                options={
                    'temperature': 0.0  # avoid hallucinations!
                }
            )

            raw_content = response['message']['content']
            logger.debug(f"Raw Ollama response: {raw_content}")
            
            json_string = self._parse_json_response(raw_content)
            parsed_data = json.loads(json_string)
            
            return LLMResponse(success=True, data=parsed_data, error=None)

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Error decoding JSON: {e}. Raw content: {raw_content}")
            return LLMResponse(success=False, data=None, error="LLM returned malformed JSON.")
        
        except Exception as e:
            logger.error(f"An error occurred during Ollama API call: {e}")
            return LLMResponse(success=False, data=None, error=f"Ollama API call failed: {e}")