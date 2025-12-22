# In src/gdpr_cookies_extractor/analysis/gemini_provider.py
from .llm_interface import AbstractLLMClient, LLMResponse
import json
import logging
from typing import Optional, Dict, Any

# Make sure to install the library: poetry add google-generativeai
try:
    import google.generativeai as genai
except ImportError:
    print("google-generativeai library not found. Please install it using 'poetry add google-generativeai'")
    genai = None

logger = logging.getLogger(__name__)

class GeminiProvider(AbstractLLMClient):
    """
    A concrete implementation of the AbstractLLMClient for Google's Gemini models.
    """
    def __init__(self, config: dict):
        if not genai:
            raise ImportError("Gemini library is not installed.")
        
        api_key = config.get('gemini_api_key')
        if not api_key or api_key == "YOUR_GEMINI_API_KEY_HERE":
            raise ValueError("Gemini API key is not set in config.json")
            
        genai.configure(api_key=api_key)
        model_name = config.get('model', 'gemini-pro')
        self.model = genai.GenerativeModel(model_name)
        logger.info(f"GeminiProvider initialized with model: {model_name}")

    async def query_json(self, user_prompt: str, system_prompt: str = None) -> LLMResponse:
        """
        Sends a prompt to the Gemini API and expects a JSON response.
        
        Note: The 'system_prompt' for Gemini is handled differently. It's often
        the first message in the history with the 'system' role. Here we will
        pass it as part of the contents.
        """
        contents = []
        if system_prompt:
            contents.append({'role': 'system', 'parts': [system_prompt]})
        contents.append({'role': 'user', 'parts': [user_prompt]})
        
        raw_content = ""
        try:
            # Gemini requires specific generation config for JSON output
            generation_config = genai.types.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.0
            )
            
            response = await self.model.generate_content_async(
                contents,
                generation_config=generation_config
            )

            raw_content = response.text
            logger.debug(f"Raw Gemini response: {raw_content}")
            
            # The response from Gemini with response_mime_type="application/json" should be a valid JSON string already
            # but we can still use the parser for safety.
            json_string = self._parse_json_response(raw_content)
            parsed_data = json.loads(json_string)
            
            return LLMResponse(success=True, data=parsed_data)

        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"Error decoding JSON from Gemini: {e}. Raw content: {raw_content}")
            return LLMResponse(success=False, data=None, error="Gemini returned malformed JSON.")
        
        except Exception as e:
            logger.error(f"An error occurred during Gemini API call: {e}")
            return LLMResponse(success=False, data=None, error=f"Gemini API call failed: {e}")
