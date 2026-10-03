import json

import httpx
import pytest

from app.providers.client import Provider, ProviderError, decode_json
from app.schemas import Decision, ModelConfig


def config(protocol="compatible"):
    return ModelConfig(name="test",url="https://example.com/v1",model="vision",protocol=protocol).model_dump()


@pytest.mark.parametrize("protocol",["compatible","anthropic","gemini"])
def test_keys_are_headers_only_and_images_have_the_correct_wire_format(protocol):
    provider=Provider(config(protocol),"secret-value")
    endpoint,headers,body=provider.wire_request("system","read",b"png")
    assert "secret-value" not in endpoint
    assert "secret-value" not in json.dumps(body)
    assert "secret-value" in json.dumps(headers)
    assert "cG5n" in json.dumps(body)


@pytest.mark.asyncio
async def test_provider_redacts_secret_and_records_real_usage():
    def handler(request):
        assert request.headers["Authorization"]=="Bearer secret-value"
        return httpx.Response(200,json={"choices":[{"message":{"content":"secret-value appeared"},"finish_reason":"stop"}],"usage":{"total_tokens":42}})
    result=await Provider(config(),"secret-value",httpx.MockTransport(handler)).complete("system","text")
    assert result["text"]=="[REDACTED] appeared"
    assert result["usage"]["total_tokens"]==42


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code",[(401,"authentication"),(404,"address"),(400,"unsupported"),(302,"address")])
async def test_provider_error_never_echoes_server_body(status,code):
    transport=httpx.MockTransport(lambda request:httpx.Response(status,json={"error":"secret-value"}))
    with pytest.raises(ProviderError) as error:await Provider(config(),"secret-value",transport).complete("system","text")
    assert error.value.code==code
    assert "secret-value" not in str(error.value)


@pytest.mark.asyncio
async def test_network_retry_is_bounded_to_model_requests():
    calls=0
    def handler(request):
        nonlocal calls
        calls+=1
        return httpx.Response(429)
    with pytest.raises(ProviderError):await Provider(config(),"",httpx.MockTransport(handler)).complete("system","text")
    assert calls==2


def test_format_and_keyboard_escape_hatches_are_rejected():
    for raw in ['not json','[]','{"incomplete":']:
        with pytest.raises(ProviderError):decode_json(raw)
    with pytest.raises(ValueError):
        Decision.model_validate({"observation_id":"x","run_version":1,"screen_type":"typing","action":{"type":"advance"}})
    with pytest.raises(ValueError):
        Decision.model_validate({"observation_id":"x","run_version":1,"screen_type":"dialogue","action":{"type":"press_key","key":"WIN"}})
    with pytest.raises(ValueError):ModelConfig(name="x",model="x",url="https://secret-value@example.com")


@pytest.mark.parametrize('action',[{'type':'press_key','key':'SPACE'},{'type':'click','x':.5,'y':.8}])
def test_dialogue_cannot_bypass_configured_advance_method(action):
    with pytest.raises(ValueError,match='advance'):
        Decision.model_validate({'observation_id':'x','run_version':1,'screen_type':'dialogue','dialogue':'完整对话','action':action})
