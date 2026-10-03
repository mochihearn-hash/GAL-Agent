from fastapi.testclient import TestClient

from app.api.server import create_app


TOKEN="a"*64
HEADERS={"Authorization":"Bearer "+TOKEN}


class Credentials:
    def __init__(self):self.values={}
    def put(self,ref,secret):self.values[ref]=secret
    def get(self,ref):return self.values[ref]
    def delete(self,ref):self.values.pop(ref,None)


def test_session_crud_and_restart_with_real_api(tmp_path):
    app=create_app(tmp_path,TOKEN,credentials=Credentials())
    with TestClient(app) as client:
        assert client.get('/sessions').status_code==401
        assert client.get('/sessions',headers={**HEADERS,"Origin":"https://example.com"}).status_code==403
        assert client.post('/sessions',headers=HEADERS,json={"name":"   "}).status_code==422
        a=client.post('/sessions',headers=HEADERS,json={"name":"海猫"}).json()
        b=client.post('/sessions',headers=HEADERS,json={"name":"海猫"}).json()
        assert a['id']!=b['id']
        assert client.patch('/sessions/'+a['id'],headers=HEADERS,json={"name":"海猫第二章"}).json()['name']=="海猫第二章"
        assert client.post('/sessions/'+a['id']+'/select',headers=HEADERS).status_code==200
        assert client.post('/sessions/'+a['id']+'/guidance',headers=HEADERS,json={"text":"关注人物关系","scope":"persistent"}).status_code==200
        assert client.get('/sessions/'+b['id'],headers=HEADERS).json()['guidance']==[]
        assert client.post('/sessions/'+a['id']+'/start',headers=HEADERS).status_code==400
    with TestClient(create_app(tmp_path,TOKEN,credentials=Credentials())) as client:
        restored=client.get('/sessions/'+a['id'],headers=HEADERS).json()
        assert restored['name']=="海猫第二章"
        assert restored['guidance'][0]['text']=="关注人物关系"
        assert client.get('/state',headers=HEADERS).json()['status']=='paused'
        assert client.delete('/sessions/'+a['id'],headers=HEADERS).status_code==200
        assert client.get('/sessions/'+a['id'],headers=HEADERS).status_code==404
        assert len(client.get('/sessions',headers=HEADERS).json())==1


def test_credentials_are_referenced_and_validation_does_not_echo_keys(tmp_path):
    credentials=Credentials()
    with TestClient(create_app(tmp_path,TOKEN,credentials=credentials)) as client:
        response=client.post('/configs',headers=HEADERS,json={"name":"视觉模型","url":"https://example.com/v1","model":"vision","api_key":"never-log-this-key"})
        assert response.status_code==200
        assert 'never-log-this-key' not in response.text
        cid=response.json()['id']
        assert credentials.values[cid]=='never-log-this-key'
        saved=client.put('/configs/'+cid,headers=HEADERS,json={"name":"视觉模型","url":"https://example.com/v1","model":"vision"})
        assert saved.status_code==200 and saved.json()['has_key']
        assert credentials.values[cid]=='never-log-this-key'
        session=client.post('/sessions',headers=HEADERS,json={"name":"无需重复配置"}).json()
        assert session['settings']['config_id']==cid
        assert client.post('/sessions/'+session['id']+'/select',headers=HEADERS).status_code==200
        assert credentials.values[cid]=='never-log-this-key'
        assert 'never-log-this-key' not in client.get('/configs',headers=HEADERS).text
        invalid=client.post('/configs',headers=HEADERS,json={"name":"","url":"bad","model":"","api_key":"never-log-this-key"})
        assert invalid.status_code==422
        assert 'never-log-this-key' not in invalid.text
        assert client.delete('/configs/'+cid,headers=HEADERS).status_code==200
        assert not credentials.values
