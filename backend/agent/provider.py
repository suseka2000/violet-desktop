"""Fixed-origin OpenAI Responses transport; no credentials in logs or documents."""
from __future__ import annotations

import http.client
import json
import math
import os
import ssl
import threading

MODELS = {
    "gpt-6.1-sol": {"label": "GPT-6.1 Sol · основной", "input": 2., "output": 10.},
    "gpt-6-luna": {"label": "GPT-6 Luna · экономный", "input": .10, "output": .50},
    "gpt-6-astra": {"label": "GPT-6 Astra · сложные задачи", "input": 10., "output": 50.},
}
PRICING_DATE = "2026-10-24"


class ProviderError(ValueError):
    pass


class Settings:
    def __init__(self):
        self.lock = threading.RLock()
        self._key = None
        self.environment_disabled = False
        self.model = "gpt-6.1-sol"
        self.budget_usd = 2.
        self.reasoning = "medium"

    def key(self):
        with self.lock:
            return self._key or ("" if self.environment_disabled else os.environ.get("OPENAI_API_KEY", ""))

    def public(self):
        with self.lock:
            return {"configured": bool(self.key()), "key_source": "session" if self._key else "environment" if self.key() else "none",
                    "model": self.model, "budget_usd": self.budget_usd, "reasoning": self.reasoning,
                    "models": [{"id": k, **v} for k, v in MODELS.items()], "pricing_date": PRICING_DATE,
                    "key_storage": "memory_only", "endpoint": "https://api.openai.com/v1/responses"}

    def update(self, value):
        if set(value) - {"api_key", "clear_key", "model", "budget_usd", "reasoning"}:
            raise ValueError("Неизвестные настройки агента")
        model = value.get("model", self.model)
        budget = value.get("budget_usd", self.budget_usd)
        reasoning = value.get("reasoning", self.reasoning)
        key = value.get("api_key")
        if model not in MODELS or reasoning not in {"low", "medium", "high"}:
            raise ValueError("Неизвестная модель или режим рассуждения")
        if type(budget) not in {float, int} or not math.isfinite(budget) or not .05 <= budget <= 100:
            raise ValueError("Бюджет задачи должен быть от $0,05 до $100")
        if key is not None and (not isinstance(key, str) or not key.startswith("sk-") or not key.isascii() or
                                not 20 <= len(key) <= 512 or any(c.isspace() for c in key)):
            raise ValueError("Проверьте API-ключ OpenAI")
        if "clear_key" in value and type(value["clear_key"]) is not bool:
            raise ValueError("clear_key должен быть логическим значением")
        with self.lock:
            if value.get("clear_key"):
                self._key = None
                self.environment_disabled = True
            if key:
                self._key = key
                self.environment_disabled = False
            self.model, self.budget_usd, self.reasoning = model, float(budget), reasoning
        return self.public()


class OpenAITransport:
    def __call__(self, key, payload, cancelled):
        if cancelled.is_set():
            raise ProviderError("Задача остановлена")
        # Bundled CA roots also work in frozen Windows/macOS applications,
        # independent of a build-machine Python certificate path.
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
        connection = http.client.HTTPSConnection("api.openai.com", timeout=60, context=context)
        try:
            connection.request("POST", "/v1/responses", body=json.dumps(payload, ensure_ascii=False).encode(),
                               headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            response = connection.getresponse()
            raw = response.read(12 * 1024**2 + 1)
            if response.status >= 300:
                descriptions = {401: "OpenAI отклонил API-ключ. Проверьте его в настройках агента.",
                                403: "OpenAI отказал в доступе. Проверьте доступность API и модели для аккаунта.",
                                429: "OpenAI сообщил о лимите запросов или недостаточном балансе API.",
                                400: "OpenAI отклонил параметры запроса; проверьте доступность выбранной модели."}
                request_id = response.getheader("x-request-id", "")[:100]
                # Never echo upstream bodies: they can contain request data or credentials.
                raise ProviderError(descriptions.get(response.status, "Сервис OpenAI временно недоступен.") +
                                    (" Номер запроса: " + request_id if request_id else ""))
            if len(raw) > 12 * 1024**2:
                raise ProviderError("Ответ OpenAI слишком большой")
            result = json.loads(raw)
            if not isinstance(result, dict) or not isinstance(result.get("output"), list):
                raise ProviderError("OpenAI вернул ответ неизвестного формата")
            return result
        except (OSError, TimeoutError, http.client.HTTPException):
            raise ProviderError("Связь с OpenAI прервалась. Автоповтора нет, чтобы не списывать деньги повторно; расход последнего запроса пока неизвестен.") from None
        finally:
            connection.close()


def reserve_cost(model, payload):
    # Base64 is transport, not text tokens. Our decoder caps images at 1600px;
    # reserve a deliberately generous vision allowance per resized image.
    image_count = 0
    def estimate(value):
        nonlocal image_count
        if isinstance(value, dict):
            if value.get("type") == "input_image":
                image_count += 1
                return {"type": "input_image"}
            return {k: estimate(v) for k, v in value.items()}
        return [estimate(v) for v in value] if isinstance(value, list) else value
    text = json.dumps(estimate(payload), ensure_ascii=False).encode()
    input_bound = len(text) + 4096 + image_count * 65536
    rates = MODELS[model]
    long = input_bound > 272000
    # Cache writes may be 2.5x uncached (12h). Reserve the more expensive path.
    return (input_bound * rates["input"] * 2.5 * (2 if long else 1) +
            payload["max_output_tokens"] * rates["output"] * (1.5 if long else 1)) / 1e6


def usage_cost(model, usage):
    rates = MODELS[model]
    incoming = max(0, int(usage.get("input_tokens", 0)))
    outgoing = max(0, int(usage.get("output_tokens", 0)))
    long = incoming > 272000
    # Deliberately do not claim cache discounts from unknown billing detail fields.
    return (incoming * rates["input"] * (2 if long else 1) + outgoing * rates["output"] * (1.5 if long else 1)) / 1e6


def usage_budget_bound(model, usage):
    """Reserve possible cache-write premiums, rather than assume a billing discount."""
    incoming = max(0, int(usage.get("input_tokens", 0)))
    extra = incoming * MODELS[model]["input"] * 1.5 * (2 if incoming > 272000 else 1) / 1e6
    return usage_cost(model, usage) + extra
