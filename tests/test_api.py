from fastapi.testclient import TestClient

from jobprep.api import create_app


def test_read_queue_and_reject_external_imports(tmp_path):
    app = create_app({'root': tmp_path, 'data_dir': tmp_path / 'data',
                      'export_dir': tmp_path / 'exports', 'ocr_max_calls': 5})
    with TestClient(app) as client:
        assert client.get('/tasks').json() == []
        assert client.post('/import-csv', json={'path': str(tmp_path.parent / 'outside.csv')}).status_code == 400
        assert client.post('/run', json={'limit': 0}).status_code == 422
        assert client.post('/export', headers={'origin': 'https://unrelated.example'}).status_code == 403


def test_layer_metadata_and_list_scope_validation(tmp_path):
    app = create_app({'root': tmp_path, 'data_dir': tmp_path / 'data',
                      'export_dir': tmp_path / 'exports', 'ocr_max_calls': 0})
    with TestClient(app) as client:
        assert client.get('/tools').status_code == 200
        adapters = client.get('/adapters').json()
        assert any(item['name'] == 'FeishuPublicPortal' for item in adapters)
        assert client.post('/run', json={'options': {'list_config': {}}}).status_code == 400
        assert client.get('/tasks').json() == []
