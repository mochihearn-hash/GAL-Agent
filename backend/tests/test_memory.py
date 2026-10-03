import json
import pytest

from app.agent.memory import compress,context,needs_summary
from app.storage.store import empty_memory


@pytest.mark.asyncio
async def test_compression_preserves_provenance_and_requests_are_bounded():
    memory=empty_memory()
    memory['recent']=[{'source_seq':i,'source_id':str(i),'dialogue':'小明答应明天见面。','speaker':'小明'} for i in range(1,21)]
    class Provider:
        config={'context_tokens':32000}
        async def complete(self,system,text):
            self.input=json.loads(text)
            return {'text':json.dumps({'summary':'小明答应明天见面。','facts':['小明作出了约定'],'hypotheses':[],'questions':['约定能否兑现？']},ensure_ascii=False),'usage':{},'elapsed_ms':1}
    provider=Provider()
    assert needs_summary(memory,provider.config)
    result,response=await compress(memory,provider)
    assert result['summarized_through']==12
    assert len(result['recent'])==8
    assert len(result['chapters'][0]['source_ids'])==12
    assert len(memory['recent'])==20
    result['chapters'][0]['source_ids']=[str(i) for i in range(50000)]
    encoded=context(result,[],provider.config)
    assert 'source_ids' not in encoded
    assert len(encoded)<12000


@pytest.mark.asyncio
async def test_bad_summary_never_overwrites_original_memory():
    memory=empty_memory();memory['recent']=[{'source_seq':1,'source_id':'a','dialogue':'重要的剧情'}]
    class Provider:
        config={'context_tokens':32000}
        async def complete(self,*args):return {'text':'{"summary": null}'}
    with pytest.raises(ValueError):await compress(memory,Provider())
    assert memory['recent'][0]['dialogue']=='重要的剧情'


@pytest.mark.asyncio
async def test_summary_repairs_bad_fields_and_preserves_failed_response():
    memory=empty_memory();memory['recent']=[{'source_seq':1,'source_id':'a','dialogue':'重要剧情'}]
    memory['facts']=[{'text':'已知事实','source_id':'old','source_seq':0}]
    attempts=[]
    class Provider:
        config={'context_tokens':32000}
        calls=0
        async def complete(self,system,text):
            self.calls+=1
            if self.calls==1:
                self.input=json.loads(text)
                return {'text':'{"summary":"摘要","facts":[{"text":"错误类型"}],"hypotheses":[],"questions":[]}'}
            assert 'repair' in text and 'facts' in text
            return {'text':'{"summary":"有效摘要","facts":["已知事实"],"hypotheses":[],"questions":[]}'}
    provider=Provider()
    result,response=await compress(memory,provider,on_attempt=attempts.append)
    assert provider.calls==2 and provider.input['previous_memory']['facts']==['已知事实']
    assert attempts[0]['valid'] is False and '错误类型' in attempts[0]['raw_text']
    assert 'facts' in attempts[0]['error']
    assert result['summarized_through']==1 and memory['recent']


@pytest.mark.asyncio
async def test_summary_repairs_are_bounded_and_never_commit_on_failure():
    memory=empty_memory();memory['recent']=[{'source_seq':1,'source_id':'a','dialogue':'重要剧情'}]
    attempts=[]
    class Provider:
        config={'context_tokens':32000}
        calls=0
        async def complete(self,*args):
            self.calls+=1
            return {'text':'{"summary":"摘要","facts":[],"hypotheses":[],"questions":'+json.dumps(['问题']*13)+'}'}
    provider=Provider()
    with pytest.raises(ValueError,match='连续3次'):await compress(memory,provider,on_attempt=attempts.append)
    assert provider.calls==3 and len(attempts)==3
    assert memory['recent'][0]['dialogue']=='重要剧情' and memory['chapters']==[]


@pytest.mark.asyncio
async def test_summary_repair_fits_small_context_budget():
    memory=empty_memory();memory['recent']=[{'source_seq':1,'source_id':'a','dialogue':'一句短剧情'}]
    class Provider:
        config={'context_tokens':12000}
        calls=0
        async def complete(self,system,text):
            self.calls+=1
            assert len(text)<2400
            return {'text':'invalid'*2000 if self.calls==1 else '{"summary":"剧情摘要","facts":[],"hypotheses":[],"questions":[]}'}
    provider=Provider();result,_=await compress(memory,provider)
    assert provider.calls==2 and result['summarized_through']==1
