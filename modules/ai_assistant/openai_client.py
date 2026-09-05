"""Small Responses API client built on the project's existing requests dependency."""

import requests


class OpenAIClientError(RuntimeError):
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.public_message = message
        self.status_code = status_code


class OpenAIResponsesClient:
    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, api_key_provider, timeout_provider):
        self.api_key_provider = api_key_provider
        self.timeout_provider = timeout_provider

    def configured(self):
        return bool((self.api_key_provider() or "").strip())

    def create_response(self, payload):
        api_key = (self.api_key_provider() or "").strip()
        if not api_key:
            raise OpenAIClientError(
                "AI-помощник ещё не подключён: на сервере не задан OPENAI_API_KEY.",
                503,
            )
        try:
            response = requests.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout_provider(),
            )
        except requests.Timeout as error:
            raise OpenAIClientError(
                "OpenAI не успел ответить. Попробуйте повторить запрос.", 504
            ) from error
        except requests.RequestException as error:
            raise OpenAIClientError(
                "Не удалось связаться с OpenAI. Проверьте подключение сервера.", 502
            ) from error

        if response.status_code >= 400:
            if response.status_code == 401:
                message = "OpenAI отклонил API-ключ. Проверьте OPENAI_API_KEY."
                status_code = 503
            elif response.status_code == 429:
                message = "Лимит OpenAI временно достигнут. Повторите запрос немного позже."
                status_code = 429
            elif response.status_code in (400, 404):
                message = "OpenAI не принял запрос. Проверьте выбранную модель и настройки агента."
                status_code = 502
            else:
                message = "OpenAI временно недоступен. Попробуйте повторить запрос."
                status_code = 502
            raise OpenAIClientError(message, status_code)

        try:
            data = response.json()
        except ValueError as error:
            raise OpenAIClientError("OpenAI вернул некорректный ответ.", 502) from error
        if not isinstance(data, dict):
            raise OpenAIClientError("OpenAI вернул некорректный ответ.", 502)
        return data

