import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .exporter import export_review
from .pipeline import Workstation


class RunRequest(BaseModel):
    limit: int = Field(10, ge=1, le=1000)
    ai_only: bool = True
    urls: list[str] = Field(default_factory=list, max_length=100)
    options: dict = Field(default_factory=dict)
    reset_attempts: bool = False


class ImportRequest(BaseModel):
    path: str = 'job_market_raw.csv'


class ManualRequest(BaseModel):
    path: str
    url: str
    kind: str = 'html'
    skip_ocr: bool = False


class RetryRequest(BaseModel):
    statuses: list[str] = Field(default_factory=lambda: ['error', 'pending_ocr'])
    ids: list[str] | None = None
    reset_attempts: bool = False


def create_app(config):
    app = FastAPI(title='Job Preprocessing Workstation', version='0.1.0')
    workstation = Workstation(config)
    app.state.worker = None
    app.state.last_run = None

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).hostname not in ('127.0.0.1', 'localhost', '::1'):
            from fastapi.responses import JSONResponse

            return JSONResponse({'detail': 'Cross-origin requests are disabled'}, status_code=403)
        return await call_next(request)

    def local_path(value):
        path = Path(value)
        path = (config['root'] / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(config['root']):
            raise HTTPException(400, 'File imports must be inside the workstation directory')
        if path.name.startswith('.env') or path.suffix.lower() not in ('.csv', '.html', '.htm', '.png', '.jpg', '.jpeg', '.webp'):
            raise HTTPException(400, 'Unsupported import file')
        if not path.is_file():
            raise HTTPException(400, 'Import file does not exist')
        return path

    def active():
        return app.state.worker is not None and not app.state.worker.done()

    @app.get('/')
    async def overview():
        return {'service': 'jobprep', 'status': '/status', 'controls': '/docs',
                'review_export': str(config['export_dir'] / 'codex_review.jsonl')}

    @app.get('/status')
    async def status():
        engine = workstation.ocr_engine()
        stats = workstation.store.stats()
        return {**stats, 'worker_running': active() or stats['statuses'].get('running', 0) > 0,
                'api_worker_running': active(), 'last_run': app.state.last_run,
                'ocr': engine.status() if hasattr(engine, 'status') else {'provider': 'baidu'}}

    @app.get('/tools')
    async def tools():
        from .app import tool_catalog

        return tool_catalog()

    @app.get('/adapters')
    async def adapters():
        return workstation.adapters.describe()

    @app.get('/tasks')
    async def tasks(status: str | None = None, limit: int = 100):
        return [{k: v for k, v in row.items() if k != 'result_json'}
                for row in workstation.store.tasks(max(1, min(limit, 1000)), status)]

    @app.get('/tasks/{task_id}')
    async def task(task_id: str):
        row = workstation.store.task(task_id)
        if row is None:
            raise HTTPException(404, 'Task not found')
        raw = row.pop('result_json')
        row['result'] = json.loads(raw) if raw else None
        return row

    @app.post('/import-csv')
    async def import_csv(request: ImportRequest):
        return await asyncio.to_thread(workstation.store.import_csv, local_path(request.path))

    @app.post('/run', status_code=202)
    async def run(request: RunRequest):
        if active():
            raise HTTPException(409, 'Worker is already running')
        if 'list_config' in request.options and len(request.urls) != 1:
            raise HTTPException(400, 'list_config requires exactly one URL; use list_sources for batch routing')
        ids = None
        if request.urls:
            try:
                ids = [workstation.store.add_task(url, priority=10000) for url in request.urls]
                workstation.store.retry(['error', 'blocked', 'partial', 'pending_ocr', 'retry_wait'], ids,
                                        reset_attempts=request.reset_attempts)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        async def work():
            try:
                app.state.last_run = await workstation.run(request.limit, request.ai_only, ids, request.options)
            except Exception as exc:
                app.state.last_run = {'error': type(exc).__name__, 'message': 'Worker failed; inspect task statuses'}
        app.state.worker = asyncio.create_task(work())
        return {'accepted': True, 'limit': request.limit, 'status_url': '/status'}

    @app.post('/retry')
    async def retry(request: RetryRequest):
        if active():
            raise HTTPException(409, 'Wait for the active worker')
        from .store import worker_lock

        try:
            with worker_lock(workstation.store.root):
                return {'requeued': workstation.store.retry(request.statuses, request.ids, request.reset_attempts)}
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/import-file')
    async def import_file(request: ManualRequest):
        if active():
            raise HTTPException(409, 'Wait for the active worker')
        try:
            return await workstation.import_file(local_path(request.path), request.url, request.kind,
                                                 skip_ocr=request.skip_ocr)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post('/export')
    async def export():
        if active():
            raise HTTPException(409, 'Wait for the active worker to get a consistent export')
        try:
            return await asyncio.to_thread(export_review, workstation.store, config['export_dir'])
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    return app
